"""Offline K3 EOD integration through real ingestion, quality, and publish layers."""

from __future__ import annotations

import gc
import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from tests.data.adapters.test_akshare_adapter import FakeFrame

from quantking.common.config import AppSettings
from quantking.common.paths import RuntimePaths
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.release_verifier import verify_s108_release
from quantking.data.adapters.sources.akshare import (
    AkshareDailyBarAdapter,
    AkshareMarketDataGateway,
)
from quantking.data.adapters.sources.baostock import (
    BAOSTOCK_DISTRIBUTION_VERSION,
    BaostockDailyBarAdapter,
    BaostockMarketDataGateway,
    BaostockSessionResult,
    BaostockSessionRunner,
)
from quantking.data.adapters.sources.budget import NetworkBudgetExceededError
from quantking.data.adapters.sources.contracts import SourceAdapterError, SourceErrorCode
from quantking.data.adapters.sources.parameters import DailyBarRequest
from quantking.data.adapters.sources.registry import PRODUCTION_UPSTREAMS
from quantking.data.eod.contracts import EodPublishIntent, EodRunRequest
from quantking.data.eod.release import S107K3_IDEMPOTENCY_KEY
from quantking.data.models import (
    DatasetKind,
    DatasetVersionStatus,
    DataUpstream,
)
from quantking.data.resolution.models import K3_RESOLUTION_RULE_VERSION
from quantking.jobs.models import JobStatus
from quantking.runtime import EodRuntime, build_eod_runtime

_NOW = UtcTimestamp(datetime(2026, 8, 12, 10, 0, tzinfo=UTC))
_START = date(2026, 6, 11)
_END = date(2026, 8, 9)
_SYMBOLS = ("000001.SZ", "600000.SH")
_Metric = tuple[float, float]
_MetricFactory = Callable[[str, date], _Metric]


def _constant(close: float) -> _MetricFactory:
    def value(_symbol: str, _trade_date: date) -> _Metric:
        return close, close * 10_000

    return value


class SyntheticBaostockRunner(BaostockSessionRunner):
    """No-vendor BaoStock session with the production response contract."""

    def __init__(
        self,
        values: _MetricFactory,
        *,
        fail: bool = False,
    ) -> None:
        self.values = values
        self.fail = fail
        self.calls: list[DailyBarRequest] = []

    def run(self, request: DailyBarRequest) -> BaostockSessionResult:
        self.calls.append(request)
        if self.fail:
            raise SourceAdapterError(SourceErrorCode.BAOSTOCK_DISCONNECTED)
        window = request.request_window
        provider_code = f"{request.symbol[7:].lower()}.{request.symbol[:6]}"
        rows: list[tuple[str, ...]] = []
        current = window.upstream_start
        while current <= window.upstream_end:
            close, amount = self.values(request.symbol, current)
            rendered = f"{close:.6f}"
            rows.append(
                (
                    current.isoformat(),
                    provider_code,
                    rendered,
                    rendered,
                    rendered,
                    rendered,
                    rendered,
                    "10000",
                    f"{amount:.6f}",
                    "3",
                    "1",
                    "0",
                )
            )
            current += timedelta(days=1)
        return BaostockSessionResult(
            rows=tuple(rows),
            distribution_version=BAOSTOCK_DISTRIBUTION_VERSION,
            runtime_version="00.9.30",
            login_messages=1,
            query_messages=1,
            logout_messages=1,
            complete=True,
            partial_reason=None,
        )


class SyntheticAkshareModule:
    """AKShare-shaped module whose endpoint calls never leave the process."""

    def __init__(self, upstream: DataUpstream, values: _MetricFactory) -> None:
        self.__version__ = "1.18.83"
        self.upstream = upstream
        self.values = values
        self.calls: list[tuple[str, dict[str, str | float]]] = []

    def _call(self, endpoint: str, kwargs: dict[str, str | float]) -> FakeFrame:
        self.calls.append((endpoint, kwargs))
        start = datetime.strptime(str(kwargs["start_date"]), "%Y%m%d").date()
        end = datetime.strptime(str(kwargs["end_date"]), "%Y%m%d").date()
        provider_symbol = str(kwargs["symbol"])
        symbol = "600000.SH" if "600000" in provider_symbol else "000001.SZ"
        rows: list[dict[str, object]] = []
        current = start
        while current <= end:
            close, amount = self.values(symbol, current)
            if self.upstream is DataUpstream.EASTMONEY:
                rows.append(
                    {
                        "日期": current.isoformat(),
                        "开盘": close,
                        "最高": close,
                        "最低": close,
                        "收盘": close,
                        "昨收": close,
                        "成交量": 100,
                        "成交额": amount,
                    }
                )
            else:
                rows.append(
                    {
                        "date": current,
                        "open": close,
                        "high": close,
                        "low": close,
                        "close": close,
                        "pre_close": close,
                        "volume": (
                            100
                            if self.upstream is DataUpstream.TENCENT and symbol == "000001.SZ"
                            else 10_000
                        ),
                        "amount": amount,
                    }
                )
            current += timedelta(days=1)
        return FakeFrame(rows)

    def stock_zh_a_hist(self, **kwargs: str | float) -> FakeFrame:
        return self._call("stock_zh_a_hist", kwargs)

    def stock_zh_a_hist_tx(self, **kwargs: str | float) -> FakeFrame:
        return self._call("stock_zh_a_hist_tx", kwargs)

    def stock_zh_a_daily(self, **kwargs: str | float) -> FakeFrame:
        return self._call("stock_zh_a_daily", kwargs)


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        paths=RuntimePaths(
            code_root=Path.cwd(),
            data_root=(tmp_path / "runtime-data").resolve(),
            offsite_backup_root=(tmp_path / "offsite").resolve(),
        ),
        web_host="127.0.0.1",
        web_port=8765,
        sqlite_busy_timeout_ms=250,
        sqlite_max_attempts=7,
        sqlite_retry_base_delay_ms=10,
        sqlite_retry_cap_delay_ms=160,
        sqlite_retry_jitter_ratio=0.0,
        job_heartbeat_min_interval_ms=1000,
        job_progress_min_interval_ms=250,
    )


def _runtime(
    tmp_path: Path,
    *,
    bao_values: _MetricFactory,
    ak_values: Mapping[DataUpstream, _MetricFactory],
    bao_fails: bool = False,
) -> tuple[EodRuntime, SyntheticBaostockRunner, dict[DataUpstream, SyntheticAkshareModule]]:
    runner = SyntheticBaostockRunner(bao_values, fail=bao_fails)
    modules = {
        upstream: SyntheticAkshareModule(upstream, ak_values[upstream])
        for upstream in (
            DataUpstream.EASTMONEY,
            DataUpstream.TENCENT,
            DataUpstream.SINA,
        )
    }
    gateways = {
        DataUpstream.BAOSTOCK: BaostockMarketDataGateway(
            BaostockDailyBarAdapter(runner=runner, clock=lambda: _NOW)
        ),
        **{
            upstream: AkshareMarketDataGateway(
                AkshareDailyBarAdapter(
                    upstream,
                    module_loader=lambda module=module: module,
                    clock=lambda: _NOW,
                )
            )
            for upstream, module in modules.items()
        },
    }
    assert tuple(gateways) == PRODUCTION_UPSTREAMS
    runtime = build_eod_runtime(_settings(tmp_path), gateways=gateways)
    return runtime, runner, modules


def _enqueue_and_run(runtime: EodRuntime, *, key: str) -> str:
    enqueued = runtime.eod_commands.enqueue(
        EodRunRequest(
            start_date=_START,
            end_date=_END,
            datasets=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        ),
        idempotency_key=key,
        requester="k3-integration-test",
    )
    runtime.worker.run(enqueued.job_id)
    return enqueued.job_id


def _stored_state(runtime: EodRuntime, job_id: str):
    with runtime.data_uow_factory() as uow:
        batches = uow.data.list_batches_for_job(job_id)
        versions = uow.data.list_versions(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            status=DatasetVersionStatus.PUBLISHED,
        )
        resolutions = uow.data.list_resolution_runs_page(before=None, limit=10)
        uow.rollback()
    return batches, versions, resolutions


def _resolution_payload(runtime: EodRuntime, relative_path: str) -> tuple[dict, dict]:
    directory = runtime.settings.paths.data_root / "artifacts" / relative_path
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    summary = json.loads((directory / "summary.json").read_text("utf-8"))
    return manifest, summary


def _snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_production_ab_agreement_publishes_k3_v2_without_c_or_d(tmp_path: Path) -> None:
    runtime, runner, modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        ak_values={
            upstream: _constant(10.0)
            for upstream in DataUpstream
            if upstream
            in {
                DataUpstream.EASTMONEY,
                DataUpstream.TENCENT,
                DataUpstream.SINA,
            }
        },
    )

    job_id = _enqueue_and_run(runtime, key="k3-ab-agreement")

    assert runtime.jobs.get_job(job_id).status is JobStatus.SUCCEEDED
    assert len(runner.calls) == len(modules[DataUpstream.EASTMONEY].calls) == 4
    assert modules[DataUpstream.TENCENT].calls == []
    assert modules[DataUpstream.SINA].calls == []
    batches, versions, resolutions = _stored_state(runtime, job_id)
    assert {item.upstream for item in batches} == {
        DataUpstream.BAOSTOCK,
        DataUpstream.EASTMONEY,
    }
    assert len(versions) == len(resolutions) == 1
    resolution = resolutions[0]
    assert resolution.rule_version == K3_RESOLUTION_RULE_VERSION
    manifest, summary = _resolution_payload(runtime, resolution.artifact_relative_path)
    assert manifest["schema_version"] == "resolution-artifact.v2"
    assert {item["upstream"] for item in summary["input_bindings"]} == {
        item.value for item in PRODUCTION_UPSTREAMS
    }
    assert [item["status"] for item in summary["input_bindings"]] == [
        "AVAILABLE",
        "AVAILABLE",
        "NOT_CALLED",
        "NOT_CALLED",
    ]
    lineage = runtime.data_center.get_version_lineage(str(versions[0].version_id))
    assert {item["provider"] for item in lineage["source_batches"]} == {
        "AKSHARE",
        "BAOSTOCK",
    }
    rendered_lineage = json.dumps(lineage, ensure_ascii=False, sort_keys=True)
    assert str(runtime.settings.paths.data_root) not in rendered_lineage
    assert "provider_payload" not in rendered_lineage
    assert DataUpstream.XTQUANT not in {item.upstream for item in batches}


def test_baostock_failure_calls_tencent_exactly_and_publishes_bc(tmp_path: Path) -> None:
    runtime, runner, modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        bao_fails=True,
        ak_values={
            DataUpstream.EASTMONEY: _constant(10.0),
            DataUpstream.TENCENT: _constant(10.0),
            DataUpstream.SINA: _constant(10.0),
        },
    )

    job_id = _enqueue_and_run(runtime, key="k3-a-failed-bc")

    assert runtime.jobs.get_job(job_id).status is JobStatus.SUCCEEDED
    assert len(runner.calls) == 1
    assert len(modules[DataUpstream.EASTMONEY].calls) == 4
    assert len(modules[DataUpstream.TENCENT].calls) == 4
    assert modules[DataUpstream.SINA].calls == []
    actual = {
        (str(call[1]["symbol"]), str(call[1]["start_date"]), str(call[1]["end_date"]))
        for call in modules[DataUpstream.TENCENT].calls
    }
    assert actual == {
        (symbol, start, end)
        for symbol in ("sz000001", "sh600000")
        for start, end in (("20260527", "20260711"), ("20260627", "20260809"))
    }
    batches, versions, _resolutions = _stored_state(runtime, job_id)
    assert len(versions) == 1
    assert {item.upstream for item in batches} == {
        DataUpstream.BAOSTOCK,
        DataUpstream.EASTMONEY,
        DataUpstream.TENCENT,
    }
    assert DataUpstream.XTQUANT not in {item.upstream for item in batches}


def test_abc_disagreement_calls_sina_and_publishes_ad_consensus(tmp_path: Path) -> None:
    runtime, runner, modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        ak_values={
            DataUpstream.EASTMONEY: _constant(11.0),
            DataUpstream.TENCENT: _constant(12.0),
            DataUpstream.SINA: _constant(10.0),
        },
    )

    job_id = _enqueue_and_run(runtime, key="k3-abcd-ad-consensus")

    assert runtime.jobs.get_job(job_id).status is JobStatus.SUCCEEDED
    assert len(runner.calls) == 4
    assert all(len(modules[upstream].calls) == 4 for upstream in modules)
    batches, versions, resolutions = _stored_state(runtime, job_id)
    assert len(versions) == len(resolutions) == 1
    assert {item.upstream for item in batches} == set(PRODUCTION_UPSTREAMS)
    _manifest, summary = _resolution_payload(
        runtime,
        resolutions[0].artifact_relative_path,
    )
    assert {item["upstream"] for item in summary["input_bindings"]} == {
        item.value for item in PRODUCTION_UPSTREAMS
    }
    assert DataUpstream.XTQUANT not in {item.upstream for item in batches}


def test_exception_budget_blocks_before_first_tencent_endpoint(tmp_path: Path) -> None:
    affected = {
        date(2026, 6, 13),
        date(2026, 6, 23),
        date(2026, 7, 3),
        date(2026, 7, 13),
        date(2026, 7, 23),
    }

    def east_values(symbol: str, trade_date: date) -> _Metric:
        close, amount = _constant(10.0)(symbol, trade_date)
        if symbol == "000001.SZ" and trade_date in affected:
            amount += 1.0
        return close, amount

    runtime, runner, modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        ak_values={
            DataUpstream.EASTMONEY: east_values,
            DataUpstream.TENCENT: _constant(10.0),
            DataUpstream.SINA: _constant(10.0),
        },
    )
    enqueued = runtime.eod_commands.enqueue(
        EodRunRequest(
            start_date=_START,
            end_date=_END,
            datasets=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        ),
        idempotency_key="k3-c-budget-preflight",
        requester="k3-integration-test",
    )

    with pytest.raises(NetworkBudgetExceededError):
        runtime.worker.run(enqueued.job_id)

    job = runtime.jobs.get_job(enqueued.job_id)
    assert job.status is JobStatus.FAILED
    assert job.failure is not None
    assert job.failure.error_type == "NetworkBudgetExceededError"
    assert len(runner.calls) == len(modules[DataUpstream.EASTMONEY].calls) == 4
    assert modules[DataUpstream.TENCENT].calls == []
    assert modules[DataUpstream.SINA].calls == []
    with runtime.data_uow_factory() as uow:
        batches = uow.data.list_batches_for_job(enqueued.job_id)
        published = uow.data.list_versions(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            status=DatasetVersionStatus.PUBLISHED,
        )
        uow.rollback()
    assert {item.upstream for item in batches} == {
        DataUpstream.BAOSTOCK,
        DataUpstream.EASTMONEY,
    }
    assert not published
    assert DataUpstream.XTQUANT not in {item.upstream for item in batches}


def test_read_only_release_verifier_accepts_k3_and_changes_no_file(tmp_path: Path) -> None:
    runtime, _runner, _modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        ak_values={
            DataUpstream.EASTMONEY: _constant(10.0),
            DataUpstream.TENCENT: _constant(10.0),
            DataUpstream.SINA: _constant(10.0),
        },
    )
    _enqueue_and_run(runtime, key=S107K3_IDEMPOTENCY_KEY)
    with runtime.data_uow_factory() as uow:
        version = uow.data.list_versions(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            status=DatasetVersionStatus.PUBLISHED,
        )[0]
        uow.rollback()
    data_root = runtime.settings.paths.data_root
    data_engine = runtime.data_uow_factory._session_factory.kw["bind"]
    job_engine = runtime.jobs._transactions._session_factory.kw["bind"]
    data_engine.dispose()
    job_engine.dispose()
    del runtime
    gc.collect()
    database = data_root / "metadata" / "quantking.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    before = _snapshot(data_root)

    result = verify_s108_release(data_root=data_root, version_id=str(version.version_id))

    assert result["status"] == "VERIFIED"
    assert _snapshot(data_root) == before
