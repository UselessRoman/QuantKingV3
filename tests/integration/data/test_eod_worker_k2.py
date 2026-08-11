"""Focused K2 production EOD integration without network or formal data roots."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest
from tests.data.adapters.test_akshare_adapter import FakeModule, _rows

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
from quantking.data.adapters.sources.akshare import (
    AkshareDailyBarAdapter,
    AkshareMarketDataGateway,
)
from quantking.data.adapters.sources.ingestion import AkshareIngestionService
from quantking.data.adapters.warehouse.canonical_warehouse import CanonicalWarehouse
from quantking.data.adapters.warehouse.publish_coordinator import CanonicalPublishCoordinator
from quantking.data.adapters.warehouse.raw_store import RawBatchPublisher
from quantking.data.eod.contracts import (
    EodDatasetConfig,
    EodPublishIntent,
    EodReferenceVersion,
    EodRunRequest,
)
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
from quantking.data.quality.models import (
    QualityOutcomeResult,
    QualityRunArtifact,
    RuleOutcomeStatus,
)
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


def _execution_config() -> EodExecutionConfig:
    return EodExecutionConfig(
        code_version="s1-07k2-test",
        adapter_version="akshare-daily-bar.v1",
        rule_version="quality-rules.v1",
        lookback_days=0,
        max_days_per_chunk=31,
        max_items_per_chunk=1,
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
):
    database = tmp_path / "metadata.sqlite3"
    initialize_database(database)
    jobs = JobService(
        SqliteUnitOfWorkFactory(create_sqlite_engine(database, busy_timeout_ms=250)),
        clock=lambda: _NOW.value,
    )
    data_uow = DataSqliteUnitOfWorkFactory(create_data_engine(database, busy_timeout_ms=250))
    selected_modules = modules or {
        upstream: FakeModule(_rows(upstream)) for upstream in DataUpstream
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
        for upstream in DataUpstream
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
        config=_execution_config(),
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
        for upstream in DataUpstream
    }


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
    source_rows = {upstream: _rows(upstream) for upstream in DataUpstream}
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
    with pytest.raises(ConfigurationError, match="EASTMONEY, TENCENT and SINA"):
        build_eod_runtime(
            replace(
                settings,
                paths=replace(settings.paths, data_root=(tmp_path / "bad").resolve()),
            ),
            gateways={DataUpstream.EASTMONEY: _formal_gateways()[DataUpstream.EASTMONEY]},
            execution_config=_execution_config(),
            quality_runner=PassingQualityRunner(),  # type: ignore[arg-type]
        )


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
