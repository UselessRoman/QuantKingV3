"""Focused K2 production EOD integration without network or formal data roots."""

from __future__ import annotations

import gc
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest
from tests.data.adapters.test_akshare_adapter import FakeFrame, FakeModule, _rows

from quantking.common.config import AppSettings
from quantking.common.errors import ConfigurationError
from quantking.common.ids import new_entity_id
from quantking.common.paths import RuntimePaths
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.adapters.eod_base import PublishedEodBaseResolver
from quantking.data.adapters.eod_k2 import (
    EodExecutionError,
    EodWorkerHandler,
    register_eod_handler,
)
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.adapters.release_verifier import (
    S108ReleaseVerificationError,
    verify_s108_release,
)
from quantking.data.adapters.sources.akshare import (
    AkshareDailyBarAdapter,
    AkshareMarketDataGateway,
)
from quantking.data.adapters.sources.budget import NetworkBudgetExceededError
from quantking.data.adapters.sources.ingestion import AkshareIngestionService
from quantking.data.adapters.sources.registry import AKSHARE_RUNTIME_UPSTREAMS
from quantking.data.adapters.warehouse.canonical_warehouse import CanonicalWarehouse
from quantking.data.adapters.warehouse.publish_coordinator import CanonicalPublishCoordinator
from quantking.data.adapters.warehouse.raw_store import RawBatchPublisher
from quantking.data.center import DataCenterReadIntegrityError
from quantking.data.eod.contracts import (
    EodDatasetConfig,
    EodPublishIntent,
    EodReferenceVersion,
    EodRunRequest,
)
from quantking.data.eod.release import S108_IDEMPOTENCY_KEY
from quantking.data.eod.service import EodCommandService, EodExecutionConfig
from quantking.data.models import (
    BatchFailureInfo,
    BatchFailureKind,
    DatasetKind,
    DatasetVersionStatus,
    DataUpstream,
    QualityRunStatus,
)
from quantking.data.quality.artifacts import QualityArtifactWriter
from quantking.data.quality.engine import QualityEngine
from quantking.data.quality.models import (
    QualityOutcomeResult,
    QualityRunArtifact,
    RuleOutcomeStatus,
)
from quantking.data.quality.rules import s108_daily_rule_set
from quantking.jobs.adapters.persistence.sqlite import (
    SqliteUnitOfWorkFactory,
    create_sqlite_engine,
    initialize_database,
)
from quantking.jobs.models import FailureInfo, JobStatus
from quantking.jobs.services import JobService
from quantking.jobs.worker import (
    JobCancelledAtCheckpoint,
    Worker,
    WorkerContext,
    WorkerRegistry,
)
from quantking.runtime import build_eod_runtime

_NOW = UtcTimestamp(datetime(2026, 8, 9, 10, 0, tzinfo=UTC))
_REFERENCE = EodReferenceVersion(
    dataset=DatasetKind.EXCHANGE_CALENDAR.value,
    version_id="data_version:99999999-9999-4999-8999-999999999999",
    content_sha256="9" * 64,
)


class FailingModule(FakeModule):
    def _call(self, endpoint: str, kwargs: dict[str, str | float]):
        self.calls.append((endpoint, kwargs))
        raise ConnectionError("third-party URL and proxy value must not escape")


class PassingQualityRunner:
    def run(self, *, request: object, table: object) -> QualityRunArtifact:
        del table
        return QualityRunArtifact(
            run_id=new_entity_id("data_quality"),
            version_id=request.version_id,  # type: ignore[attr-defined]
            rule_version=request.rule_version,  # type: ignore[attr-defined]
            schema_version=request.schema_version,  # type: ignore[attr-defined]
            content_sha256=request.content_sha256,  # type: ignore[attr-defined]
            started_at=_NOW,
            ended_at=_NOW,
            status=QualityRunStatus.PASSED,
            results=(
                QualityOutcomeResult(
                    rule_code="synthetic_pass",
                    status=RuleOutcomeStatus.PASSED,
                    affected_count=0,
                    summary="synthetic source/final quality passed",
                    row_evidence_relative_path=None,
                ),
            ),
            failure=None,
        )


class SequencedQualityRunner(PassingQualityRunner):
    def __init__(self, statuses: list[QualityRunStatus]) -> None:
        self._statuses = iter(statuses)

    def run(self, *, request: object, table: object) -> QualityRunArtifact:
        status = next(self._statuses)
        if status is QualityRunStatus.PASSED:
            return super().run(request=request, table=table)
        del table
        return QualityRunArtifact(
            run_id=new_entity_id("data_quality"),
            version_id=request.version_id,  # type: ignore[attr-defined]
            rule_version=request.rule_version,  # type: ignore[attr-defined]
            schema_version=request.schema_version,  # type: ignore[attr-defined]
            content_sha256=request.content_sha256,  # type: ignore[attr-defined]
            started_at=_NOW,
            ended_at=_NOW,
            status=status,
            results=(
                QualityOutcomeResult(
                    rule_code="synthetic_terminal",
                    status=(
                        RuleOutcomeStatus.FAILED
                        if status is QualityRunStatus.FAILED
                        else RuleOutcomeStatus.SKIPPED
                    ),
                    affected_count=0,
                    summary="synthetic terminal quality result",
                    row_evidence_relative_path=None,
                ),
            ),
            failure=(
                BatchFailureInfo(
                    BatchFailureKind.QUALITY,
                    "synthetic quality failure",
                    False,
                )
                if status is QualityRunStatus.FAILED
                else None
            ),
        )


class RaisingFinalQualityRunner(PassingQualityRunner):
    def __init__(self) -> None:
        self._calls = 0

    def run(self, *, request: object, table: object) -> QualityRunArtifact:
        self._calls += 1
        if self._calls == 3:
            raise RuntimeError("sensitive quality implementation detail")
        return super().run(request=request, table=table)


class CancellingModule(FakeModule):
    def __init__(self, rows: list[dict[str, object]]) -> None:
        super().__init__(rows)
        self.cancel = lambda: None

    def _call(self, endpoint: str, kwargs: dict[str, str | float]):
        result = super()._call(endpoint, kwargs)
        self.cancel()
        return result


class WindowFilteringModule:
    def __init__(
        self,
        upstream: DataUpstream,
        *,
        affected: set[date] | None = None,
    ) -> None:
        self.__version__ = "1.18.83"
        self.upstream = upstream
        self.calls: list[tuple[str, dict[str, str | float]]] = []
        self.affected = affected

    def _call(self, endpoint: str, kwargs: dict[str, str | float]) -> FakeFrame:
        self.calls.append((endpoint, kwargs))
        start = datetime.strptime(str(kwargs["start_date"]), "%Y%m%d").date()
        end = datetime.strptime(str(kwargs["end_date"]), "%Y%m%d").date()
        symbol = str(kwargs["symbol"])
        affected = self.affected
        if affected is None:
            affected = {
                date(2026, 6, 11),
                date(2026, 6, 13),
                date(2026, 6, 15),
                date(2026, 6, 17),
                date(2026, 6, 19),
            }
        rows: list[dict[str, object]] = []
        current = start
        while current <= end:
            close = (
                10.3
                if self.upstream is DataUpstream.TENCENT
                and symbol == "sz000001"
                and current in affected
                else 10.2
            )
            if self.upstream is DataUpstream.EASTMONEY:
                rows.append(
                    {
                        "日期": current.isoformat(),
                        "开盘": 10.0,
                        "最高": 10.5,
                        "最低": 9.9,
                        "收盘": close,
                        "昨收": 10.0,
                        "成交量": 100,
                        "成交额": 1000.0,
                    }
                )
            else:
                rows.append(
                    {
                        "date": current,
                        "open": 10.0,
                        "high": 10.5,
                        "low": 9.9,
                        "close": close,
                        "pre_close": 10.0,
                        "volume": (
                            100
                            if self.upstream is DataUpstream.TENCENT and symbol == "sz000001"
                            else 10_000
                        ),
                        "amount": 1000.0,
                        "turnover": 0.1,
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


class FailingAfterWindowModule(WindowFilteringModule):
    def __init__(self, upstream: DataUpstream, *, successful_calls: int) -> None:
        super().__init__(upstream, affected=set())
        self.successful_calls = successful_calls

    def _call(self, endpoint: str, kwargs: dict[str, str | float]) -> FakeFrame:
        if len(self.calls) >= self.successful_calls:
            self.calls.append((endpoint, kwargs))
            raise ConnectionError("synthetic bounded mid-plan failure")
        return super()._call(endpoint, kwargs)


class FirstSourceFailedThenRealQualityRunner:
    def __init__(self) -> None:
        self._calls = 0
        self._engine = QualityEngine(rule_set=s108_daily_rule_set())

    def run(self, *, request: object, table: pa.Table) -> QualityRunArtifact:
        self._calls += 1
        artifact = self._engine.execute(
            request=request,  # type: ignore[arg-type]
            table=table,
            reference_tables={},
        )
        if self._calls != 1:
            return artifact
        failed_result = replace(
            artifact.results[0],
            status=RuleOutcomeStatus.FAILED,
            affected_count=1,
            summary="synthetic source-only quality failure",
        )
        return replace(
            artifact,
            status=QualityRunStatus.FAILED,
            results=(failed_result, *artifact.results[1:]),
        )


def _execution_config() -> EodExecutionConfig:
    return EodExecutionConfig(
        code_version="s1-07k2-test",
        adapter_version="akshare-daily-bar.v1",
        rule_version="quality-rules.v1",
        resolution_rule_version="source-resolution.daily-bar-unadjusted.k2.v1",
        source_route_version="EAST-TX-SINA.k2.v1",
        baostock_version="not-applicable",
        baostock_unit_policy_version="not-applicable",
        planner_lookback_days=0,
        adapter_context_lookback_days=15,
        max_days_per_chunk=31,
        max_items_per_chunk=1,
        endpoint_invocation_limit=12,
        c_endpoint_invocation_limit=4,
        d_endpoint_invocation_limit=0,
        estimated_http_request_limit=24,
        estimated_baostock_protocol_message_limit=0,
        legacy_import_manifest_sha256=None,
        datasets={
            DatasetKind.DAILY_BAR_UNADJUSTED.value: EodDatasetConfig(
                scope=("600000.SH",),
                references={"exchange_calendar": _REFERENCE},
            )
        },
    )


def _runtime(
    tmp_path: Path,
    *,
    modules: dict[DataUpstream, FakeModule] | None = None,
    quality_runner: object | None = None,
    fault_injector=None,
    base_resolver: object | None = None,
    execution_config: EodExecutionConfig | None = None,
):
    database = tmp_path / "metadata.sqlite3"
    initialize_database(database)
    jobs = JobService(
        SqliteUnitOfWorkFactory(create_sqlite_engine(database, busy_timeout_ms=250)),
        clock=lambda: _NOW.value,
    )
    data_uow = DataSqliteUnitOfWorkFactory(create_data_engine(database, busy_timeout_ms=250))
    selected_modules = modules or {
        upstream: FakeModule(_rows(upstream)) for upstream in AKSHARE_RUNTIME_UPSTREAMS
    }
    ingestions = {
        upstream: AkshareIngestionService(
            gateway=AkshareMarketDataGateway(
                AkshareDailyBarAdapter(
                    upstream,
                    module_loader=lambda module=selected_modules[upstream]: module,
                    clock=lambda: _NOW,
                )
            ),
            raw_publisher=RawBatchPublisher(data_root=tmp_path),
            data_root=tmp_path,
            uow_factory=data_uow,
            clock=lambda: _NOW,
        )
        for upstream in AKSHARE_RUNTIME_UPSTREAMS
    }
    handler = EodWorkerHandler(
        ingestions=ingestions,
        quality_runner=quality_runner or PassingQualityRunner(),  # type: ignore[arg-type]
        quality_writer=QualityArtifactWriter(artifacts_root=tmp_path / "artifacts" / "quality"),
        publish_coordinator=CanonicalPublishCoordinator(
            warehouse=CanonicalWarehouse(data_root=tmp_path),
            uow_factory=data_uow,
        ),
        data_uow_factory=data_uow,
        data_root=tmp_path,
        clock=lambda: _NOW,
        log_root=tmp_path,
        fault_injector=fault_injector,
    )
    registry = WorkerRegistry()
    register_eod_handler(registry, handler)
    worker = Worker(jobs, registry, worker_id="k2-worker")
    commands = EodCommandService(
        jobs,
        config=execution_config or _execution_config(),
        base_resolver=base_resolver,  # type: ignore[arg-type]
    )
    return jobs, data_uow, selected_modules, handler, worker, commands


def _enqueue(commands: EodCommandService, key: str):
    return commands.enqueue(
        EodRunRequest(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 8),
            datasets=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        ),
        idempotency_key=key,
        requester="test",
    )


def _daily_row(code: str, trade_date: date, close: float) -> dict[str, object]:
    return {
        "code": code,
        "trade_date": trade_date,
        "open": close - 0.1,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "pre_close": close - 0.3,
        "volume": 10_000,
        "amount": close * 10_000,
    }


def _assert_no_nonterminal_candidate(data_uow: DataSqliteUnitOfWorkFactory) -> None:
    with data_uow() as uow:
        versions = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        uow.rollback()
    assert all(
        item.status in {DatasetVersionStatus.PUBLISHED, DatasetVersionStatus.FAILED}
        for item in versions
    )


def _formal_settings(tmp_path: Path) -> AppSettings:
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


def _formal_gateways() -> dict[DataUpstream, AkshareMarketDataGateway]:
    return {
        upstream: AkshareMarketDataGateway(
            AkshareDailyBarAdapter(
                upstream,
                module_loader=lambda upstream=upstream: FakeModule(_rows(upstream)),
                clock=lambda: _NOW,
            )
        )
        for upstream in AKSHARE_RUNTIME_UPSTREAMS
    }


def _formal_k2_config(
    settings: AppSettings,
    *,
    code_version: str = "s1-07k2-test",
) -> EodExecutionConfig:
    return replace(
        _execution_config(),
        code_version=code_version,
        rule_version="quality-rules.daily-bar-first-release.v1",
        datasets={
            DatasetKind.DAILY_BAR_UNADJUSTED.value: EodDatasetConfig(
                scope=settings.eod_symbols,
            )
        },
    )


@pytest.mark.parametrize(
    "dataset",
    tuple(item for item in DatasetKind if item is not DatasetKind.DAILY_BAR_UNADJUSTED),
)
def test_static_dataset_is_rejected_before_any_gateway_call(
    tmp_path: Path,
    dataset: DatasetKind,
) -> None:
    _jobs, _data_uow, modules, _handler, _worker, commands = _runtime(tmp_path)
    request = EodRunRequest(
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 8),
        datasets=(dataset.value,),
        publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
        base_versions={},
    )

    with pytest.raises(ValueError, match="execution config"):
        commands.enqueue(request, idempotency_key=f"static-{dataset.value}", requester="test")

    assert all(not module.calls for module in modules.values())


def test_partial_source_batches_never_publish_and_leave_no_candidate_open(
    tmp_path: Path,
) -> None:
    source_rows = {upstream: _rows(upstream) for upstream in AKSHARE_RUNTIME_UPSTREAMS}
    source_rows[DataUpstream.EASTMONEY][0].pop("昨收")
    source_rows[DataUpstream.TENCENT][0].pop("pre_close")
    source_rows[DataUpstream.SINA][0].pop("pre_close")
    modules = {source: FakeModule(rows) for source, rows in source_rows.items()}
    jobs, data_uow, _modules, _handler, worker, commands = _runtime(
        tmp_path,
        modules=modules,
    )
    enqueued = _enqueue(commands, "partial-sources")

    with pytest.raises(EodExecutionError):
        worker.run(enqueued.job_id)

    assert jobs.get_job(enqueued.job_id).status is JobStatus.FAILED
    with data_uow() as uow:
        batches = uow.data.list_batches_for_job(enqueued.job_id)
        versions = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        uow.rollback()
    assert len(batches) == 3
    assert all(batch.failure is not None for batch in batches)
    assert not any(item.status is DatasetVersionStatus.PUBLISHED for item in versions)
    _assert_no_nonterminal_candidate(data_uow)


@pytest.mark.parametrize("terminal", [QualityRunStatus.FAILED, QualityRunStatus.SKIPPED])
def test_final_quality_nonpass_blocks_publish_and_closes_candidate(
    tmp_path: Path,
    terminal: QualityRunStatus,
) -> None:
    quality = SequencedQualityRunner([QualityRunStatus.PASSED, QualityRunStatus.PASSED, terminal])
    jobs, data_uow, _modules, _handler, worker, commands = _runtime(
        tmp_path,
        quality_runner=quality,
    )
    enqueued = _enqueue(commands, f"final-quality-{terminal.value.lower()}")

    with pytest.raises(EodExecutionError, match="FINAL_QUALITY_NOT_PASSED"):
        worker.run(enqueued.job_id)

    assert jobs.get_job(enqueued.job_id).status is JobStatus.FAILED
    with data_uow() as uow:
        versions = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        uow.rollback()
    assert versions and all(item.status is DatasetVersionStatus.FAILED for item in versions)
    _assert_no_nonterminal_candidate(data_uow)


def test_final_quality_exception_blocks_publish_and_closes_candidate(tmp_path: Path) -> None:
    jobs, data_uow, _modules, _handler, worker, commands = _runtime(
        tmp_path,
        quality_runner=RaisingFinalQualityRunner(),
    )
    enqueued = _enqueue(commands, "final-quality-exception")

    with pytest.raises(EodExecutionError, match="FINAL_QUALITY_NOT_PASSED"):
        worker.run(enqueued.job_id)

    assert jobs.get_job(enqueued.job_id).status is JobStatus.FAILED
    with data_uow() as uow:
        quality_runs = uow.data.list_quality_runs(
            version_id=str(
                uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)[0].version_id
            )
        )
        uow.rollback()
    assert all(
        run.failure is None or "sensitive" not in run.failure.message for run in quality_runs
    )
    _assert_no_nonterminal_candidate(data_uow)


def test_tampered_frozen_manifest_is_rejected_before_any_gateway_call(
    tmp_path: Path,
) -> None:
    jobs, data_uow, modules, _handler, worker, commands = _runtime(tmp_path)
    enqueued = _enqueue(commands, "tampered-frozen-command")
    database = tmp_path / "metadata.sqlite3"
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
        worker.run(enqueued.job_id)

    assert jobs.get_job(enqueued.job_id).status is JobStatus.FAILED
    assert all(not module.calls for module in modules.values())
    _assert_no_nonterminal_candidate(data_uow)


def test_incremental_base_preserves_history_and_creates_child_version(tmp_path: Path) -> None:
    jobs, data_uow, modules, _handler, worker, commands = _runtime(tmp_path)
    first = _enqueue(commands, "base-first")
    worker.run(first.job_id)
    with data_uow() as uow:
        original = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)[0]
        uow.rollback()
    original_manifest = (
        tmp_path
        / "warehouse"
        / DatasetKind.DAILY_BAR_UNADJUSTED.value
        / str(original.version_id).replace(":", "--")
        / "manifest.json"
    ).read_bytes()
    base_commands = EodCommandService(
        jobs,
        config=_execution_config(),
        base_resolver=PublishedEodBaseResolver(data_root=tmp_path, uow_factory=data_uow),
    )
    second = base_commands.enqueue(
        EodRunRequest(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 8),
            datasets=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={DatasetKind.DAILY_BAR_UNADJUSTED.value: str(original.version_id)},
        ),
        idempotency_key="base-second",
        requester="test",
    )
    worker.run(second.job_id)

    with data_uow() as uow:
        versions = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        uow.rollback()
    published = [item for item in versions if item.status is DatasetVersionStatus.PUBLISHED]
    assert len(published) == 2
    child = next(item for item in published if item.version_id != original.version_id)
    assert child.parent_version_id == original.version_id
    assert (
        original_manifest
        == (
            tmp_path
            / "warehouse"
            / DatasetKind.DAILY_BAR_UNADJUSTED.value
            / str(original.version_id).replace(":", "--")
            / "manifest.json"
        ).read_bytes()
    )
    assert all(module.calls for module in modules.values() if module.calls)


def test_forged_base_credentials_fail_before_new_gateway_calls(tmp_path: Path) -> None:
    jobs, data_uow, modules, _handler, worker, commands = _runtime(tmp_path)
    first = _enqueue(commands, "base-credential-first")
    worker.run(first.job_id)
    with data_uow() as uow:
        original = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)[0]
        uow.rollback()
    exact = PublishedEodBaseResolver(data_root=tmp_path, uow_factory=data_uow)

    class ForgedResolver:
        def resolve(self, **kwargs):
            return replace(exact.resolve(**kwargs), content_sha256="f" * 64)

    forged_commands = EodCommandService(
        jobs,
        config=_execution_config(),
        base_resolver=ForgedResolver(),
    )
    second = forged_commands.enqueue(
        EodRunRequest(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 8),
            datasets=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={DatasetKind.DAILY_BAR_UNADJUSTED.value: str(original.version_id)},
        ),
        idempotency_key="forged-base-credential",
        requester="test",
    )
    initial_calls = {source: len(module.calls) for source, module in modules.items()}

    with pytest.raises(EodExecutionError, match="BASE_VERSION_INVALID"):
        worker.run(second.job_id)

    assert {source: len(module.calls) for source, module in modules.items()} == initial_calls
    assert jobs.get_job(second.job_id).status is JobStatus.FAILED
    _assert_no_nonterminal_candidate(data_uow)


def test_authoritative_target_slice_removes_missing_base_row() -> None:
    schema = canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value)
    base = pa.Table.from_pylist(
        [
            _daily_row("600000.SH", date(2026, 8, 6), 10.0),
            _daily_row("600000.SH", date(2026, 8, 7), 10.1),
        ],
        schema=schema,
    )
    selected = pa.Table.from_pylist(
        [_daily_row("600000.SH", date(2026, 8, 8), 10.2)],
        schema=schema,
    )

    merged = EodWorkerHandler._merge_base(
        base,
        selected,
        symbols=("600000.SH",),
        start_date=date(2026, 8, 7),
        end_date=date(2026, 8, 8),
    )

    assert [row["trade_date"] for row in merged.to_pylist()] == [
        date(2026, 8, 6),
        date(2026, 8, 8),
    ]


@pytest.mark.parametrize(
    "row",
    [
        _daily_row("000001.SZ", date(2026, 8, 7), 10.0),
        _daily_row("600000.SH", date(2026, 8, 9), 10.0),
    ],
)
def test_merge_rejects_selected_rows_outside_frozen_inventory(
    row: dict[str, object],
) -> None:
    schema = canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value)
    base = pa.Table.from_pylist([], schema=schema)
    selected = pa.Table.from_pylist([row], schema=schema)

    with pytest.raises(EodExecutionError, match="SELECTED_SCOPE_INVALID"):
        EodWorkerHandler._merge_base(
            base,
            selected,
            symbols=("600000.SH",),
            start_date=date(2026, 8, 7),
            end_date=date(2026, 8, 8),
        )


def test_cancel_at_source_checkpoint_leaves_no_published_or_open_candidate(
    tmp_path: Path,
) -> None:
    cancelling = CancellingModule(_rows(DataUpstream.EASTMONEY))
    modules = {
        DataUpstream.EASTMONEY: cancelling,
        DataUpstream.TENCENT: FakeModule(_rows(DataUpstream.TENCENT)),
        DataUpstream.SINA: FakeModule(_rows(DataUpstream.SINA)),
    }
    jobs, data_uow, _modules, _handler, worker, commands = _runtime(
        tmp_path,
        modules=modules,
    )
    enqueued = _enqueue(commands, "cancel-at-source-checkpoint")
    cancelling.cancel = lambda: jobs.request_cancel(
        enqueued.job_id,
        reason="synthetic safe cancellation",
    )

    with pytest.raises(JobCancelledAtCheckpoint):
        worker.run(enqueued.job_id)

    assert jobs.get_job(enqueued.job_id).status is JobStatus.CANCELLED
    with data_uow() as uow:
        versions = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        uow.rollback()
    assert not any(item.status is DatasetVersionStatus.PUBLISHED for item in versions)
    _assert_no_nonterminal_candidate(data_uow)


def test_formal_runtime_requires_and_builds_all_three_upstreams(tmp_path: Path) -> None:
    settings = _formal_settings(tmp_path)
    runtime = build_eod_runtime(
        settings,
        gateways=_formal_gateways(),
        execution_config=_execution_config(),
        quality_runner=PassingQualityRunner(),  # type: ignore[arg-type]
    )

    assert runtime.eod_handler is runtime.registry.resolve("data.eod.v1")
    with pytest.raises(ConfigurationError, match="frozen source route"):
        build_eod_runtime(
            replace(
                settings,
                paths=replace(settings.paths, data_root=(tmp_path / "bad").resolve()),
            ),
            gateways={DataUpstream.EASTMONEY: _formal_gateways()[DataUpstream.EASTMONEY]},
            execution_config=_execution_config(),
            quality_runner=PassingQualityRunner(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("business_root", ["metadata", "raw", "artifacts", "warehouse"])
def test_formal_runtime_refuses_unknown_initial_business_state(
    tmp_path: Path, business_root: str
) -> None:
    settings = _formal_settings(tmp_path)
    unexpected = settings.paths.data_root / business_root
    unexpected.mkdir(parents=True)
    (unexpected / "unknown.bin").write_bytes(b"do-not-modify")

    with pytest.raises(ConfigurationError, match="unknown business state"):
        build_eod_runtime(settings, gateways=_formal_gateways())

    assert (unexpected / "unknown.bin").read_bytes() == b"do-not-modify"
    assert not (settings.paths.data_root / "metadata" / "quantking.db").exists()


def test_formal_runtime_real_quality_runner_needs_no_calendar_reference(
    tmp_path: Path,
) -> None:
    settings = _formal_settings(tmp_path)
    runtime = build_eod_runtime(
        settings,
        gateways=_formal_gateways(),
        execution_config=_formal_k2_config(settings),
    )
    enqueued = runtime.eod_commands.enqueue(
        EodRunRequest(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 8),
            datasets=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        ),
        idempotency_key="formal-real-quality-no-calendar",
        requester="test",
    )

    runtime.worker.run(enqueued.job_id)

    assert runtime.jobs.get_job(enqueued.job_id).status is JobStatus.SUCCEEDED
    with runtime.data_uow_factory() as uow:
        versions = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        quality = uow.data.list_quality_runs()
        uow.rollback()
    assert [item.status for item in versions] == [DatasetVersionStatus.PUBLISHED]
    assert quality
    assert all(item.status is QualityRunStatus.PASSED for item in quality)
    assert all(item.rule_version == "quality-rules.daily-bar-first-release.v1" for item in quality)


def test_data_center_reads_k2_shared_candidate_source_quality_lineage(
    tmp_path: Path,
) -> None:
    settings = _formal_settings(tmp_path)
    gateways = _formal_gateways()
    sina_rows = _rows(DataUpstream.SINA)
    sina_rows[0]["volume"] = 1_230_000
    gateways[DataUpstream.SINA] = AkshareMarketDataGateway(
        AkshareDailyBarAdapter(
            DataUpstream.SINA,
            module_loader=lambda: FakeModule(sina_rows),
            clock=lambda: _NOW,
        )
    )
    runtime = build_eod_runtime(
        settings,
        gateways=gateways,
        execution_config=_formal_k2_config(settings),
    )
    enqueued = runtime.eod_commands.enqueue(
        EodRunRequest(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 8),
            datasets=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        ),
        idempotency_key="shared-candidate-data-center",
        requester="test",
    )
    runtime.worker.run(enqueued.job_id)
    with runtime.data_uow_factory() as uow:
        version = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)[0]
        reports = uow.data.list_difference_reports(version_id=str(version.version_id))
        uow.rollback()
    report = next(item for item in reports if item.total_differences > 0)
    assert version.content_sha256 != report.primary_content_sha256 or (
        version.content_sha256 != report.fallback_content_sha256
    )

    lineage = runtime.data_center.get_version_lineage(str(version.version_id))
    detail = runtime.data_center.get_difference_report(str(report.report_id))
    rows = runtime.data_center.difference_rows(
        report_id=str(report.report_id), cursor=None, limit=50
    )

    assert detail["report_id"] == str(report.report_id)
    assert rows.items
    serialized = json.dumps(lineage, sort_keys=True)
    assert "artifact_relative_path" not in serialized
    assert "request_params" not in serialized
    assert str(settings.paths.data_root) not in serialized


class _TamperedRepository:
    def __init__(
        self,
        repository: object,
        *,
        mutation: str,
        source_quality_id: str,
        final_quality_id: str,
        primary_batch_id: str,
        shared_version_id: str,
    ) -> None:
        self._repository = repository
        self._mutation = mutation
        self._source_quality_id = source_quality_id
        self._final_quality_id = final_quality_id
        self._primary_batch_id = primary_batch_id
        self._shared_version_id = shared_version_id

    def __getattr__(self, name: str) -> object:
        return getattr(self._repository, name)

    def get_quality_run(self, run_id: str):
        quality = self._repository.get_quality_run(run_id)  # type: ignore[attr-defined]
        if run_id == self._source_quality_id:
            if self._mutation == "source_quality_sha":
                return replace(quality, content_sha256="f" * 64)
            if self._mutation == "source_quality_status":
                return replace(
                    quality,
                    status=QualityRunStatus.FAILED,
                    results=(
                        replace(quality.results[0], passed=False, affected_count=1),
                        *quality.results[1:],
                    ),
                )
            if self._mutation == "source_quality_version":
                return replace(quality, version_id=new_entity_id("data_version"))
        if run_id == self._final_quality_id and self._mutation == "final_quality_sha":
            return replace(quality, content_sha256="e" * 64)
        return quality

    def get_batch(self, batch_id: str):
        batch = self._repository.get_batch(batch_id)  # type: ignore[attr-defined]
        if batch_id == self._primary_batch_id and self._mutation == "batch_source":
            return replace(batch, upstream=DataUpstream.SINA)
        return batch

    def get_version(self, version_id: str):
        version = self._repository.get_version(version_id)  # type: ignore[attr-defined]
        if version_id == self._shared_version_id and self._mutation == "batch_lineage":
            return replace(
                version,
                input_batch_ids=tuple(
                    item for item in version.input_batch_ids if str(item) != self._primary_batch_id
                ),
            )
        return version


class _TamperedUow:
    def __init__(self, factory: object, **kwargs: str) -> None:
        self._factory = factory
        self._kwargs = kwargs
        self._inner = None
        self.data = None

    def __enter__(self):
        self._inner = self._factory()  # type: ignore[operator]
        entered = self._inner.__enter__()
        self.data = _TamperedRepository(entered.data, **self._kwargs)
        return self

    def __exit__(self, *args: object):
        assert self._inner is not None
        return self._inner.__exit__(*args)


@pytest.mark.parametrize(
    "mutation",
    [
        "source_quality_sha",
        "source_quality_status",
        "source_quality_version",
        "batch_source",
        "batch_lineage",
        "final_quality_sha",
    ],
)
def test_data_center_shared_candidate_tamper_fails_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    settings = _formal_settings(tmp_path)
    runtime = build_eod_runtime(
        settings,
        gateways=_formal_gateways(),
        execution_config=_formal_k2_config(settings),
    )
    enqueued = runtime.eod_commands.enqueue(
        EodRunRequest(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 8),
            datasets=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        ),
        idempotency_key=f"shared-candidate-tamper-{mutation}",
        requester="test",
    )
    runtime.worker.run(enqueued.job_id)
    with runtime.data_uow_factory() as uow:
        version = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)[0]
        report = uow.data.list_difference_reports(version_id=str(version.version_id))[0]
        uow.rollback()
    original_factory = runtime.data_uow_factory
    runtime.data_center._uow_factory = (  # type: ignore[attr-defined]
        lambda: _TamperedUow(
            original_factory,
            mutation=mutation,
            source_quality_id=str(report.primary_quality_run_id),
            final_quality_id=str(version.quality_run_id),
            primary_batch_id=str(report.primary_batch_id),
            shared_version_id=str(version.version_id),
        )
    )

    with pytest.raises(DataCenterReadIntegrityError, match="lineage"):
        runtime.data_center.get_difference_report(str(report.report_id))


def test_data_center_shared_candidate_rejects_final_manifest_tamper(tmp_path: Path) -> None:
    settings = _formal_settings(tmp_path)
    runtime = build_eod_runtime(
        settings,
        gateways=_formal_gateways(),
        execution_config=_formal_k2_config(settings),
    )
    enqueued = runtime.eod_commands.enqueue(
        EodRunRequest(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 8),
            datasets=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        ),
        idempotency_key="shared-candidate-final-manifest-tamper",
        requester="test",
    )
    runtime.worker.run(enqueued.job_id)
    with runtime.data_uow_factory() as uow:
        version = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)[0]
        report = uow.data.list_difference_reports(version_id=str(version.version_id))[0]
        uow.rollback()
    manifest = (
        settings.paths.data_root
        / "warehouse"
        / DatasetKind.DAILY_BAR_UNADJUSTED.value
        / str(version.version_id).replace(":", "--")
        / "manifest.json"
    )
    manifest.write_bytes(manifest.read_bytes() + b"tampered")

    with pytest.raises(DataCenterReadIntegrityError, match="manifest"):
        runtime.data_center.get_difference_report(str(report.report_id))


def _file_snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _fixed_release_gateways(
    *,
    failing_upstream: DataUpstream | None = None,
    partial_upstream: DataUpstream | None = None,
) -> dict[DataUpstream, AkshareMarketDataGateway]:
    modules = {
        upstream: (
            FailingModule([])
            if upstream is failing_upstream
            else (
                FailingAfterWindowModule(upstream, successful_calls=1)
                if upstream is partial_upstream
                else WindowFilteringModule(upstream, affected=set())
            )
        )
        for upstream in AKSHARE_RUNTIME_UPSTREAMS
    }
    return {
        upstream: AkshareMarketDataGateway(
            AkshareDailyBarAdapter(
                upstream,
                module_loader=lambda upstream=upstream: modules[upstream],
                clock=lambda: _NOW,
            )
        )
        for upstream in AKSHARE_RUNTIME_UPSTREAMS
    }


def _create_fixed_release(
    tmp_path: Path,
    *,
    failing_upstream: DataUpstream | None = None,
    partial_upstream: DataUpstream | None = None,
    quality_failed_first: bool = False,
) -> tuple[AppSettings, str]:
    settings = _formal_settings(tmp_path)
    runtime = build_eod_runtime(
        settings,
        gateways=_fixed_release_gateways(
            failing_upstream=failing_upstream,
            partial_upstream=partial_upstream,
        ),
        execution_config=_formal_k2_config(settings, code_version="s1-08.v1"),
        quality_runner=(FirstSourceFailedThenRealQualityRunner() if quality_failed_first else None),  # type: ignore[arg-type]
    )
    enqueued = runtime.eod_commands.enqueue(
        EodRunRequest(
            start_date=date(2026, 6, 11),
            end_date=date(2026, 8, 9),
            datasets=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        ),
        idempotency_key=S108_IDEMPOTENCY_KEY,
        requester="test",
    )
    runtime.worker.run(enqueued.job_id)
    assert runtime.jobs.get_job(enqueued.job_id).status is JobStatus.SUCCEEDED
    with runtime.data_uow_factory() as uow:
        version = uow.data.list_versions(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            status=DatasetVersionStatus.PUBLISHED,
        )[0]
        uow.rollback()
    data_engine = runtime.data_uow_factory._session_factory.kw["bind"]
    job_engine = runtime.jobs._transactions._session_factory.kw["bind"]
    data_engine.dispose()
    job_engine.dispose()
    del runtime
    gc.collect()
    database = settings.paths.data_root / "metadata" / "quantking.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    assert not database.with_name("quantking.db-wal").exists()
    assert not database.with_name("quantking.db-shm").exists()
    return settings, str(version.version_id)


def test_s108_release_verifier_is_strict_and_read_only(tmp_path: Path) -> None:
    settings, version_id = _create_fixed_release(tmp_path)
    before = _file_snapshot(settings.paths.data_root)

    result = verify_s108_release(
        data_root=settings.paths.data_root,
        version_id=version_id,
    )

    assert result["status"] == "VERIFIED"
    assert result["scope"] == ["000001.SZ", "600000.SH"]
    assert result["endpoint_invocations"] == {
        "EASTMONEY": 4,
        "SINA": 0,
        "TENCENT": 4,
    }
    assert result["estimated_http_request_upper_bound"] == 24
    assert result["estimated_http_requests_for_actual_inventory"] == 16
    assert result["data_center_explicit_version_read"] == {
        "lineage_batch_count": 2,
        "preview_first_key": ["000001.SZ", "2026-06-11"],
        "preview_has_next": True,
        "version_id": version_id,
    }
    assert _file_snapshot(settings.paths.data_root) == before


def test_s108_release_verifier_accepts_a_failure_with_bc_consensus(tmp_path: Path) -> None:
    settings, version_id = _create_fixed_release(
        tmp_path,
        failing_upstream=DataUpstream.EASTMONEY,
    )

    result = verify_s108_release(
        data_root=settings.paths.data_root,
        version_id=version_id,
    )

    assert result["endpoint_invocations"] == {
        "EASTMONEY": 1,
        "SINA": 4,
        "TENCENT": 4,
    }
    assert result["estimated_http_requests_for_actual_inventory"] == 21


def test_s108_release_verifier_accepts_partial_a_with_bc_consensus(tmp_path: Path) -> None:
    settings, version_id = _create_fixed_release(
        tmp_path,
        partial_upstream=DataUpstream.EASTMONEY,
    )

    result = verify_s108_release(
        data_root=settings.paths.data_root,
        version_id=version_id,
    )

    assert result["endpoint_invocations"] == {
        "EASTMONEY": 2,
        "SINA": 4,
        "TENCENT": 4,
    }
    assert result["estimated_http_requests_for_actual_inventory"] == 22


def test_s108_release_verifier_accepts_a_quality_failure_with_bc_consensus(
    tmp_path: Path,
) -> None:
    settings, version_id = _create_fixed_release(
        tmp_path,
        quality_failed_first=True,
    )

    result = verify_s108_release(
        data_root=settings.paths.data_root,
        version_id=version_id,
    )

    assert result["endpoint_invocations"] == {
        "EASTMONEY": 4,
        "SINA": 4,
        "TENCENT": 4,
    }
    assert result["estimated_http_requests_for_actual_inventory"] == 24
    assert result["network_budget_log"]["planned"]["SINA"] == 4  # type: ignore[index]


def test_s108_release_verifier_rejects_failed_quality_artifact_tamper(
    tmp_path: Path,
) -> None:
    settings, version_id = _create_fixed_release(
        tmp_path,
        quality_failed_first=True,
    )
    database = settings.paths.data_root / "metadata" / "quantking.db"
    with sqlite3.connect(database) as connection:
        failed_run_id = str(
            connection.execute(
                "SELECT run_id FROM quality_runs WHERE version_id = ? AND status = 'FAILED'",
                (version_id,),
            ).fetchone()[0]
        )
    failure = (
        settings.paths.data_root
        / "artifacts"
        / "quality"
        / "data-quality"
        / failed_run_id.replace(":", "--")
        / "failure.json"
    )
    failure.write_bytes(failure.read_bytes() + b"\n")

    with pytest.raises(S108ReleaseVerificationError, match="QualityRun"):
        verify_s108_release(data_root=settings.paths.data_root, version_id=version_id)


def test_s108_release_verifier_rejects_budget_actual_without_endpoint_evidence(
    tmp_path: Path,
) -> None:
    settings, version_id = _create_fixed_release(tmp_path)
    database = settings.paths.data_root / "metadata" / "quantking.db"
    with sqlite3.connect(database) as connection:
        log_id = str(
            connection.execute(
                "SELECT log_id FROM job_runs WHERE status = 'SUCCEEDED'",
            ).fetchone()[0]
        )
    log_path = settings.paths.data_root / log_id
    events = [json.loads(line) for line in log_path.read_text("utf-8").splitlines()]
    summary = next(item for item in events if item["event"] == "network_budget_summary")
    summary["network_budget"]["actual"]["EASTMONEY"] = 3
    summary["network_budget"]["actual_total"] = 7
    log_path.write_text(
        "\n".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            for item in events
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(S108ReleaseVerificationError, match="network budget"):
        verify_s108_release(data_root=settings.paths.data_root, version_id=version_id)


@pytest.mark.parametrize("quality_kind", ["final", "source"])
def test_s108_release_verifier_rejects_quality_artifact_tamper(
    tmp_path: Path,
    quality_kind: str,
) -> None:
    settings, version_id = _create_fixed_release(tmp_path)
    database = settings.paths.data_root / "metadata" / "quantking.db"
    with sqlite3.connect(database) as connection:
        final_run_id = str(
            connection.execute(
                "SELECT quality_run_id FROM dataset_versions WHERE version_id = ?",
                (version_id,),
            ).fetchone()[0]
        )
        if quality_kind == "final":
            run_id = final_run_id
        else:
            run_id = str(
                connection.execute(
                    "SELECT run_id FROM quality_runs "
                    "WHERE version_id = ? AND run_id != ? ORDER BY run_id LIMIT 1",
                    (version_id, final_run_id),
                ).fetchone()[0]
            )
    manifest = (
        settings.paths.data_root
        / "artifacts"
        / "quality"
        / "data-quality"
        / run_id.replace(":", "--")
        / "manifest.json"
    )
    manifest.write_bytes(manifest.read_bytes() + b"\n")

    with pytest.raises(S108ReleaseVerificationError, match="QualityRun"):
        verify_s108_release(data_root=settings.paths.data_root, version_id=version_id)


@pytest.mark.parametrize("target", ["manifest", "parquet"])
def test_s108_release_verifier_rejects_manifest_or_content_tamper(
    tmp_path: Path,
    target: str,
) -> None:
    settings, version_id = _create_fixed_release(tmp_path)
    directory = (
        settings.paths.data_root
        / "warehouse"
        / DatasetKind.DAILY_BAR_UNADJUSTED.value
        / version_id.replace(":", "--")
    )
    if target == "manifest":
        path = directory / "manifest.json"
    else:
        path = next(directory.rglob("*.parquet"))
    path.write_bytes(path.read_bytes() + b"tampered")

    with pytest.raises(S108ReleaseVerificationError, match="integrity"):
        verify_s108_release(data_root=settings.paths.data_root, version_id=version_id)


def test_s108_release_verifier_missing_database_does_not_create_any_file(tmp_path: Path) -> None:
    data_root = (tmp_path / "missing-database").resolve()
    data_root.mkdir()
    before = _file_snapshot(data_root)

    with pytest.raises(S108ReleaseVerificationError, match="database is missing"):
        verify_s108_release(
            data_root=data_root,
            version_id="data_version:00000000-0000-4000-8000-000000000000",
        )

    assert _file_snapshot(data_root) == before


@pytest.mark.parametrize(
    "version_id",
    [
        "latest",
        "../data_version:00000000-0000-4000-8000-000000000000",
        "job:00000000-0000-4000-8000-000000000000",
        "DATA_VERSION:00000000-0000-4000-8000-000000000000",
    ],
)
def test_s108_release_verifier_rejects_non_explicit_ids_without_creating_root(
    tmp_path: Path,
    version_id: str,
) -> None:
    data_root = (tmp_path / "absent").resolve()

    with pytest.raises(S108ReleaseVerificationError, match="version_id"):
        verify_s108_release(data_root=data_root, version_id=version_id)

    assert not data_root.exists()


def test_s108_release_verifier_does_not_create_or_repair_bad_database(tmp_path: Path) -> None:
    data_root = (tmp_path / "bad-revision").resolve()
    metadata = data_root / "metadata"
    metadata.mkdir(parents=True)
    database = metadata / "quantking.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('forged_revision')")
        connection.commit()
    before = _file_snapshot(data_root)

    with pytest.raises(S108ReleaseVerificationError, match="revision"):
        verify_s108_release(
            data_root=data_root,
            version_id="data_version:00000000-0000-4000-8000-000000000000",
        )

    assert _file_snapshot(data_root) == before
    assert not database.with_name("quantking.db-wal").exists()
    assert not database.with_name("quantking.db-shm").exists()


def test_formal_runtime_rejects_existing_database_at_wrong_revision(tmp_path: Path) -> None:
    settings = _formal_settings(tmp_path)
    build_eod_runtime(
        settings,
        gateways=_formal_gateways(),
        execution_config=_execution_config(),
        quality_runner=PassingQualityRunner(),  # type: ignore[arg-type]
    )
    database = settings.paths.data_root / "metadata" / "quantking.db"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'forged_revision'")
        connection.commit()

    with pytest.raises(ConfigurationError, match="revision"):
        build_eod_runtime(
            settings,
            gateways=_formal_gateways(),
            execution_config=_execution_config(),
            quality_runner=PassingQualityRunner(),  # type: ignore[arg-type]
        )


def test_web_cli_runtime_does_not_recover_a_fresh_worker_lease(tmp_path: Path) -> None:
    settings = _formal_settings(tmp_path)
    runtime = build_eod_runtime(
        settings,
        gateways=_formal_gateways(),
        execution_config=_execution_config(),
        quality_runner=PassingQualityRunner(),  # type: ignore[arg-type]
    )
    enqueued = _enqueue(runtime.eod_commands, "formal-fresh-lease")
    started = UtcTimestamp(datetime(2026, 8, 9, 1, 0, tzinfo=UTC))
    runtime.jobs.transition(
        enqueued.job_id,
        JobStatus.RUNNING,
        worker_id="active-worker",
        at=started,
    )
    heartbeat = UtcTimestamp(started.value + timedelta(seconds=30))
    runtime.jobs.heartbeat(enqueued.job_id, at=heartbeat)

    web_cli_runtime = build_eod_runtime(
        settings,
        gateways=_formal_gateways(),
        execution_config=_execution_config(),
        quality_runner=PassingQualityRunner(),  # type: ignore[arg-type]
    )
    assert web_cli_runtime.jobs.get_job(enqueued.job_id).status is JobStatus.RUNNING
    assert (
        web_cli_runtime.jobs.recover_stale_running_jobs(
            reason="expired lease",
            stale_before=UtcTimestamp(heartbeat.value - timedelta(seconds=1)),
            at=UtcTimestamp(heartbeat.value + timedelta(minutes=3)),
        )
        == 0
    )
    assert web_cli_runtime.jobs.get_job(enqueued.job_id).status is JobStatus.RUNNING


def test_ab_agreement_publishes_with_c_zero_and_full_k2_bindings(tmp_path: Path) -> None:
    jobs, data_uow, modules, _handler, worker, commands = _runtime(tmp_path)
    enqueued = _enqueue(commands, "k2-ab-agreement")

    worker.run(enqueued.job_id)

    assert jobs.get_job(enqueued.job_id).status is JobStatus.SUCCEEDED
    assert len(modules[DataUpstream.EASTMONEY].calls) == 1
    assert len(modules[DataUpstream.TENCENT].calls) == 1
    assert modules[DataUpstream.SINA].calls == []
    with data_uow() as uow:
        batches = uow.data.list_batches_for_job(enqueued.job_id)
        versions = uow.data.list_versions(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            status=DatasetVersionStatus.PUBLISHED,
        )
        resolutions = uow.data.list_resolution_runs_page(before=None, limit=10)
        reports = uow.data.list_difference_reports(version_id=str(versions[0].version_id))
        uow.rollback()
    assert {item.upstream for item in batches} == {
        DataUpstream.EASTMONEY,
        DataUpstream.TENCENT,
    }
    assert all(item.job_run_id == enqueued.job_id for item in batches)
    assert len(versions) == len(resolutions) == len(reports) == 1
    directory = (
        tmp_path
        / "warehouse"
        / DatasetKind.DAILY_BAR_UNADJUSTED.value
        / str(versions[0].version_id).replace(":", "--")
    )
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    assert manifest["input_batch_ids"] == [str(item.batch_id) for item in batches]
    assert manifest["resolution"]["run_id"] == str(resolutions[0].run_id)
    assert manifest["resolution"]["difference_reports"] == [
        {
            "report_id": str(reports[0].report_id),
            "artifact_relative_path": reports[0].report_relative_path,
            "manifest_sha256": next(
                item["manifest_sha256"]
                for item in manifest["resolution"]["difference_reports"]
                if item["report_id"] == str(reports[0].report_id)
            ),
        }
    ]
    for batch in batches:
        raw = (
            tmp_path
            / "raw"
            / batch.upstream.value.lower()
            / batch.dataset.value
            / str(batch.batch_id).replace(":", "--")
            / "manifest.json"
        )
        assert json.loads(raw.read_text("utf-8"))["code_version"] == "s1-07k2-test"


def test_a_network_failure_calls_c_full_range_and_publishes_bc_consensus(
    tmp_path: Path,
) -> None:
    modules = {
        DataUpstream.EASTMONEY: FailingModule([]),
        DataUpstream.TENCENT: FakeModule(_rows(DataUpstream.TENCENT)),
        DataUpstream.SINA: FakeModule(_rows(DataUpstream.SINA)),
    }
    jobs, data_uow, modules, _handler, worker, commands = _runtime(
        tmp_path,
        modules=modules,
    )
    enqueued = _enqueue(commands, "k2-a-network")

    worker.run(enqueued.job_id)

    assert jobs.get_job(enqueued.job_id).status is JobStatus.SUCCEEDED
    assert len(modules[DataUpstream.SINA].calls) == 1
    sina_params = modules[DataUpstream.SINA].calls[0][1]
    assert sina_params["start_date"] == "20260717"
    assert sina_params["end_date"] == "20260808"
    with data_uow() as uow:
        batches = uow.data.list_batches_for_job(enqueued.job_id)
        versions = uow.data.list_versions(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            status=DatasetVersionStatus.PUBLISHED,
        )
        uow.rollback()
    assert len(batches) == 3
    assert len(versions) == 1
    failed = next(item for item in batches if item.upstream is DataUpstream.EASTMONEY)
    failure_file = (
        tmp_path
        / "raw"
        / "eastmoney"
        / DatasetKind.DAILY_BAR_UNADJUSTED.value
        / f"{str(failed.batch_id).replace(':', '--')}.incomplete"
        / "failure.json"
    )
    failure = json.loads(failure_file.read_text("utf-8"))
    assert failure["code_version"] == "s1-07k2-test"
    assert "URL" not in json.dumps(failure)


def test_three_source_disagreement_persists_conflict_and_fails_candidate(
    tmp_path: Path,
) -> None:
    tencent = [dict(item) for item in _rows(DataUpstream.TENCENT)]
    sina = [dict(item) for item in _rows(DataUpstream.SINA)]
    tencent[0]["close"] = 10.3
    sina[0]["close"] = 10.4
    modules = {
        DataUpstream.EASTMONEY: FakeModule(_rows(DataUpstream.EASTMONEY)),
        DataUpstream.TENCENT: FakeModule(tencent),
        DataUpstream.SINA: FakeModule(sina),
    }
    jobs, data_uow, _modules, _handler, worker, commands = _runtime(
        tmp_path,
        modules=modules,
    )
    enqueued = _enqueue(commands, "k2-no-consensus")

    with pytest.raises(Exception, match="RESOLUTION_BLOCKED"):
        worker.run(enqueued.job_id)

    assert jobs.get_job(enqueued.job_id).status is JobStatus.FAILED
    with data_uow() as uow:
        versions = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        resolutions = uow.data.list_resolution_runs_page(
            before=None,
            limit=10,
            conflicts_only=True,
        )
        reports = uow.data.list_difference_reports(version_id=str(versions[0].version_id))
        uow.rollback()
    assert len(versions) == len(resolutions) == 1
    assert versions[0].status is DatasetVersionStatus.FAILED
    assert resolutions[0].candidate_version_id is None
    assert resolutions[0].conflict_count == 1
    assert len(reports) == 3


def test_failed_c_after_ab_difference_cannot_publish(tmp_path: Path) -> None:
    tencent = [dict(item) for item in _rows(DataUpstream.TENCENT)]
    tencent[0]["close"] = 10.3
    modules = {
        DataUpstream.EASTMONEY: FakeModule(_rows(DataUpstream.EASTMONEY)),
        DataUpstream.TENCENT: FakeModule(tencent),
        DataUpstream.SINA: FailingModule([]),
    }
    jobs, data_uow, selected, _handler, worker, commands = _runtime(
        tmp_path,
        modules=modules,
    )
    enqueued = _enqueue(commands, "k2-failed-c-blocks-publish")

    with pytest.raises(EodExecutionError, match="RESOLUTION_BLOCKED"):
        worker.run(enqueued.job_id)

    assert jobs.get_job(enqueued.job_id).status is JobStatus.FAILED
    assert len(selected[DataUpstream.SINA].calls) == 1
    with data_uow() as uow:
        versions = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        batches = uow.data.list_batches_for_job(enqueued.job_id)
        uow.rollback()
    assert not any(item.status is DatasetVersionStatus.PUBLISHED for item in versions)
    sina_batch = next(item for item in batches if item.upstream is DataUpstream.SINA)
    assert sina_batch.failure is not None


def test_a_source_quality_failure_calls_c_and_publishes_bc_consensus(tmp_path: Path) -> None:
    jobs, data_uow, modules, _handler, worker, commands = _runtime(
        tmp_path,
        quality_runner=SequencedQualityRunner(
            [
                QualityRunStatus.FAILED,
                QualityRunStatus.PASSED,
                QualityRunStatus.PASSED,
                QualityRunStatus.PASSED,
            ]
        ),
    )
    enqueued = _enqueue(commands, "k2-a-quality-failed")

    worker.run(enqueued.job_id)

    assert jobs.get_job(enqueued.job_id).status is JobStatus.SUCCEEDED
    assert len(modules[DataUpstream.SINA].calls) == 1
    with data_uow() as uow:
        qualities = uow.data.list_quality_runs(status=QualityRunStatus.FAILED)
        published = uow.data.list_versions(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            status=DatasetVersionStatus.PUBLISHED,
        )
        uow.rollback()
    assert len(qualities) == 1
    assert len(published) == 1


def test_ab_field_difference_calls_c_only_for_affected_date_and_selects_ac(
    tmp_path: Path,
) -> None:
    tencent = [dict(item) for item in _rows(DataUpstream.TENCENT)]
    tencent[0]["close"] = 10.3
    modules = {
        DataUpstream.EASTMONEY: FakeModule(_rows(DataUpstream.EASTMONEY)),
        DataUpstream.TENCENT: FakeModule(tencent),
        DataUpstream.SINA: FakeModule(_rows(DataUpstream.SINA)),
    }
    jobs, data_uow, modules, _handler, worker, commands = _runtime(
        tmp_path,
        modules=modules,
    )
    enqueued = _enqueue(commands, "k2-field-difference")

    worker.run(enqueued.job_id)

    assert jobs.get_job(enqueued.job_id).status is JobStatus.SUCCEEDED
    assert len(modules[DataUpstream.SINA].calls) == 1
    params = modules[DataUpstream.SINA].calls[0][1]
    assert params["start_date"] == "20260723"
    assert params["end_date"] == "20260807"
    with data_uow() as uow:
        resolution = uow.data.list_resolution_runs_page(before=None, limit=10)[0]
        uow.rollback()
    assert resolution.exception_count == 1
    assert resolution.conflict_count == 0


def test_exception_after_resolution_summary_closes_candidate_failed(tmp_path: Path) -> None:
    def fail(point: str) -> None:
        if point == "after_resolution_summary_commit":
            raise RuntimeError("simulated process-local failure")

    jobs, data_uow, _modules, _handler, worker, commands = _runtime(
        tmp_path,
        fault_injector=fail,
    )
    enqueued = _enqueue(commands, "k2-resolution-crash-window")

    with pytest.raises(RuntimeError, match="simulated"):
        worker.run(enqueued.job_id)

    assert jobs.get_job(enqueued.job_id).status is JobStatus.FAILED
    with data_uow() as uow:
        versions = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        uow.rollback()
    assert len(versions) == 1
    assert versions[0].status is DatasetVersionStatus.FAILED


def test_stale_interrupted_recovery_closes_only_job_candidate_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crash_enabled = False

    def fail(point: str) -> None:
        if crash_enabled and point == "after_resolution_summary_commit":
            raise RuntimeError("simulated hard crash window")

    jobs, data_uow, _modules, handler, worker, commands = _runtime(
        tmp_path,
        fault_injector=fail,
    )
    other = _enqueue(commands, "k2-unrelated-published")
    worker.run(other.job_id)
    with data_uow() as uow:
        unrelated_before = next(
            item
            for item in uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
            if item.status is DatasetVersionStatus.PUBLISHED
        )
        uow.rollback()
    crash_enabled = True
    enqueued = commands.enqueue(
        EodRunRequest(
            start_date=date(2026, 8, 2),
            end_date=date(2026, 8, 8),
            datasets=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        ),
        idempotency_key="k2-stale-interrupted",
        requester="test",
    )
    jobs.transition(
        enqueued.job_id,
        JobStatus.RUNNING,
        worker_id="crashed-worker",
        summary="claimed",
    )
    monkeypatch.setattr(handler, "_fail_candidate_by_id", lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match="hard crash"):
        handler(WorkerContext(jobs, enqueued.job_id, "crashed-worker"))
    with data_uow() as uow:
        before = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        uow.rollback()
    assert {item.status for item in before} == {
        DatasetVersionStatus.PUBLISHED,
        DatasetVersionStatus.BUILDING,
    }
    jobs.transition(
        enqueued.job_id,
        JobStatus.INTERRUPTED,
        failure=FailureInfo("STALE_LEASE", "stable interruption", False),
        summary="stale running job interrupted",
    )
    monkeypatch.undo()

    handler.recover_interrupted(jobs, enqueued.job_id)
    handler.recover_interrupted(jobs, enqueued.job_id)

    with data_uow() as uow:
        after = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        uow.rollback()
    assert len(after) == 2
    unrelated_after = next(item for item in after if item.version_id == unrelated_before.version_id)
    interrupted = next(item for item in after if item.version_id != unrelated_before.version_id)
    assert unrelated_after == unrelated_before
    assert interrupted.status is DatasetVersionStatus.FAILED


def test_post_publish_failure_preserves_published_version(tmp_path: Path) -> None:
    def fail(point: str) -> None:
        if point == "after_publish_before_job_success":
            raise RuntimeError("post publish failure")

    jobs, data_uow, _modules, _handler, worker, commands = _runtime(
        tmp_path,
        fault_injector=fail,
    )
    enqueued = _enqueue(commands, "k2-post-publish-failure")

    with pytest.raises(RuntimeError, match="post publish"):
        worker.run(enqueued.job_id)

    assert jobs.get_job(enqueued.job_id).status is JobStatus.FAILED
    with data_uow() as uow:
        versions = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        uow.rollback()
    assert len(versions) == 1
    assert versions[0].status is DatasetVersionStatus.PUBLISHED


def test_same_fingerprint_reuses_only_strictly_verified_publish(tmp_path: Path) -> None:
    jobs, data_uow, modules, _handler, worker, commands = _runtime(tmp_path)
    first = _enqueue(commands, "k2-first-publish")
    worker.run(first.job_id)
    initial_calls = {source: len(module.calls) for source, module in modules.items()}

    second = _enqueue(commands, "k2-reuse-publish")
    worker.run(second.job_id)

    assert jobs.get_job(second.job_id).status is JobStatus.SUCCEEDED
    assert {source: len(module.calls) for source, module in modules.items()} == initial_calls
    with data_uow() as uow:
        versions = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        resolutions = uow.data.list_resolution_runs_page(before=None, limit=10)
        uow.rollback()
    assert len(versions) == len(resolutions) == 1


@pytest.mark.parametrize("target", ["difference_complete", "raw_manifest"])
def test_reuse_rejects_tampered_difference_or_raw_without_new_calls(
    tmp_path: Path,
    target: str,
) -> None:
    jobs, data_uow, modules, _handler, worker, commands = _runtime(tmp_path)
    first = _enqueue(commands, "k2-first-publish")
    worker.run(first.job_id)
    initial_calls = {source: len(module.calls) for source, module in modules.items()}
    with data_uow() as uow:
        version = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)[0]
        report = uow.data.list_difference_reports(version_id=str(version.version_id))[0]
        batch = uow.data.list_batches_for_job(first.job_id)[0]
        uow.rollback()
    if target == "difference_complete":
        (tmp_path / "artifacts" / report.report_relative_path / "COMPLETE").write_text(
            "{}",
            encoding="utf-8",
        )
    else:
        raw_manifest = (
            tmp_path
            / "raw"
            / batch.upstream.value.lower()
            / batch.dataset.value
            / str(batch.batch_id).replace(":", "--")
            / "manifest.json"
        )
        payload = json.loads(raw_manifest.read_text("utf-8"))
        payload["code_version"] = "tampered"
        raw_manifest.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

    second = _enqueue(commands, f"k2-reuse-tampered-{target}")
    with pytest.raises(EodExecutionError, match="REUSABLE_RESULT_INVALID"):
        worker.run(second.job_id)

    assert jobs.get_job(second.job_id).status is JobStatus.FAILED
    assert {source: len(module.calls) for source, module in modules.items()} == initial_calls


def test_five_discrete_c_spans_fail_before_first_sina_invocation(tmp_path: Path) -> None:
    modules = {upstream: WindowFilteringModule(upstream) for upstream in AKSHARE_RUNTIME_UPSTREAMS}
    config = replace(
        _execution_config(),
        datasets={
            DatasetKind.DAILY_BAR_UNADJUSTED.value: EodDatasetConfig(
                scope=("000001.SZ", "600000.SH"),
                references={"exchange_calendar": _REFERENCE},
            )
        },
    )
    jobs, data_uow, selected, _handler, worker, commands = _runtime(
        tmp_path,
        modules=modules,  # type: ignore[arg-type]
        execution_config=config,
    )
    enqueued = commands.enqueue(
        EodRunRequest(
            start_date=date(2026, 6, 11),
            end_date=date(2026, 8, 9),
            datasets=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        ),
        idempotency_key="five-c-spans-budget-block",
        requester="test",
    )

    with pytest.raises(NetworkBudgetExceededError, match="NETWORK_BUDGET_EXCEEDED"):
        worker.run(enqueued.job_id)

    assert len(selected[DataUpstream.EASTMONEY].calls) == 4
    assert len(selected[DataUpstream.TENCENT].calls) == 4
    assert len(selected[DataUpstream.SINA].calls) == 0
    failed_job = jobs.get_job(enqueued.job_id)
    assert failed_job.failure is not None
    assert failed_job.failure.error_type == "NetworkBudgetExceededError"
    log_path = tmp_path / failed_job.log_id
    events = [json.loads(line) for line in log_path.read_text("utf-8").splitlines()]
    failure = next(item for item in events if item["event"] == "eod_failed")
    summary = next(item for item in events if item["event"] == "network_budget_summary")
    assert failure["error_code"] == "NETWORK_BUDGET_EXCEEDED"
    assert summary["network_budget"]["planned"] == {
        "EASTMONEY": 4,
        "SINA": 0,
        "TENCENT": 4,
    }
    assert summary["network_budget"]["actual"] == {
        "EASTMONEY": 4,
        "SINA": 0,
        "TENCENT": 4,
    }
    with data_uow() as uow:
        versions = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        uow.rollback()
    assert not any(item.status is DatasetVersionStatus.PUBLISHED for item in versions)


@pytest.mark.parametrize(
    ("field", "value"),
    [("adapter_version", "tampered-adapter"), ("error_code", "SCHEMA_DRIFT")],
)
def test_reuse_rejects_tampered_failed_raw_binding(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    modules = {
        DataUpstream.EASTMONEY: FailingModule([]),
        DataUpstream.TENCENT: FakeModule(_rows(DataUpstream.TENCENT)),
        DataUpstream.SINA: FakeModule(_rows(DataUpstream.SINA)),
    }
    jobs, data_uow, modules, _handler, worker, commands = _runtime(
        tmp_path,
        modules=modules,
    )
    first = _enqueue(commands, "k2-first-network-publish")
    worker.run(first.job_id)
    initial_calls = {source: len(module.calls) for source, module in modules.items()}
    with data_uow() as uow:
        batch = next(
            item
            for item in uow.data.list_batches_for_job(first.job_id)
            if item.upstream is DataUpstream.EASTMONEY
        )
        uow.rollback()
    failure_path = (
        tmp_path
        / "raw"
        / "eastmoney"
        / batch.dataset.value
        / f"{str(batch.batch_id).replace(':', '--')}.incomplete"
        / "failure.json"
    )
    failure = json.loads(failure_path.read_text("utf-8"))
    failure[field] = value
    failure_path.write_text(
        json.dumps(failure, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )

    second = _enqueue(commands, f"k2-failed-raw-tampered-{field}")
    with pytest.raises(EodExecutionError, match="REUSABLE_RESULT_INVALID"):
        worker.run(second.job_id)
    assert jobs.get_job(second.job_id).status is JobStatus.FAILED
    assert {source: len(module.calls) for source, module in modules.items()} == initial_calls
