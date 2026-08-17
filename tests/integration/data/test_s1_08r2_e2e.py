"""S1-08R2 C4: full synthetic E2E beyond the first-release fixed scope.

Chain: synthetic release manifest -> EodCommandService enqueue -> synthetic
worker (fake three-role providers) -> quality/resolution/publish -> explicit
version read-only manifest verifier -> DataCenter/API. Covers more than two
securities, more than the first-release chunk count, per-security gap
coverage, budget conservation, source projection and bounded pagination.
All roots are temporary; all providers are in-process fakes.
"""

from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.data.adapters.test_akshare_adapter import FakeFrame, FakeRunner
from tests.integration.data.test_eod_worker_k4 import (
    FakeBaostockGateway,
    _calendar_table,
    _constant,
    _publish_exchange_calendar,
)

from quantking.common.config import AppSettings
from quantking.common.ids import new_entity_id
from quantking.common.paths import RuntimePaths
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.canonical_content import canonical_content_sha256
from quantking.data.adapters.release_verifier import (
    ManifestReleaseVerificationError,
    verify_manifest_release,
)
from quantking.data.adapters.sources.akshare import (
    AkshareDailyBarAdapter,
    AkshareMarketDataGateway,
)
from quantking.data.adapters.sources.registry import PRODUCTION_UPSTREAMS
from quantking.data.eod.contracts import (
    EodDatasetConfig,
    EodPublishIntent,
    EodReferenceVersion,
    EodRunRequest,
)
from quantking.data.eod.release_manifest import build_release_manifest
from quantking.data.eod.service import EodExecutionConfig
from quantking.data.models import DatasetKind, DatasetVersionStatus, DataUpstream
from quantking.jobs.models import JobStatus
from quantking.runtime import EodRuntime, build_eod_runtime

_NOW = UtcTimestamp(datetime(2026, 8, 17, 9, 0, tzinfo=UTC))
# Captured at import time (before the offline guard patches the class) so the
# TestClient/asyncio loopback socketpair keeps working during the Web phase.
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_START = date(2026, 6, 11)
_END = date(2026, 8, 9)
# Four securities and a 60-day window: 2 chunks per symbol => 8 chunks per
# role, double the frozen first-release batch count of 4.
_SYMBOLS = ("000001.SZ", "000858.SZ", "300750.SZ", "600000.SH")
_SUSPENDED = "300750.SZ"
_SUSPENSION = (date(2026, 7, 1), date(2026, 7, 3))


class MultiSymbolAkshareModule:
    """AKShare-shaped fake that serves any symbol of the synthetic scope."""

    def __init__(self, upstream: DataUpstream, symbols: tuple[str, ...]) -> None:
        self.__version__ = "1.18.83"
        self.upstream = upstream
        self._symbols = symbols
        self.calls: list[tuple[str, dict[str, str | float]]] = []

    def _symbol_for(self, provider_symbol: str) -> str:
        digits = "".join(character for character in str(provider_symbol) if character.isdigit())
        for symbol in self._symbols:
            if symbol.startswith(digits):
                return symbol
        raise AssertionError(f"unexpected provider symbol {provider_symbol!r}")

    def _call(self, endpoint: str, kwargs: dict[str, str | float]) -> FakeFrame:
        self.calls.append((endpoint, kwargs))
        start = datetime.strptime(str(kwargs["start_date"]), "%Y%m%d").date()
        end = datetime.strptime(str(kwargs["end_date"]), "%Y%m%d").date()
        symbol = self._symbol_for(str(kwargs["symbol"]))
        rows: list[dict[str, object]] = []
        current = start
        while current <= end:
            if not (_SUSPENSION[0] <= current <= _SUSPENSION[1] and symbol == _SUSPENDED):
                rows.append(
                    {
                        "date": current,
                        "open": 10.0,
                        "high": 10.0,
                        "low": 10.0,
                        "close": 10.0,
                        "volume": (
                            100
                            if self.upstream is DataUpstream.TENCENT and symbol.startswith("000")
                            else 10_000
                        ),
                        "amount": 100_000.0,
                    }
                )
            current += timedelta(days=1)
        return FakeFrame(rows)

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
        eod_symbols=_SYMBOLS,
    )


def _manifest() -> object:
    return build_release_manifest(
        symbols=_SYMBOLS,
        target_start=_START,
        target_end=_END,
        lookback_days=15,
        calendar_version_id="exchange-calendar-baostock.v1",
        calendar_sha256="a" * 64,
        exemptions=((_SUSPENDED, _SUSPENSION[0], _SUSPENSION[1], "SUSPENSION"),),
        source_route_version="BAO-TX-SINA.k4.v1",
        adapter_version="baostock-akshare-daily-bar.k4.v1",
        code_version="s1-08r2.v1",
        quality_rule_version="quality-rules.daily-bar-unadjusted.k4.v1",
        threshold_rule_version="comparison-thresholds.daily-bar-unadjusted.k4.v1",
        resolution_rule_version="source-resolution.daily-bar-unadjusted.k4.v1",
        unit_policy_versions={"baostock": "b", "akshare": "a"},
    )


def _runtime(tmp_path: Path, manifest) -> tuple[EodRuntime, FakeBaostockGateway, dict]:
    calendar_table = _calendar_table()
    calendar_sha = canonical_content_sha256(DatasetKind.EXCHANGE_CALENDAR, [calendar_table])
    calendar_version_id = new_entity_id("data_version")
    settings = _settings(tmp_path)
    config = replace(
        EodExecutionConfig.from_release_manifest(manifest),
        datasets={
            DatasetKind.DAILY_BAR_UNADJUSTED.value: EodDatasetConfig(
                scope=manifest.symbols,
                references={
                    "exchange_calendar": EodReferenceVersion(
                        dataset=DatasetKind.EXCHANGE_CALENDAR.value,
                        version_id=str(calendar_version_id),
                        content_sha256=calendar_sha,
                    )
                },
                security_exceptions={
                    _SUSPENDED: ((_SUSPENSION[0].isoformat(), _SUSPENSION[1].isoformat()),)
                },
            )
        },
    )
    bao_gateway = FakeBaostockGateway(
        _constant(10.0),
        skip_when=lambda symbol, trade_date: (
            symbol == _SUSPENDED and _SUSPENSION[0] <= trade_date <= _SUSPENSION[1]
        ),
    )
    modules = {
        upstream: MultiSymbolAkshareModule(upstream, _SYMBOLS)
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
    runtime = build_eod_runtime(settings, gateways=gateways, execution_config=config)
    published = _publish_exchange_calendar(
        runtime,
        version_id=calendar_version_id,
        table=calendar_table,
        content_sha=calendar_sha,
    )
    assert published == str(calendar_version_id)
    return runtime, bao_gateway, modules


def _request() -> EodRunRequest:
    return EodRunRequest(
        start_date=_START,
        end_date=_END,
        datasets=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
        publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
        base_versions={},
    )


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


def test_full_chain_publishes_verifies_and_serves_beyond_the_first_release_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Permit only the loopback socketpair that TestClient/asyncio require.
    from typing import Any

    def connect(client: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple) and address and address[0] in {"127.0.0.1", "::1"}:
            return _REAL_CONNECT(client, address)
        raise RuntimeError("E2E test cannot access external network")

    def connect_ex(client: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple) and address and address[0] in {"127.0.0.1", "::1"}:
            return _REAL_CONNECT_EX(client, address)
        raise RuntimeError("E2E test cannot access external network")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)

    manifest = _manifest()
    runtime, bao_gateway, modules = _runtime(tmp_path, manifest)

    enqueued = runtime.eod_commands.enqueue(
        _request(),
        idempotency_key="s1-08r2-e2e-20260611-20260809-v1",
        requester="r2-e2e",
    )
    runtime.worker.run(enqueued.job_id)

    assert runtime.jobs.get_job(enqueued.job_id).status is JobStatus.SUCCEEDED
    # 4 symbols x 2 chunks = 8 endpoint calls per primary role; C stays zero.
    assert len(bao_gateway.calls) == 8
    assert len(modules[DataUpstream.TENCENT].calls) == 8
    assert modules[DataUpstream.SINA].calls == []

    with runtime.data_uow_factory() as uow:
        versions = uow.data.list_versions(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            status=DatasetVersionStatus.PUBLISHED,
        )
        uow.rollback()
    assert len(versions) == 1
    version_id = str(versions[0].version_id)
    data_root = runtime.settings.paths.data_root

    # -- DataCenter service + Web API ----------------------------------------
    lineage = runtime.data_center.get_version_lineage(version_id)
    rendered = json.dumps(lineage, ensure_ascii=False, sort_keys=True)
    assert str(data_root) not in rendered
    assert "provider_payload" not in rendered

    projection = runtime.data_center.get_version_source_projection(version_id)
    by_role = {item["role"]: item for item in projection["sources"]}
    assert by_role["A"]["planned_endpoints"] == 8
    assert by_role["A"]["actual_endpoints"] == 8
    assert by_role["B"]["planned_endpoints"] == 8
    assert by_role["B"]["actual_endpoints"] == 8
    assert by_role["C"]["actual_endpoints"] == 0
    assert projection["legacy_disposition"]["status"] == "REJECTED"
    assert projection["release_manifest_sha256"] == manifest.manifest_sha256()

    client = TestClient(runtime.web_app)
    detail = client.get(f"/api/data/versions/{version_id}")
    assert detail.status_code == 200
    assert detail.json()["version_id"] == version_id

    sources = client.get(f"/api/data/versions/{version_id}/sources")
    assert sources.status_code == 200
    payload = sources.json()
    assert payload["legacy_disposition"]["adr"] == "ADR-027"
    assert {item["role"] for item in payload["sources"]} == {"A", "B", "C"}

    coverage = client.get(f"/api/data/versions/{version_id}/security-coverage", params={"limit": 2})
    assert coverage.status_code == 200
    page_one = coverage.json()
    assert len(page_one["items"]) == 2
    assert page_one["next_cursor"]
    coverage_two = client.get(
        f"/api/data/versions/{version_id}/security-coverage",
        params={"limit": 2, "cursor": page_one["next_cursor"]},
    )
    assert coverage_two.status_code == 200
    page_two = coverage_two.json()
    assert len(page_two["items"]) == 2
    assert page_two["next_cursor"] is None
    merged = page_one["items"] + page_two["items"]
    assert [item["code"] for item in merged] == list(_SYMBOLS)
    coverage_by_code = {item["code"]: item for item in merged}
    assert coverage_by_code["000001.SZ"]["row_count"] == 60
    assert coverage_by_code["000001.SZ"]["segments"] == [
        {"start": "2026-06-11", "end": "2026-08-09"}
    ]
    suspended = coverage_by_code[_SUSPENDED]
    assert suspended["row_count"] == 57
    assert suspended["segments"] == [
        {"start": "2026-06-11", "end": "2026-06-30"},
        {"start": "2026-07-04", "end": "2026-08-09"},
    ]

    preview = client.get(f"/api/data/versions/{version_id}/preview", params={"limit": 5})
    assert preview.status_code == 200
    assert len(preview.json()["items"]) == 5

    # -- Idempotent replay: same command, no new version ---------------------
    replay = runtime.eod_commands.enqueue(
        _request(),
        idempotency_key="s1-08r2-e2e-20260611-20260809-v1",
        requester="r2-e2e",
    )
    assert replay.created is False
    assert replay.job_id == enqueued.job_id
    assert replay.status is JobStatus.SUCCEEDED
    with runtime.data_uow_factory() as uow:
        replay_versions = uow.data.list_versions(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            status=DatasetVersionStatus.PUBLISHED,
        )
        uow.rollback()
    assert len(replay_versions) == 1

    # -- Read-only manifest verifier with invariance proof -------------------
    data_engine = runtime.data_uow_factory._session_factory.kw["bind"]
    job_engine = runtime.jobs._transactions._session_factory.kw["bind"]
    data_engine.dispose()
    job_engine.dispose()
    del runtime, client
    import gc

    gc.collect()
    import sqlite3

    database = data_root / "metadata" / "quantking.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    before = _snapshot(data_root)

    result = verify_manifest_release(
        data_root=data_root,
        version_id=version_id,
        manifest=manifest,
    )

    assert _snapshot(data_root) == before
    assert not (data_root / "metadata" / "quantking.db-wal").exists()
    assert not (data_root / "metadata" / "quantking.db-shm").exists()

    assert result["symbols"] == list(_SYMBOLS)
    assert result["batches_actual_per_upstream"] == {"BAOSTOCK": 1, "TENCENT": 1}
    assert result["endpoints_actual_per_upstream"] == {"BAOSTOCK": 8, "TENCENT": 8}
    assert result["budgets_planned"]["endpoint_total"] == 24
    assert result["rows_per_security"] == {
        "000001.SZ": 60,
        "000858.SZ": 60,
        "300750.SZ": 57,
        "600000.SH": 60,
    }
    assert result["legacy_disposition"]["status"] == "REJECTED"

    # A tampered manifest against the same release must fail closed.
    tampered = build_release_manifest(
        symbols=(*_SYMBOLS[:3], "688981.SH"),
        target_start=_START,
        target_end=_END,
        lookback_days=15,
        calendar_version_id="exchange-calendar-baostock.v1",
        calendar_sha256="a" * 64,
        source_route_version="BAO-TX-SINA.k4.v1",
        adapter_version="baostock-akshare-daily-bar.k4.v1",
        code_version="s1-08r2.v1",
        quality_rule_version="quality-rules.daily-bar-unadjusted.k4.v1",
        threshold_rule_version="comparison-thresholds.daily-bar-unadjusted.k4.v1",
        resolution_rule_version="source-resolution.daily-bar-unadjusted.k4.v1",
        unit_policy_versions={"baostock": "b"},
    )
    try:
        verify_manifest_release(data_root=data_root, version_id=version_id, manifest=tampered)
    except ManifestReleaseVerificationError:
        pass
    else:
        raise AssertionError("tampered manifest must fail closed")

    # Forbidden identifiers never reach the database layer.
    for bad in ("latest", "../escape", "data_version:not-a-uuid", ""):
        try:
            verify_manifest_release(data_root=data_root, version_id=bad, manifest=manifest)
        except ManifestReleaseVerificationError:
            continue
        raise AssertionError(f"version_id {bad!r} must be rejected")
