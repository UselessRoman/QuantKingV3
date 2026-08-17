"""Offline K4 three-role EOD integration through real ingestion, quality, and publish layers."""

from __future__ import annotations

import gc
import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest
from tests.data.adapters.test_akshare_adapter import FakeFrame, FakeRunner

from quantking.common.config import AppSettings
from quantking.common.ids import EntityId, new_entity_id
from quantking.common.paths import RuntimePaths
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.adapters.eod_worker import EodExecutionError
from quantking.data.adapters.release_verifier import verify_s108_release
from quantking.data.adapters.sources.akshare import (
    AkshareDailyBarAdapter,
    AkshareMarketDataGateway,
)
from quantking.data.adapters.sources.budget import NetworkBudgetExceededError
from quantking.data.adapters.sources.contracts import (
    AmountUnit,
    SourceAdapterError,
    SourceErrorCode,
    SourceLineage,
    SupplierResponse,
    SupplierResponseContract,
    VolumeUnit,
)
from quantking.data.adapters.sources.registry import PRODUCTION_UPSTREAMS
from quantking.data.adapters.sources.request_window import fixed_akshare_request_window
from quantking.data.adapters.warehouse.canonical_warehouse import CanonicalWarehouse
from quantking.data.adapters.warehouse.manifest import path_safe_id
from quantking.data.adapters.warehouse.publish_coordinator import (
    CanonicalPublishCoordinator,
    CanonicalPublishCoordinatorRequest,
)
from quantking.data.content import canonical_content_sha256
from quantking.data.eod.contracts import (
    EodDatasetConfig,
    EodPublishIntent,
    EodReferenceVersion,
    EodRunRequest,
)
from quantking.data.eod.release import S107K4_CODE_VERSION, S107K4_IDEMPOTENCY_KEY
from quantking.data.eod.service import EodExecutionConfig
from quantking.data.incremental import FetchChunk
from quantking.data.models import (
    DataProvider,
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    DataUpstream,
    IngestionBatchStatus,
    QualityResult,
    QualityRun,
    QualityRunStatus,
)
from quantking.data.quality.artifacts import QualityArtifactWriter
from quantking.data.quality.engine import QualityEngine
from quantking.data.quality.models import (
    QualityRequest,
    RuleOutcomeStatus,
)
from quantking.data.quality.rules import default_rule_set
from quantking.data.resolution.models import K4_RESOLUTION_RULE_VERSION
from quantking.jobs.models import JobStatus
from quantking.jobs.worker import JobCancelledAtCheckpoint
from quantking.runtime import EodRuntime, build_eod_runtime

_NOW = UtcTimestamp(datetime(2026, 8, 12, 10, 0, tzinfo=UTC))
_START = date(2026, 6, 11)
_END = date(2026, 8, 9)
_SYMBOLS = ("000001.SZ", "600000.SH")
_Metric = tuple[float, float]
_MetricFactory = Callable[[str, date], _Metric]
_CALENDAR_CODE_VERSION = "s1-07k4-calendar.v1"
_CALENDAR_RULE_VERSION = "quality-rules.v1"


def _calendar_table(
    open_ranges: Mapping[str, tuple[date, date]] | None = None,
    *,
    skip: set[tuple[str, date]] | None = None,
) -> pa.Table:
    """Synthetic exchange calendar; None means every natural day open."""
    rows: list[dict[str, object]] = []
    for exchange in ("SH", "SZ"):
        current = _START
        while current <= _END:
            if (exchange, current) not in (skip or set()):
                is_open = True
                if open_ranges is not None:
                    window = open_ranges.get(exchange)
                    is_open = window is not None and window[0] <= current <= window[1]
                rows.append({"exchange": exchange, "trade_date": current, "is_open": is_open})
            current += timedelta(days=1)
    raw = pa.Table.from_pylist(rows)
    descriptor = canonical_schema(DatasetKind.EXCHANGE_CALENDAR.value)
    return pa.Table.from_arrays(
        [raw.column(field.name).cast(field.type, safe=False) for field in descriptor],
        schema=pa.schema(
            [pa.field(field.name, field.type, nullable=field.nullable) for field in descriptor]
        ),
    )


def _publish_exchange_calendar(
    runtime: EodRuntime,
    *,
    version_id: EntityId,
    table: pa.Table,
    content_sha: str,
) -> str:
    """Publish a real exchange_calendar version the K4 quality runner can load."""
    version = DatasetVersion.building(
        version_id=version_id,
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        schema_version="exchange_calendar.v1",
        code_version=_CALENDAR_CODE_VERSION,
        rule_version=_CALENDAR_RULE_VERSION,
        parent_version_id=None,
        input_batch_ids=(),
        created_at=_NOW,
    )
    request = QualityRequest(
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        version_id=version_id,
        rule_version=_CALENDAR_RULE_VERSION,
        schema_version="exchange_calendar.v1",
        code_version=_CALENDAR_CODE_VERSION,
        content_sha256=content_sha,
        input_batch_ids=(),
        expected_securities=(),
        expected_exchanges=("SH", "SZ"),
        coverage_start=TradingDate(_START),
        coverage_end=TradingDate(_END),
        reference_data={},
        empty_result=None,
    )
    outcome = QualityEngine(rule_set=default_rule_set()).execute(
        request=request,
        table=table,
        reference_tables={},
    )
    assert outcome.status is QualityRunStatus.PASSED
    QualityArtifactWriter(
        artifacts_root=runtime.settings.paths.data_root / "artifacts" / "quality"
    ).write(outcome=outcome, row_evidence_tables={})
    run = QualityRun(
        run_id=outcome.run_id,
        version_id=version_id,
        status=QualityRunStatus.PASSED,
        rule_version=_CALENDAR_RULE_VERSION,
        schema_version="exchange_calendar.v1",
        content_sha256=content_sha,
        started_at=outcome.started_at,
        ended_at=outcome.ended_at,
        results=tuple(
            QualityResult(
                rule_code=item.rule_code,
                passed=item.status is RuleOutcomeStatus.PASSED,
                affected_count=item.affected_count,
                summary=item.summary,
                report_relative_path=item.row_evidence_relative_path,
            )
            for item in outcome.results
            if item.status is not RuleOutcomeStatus.SKIPPED
        ),
        summary_report_relative_path=None,
        failure=None,
    )
    with runtime.data_uow_factory() as uow:
        uow.data.save_version(version)
        uow.data.save_quality_run(run)
        uow.commit()
    coordinator = CanonicalPublishCoordinator(
        warehouse=CanonicalWarehouse(data_root=runtime.settings.paths.data_root),
        uow_factory=runtime.data_uow_factory,
    )
    published = coordinator.coordinate(
        CanonicalPublishCoordinatorRequest(
            version=version,
            tables=(table,),
            code_version=_CALENDAR_CODE_VERSION,
            rule_version=_CALENDAR_RULE_VERSION,
            quality_run_id=run.run_id,
            empty_result=None,
        )
    )
    return str(published.version.version_id)


def _execution_config(
    settings: AppSettings,
    *,
    calendar_version_id: str,
    calendar_sha: str,
    security_exceptions: Mapping[str, tuple[tuple[str, str], ...]] | None = None,
) -> EodExecutionConfig:
    config = EodExecutionConfig.production_k4(code_version=S107K4_CODE_VERSION, settings=settings)
    return replace(
        config,
        datasets={
            DatasetKind.DAILY_BAR_UNADJUSTED.value: EodDatasetConfig(
                scope=settings.eod_symbols,
                references={
                    "exchange_calendar": EodReferenceVersion(
                        dataset=DatasetKind.EXCHANGE_CALENDAR.value,
                        version_id=calendar_version_id,
                        content_sha256=calendar_sha,
                    )
                },
                security_exceptions=security_exceptions or {},
            )
        },
    )


def _constant(close: float) -> _MetricFactory:
    def value(_symbol: str, _trade_date: date) -> _Metric:
        return close, close * 10_000

    return value


class FakeBaostockGateway:
    """Duck-typed BaoStock MarketDataGateway with the production response contract."""

    def __init__(
        self,
        values: _MetricFactory,
        *,
        fail: bool = False,
        skip_when: Callable[[str, date], bool] | None = None,
    ) -> None:
        self._values = values
        self.fail = fail
        self._skip_when = skip_when or (lambda _symbol, _trade_date: False)
        self.calls: list[FetchChunk] = []

    @property
    def provider(self) -> DataProvider:
        return DataProvider.BAOSTOCK

    @property
    def upstream(self) -> DataUpstream:
        return DataUpstream.BAOSTOCK

    @property
    def endpoint(self) -> str:
        return "query_history_k_data_plus"

    def library_version(self) -> str:
        return "0.9.3"

    def fetch(self, chunk: FetchChunk) -> SupplierResponse:
        self.calls.append(chunk)
        if self.fail:
            raise SourceAdapterError(SourceErrorCode.BAOSTOCK_DISCONNECTED)
        symbol = chunk.scope[0]
        records: list[dict[str, object]] = []
        current = chunk.start.value
        while current <= chunk.end.value:
            if not self._skip_when(symbol, current):
                close, amount = self._values(symbol, current)
                records.append(
                    {
                        "stock_code": symbol,
                        "date": current.isoformat(),
                        "open": close,
                        "high": close,
                        "low": close,
                        "close": close,
                        "pre_close": close,
                        "volume": 10_000,
                        "amount": amount,
                    }
                )
            current += timedelta(days=1)
        return SupplierResponse.create_bound(
            chunk=chunk,
            records=tuple(records),
            contract=SupplierResponseContract(
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                contract_version="baostock-daily-bar.v1",
                required_fields=(
                    "stock_code",
                    "date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "pre_close",
                    "volume",
                    "amount",
                ),
                volume_unit=VolumeUnit.SHARE,
                amount_unit=AmountUnit.CNY_YUAN,
                unadjusted=True,
                lineage=SourceLineage(
                    provider=DataProvider.BAOSTOCK,
                    upstream=DataUpstream.BAOSTOCK,
                    endpoint="query_history_k_data_plus",
                    library_version="0.9.3",
                    fetched_at=_NOW.to_iso(),
                ),
            ),
            complete=True,
            partial_reason=None,
            provider_payload={"synthetic": True},
            request_window=fixed_akshare_request_window(chunk.start.value, chunk.end.value),
            upstream_row_count=len(records),
        )


class SyntheticAkshareModule:
    """AKShare-shaped module whose endpoint calls never leave the process."""

    def __init__(
        self,
        upstream: DataUpstream,
        values: _MetricFactory,
        *,
        fail: bool = False,
        skip_when: Callable[[str, date], bool] | None = None,
    ) -> None:
        self.__version__ = "1.18.83"
        self.upstream = upstream
        self.values = values
        self.fail = fail
        self._skip_when = skip_when or (lambda _symbol, _trade_date: False)
        self.calls: list[tuple[str, dict[str, str | float]]] = []

    def _call(self, endpoint: str, kwargs: dict[str, str | float]) -> FakeFrame:
        self.calls.append((endpoint, kwargs))
        if self.fail:
            raise SourceAdapterError(SourceErrorCode.AKSHARE_NETWORK)
        start = datetime.strptime(str(kwargs["start_date"]), "%Y%m%d").date()
        end = datetime.strptime(str(kwargs["end_date"]), "%Y%m%d").date()
        provider_symbol = str(kwargs["symbol"])
        symbol = "600000.SH" if "600000" in provider_symbol else "000001.SZ"
        rows: list[dict[str, object]] = []
        current = start
        while current <= end:
            if self._skip_when(symbol, current):
                current += timedelta(days=1)
                continue
            close, amount = self.values(symbol, current)
            rows.append(
                {
                    "date": current,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
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

    def stock_zh_a_hist_tx(self, **kwargs: str | float) -> FakeFrame:
        return self._call("stock_zh_a_hist_tx", kwargs)

    def stock_zh_a_daily(self, **kwargs: str | float) -> FakeFrame:
        return self._call("stock_zh_a_daily", kwargs)


class CancellingModule(SyntheticAkshareModule):
    def __init__(self, upstream: DataUpstream, values: _MetricFactory) -> None:
        super().__init__(upstream, values)
        self.cancel = lambda: None

    def _call(self, endpoint: str, kwargs: dict[str, str | float]) -> FakeFrame:
        result = super()._call(endpoint, kwargs)
        self.cancel()
        return result


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
        eod_symbols=_SYMBOLS,
    )


def _build_runtime(
    settings: AppSettings,
    gateways: Mapping[DataUpstream, object],
    *,
    calendar_table: pa.Table | None = None,
    security_exceptions: Mapping[str, tuple[tuple[str, str], ...]] | None = None,
) -> EodRuntime:
    """Build the K4 runtime and publish its bound exchange_calendar reference."""
    calendar_table = _calendar_table() if calendar_table is None else calendar_table
    calendar_sha = canonical_content_sha256(DatasetKind.EXCHANGE_CALENDAR, [calendar_table])
    calendar_version_id = new_entity_id("data_version")
    runtime = build_eod_runtime(
        settings,
        gateways=gateways,
        execution_config=_execution_config(
            settings,
            calendar_version_id=str(calendar_version_id),
            calendar_sha=calendar_sha,
            security_exceptions=security_exceptions,
        ),
    )
    published_id = _publish_exchange_calendar(
        runtime,
        version_id=calendar_version_id,
        table=calendar_table,
        content_sha=calendar_sha,
    )
    assert published_id == str(calendar_version_id)
    return runtime


def _runtime(
    tmp_path: Path,
    *,
    bao_values: _MetricFactory,
    ak_values: Mapping[DataUpstream, _MetricFactory],
    bao_fails: bool = False,
    calendar_table: pa.Table | None = None,
    skip_when: Callable[[str, date], bool] | None = None,
    ak_fail: Mapping[DataUpstream, bool] | None = None,
    security_exceptions: Mapping[str, tuple[tuple[str, str], ...]] | None = None,
) -> tuple[EodRuntime, FakeBaostockGateway, dict[DataUpstream, SyntheticAkshareModule]]:
    bao_gateway = FakeBaostockGateway(bao_values, fail=bao_fails, skip_when=skip_when)
    modules = {
        upstream: SyntheticAkshareModule(
            upstream,
            ak_values[upstream],
            fail=bool((ak_fail or {}).get(upstream)),
            skip_when=skip_when,
        )
        for upstream in (DataUpstream.TENCENT, DataUpstream.SINA)
    }
    gateways = {
        DataUpstream.BAOSTOCK: bao_gateway,
        **{
            upstream: AkshareMarketDataGateway(
                AkshareDailyBarAdapter(
                    upstream,
                    runner=FakeRunner(module),
                    module_loader=lambda module=module: module,
                    clock=lambda: _NOW,
                )
            )
            for upstream, module in modules.items()
        },
    }
    assert tuple(gateways) == PRODUCTION_UPSTREAMS
    runtime = _build_runtime(
        _settings(tmp_path),
        gateways,
        calendar_table=calendar_table,
        security_exceptions=security_exceptions,
    )
    return runtime, bao_gateway, modules


def _request() -> EodRunRequest:
    return EodRunRequest(
        start_date=_START,
        end_date=_END,
        datasets=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
        publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
        base_versions={},
    )


def _enqueue_and_run(runtime: EodRuntime, *, key: str) -> str:
    enqueued = runtime.eod_commands.enqueue(
        _request(),
        idempotency_key=key,
        requester="k4-integration-test",
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


def _agreeing_values() -> Mapping[DataUpstream, _MetricFactory]:
    return {upstream: _constant(10.0) for upstream in (DataUpstream.TENCENT, DataUpstream.SINA)}


def test_production_ab_agreement_publishes_k4_without_c(tmp_path: Path) -> None:
    runtime, bao_gateway, modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        ak_values=_agreeing_values(),
    )

    job_id = _enqueue_and_run(runtime, key="k4-ab-agreement")

    assert runtime.jobs.get_job(job_id).status is JobStatus.SUCCEEDED
    assert len(bao_gateway.calls) == len(modules[DataUpstream.TENCENT].calls) == 4
    assert modules[DataUpstream.SINA].calls == []
    batches, versions, resolutions = _stored_state(runtime, job_id)
    assert {item.upstream for item in batches} == {
        DataUpstream.BAOSTOCK,
        DataUpstream.TENCENT,
    }
    assert len(versions) == len(resolutions) == 1
    resolution = resolutions[0]
    assert resolution.rule_version == K4_RESOLUTION_RULE_VERSION
    _manifest, summary = _resolution_payload(runtime, resolution.artifact_relative_path)
    assert {item["upstream"] for item in summary["input_bindings"]} == {
        item.value for item in PRODUCTION_UPSTREAMS
    }
    assert [item["status"] for item in summary["input_bindings"]] == [
        "AVAILABLE",
        "AVAILABLE",
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


def test_baostock_failure_calls_tencent_and_sina_and_publishes_bc(tmp_path: Path) -> None:
    runtime, bao_gateway, modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        bao_fails=True,
        ak_values=_agreeing_values(),
    )

    job_id = _enqueue_and_run(runtime, key="k4-a-failed-bc")

    assert runtime.jobs.get_job(job_id).status is JobStatus.SUCCEEDED
    assert len(bao_gateway.calls) == 1
    assert len(modules[DataUpstream.TENCENT].calls) == 4
    assert len(modules[DataUpstream.SINA].calls) == 4
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
    assert {item.upstream for item in batches} == set(PRODUCTION_UPSTREAMS)
    assert DataUpstream.XTQUANT not in {item.upstream for item in batches}


def test_ab_disagreement_calls_sina_and_publishes_ac_consensus(tmp_path: Path) -> None:
    runtime, bao_gateway, modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        ak_values={
            DataUpstream.TENCENT: _constant(12.0),
            DataUpstream.SINA: _constant(10.0),
        },
    )

    job_id = _enqueue_and_run(runtime, key="k4-abc-ac-consensus")

    assert runtime.jobs.get_job(job_id).status is JobStatus.SUCCEEDED
    assert len(bao_gateway.calls) == 4
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


def test_exception_budget_blocks_before_first_sina_endpoint(tmp_path: Path) -> None:
    affected = {
        date(2026, 6, 13),
        date(2026, 6, 23),
        date(2026, 7, 3),
        date(2026, 7, 13),
        date(2026, 7, 23),
    }

    def tencent_values(symbol: str, trade_date: date) -> _Metric:
        close, amount = _constant(10.0)(symbol, trade_date)
        if symbol == "000001.SZ" and trade_date in affected:
            # Must exceed the frozen K4 amount tolerance (50 yuan) to count as
            # an affected span that triggers the C (Sina) budget preflight.
            amount += 100.0
        return close, amount

    runtime, bao_gateway, modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        ak_values={
            DataUpstream.TENCENT: tencent_values,
            DataUpstream.SINA: _constant(10.0),
        },
    )
    enqueued = runtime.eod_commands.enqueue(
        _request(),
        idempotency_key="k4-c-budget-preflight",
        requester="k4-integration-test",
    )

    with pytest.raises(NetworkBudgetExceededError):
        runtime.worker.run(enqueued.job_id)

    job = runtime.jobs.get_job(enqueued.job_id)
    assert job.status is JobStatus.FAILED
    assert job.failure is not None
    assert job.failure.error_type == "NetworkBudgetExceededError"
    assert len(bao_gateway.calls) == len(modules[DataUpstream.TENCENT].calls) == 4
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
        DataUpstream.TENCENT,
    }
    assert not published
    assert DataUpstream.XTQUANT not in {item.upstream for item in batches}


def test_read_only_release_verifier_accepts_k4_and_changes_no_file(tmp_path: Path) -> None:
    runtime, _bao_gateway, _modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        ak_values=_agreeing_values(),
    )
    _enqueue_and_run(runtime, key=S107K4_IDEMPOTENCY_KEY)
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


def test_reuse_published_skips_new_gateway_calls(tmp_path: Path) -> None:
    runtime, bao_gateway, modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        ak_values=_agreeing_values(),
    )
    first = runtime.eod_commands.enqueue(
        _request(),
        idempotency_key="k4-reuse-first",
        requester="k4-integration-test",
    )
    runtime.worker.run(first.job_id)
    assert runtime.jobs.get_job(first.job_id).status is JobStatus.SUCCEEDED
    bao_calls = len(bao_gateway.calls)
    tencent_calls = len(modules[DataUpstream.TENCENT].calls)

    second = runtime.eod_commands.enqueue(
        _request(),
        idempotency_key="k4-reuse-second",
        requester="k4-integration-test",
    )
    runtime.worker.run(second.job_id)

    assert runtime.jobs.get_job(second.job_id).status is JobStatus.SUCCEEDED
    assert len(bao_gateway.calls) == bao_calls
    assert len(modules[DataUpstream.TENCENT].calls) == tencent_calls


def test_tampered_frozen_manifest_is_rejected_before_any_gateway_call(tmp_path: Path) -> None:
    runtime, bao_gateway, modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        ak_values=_agreeing_values(),
    )
    enqueued = runtime.eod_commands.enqueue(
        _request(),
        idempotency_key="k4-tampered-frozen-command",
        requester="k4-integration-test",
    )
    database = runtime.settings.paths.data_root / "metadata" / "quantking.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE job_runs SET input_manifest_json = ? WHERE job_id = ?",
            (
                '{"request_json":"{}","request_sha256":"'
                + "0" * 64
                + '","schema_version":"eod-request-manifest.v1"}',
                enqueued.job_id,
            ),
        )
        connection.commit()

    with pytest.raises(EodExecutionError, match="FROZEN_COMMAND_INVALID"):
        runtime.worker.run(enqueued.job_id)

    assert runtime.jobs.get_job(enqueued.job_id).status is JobStatus.FAILED
    assert not bao_gateway.calls
    assert all(not module.calls for module in modules.values())


def test_cancel_at_source_checkpoint_leaves_no_published_candidate(tmp_path: Path) -> None:
    cancelling = CancellingModule(DataUpstream.TENCENT, _constant(10.0))
    modules = {
        DataUpstream.TENCENT: cancelling,
        DataUpstream.SINA: SyntheticAkshareModule(DataUpstream.SINA, _constant(10.0)),
    }
    gateways = {
        DataUpstream.BAOSTOCK: FakeBaostockGateway(_constant(10.0)),
        **{
            upstream: AkshareMarketDataGateway(
                AkshareDailyBarAdapter(
                    upstream,
                    runner=FakeRunner(module),
                    module_loader=lambda module=module: module,
                    clock=lambda: _NOW,
                )
            )
            for upstream, module in modules.items()
        },
    }
    settings = _settings(tmp_path)
    runtime = _build_runtime(settings, gateways)
    enqueued = runtime.eod_commands.enqueue(
        _request(),
        idempotency_key="k4-cancel-at-source-checkpoint",
        requester="k4-integration-test",
    )
    cancelling.cancel = lambda: runtime.jobs.request_cancel(
        enqueued.job_id,
        reason="synthetic safe cancellation",
    )

    with pytest.raises(JobCancelledAtCheckpoint):
        runtime.worker.run(enqueued.job_id)

    assert runtime.jobs.get_job(enqueued.job_id).status is JobStatus.CANCELLED
    with runtime.data_uow_factory() as uow:
        versions = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        uow.rollback()
    assert not any(item.status is DatasetVersionStatus.PUBLISHED for item in versions)


# ---------------------------------------------------------------------------
# KB-01: legal zero-row consensus reaches the real EOD worker chain.
# ---------------------------------------------------------------------------


def test_all_closed_calendar_ab_empty_publishes_zero_row_without_c(tmp_path: Path) -> None:
    runtime, bao_gateway, modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        ak_values=_agreeing_values(),
        calendar_table=_calendar_table(open_ranges={}),
        skip_when=lambda _symbol, _trade_date: True,
    )

    job_id = _enqueue_and_run(runtime, key="k4b-closed-window-zero-row")

    assert runtime.jobs.get_job(job_id).status is JobStatus.SUCCEEDED
    # A/B both landed legal empty Raw; C planned/actual remain zero.
    assert len(bao_gateway.calls) == 4
    assert len(modules[DataUpstream.TENCENT].calls) == 4
    assert modules[DataUpstream.SINA].calls == []
    batches, versions, resolutions = _stored_state(runtime, job_id)
    assert {item.upstream for item in batches} == {
        DataUpstream.BAOSTOCK,
        DataUpstream.TENCENT,
    }
    assert all(item.status is IngestionBatchStatus.SUCCEEDED for item in batches)
    assert all(item.empty_result is not None for item in batches)
    assert len(versions) == len(resolutions) == 1
    assert versions[0].row_count == 0
    resolution = resolutions[0]
    manifest, summary = _resolution_payload(runtime, resolution.artifact_relative_path)
    assert summary["status"] == "EMPTY_CONSENSUS"
    assert summary["zero_row_reason_code"] == "ZERO_ROW_LEGAL_EMPTY_CONSENSUS"
    assert manifest["zero_row_reason_code"] == "ZERO_ROW_LEGAL_EMPTY_CONSENSUS"
    bindings = {item["upstream"]: item["status"] for item in summary["input_bindings"]}
    assert bindings == {
        "BAOSTOCK": "EMPTY",
        "TENCENT": "EMPTY",
        "SINA": "NOT_CALLED",
    }


def test_open_calendar_ab_empty_blocks_without_legal_zero_row(tmp_path: Path) -> None:
    runtime, _bao_gateway, _modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        ak_values=_agreeing_values(),
        calendar_table=_calendar_table(open_ranges=None),
        skip_when=lambda _symbol, _trade_date: True,
    )

    enqueued = runtime.eod_commands.enqueue(
        _request(),
        idempotency_key="k4b-open-window-fake-empty",
        requester="k4b-integration-test",
    )
    with pytest.raises(EodExecutionError, match="RESOLUTION_BLOCKED"):
        runtime.worker.run(enqueued.job_id)

    assert runtime.jobs.get_job(enqueued.job_id).status is JobStatus.FAILED
    with runtime.data_uow_factory() as uow:
        batches = uow.data.list_batches_for_job(enqueued.job_id)
        published = uow.data.list_versions(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            status=DatasetVersionStatus.PUBLISHED,
        )
        resolutions = uow.data.list_resolution_runs_page(before=None, limit=10)
        uow.rollback()
    assert not published
    assert all(item.empty_result is None for item in batches)
    assert any(item.status is IngestionBatchStatus.FAILED for item in batches)
    assert len(resolutions) == 1
    _manifest, summary = _resolution_payload(runtime, resolutions[0].artifact_relative_path)
    assert "ALL_SOURCES_UNAVAILABLE" in summary["run_blockers"]
    assert summary.get("zero_row_reason_code") is None


def test_single_legal_empty_with_bc_failures_blocks_insufficient_consensus(
    tmp_path: Path,
) -> None:
    runtime, _bao_gateway, modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        ak_values=_agreeing_values(),
        calendar_table=_calendar_table(open_ranges={}),
        skip_when=lambda _symbol, _trade_date: True,
        ak_fail={DataUpstream.TENCENT: True, DataUpstream.SINA: True},
    )

    enqueued = runtime.eod_commands.enqueue(
        _request(),
        idempotency_key="k4b-single-empty-bc-failed",
        requester="k4b-integration-test",
    )
    with pytest.raises(EodExecutionError, match="RESOLUTION_BLOCKED"):
        runtime.worker.run(enqueued.job_id)

    assert runtime.jobs.get_job(enqueued.job_id).status is JobStatus.FAILED
    # C was called for the affected ranges but could not form a second
    # consensus: each of its two symbol fetches fails on the first chunk.
    assert len(modules[DataUpstream.SINA].calls) == 2
    with runtime.data_uow_factory() as uow:
        published = uow.data.list_versions(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            status=DatasetVersionStatus.PUBLISHED,
        )
        resolutions = uow.data.list_resolution_runs_page(before=None, limit=10)
        uow.rollback()
    assert not published
    assert len(resolutions) == 1
    _manifest, summary = _resolution_payload(runtime, resolutions[0].artifact_relative_path)
    assert "INSUFFICIENT_EMPTY_CONSENSUS" in summary["run_blockers"]


def test_mixed_batch_keeps_legal_empty_chunks_without_marking_batch_empty(
    tmp_path: Path,
) -> None:
    # SH closes in the second half of the window: its LAST chunk is a legal
    # empty range while SZ and the SH first chunk carry data. SH also has open
    # days in the lookback context (from 2026-05-27) so the first target row
    # keeps a derivable pre_close, exactly like a real trading calendar.
    open_ranges = {
        "SZ": (date(2026, 5, 27), _END),
        "SH": (date(2026, 5, 27), date(2026, 7, 11)),
    }

    def calendar_skip(symbol: str, trade_date: date) -> bool:
        window = open_ranges[symbol.rsplit(".", 1)[-1]]
        return not window[0] <= trade_date <= window[1]

    runtime, bao_gateway, modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        ak_values=_agreeing_values(),
        calendar_table=_calendar_table(open_ranges=open_ranges),
        skip_when=calendar_skip,
    )

    job_id = _enqueue_and_run(runtime, key="k4b-mixed-legal-empty-chunk")

    assert runtime.jobs.get_job(job_id).status is JobStatus.SUCCEEDED
    assert modules[DataUpstream.SINA].calls == []
    batches, versions, _resolutions = _stored_state(runtime, job_id)
    assert len(versions) == 1
    expected_rows = 60 + 31  # SZ every natural day; SH only through 2026-07-11
    assert versions[0].row_count == expected_rows
    # The batch carries data rows, so it must not be batch-level empty.
    assert all(item.empty_result is None for item in batches)
    # The legally empty SH chunk is recorded per chunk in the Raw manifest.
    data_root = runtime.settings.paths.data_root
    classified_by_batch = []
    for batch in batches:
        manifest_path = (
            data_root
            / "raw"
            / batch.upstream.value.lower()
            / batch.dataset.value
            / path_safe_id(str(batch.batch_id))
            / "manifest.json"
        )
        params = json.loads(manifest_path.read_text("utf-8")).get("request_params", {})
        classified = set(json.loads(str(params.get("legal_empty_by_chunk", "{}"))))
        classified_by_batch.append(classified)
    # The SH last chunk (chunk index 3 of 4) is classified NO_TRADING_SESSION.
    assert all(chunks == {"chunk-00003"} for chunks in classified_by_batch)
    assert len(bao_gateway.calls) == 4
    assert len(modules[DataUpstream.TENCENT].calls) == 4


# ---------------------------------------------------------------------------
# KB-02: security_exceptions reach source and final quality through the
# frozen v4 config, input fingerprint and quality artifacts.
# ---------------------------------------------------------------------------

_MISSING_SH_DAY = date(2026, 7, 15)


def _skip_missing_sh_day(symbol: str, trade_date: date) -> bool:
    return symbol == "600000.SH" and trade_date == _MISSING_SH_DAY


def test_missing_session_without_exception_fails_source_quality(tmp_path: Path) -> None:
    runtime, _bao_gateway, _modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        ak_values=_agreeing_values(),
        skip_when=_skip_missing_sh_day,
    )

    enqueued = runtime.eod_commands.enqueue(
        _request(),
        idempotency_key="k4b-missing-session-no-exception",
        requester="k4b-integration-test",
    )
    with pytest.raises(EodExecutionError, match="RESOLUTION_BLOCKED"):
        runtime.worker.run(enqueued.job_id)

    assert runtime.jobs.get_job(enqueued.job_id).status is JobStatus.FAILED
    with runtime.data_uow_factory() as uow:
        published = uow.data.list_versions(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            status=DatasetVersionStatus.PUBLISHED,
        )
        uow.rollback()
    assert not published


def test_missing_session_frozen_as_exception_publishes(tmp_path: Path) -> None:
    runtime, _bao_gateway, _modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        ak_values=_agreeing_values(),
        skip_when=_skip_missing_sh_day,
        security_exceptions={
            "600000.SH": ((_MISSING_SH_DAY.isoformat(), _MISSING_SH_DAY.isoformat()),)
        },
    )

    job_id = _enqueue_and_run(runtime, key="k4b-exception-covers-gap")

    assert runtime.jobs.get_job(job_id).status is JobStatus.SUCCEEDED
    _batches, versions, resolutions = _stored_state(runtime, job_id)
    assert len(versions) == 1
    assert versions[0].row_count == 120 - 1  # one frozen suspension day
    assert len(resolutions) == 1
    summary = _resolution_payload(runtime, resolutions[0].artifact_relative_path)[1]
    assert summary["conflict_count"] == 0


def test_wrong_exception_security_or_date_still_fails(tmp_path: Path) -> None:
    for exceptions in (
        {  # wrong security: the gap is on 600000.SH
            "000001.SZ": ((_MISSING_SH_DAY.isoformat(), _MISSING_SH_DAY.isoformat()),)
        },
        {  # wrong date: the gap is on 2026-07-15
            "600000.SH": (("2026-07-16", "2026-07-16"),)
        },
    ):
        runtime, _bao_gateway, _modules = _runtime(
            tmp_path,
            bao_values=_constant(10.0),
            ak_values=_agreeing_values(),
            skip_when=_skip_missing_sh_day,
            security_exceptions=exceptions,
        )
        enqueued = runtime.eod_commands.enqueue(
            _request(),
            idempotency_key=f"k4b-wrong-exception-{sorted(exceptions)[0]}",
            requester="k4b-integration-test",
        )
        with pytest.raises(EodExecutionError, match="RESOLUTION_BLOCKED"):
            runtime.worker.run(enqueued.job_id)
        assert runtime.jobs.get_job(enqueued.job_id).status is JobStatus.FAILED
        with runtime.data_uow_factory() as uow:
            published = uow.data.list_versions(
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                status=DatasetVersionStatus.PUBLISHED,
            )
            uow.rollback()
        assert not published


# ---------------------------------------------------------------------------
# Calendar completeness: a truncated calendar blocks before any network call.
# ---------------------------------------------------------------------------


def test_incomplete_calendar_blocks_with_zero_network_calls_and_zero_publish(
    tmp_path: Path,
) -> None:
    runtime, bao_gateway, modules = _runtime(
        tmp_path,
        bao_values=_constant(10.0),
        ak_values=_agreeing_values(),
        calendar_table=_calendar_table(
            open_ranges=None,
            skip={("SH", date(2026, 7, 1))},
        ),
    )

    enqueued = runtime.eod_commands.enqueue(
        _request(),
        idempotency_key="k4b-incomplete-calendar",
        requester="k4b-integration-test",
    )
    with pytest.raises(EodExecutionError, match="CALENDAR_REFERENCE_INCOMPLETE"):
        runtime.worker.run(enqueued.job_id)

    assert runtime.jobs.get_job(enqueued.job_id).status is JobStatus.FAILED
    # Fail-closed happens before the first vendor invocation.
    assert bao_gateway.calls == []
    assert all(not module.calls for module in modules.values())
    with runtime.data_uow_factory() as uow:
        published = uow.data.list_versions(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            status=DatasetVersionStatus.PUBLISHED,
        )
        batches = uow.data.list_batches_for_job(enqueued.job_id)
        uow.rollback()
    assert not published
    assert not batches
