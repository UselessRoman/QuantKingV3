from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest

import quantking.common.time as time_module
import quantking.runtime as runtime_module
from quantking.cli.eod import main as eod_cli_main
from quantking.common.config import AppSettings
from quantking.common.ids import new_entity_id
from quantking.common.paths import RuntimePaths
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.adapters.eod import (
    EodExecutionError,
    EodWorkerHandler,
    register_eod_handler,
)
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.adapters.sources.contracts import (
    AmountUnit,
    CodeFormat,
    DateFormat,
    SupplierResponse,
    SupplierResponseContract,
    VolumeUnit,
)
from quantking.data.adapters.sources.ingestion import XtQuantIngestionService
from quantking.data.adapters.sources.normalization import required_fields_for
from quantking.data.adapters.warehouse.canonical_warehouse import CanonicalWarehouse
from quantking.data.adapters.warehouse.publish_coordinator import CanonicalPublishCoordinator
from quantking.data.adapters.warehouse.query import CanonicalQueryAdapter
from quantking.data.adapters.warehouse.raw_store import RawBatchPublisher
from quantking.data.eod.artifacts import (
    EodEvidenceConflictError,
    EodEvidenceWriter,
    verify_eod_failure_evidence,
    verify_eod_success_evidence,
    verify_eod_success_evidence_deep,
    verify_eod_terminal_failure,
    verify_eod_terminal_success,
)
from quantking.data.eod.contracts import (
    EodBaseVersion,
    EodDatasetConfig,
    EodPublishIntent,
    EodRunRequest,
)
from quantking.data.eod.service import EodCommandService, EodExecutionConfig
from quantking.data.incremental import FetchChunk
from quantking.data.models import (
    BatchFailureInfo,
    BatchFailureKind,
    DatasetKind,
    QualityRunStatus,
)
from quantking.data.quality.artifacts import QualityArtifactWriter
from quantking.data.quality.engine import QualityEngine
from quantking.data.quality.models import QualityRunArtifact
from quantking.data.quality.references import PublishedReferenceLoader
from quantking.data.quality.rules import default_rule_set
from quantking.data.quality.runner import QualityRunner
from quantking.jobs.adapters.persistence.sqlite import (
    SqliteUnitOfWorkFactory,
    create_sqlite_engine,
    initialize_database,
)
from quantking.jobs.models import JobStatus
from quantking.jobs.services import JobService
from quantking.jobs.worker import Worker, WorkerRegistry
from quantking.runtime import build_eod_runtime

_NOW = UtcTimestamp(datetime(2026, 7, 15, 18, 0, tzinfo=UTC))


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _resign_success_directory(
    directory: Path,
    *,
    request: dict[str, object] | None = None,
    result: dict[str, object] | None = None,
) -> None:
    request_value = request or json.loads((directory / "request.json").read_bytes())
    result_value = result or json.loads((directory / "result.json").read_bytes())
    request_value["request_sha256"] = hashlib.sha256(
        _canonical_bytes(request_value["request"])
    ).hexdigest()
    request_value["config_sha256"] = hashlib.sha256(
        _canonical_bytes(request_value["config"])
    ).hexdigest()
    request_bytes = _canonical_bytes(request_value)
    result_bytes = _canonical_bytes(result_value)
    job_id = result_value["job_id"]
    unsigned = {
        "schema_version": "eod-evidence-manifest.v1",
        "job_id": job_id,
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "authority_state": "PROVISIONAL_UNTIL_JOB_SUCCEEDED",
    }
    manifest_sha = hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()
    (directory / "request.json").write_bytes(request_bytes)
    (directory / "result.json").write_bytes(result_bytes)
    (directory / "manifest.json").write_bytes(
        _canonical_bytes({**unsigned, "manifest_sha256": manifest_sha})
    )
    (directory / "COMPLETE").write_bytes(
        _canonical_bytes(
            {
                "schema_version": "eod-evidence-complete.v1",
                "job_id": job_id,
                "manifest_sha256": manifest_sha,
            }
        )
    )


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        paths=RuntimePaths(
            code_root=Path.cwd(),
            data_root=tmp_path / "runtime-data",
            offsite_backup_root=tmp_path / "offsite",
        ),
        xtquant_site_packages_root=tmp_path / "missing-sdk",
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


def _data_uow(tmp_path: Path) -> DataSqliteUnitOfWorkFactory:
    return DataSqliteUnitOfWorkFactory(
        create_data_engine(tmp_path / "metadata.db", busy_timeout_ms=250)
    )


class CalendarGateway:
    def __init__(self, *, complete: bool = True) -> None:
        self.calls = 0
        self.complete = complete

    def fetch(self, chunk: object) -> SupplierResponse:
        self.calls += 1
        start = chunk.start.value  # type: ignore[attr-defined]
        end = chunk.end.value  # type: ignore[attr-defined]
        records = tuple(
            {
                "market": exchange,
                "calendar_date": day.isoformat(),
                "open_flag": True,
            }
            for exchange in ("BJ", "SH", "SZ")
            for day in (start + timedelta(days=offset) for offset in range((end - start).days + 1))
        )
        contract = SupplierResponseContract(
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            contract_version="xtquant-synthetic.v1",
            required_fields=("market", "calendar_date", "open_flag"),
            code_format=CodeFormat.CANONICAL,
            date_format=DateFormat.ISO_YYYY_MM_DD,
            exchange_map={"SZ": "SZ", "SH": "SH", "BJ": "BJ"},
            volume_unit=VolumeUnit.SHARE,
            amount_unit=AmountUnit.CNY_YUAN,
            point_in_time=False,
            unadjusted=False,
            official_status=False,
            adjustment_formula=None,
            semantics_verified=True,
        )
        return SupplierResponse.create_bound(
            chunk=chunk,  # type: ignore[arg-type]
            records=records,
            contract=contract,
            complete=self.complete,
            partial_reason=None if self.complete else "synthetic partial",
            provider_payload={"synthetic": True},
        )


class TerminalQualityRunner:
    def __init__(self, status: QualityRunStatus) -> None:
        self.status = status

    def run(self, *, request: object, table: object) -> QualityRunArtifact:
        del table
        failure = (
            BatchFailureInfo(
                error_type=BatchFailureKind.QUALITY,
                message="synthetic quality failure",
                retryable=False,
            )
            if self.status is QualityRunStatus.FAILED
            else None
        )
        return QualityRunArtifact(
            run_id=new_entity_id("data_quality"),
            version_id=request.version_id,  # type: ignore[attr-defined]
            rule_version=request.rule_version,  # type: ignore[attr-defined]
            schema_version=request.schema_version,  # type: ignore[attr-defined]
            content_sha256=request.content_sha256,  # type: ignore[attr-defined]
            started_at=_NOW,
            ended_at=_NOW,
            status=self.status,
            results=(),
            failure=failure,
        )


class ExplodingQualityRunner:
    def run(self, *, request: object, table: object) -> QualityRunArtifact:
        del request, table
        raise RuntimeError(r"password=secret E:\private\payload")


class ExactBaseResolver:
    def __init__(self, bases: dict[str, EodBaseVersion]) -> None:
        self.bases = bases

    def resolve(self, *, dataset: str, version_id: str, scope: tuple[str, ...]):
        del scope
        base = self.bases[version_id]
        if base.dataset != dataset:
            raise ValueError("dataset mismatch")
        return base


class SixDatasetGateway:
    def __init__(self) -> None:
        self.calls: list[DatasetKind] = []

    def fetch(self, chunk: object) -> SupplierResponse:
        dataset = chunk.dataset  # type: ignore[attr-defined]
        self.calls.append(dataset)
        common_bar = {
            "stock_code": "000001.SZ",
            "date": "2026-07-15",
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "pre_close": 9.9,
            "volume": 10_000,
            "amount": 102_000.0,
        }
        records = {
            DatasetKind.EXCHANGE_CALENDAR: (
                {
                    "market": "SZ",
                    "calendar_date": "2026-07-15",
                    "open_flag": True,
                },
            ),
            DatasetKind.SECURITY_MASTER: (
                {
                    "stock_code": "000001.SZ",
                    "exchange_id": "SZ",
                    "instrument_name": "Synthetic",
                    "board": "MAIN",
                    "open_date": "1991-04-03",
                    "expire_date": None,
                    "effective_from": "2026-07-15",
                    "effective_to": None,
                    "listing_status": "LISTED",
                },
            ),
            DatasetKind.DAILY_BAR_UNADJUSTED: (common_bar,),
            DatasetKind.ADJUSTMENT_FACTOR: (
                {
                    "stock_code": "000001.SZ",
                    "date": "2026-07-15",
                    "price_multiplier": 1.0,
                    "volume_multiplier": 1.0,
                    "normalization_date": "2026-07-15",
                    "algorithm_version": "synthetic.v1",
                },
            ),
            DatasetKind.TRADING_STATUS: (
                {
                    "stock_code": "000001.SZ",
                    "date": "2026-07-15",
                    "is_trading": True,
                    "is_suspended": False,
                    "is_st": False,
                    "is_star_st": False,
                    "is_delisting_period": False,
                    "limit_up_price": 11.0,
                    "limit_down_price": 9.0,
                    "status_reason": None,
                },
            ),
            DatasetKind.BENCHMARK_INDEX_DAILY: (common_bar,),
        }[dataset]
        contract = SupplierResponseContract(
            dataset=dataset,
            contract_version="xtquant-synthetic.v1",
            required_fields=required_fields_for(dataset),
            code_format=CodeFormat.CANONICAL,
            date_format=DateFormat.ISO_YYYY_MM_DD,
            exchange_map={"SZ": "SZ", "SH": "SH", "BJ": "BJ"},
            volume_unit=VolumeUnit.SHARE,
            amount_unit=AmountUnit.CNY_YUAN,
            point_in_time=dataset is DatasetKind.SECURITY_MASTER,
            unadjusted=dataset
            in {
                DatasetKind.DAILY_BAR_UNADJUSTED,
                DatasetKind.BENCHMARK_INDEX_DAILY,
            },
            official_status=dataset is DatasetKind.TRADING_STATUS,
            adjustment_formula=(
                "p_adj=p_raw*price_multiplier;v_adj=v_raw*volume_multiplier"
                if dataset is DatasetKind.ADJUSTMENT_FACTOR
                else None
            ),
            semantics_verified=True,
        )
        return SupplierResponse.create_bound(
            chunk=chunk,  # type: ignore[arg-type]
            records=records,
            contract=contract,
            complete=True,
            partial_reason=None,
            provider_payload={"synthetic": True},
        )


def _runtime(
    tmp_path: Path,
    gateway: object,
    *,
    capability_check=None,
    quality_runner_override=None,
    execution_config: EodExecutionConfig | None = None,
    base_resolver=None,
    fault_injector=None,
) -> tuple[JobService, EodCommandService, Worker]:
    database = tmp_path / "metadata.db"
    initialize_database(database)
    jobs_engine = create_sqlite_engine(database, busy_timeout_ms=250)
    data_engine = create_data_engine(database, busy_timeout_ms=250)
    jobs = JobService(SqliteUnitOfWorkFactory(jobs_engine), clock=lambda: _NOW.value)
    data_uow = DataSqliteUnitOfWorkFactory(data_engine)
    ingestion = XtQuantIngestionService(
        gateway=gateway,  # type: ignore[arg-type]
        raw_publisher=RawBatchPublisher(data_root=tmp_path),
        data_root=tmp_path,
        uow_factory=data_uow,
        clock=lambda: _NOW,
    )
    quality_runner = quality_runner_override or QualityRunner(
        engine=QualityEngine(rule_set=default_rule_set()),
        reference_loader=PublishedReferenceLoader(
            data_root=tmp_path,
            uow_factory=data_uow,
        ),
    )
    handler = EodWorkerHandler(
        ingestion=ingestion,
        quality_runner=quality_runner,
        quality_writer=QualityArtifactWriter(artifacts_root=tmp_path / "artifacts" / "quality"),
        publish_coordinator=CanonicalPublishCoordinator(
            warehouse=CanonicalWarehouse(data_root=tmp_path),
            uow_factory=data_uow,
        ),
        data_uow_factory=data_uow,
        evidence_writer=EodEvidenceWriter(artifacts_root=tmp_path / "artifacts" / "eod"),
        data_root=tmp_path,
        clock=lambda: _NOW,
        capability_check=capability_check,
        log_root=tmp_path,
        fault_injector=fault_injector,
    )
    registry = WorkerRegistry()
    register_eod_handler(registry, handler)
    command = EodCommandService(
        jobs,
        config=execution_config or EodExecutionConfig.production(code_version="test-code"),
        base_resolver=base_resolver,
    )
    return jobs, command, Worker(jobs, registry, worker_id="worker-1")


def _base_from_job(
    tmp_path: Path,
    jobs: JobService,
    job_id: str,
) -> EodBaseVersion:
    evidence = verify_eod_success_evidence(
        tmp_path / jobs.get_job(job_id).artifacts[0],
        artifacts_root=tmp_path / "artifacts" / "eod",
    )
    result = evidence.result["datasets"][0]  # type: ignore[index]
    return EodBaseVersion(
        version_id=result["version_id"],
        dataset=DatasetKind.EXCHANGE_CALENDAR.value,
        schema_version="exchange_calendar.v1",
        content_sha256=result["content_sha256"],
        manifest_sha256=result["manifest_sha256"],
        coverage=tuple((exchange, "2026-07-15", "2026-07-15") for exchange in ("BJ", "SH", "SZ")),
    )


def _enqueue(
    command: EodCommandService,
    *,
    datasets=("exchange_calendar",),
    key: str = "eod-worker-test",
    base_versions: dict[str, str] | None = None,
) -> str:
    return command.enqueue(
        EodRunRequest(
            start_date=_NOW.value.date(),
            end_date=_NOW.value.date(),
            datasets=datasets,
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={} if base_versions is None else base_versions,
        ),
        idempotency_key=key,
        requester="pytest",
    ).job_id


def test_complete_fake_gateway_flow_publishes_and_writes_strict_evidence(
    tmp_path: Path,
) -> None:
    gateway = CalendarGateway()
    jobs, command, worker = _runtime(tmp_path, gateway)
    job_id = _enqueue(command)

    worker.run(job_id)

    job = jobs.get_job(job_id)
    assert job.status is JobStatus.SUCCEEDED
    assert gateway.calls == 1
    assert len(job.artifacts) == 1
    assert (tmp_path / job.log_id).is_file()
    audits = jobs.list_audits(action="eod_dataset_published")
    assert len(audits) == 1
    assert audits[0].entity == job_id
    evidence = verify_eod_success_evidence(
        tmp_path / job.artifacts[0],
        artifacts_root=tmp_path / "artifacts" / "eod",
    )
    verify_eod_terminal_success(
        tmp_path / job.artifacts[0],
        artifacts_root=tmp_path / "artifacts" / "eod",
        data_root=tmp_path,
        uow_factory=DataSqliteUnitOfWorkFactory(
            create_data_engine(tmp_path / "metadata.db", busy_timeout_ms=250)
        ),
        job=job,
    )
    result = evidence.result["datasets"][0]  # type: ignore[index]
    assert result["status"] == "PUBLISHED_NEW"
    assert result["batch_id"].startswith("data_batch:")
    assert result["version_id"].startswith("data_version:")
    assert result["quality_run_id"].startswith("data_quality:")


def test_fake_gateway_can_exercise_all_six_datasets_without_changing_production_matrix(
    tmp_path: Path,
) -> None:
    gateway = SixDatasetGateway()
    datasets = tuple(item.value for item in DatasetKind)
    config = EodExecutionConfig(
        code_version="test-code",
        adapter_version="xtquant-synthetic.v1",
        rule_version="quality-rules.v1",
        lookback_days=0,
        max_days_per_chunk=1,
        max_items_per_chunk=10,
        datasets={
            item.value: EodDatasetConfig(
                scope=("SZ",) if item is DatasetKind.EXCHANGE_CALENDAR else ("000001.SZ",)
            )
            for item in DatasetKind
        },
    )
    jobs, command, worker = _runtime(
        tmp_path,
        gateway,
        capability_check=lambda dataset: True,
        execution_config=config,
    )
    job_id = _enqueue(command, datasets=datasets)

    worker.run(job_id)

    assert jobs.get_job(job_id).status is JobStatus.SUCCEEDED
    assert gateway.calls == [
        DatasetKind.EXCHANGE_CALENDAR,
        DatasetKind.SECURITY_MASTER,
        DatasetKind.DAILY_BAR_UNADJUSTED,
        DatasetKind.ADJUSTMENT_FACTOR,
        DatasetKind.TRADING_STATUS,
        DatasetKind.BENCHMARK_INDEX_DAILY,
    ]


def test_unverified_capability_fails_before_gateway_and_retains_failure_evidence(
    tmp_path: Path,
) -> None:
    gateway = CalendarGateway()
    jobs, command, worker = _runtime(
        tmp_path,
        gateway,
        capability_check=lambda dataset: False,
    )
    job_id = _enqueue(command)

    with pytest.raises(Exception, match="capability"):
        worker.run(job_id)

    job = jobs.get_job(job_id)
    assert job.status is JobStatus.FAILED
    assert gateway.calls == 0
    assert len(job.artifacts) == 1
    failure = (tmp_path / job.artifacts[0] / "failure.json").read_text(encoding="utf-8")
    assert "CAPABILITY_UNAVAILABLE" in failure
    assert "path" not in failure.casefold()


@pytest.mark.parametrize(
    "dataset",
    [
        "security_master",
        "daily_bar_unadjusted",
        "adjustment_factor",
        "trading_status",
        "benchmark_index_daily",
    ],
)
def test_each_unverified_production_capability_fails_before_gateway(
    tmp_path: Path,
    dataset: str,
) -> None:
    gateway = CalendarGateway()
    jobs, command, worker = _runtime(tmp_path, gateway)
    job_id = _enqueue(command, datasets=(dataset,))

    with pytest.raises(Exception, match="capability"):
        worker.run(job_id)

    assert jobs.get_job(job_id).status is JobStatus.FAILED
    assert gateway.calls == 0


def test_partial_ingestion_never_publishes_and_job_is_terminal_failed(tmp_path: Path) -> None:
    gateway = CalendarGateway(complete=False)
    jobs, command, worker = _runtime(tmp_path, gateway)
    job_id = _enqueue(command)

    with pytest.raises(Exception, match="complete candidate"):
        worker.run(job_id)

    job = jobs.get_job(job_id)
    assert job.status is JobStatus.FAILED
    assert not (tmp_path / "warehouse").exists()
    assert gateway.calls == 1


@pytest.mark.parametrize(
    "status",
    [QualityRunStatus.FAILED, QualityRunStatus.SKIPPED],
)
def test_failed_or_skipped_quality_never_publishes(
    tmp_path: Path,
    status: QualityRunStatus,
) -> None:
    gateway = CalendarGateway()
    jobs, command, worker = _runtime(
        tmp_path,
        gateway,
        quality_runner_override=TerminalQualityRunner(status),
    )
    job_id = _enqueue(command)

    with pytest.raises(Exception, match="quality"):
        worker.run(job_id)

    assert jobs.get_job(job_id).status is JobStatus.FAILED
    assert not (tmp_path / "warehouse").exists()


def test_quality_exception_is_sanitized_and_never_leaves_running(tmp_path: Path) -> None:
    gateway = CalendarGateway()
    jobs, command, worker = _runtime(
        tmp_path,
        gateway,
        quality_runner_override=ExplodingQualityRunner(),
    )
    job_id = _enqueue(command)

    with pytest.raises(RuntimeError, match="secret"):
        worker.run(job_id)

    job = jobs.get_job(job_id)
    assert job.status is JobStatus.FAILED
    assert job.failure is not None
    assert "secret" not in job.failure.message
    failure = (tmp_path / job.artifacts[0] / "failure.json").read_text(encoding="utf-8")
    assert "secret" not in failure
    assert "E:\\" not in failure


def test_worker_rejects_tampered_persisted_manifest_before_gateway(tmp_path: Path) -> None:
    gateway = CalendarGateway()
    jobs, command, worker = _runtime(tmp_path, gateway)
    job_id = _enqueue(command)
    with sqlite3.connect(tmp_path / "metadata.db") as connection:
        connection.execute(
            "UPDATE job_runs SET input_manifest_json = ? WHERE job_id = ?",
            (
                '{"request_json":"{}","request_sha256":"'
                + "0" * 64
                + '","schema_version":"eod-request-manifest.v1"}',
                job_id,
            ),
        )
        connection.commit()

    with pytest.raises(Exception, match="request"):
        worker.run(job_id)

    job = jobs.get_job(job_id)
    assert job.status is JobStatus.FAILED
    assert gateway.calls == 0
    assert len(job.artifacts) == 1
    assert "REQUEST_MANIFEST_INVALID" in (tmp_path / job.artifacts[0] / "failure.json").read_text(
        encoding="utf-8"
    )
    verify_eod_terminal_failure(
        tmp_path / job.artifacts[0],
        artifacts_root=tmp_path / "artifacts" / "eod",
        data_root=tmp_path,
        uow_factory=_data_uow(tmp_path),
        job=job,
    )


def test_failed_retry_requires_new_job_key_and_creates_new_batch_and_version(
    tmp_path: Path,
) -> None:
    gateway = CalendarGateway(complete=False)
    jobs, command, worker = _runtime(tmp_path, gateway)
    failed_job_id = _enqueue(command, key="first-attempt")
    with pytest.raises(EodExecutionError):
        worker.run(failed_job_id)

    gateway.complete = True
    retry_job_id = _enqueue(command, key="second-attempt")
    worker.run(retry_job_id)

    assert failed_job_id != retry_job_id
    assert jobs.get_job(failed_job_id).status is JobStatus.FAILED
    assert jobs.get_job(retry_job_id).status is JobStatus.SUCCEEDED
    with sqlite3.connect(tmp_path / "metadata.db") as connection:
        batches = connection.execute(
            "SELECT batch_id, status FROM ingestion_batches ORDER BY started_at, batch_id"
        ).fetchall()
        versions = connection.execute("SELECT version_id, status FROM dataset_versions").fetchall()
    assert len({row[0] for row in batches}) == 2
    assert {row[1] for row in batches} == {"FAILED", "SUCCEEDED"}
    assert len(versions) == 1
    assert versions[0][1] == "PUBLISHED"


def test_second_incremental_publish_preserves_base_history(tmp_path: Path) -> None:
    gateway = CalendarGateway()
    jobs, first_command, worker = _runtime(tmp_path, gateway)
    first_job_id = _enqueue(first_command, key="history-first")
    worker.run(first_job_id)
    base = _base_from_job(tmp_path, jobs, first_job_id)

    second_command = EodCommandService(
        jobs,
        config=EodExecutionConfig(
            code_version="test-code",
            adapter_version="xtquant-readonly.v1",
            rule_version="quality-rules.v1",
            lookback_days=0,
            max_days_per_chunk=31,
            max_items_per_chunk=500,
            datasets={
                DatasetKind.EXCHANGE_CALENDAR.value: EodDatasetConfig(
                    scope=("BJ", "SH", "SZ"),
                )
            },
        ),
        base_resolver=ExactBaseResolver({base.version_id: base}),
    )
    second_job_id = second_command.enqueue(
        EodRunRequest(
            start_date=_NOW.value.date(),
            end_date=_NOW.value.date() + timedelta(days=1),
            datasets=("exchange_calendar",),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={"exchange_calendar": base.version_id},
        ),
        idempotency_key="history-second",
        requester="pytest",
    ).job_id
    worker.run(second_job_id)

    evidence = verify_eod_success_evidence(
        tmp_path / jobs.get_job(second_job_id).artifacts[0],
        artifacts_root=tmp_path / "artifacts" / "eod",
    )
    version_id = evidence.result["datasets"][0]["version_id"]  # type: ignore[index]
    query = CanonicalQueryAdapter(
        data_root=tmp_path,
        uow_factory=DataSqliteUnitOfWorkFactory(
            create_data_engine(tmp_path / "metadata.db", busy_timeout_ms=250)
        ),
    )
    table = query.query_by_id(
        version_id=version_id,
        dataset=DatasetKind.EXCHANGE_CALENDAR.value,
    )

    assert sorted({item.isoformat() for item in table.column("trade_date").to_pylist()}) == [
        "2026-07-15",
        "2026-07-16",
    ]


def test_formal_runtime_resolves_exact_base_and_reuses_no_change(tmp_path: Path) -> None:
    gateway = CalendarGateway()
    runtime = build_eod_runtime(_settings(tmp_path), gateway=gateway)
    first = runtime.eod_commands.enqueue(
        EodRunRequest(
            start_date=date(2026, 7, 15),
            end_date=date(2026, 7, 15),
            datasets=("exchange_calendar",),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        ),
        idempotency_key="formal-root",
        requester="pytest",
    )
    runtime.worker.run(first.job_id)
    first_result = verify_eod_success_evidence(
        runtime.settings.paths.data_root / runtime.jobs.get_job(first.job_id).artifacts[0],
        artifacts_root=runtime.settings.paths.data_root / "artifacts" / "eod",
    ).parsed_result.datasets[0]

    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("QUANTKING_")
    }
    environment.update(
        {
            "QUANTKING_CODE_ROOT": str(Path.cwd()),
            "QUANTKING_DATA_ROOT": str(runtime.settings.paths.data_root),
            "QUANTKING_OFFSITE_BACKUP_ROOT": str(tmp_path / "offsite"),
            "QUANTKING_XTQUANT_SITE_PACKAGES_ROOT": str(tmp_path / "missing-sdk"),
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "quantking.cli.eod",
            "eod-enqueue",
            "--trade-date",
            "2026-07-16",
            "--datasets",
            "exchange_calendar",
            "--base-version",
            f"exchange_calendar={first_result.version_id}",
            "--idempotency-key",
            "formal-child",
            "--publish-intent",
            "PUBLISH_AFTER_QUALITY",
        ],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    second_job_id = json.loads(completed.stdout)["job_id"]
    second_runtime = build_eod_runtime(_settings(tmp_path), gateway=gateway)
    second_runtime.worker.run(second_job_id)
    second_result = verify_eod_success_evidence(
        second_runtime.settings.paths.data_root
        / second_runtime.jobs.get_job(second_job_id).artifacts[0],
        artifacts_root=second_runtime.settings.paths.data_root / "artifacts" / "eod",
    ).parsed_result.datasets[0]
    assert second_result.parent_version_id == first_result.version_id

    third_job_id = second_runtime.eod_commands.enqueue(
        EodRunRequest(
            start_date=date(2026, 7, 16),
            end_date=date(2026, 7, 16),
            datasets=("exchange_calendar",),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={"exchange_calendar": second_result.version_id},
        ),
        idempotency_key="formal-no-change",
        requester="pytest",
    ).job_id
    second_runtime.worker.run(third_job_id)
    third_result = verify_eod_success_evidence(
        second_runtime.settings.paths.data_root
        / second_runtime.jobs.get_job(third_job_id).artifacts[0],
        artifacts_root=second_runtime.settings.paths.data_root / "artifacts" / "eod",
    ).parsed_result.datasets[0]
    with sqlite3.connect(
        second_runtime.settings.paths.data_root / "metadata" / "quantking.db"
    ) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ingestion_batches").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM dataset_versions").fetchone() == (2,)
    assert third_result.status == "REUSED_UNCHANGED"
    assert third_result.version_id == second_result.version_id


def test_web_cli_runtime_never_recovers_fresh_worker_lease(tmp_path: Path) -> None:
    runtime = build_eod_runtime(_settings(tmp_path), gateway=CalendarGateway())
    outcome = runtime.eod_commands.enqueue(
        EodRunRequest(
            start_date=date(2026, 7, 15),
            end_date=date(2026, 7, 15),
            datasets=("exchange_calendar",),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        ),
        idempotency_key="runtime-lease",
        requester="pytest",
    )
    started = UtcTimestamp(datetime(2026, 7, 15, 1, 0, tzinfo=UTC))
    runtime.jobs.transition(
        outcome.job_id,
        JobStatus.RUNNING,
        worker_id="active-worker",
        at=started,
    )
    heartbeat = UtcTimestamp(started.value + timedelta(seconds=30))
    runtime.jobs.heartbeat(outcome.job_id, at=heartbeat)

    web_cli_runtime = build_eod_runtime(_settings(tmp_path), gateway=CalendarGateway())
    assert web_cli_runtime.jobs.get_job(outcome.job_id).status is JobStatus.RUNNING
    assert (
        web_cli_runtime.jobs.recover_stale_running_jobs(
            reason="expired lease",
            stale_before=UtcTimestamp(heartbeat.value - timedelta(seconds=1)),
            at=UtcTimestamp(heartbeat.value + timedelta(minutes=3)),
        )
        == 0
    )
    assert web_cli_runtime.jobs.get_job(outcome.job_id).status is JobStatus.RUNNING
    assert (
        web_cli_runtime.jobs.recover_stale_running_jobs(
            reason="expired lease",
            stale_before=UtcTimestamp(heartbeat.value + timedelta(minutes=2)),
            at=UtcTimestamp(heartbeat.value + timedelta(minutes=3)),
        )
        == 1
    )
    assert web_cli_runtime.jobs.get_job(outcome.job_id).status is JobStatus.INTERRUPTED


def test_formal_runtime_rejects_existing_database_at_wrong_revision(tmp_path: Path) -> None:
    runtime = build_eod_runtime(_settings(tmp_path), gateway=CalendarGateway())
    database = runtime.settings.paths.data_root / "metadata" / "quantking.db"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'forged_revision'")
        connection.commit()

    with pytest.raises(Exception, match="revision"):
        build_eod_runtime(_settings(tmp_path), gateway=CalendarGateway())


def test_post_publish_failure_discovers_version_in_failure_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = CalendarGateway()
    jobs, command, worker = _runtime(tmp_path, gateway)
    original = CanonicalPublishCoordinator.coordinate

    def publish_then_fail(self, request):
        original(self, request)
        raise RuntimeError("synthetic fault after coordinator returned")

    monkeypatch.setattr(CanonicalPublishCoordinator, "coordinate", publish_then_fail)
    job_id = _enqueue(command, key="post-publish-fault")

    with pytest.raises(EodExecutionError, match="RuntimeError"):
        worker.run(job_id)

    with sqlite3.connect(tmp_path / "metadata.db") as connection:
        published = connection.execute(
            "SELECT version_id FROM dataset_versions WHERE status = 'PUBLISHED'"
        ).fetchall()
    failure = json.loads(
        (tmp_path / jobs.get_job(job_id).artifacts[0] / "failure.json").read_bytes()
    )

    assert len(published) == 1
    assert [item["version_id"] for item in failure["completed_datasets"]] == [published[0][0]]


def test_fully_covered_request_reuses_base_without_empty_batch_or_version(
    tmp_path: Path,
) -> None:
    gateway = CalendarGateway()
    jobs, first_command, worker = _runtime(tmp_path, gateway)
    first_job_id = _enqueue(first_command, key="no-change-first")
    worker.run(first_job_id)
    base = _base_from_job(tmp_path, jobs, first_job_id)
    command = EodCommandService(
        jobs,
        config=EodExecutionConfig(
            code_version="test-code",
            adapter_version="xtquant-readonly.v1",
            rule_version="quality-rules.v1",
            lookback_days=0,
            max_days_per_chunk=31,
            max_items_per_chunk=500,
            datasets={
                DatasetKind.EXCHANGE_CALENDAR.value: EodDatasetConfig(
                    scope=("BJ", "SH", "SZ"),
                )
            },
        ),
        base_resolver=ExactBaseResolver({base.version_id: base}),
    )
    job_id = _enqueue(
        command,
        key="no-change-second",
        base_versions={"exchange_calendar": base.version_id},
    )
    worker.run(job_id)

    evidence = verify_eod_success_evidence(
        tmp_path / jobs.get_job(job_id).artifacts[0],
        artifacts_root=tmp_path / "artifacts" / "eod",
    )
    result = evidence.result["datasets"][0]  # type: ignore[index]
    with sqlite3.connect(tmp_path / "metadata.db") as connection:
        batch_count = connection.execute("SELECT COUNT(*) FROM ingestion_batches").fetchone()[0]
        version_count = connection.execute("SELECT COUNT(*) FROM dataset_versions").fetchone()[0]

    assert result["status"] == "REUSED_UNCHANGED"
    assert result["batch_id"] is None
    assert result["version_id"] == base.version_id
    assert gateway.calls == 1
    assert batch_count == 1
    assert version_count == 1


def test_tampered_base_credentials_fail_before_gateway(tmp_path: Path) -> None:
    gateway = CalendarGateway()
    jobs, first_command, worker = _runtime(tmp_path, gateway)
    first_job_id = _enqueue(first_command, key="base-valid")
    worker.run(first_job_id)
    base = _base_from_job(tmp_path, jobs, first_job_id)
    invalid = EodBaseVersion(
        version_id=base.version_id,
        dataset=base.dataset,
        schema_version=base.schema_version,
        content_sha256="f" * 64,
        manifest_sha256=base.manifest_sha256,
        coverage=base.coverage,
    )
    command = EodCommandService(
        jobs,
        config=EodExecutionConfig(
            code_version="test-code",
            adapter_version="xtquant-readonly.v1",
            rule_version="quality-rules.v1",
            lookback_days=0,
            max_days_per_chunk=31,
            max_items_per_chunk=500,
            datasets={
                DatasetKind.EXCHANGE_CALENDAR.value: EodDatasetConfig(
                    scope=("BJ", "SH", "SZ"),
                )
            },
        ),
        base_resolver=ExactBaseResolver({invalid.version_id: invalid}),
    )
    job_id = _enqueue(
        command,
        key="base-invalid",
        base_versions={"exchange_calendar": invalid.version_id},
    )

    with pytest.raises(EodExecutionError, match="base version"):
        worker.run(job_id)

    assert gateway.calls == 1
    assert jobs.get_job(job_id).status is JobStatus.FAILED


@pytest.mark.parametrize(
    "fault_point",
    [
        "after_coordinator_return",
        "after_warehouse_verify",
        "before_checkpoint_write",
        "after_checkpoint_write",
        "before_job_event_audit",
        "after_success_evidence_before_job_succeeded",
    ],
)
def test_each_post_publish_fault_retains_completed_failure_evidence(
    tmp_path: Path,
    fault_point: str,
) -> None:
    def inject(point: str) -> None:
        if point == fault_point:
            raise RuntimeError(f"fault:{point}")

    gateway = CalendarGateway()
    jobs, command, worker = _runtime(
        tmp_path,
        gateway,
        fault_injector=inject,
    )
    job_id = _enqueue(command, key=f"fault-{fault_point}")

    with pytest.raises((EodExecutionError, RuntimeError)):
        worker.run(job_id)

    job = jobs.get_job(job_id)
    failure = json.loads((tmp_path / job.artifacts[0] / "failure.json").read_bytes())
    assert job.status is JobStatus.FAILED
    assert len(failure["completed_datasets"]) == 1
    assert failure["completed_datasets"][0]["status"] == "PUBLISHED_NEW"
    if fault_point == "after_success_evidence_before_job_succeeded":
        forensic = tmp_path / "artifacts" / "eod" / job_id.replace(":", "_")
        assert forensic.is_dir()
        assert (
            json.loads((forensic / "manifest.json").read_bytes())["authority_state"]
            == "PROVISIONAL_UNTIL_JOB_SUCCEEDED"
        )
        with pytest.raises(EodEvidenceConflictError, match="forensic"):
            verify_eod_terminal_success(
                forensic,
                artifacts_root=tmp_path / "artifacts" / "eod",
                data_root=tmp_path,
                uow_factory=DataSqliteUnitOfWorkFactory(
                    create_data_engine(tmp_path / "metadata.db", busy_timeout_ms=250)
                ),
                job=job,
            )


def test_lookback_authoritative_slice_removes_missing_base_row() -> None:
    base = pa.Table.from_pylist(
        [
            {"exchange": "SZ", "trade_date": date(2026, 7, 13), "is_open": True},
            {"exchange": "SZ", "trade_date": date(2026, 7, 14), "is_open": True},
            {"exchange": "SZ", "trade_date": date(2026, 7, 15), "is_open": True},
        ],
        schema=canonical_schema("exchange_calendar"),
    )
    candidate = pa.Table.from_pylist(
        [{"exchange": "SZ", "trade_date": date(2026, 7, 15), "is_open": False}],
        schema=canonical_schema("exchange_calendar"),
    )
    chunk = FetchChunk(
        sequence=0,
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        scope=("SZ",),
        start=TradingDate(date(2026, 7, 14)),
        end=TradingDate(date(2026, 7, 15)),
        request_sha256="a" * 64,
    )

    merged = EodWorkerHandler._merge_snapshot(
        DatasetKind.EXCHANGE_CALENDAR,
        base=base,
        candidate=candidate,
        chunks=(chunk,),
    )

    assert merged.to_pylist() == [
        {"exchange": "SZ", "trade_date": date(2026, 7, 13), "is_open": True},
        {"exchange": "SZ", "trade_date": date(2026, 7, 15), "is_open": False},
    ]


@pytest.mark.parametrize("case", ["partial_scope", "duplicate_pk", "out_of_range"])
def test_lookback_merge_rejects_unsafe_candidate_inventory(case: str) -> None:
    base = pa.Table.from_pylist(
        [
            {"exchange": exchange, "trade_date": date(2026, 7, 15), "is_open": True}
            for exchange in ("SH", "SZ")
        ],
        schema=canonical_schema("exchange_calendar"),
    )
    records = [{"exchange": "SZ", "trade_date": date(2026, 7, 15), "is_open": False}]
    scope = ("SH", "SZ")
    if case == "duplicate_pk":
        records.append(dict(records[0]))
        scope = ("SZ",)
    elif case == "out_of_range":
        records[0]["trade_date"] = date(2026, 7, 16)
        scope = ("SZ",)
    candidate = pa.Table.from_pylist(
        records,
        schema=canonical_schema("exchange_calendar"),
    )
    chunk = FetchChunk(
        sequence=0,
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        scope=scope,
        start=TradingDate(date(2026, 7, 15)),
        end=TradingDate(date(2026, 7, 15)),
        request_sha256="a" * 64,
    )

    with pytest.raises(EodExecutionError, match="candidate"):
        EodWorkerHandler._merge_snapshot(
            DatasetKind.EXCHANGE_CALENDAR,
            base=base,
            candidate=candidate,
            chunks=(chunk,),
        )


def test_retry_reuses_strictly_verified_post_publish_result(tmp_path: Path) -> None:
    armed = True

    def inject(point: str) -> None:
        nonlocal armed
        if armed and point == "after_checkpoint_write":
            armed = False
            raise RuntimeError("one-shot post-publish fault")

    gateway = CalendarGateway()
    jobs, command, worker = _runtime(
        tmp_path,
        gateway,
        fault_injector=inject,
    )
    failed_job = _enqueue(command, key="published-retry-first")
    with pytest.raises(EodExecutionError):
        worker.run(failed_job)
    retry_job = _enqueue(command, key="published-retry-second")
    worker.run(retry_job)

    with sqlite3.connect(tmp_path / "metadata.db") as connection:
        versions = connection.execute(
            "SELECT version_id FROM dataset_versions WHERE status = 'PUBLISHED'"
        ).fetchall()
        batches = connection.execute("SELECT batch_id FROM ingestion_batches").fetchall()
    first_failure = json.loads(
        (tmp_path / jobs.get_job(failed_job).artifacts[0] / "failure.json").read_bytes()
    )
    retry_result = verify_eod_success_evidence(
        tmp_path / jobs.get_job(retry_job).artifacts[0],
        artifacts_root=tmp_path / "artifacts" / "eod",
    ).result["datasets"][0]  # type: ignore[index]

    assert len(versions) == 1
    assert len(batches) == 1
    assert retry_result["version_id"] == first_failure["completed_datasets"][0]["version_id"]


def test_deep_success_verifier_rejects_resigned_repository_mismatch(
    tmp_path: Path,
) -> None:
    gateway = CalendarGateway()
    jobs, command, worker = _runtime(tmp_path, gateway)
    job_id = _enqueue(command, key="deep-verifier")
    worker.run(job_id)
    directory = tmp_path / jobs.get_job(job_id).artifacts[0]
    result = json.loads((directory / "result.json").read_bytes())
    result["datasets"][0]["content_sha256"] = "f" * 64

    def canonical_bytes(value: object) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()

    result_bytes = canonical_bytes(result)
    (directory / "result.json").write_bytes(result_bytes)
    request_bytes = (directory / "request.json").read_bytes()
    unsigned = {
        "schema_version": "eod-evidence-manifest.v1",
        "job_id": job_id,
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "authority_state": "PROVISIONAL_UNTIL_JOB_SUCCEEDED",
    }
    manifest_sha = hashlib.sha256(canonical_bytes(unsigned)).hexdigest()
    (directory / "manifest.json").write_bytes(
        canonical_bytes({**unsigned, "manifest_sha256": manifest_sha})
    )
    (directory / "COMPLETE").write_bytes(
        canonical_bytes(
            {
                "schema_version": "eod-evidence-complete.v1",
                "job_id": job_id,
                "manifest_sha256": manifest_sha,
            }
        )
    )
    verify_eod_success_evidence(
        directory,
        artifacts_root=tmp_path / "artifacts" / "eod",
    )

    with pytest.raises(EodEvidenceConflictError, match="credentials"):
        verify_eod_success_evidence_deep(
            directory,
            artifacts_root=tmp_path / "artifacts" / "eod",
            data_root=tmp_path,
            uow_factory=DataSqliteUnitOfWorkFactory(
                create_data_engine(tmp_path / "metadata.db", busy_timeout_ms=250)
            ),
        )


@pytest.mark.parametrize(
    "attack",
    ["request_date", "scope", "base", "chunk", "batch", "schema_version"],
)
def test_deep_success_verifier_rejects_resigned_frozen_command_attack(
    tmp_path: Path,
    attack: str,
) -> None:
    gateway = CalendarGateway()
    jobs, command, worker = _runtime(tmp_path, gateway)
    job_id = _enqueue(command, key="deep-request-binding")
    worker.run(job_id)
    directory = tmp_path / jobs.get_job(job_id).artifacts[0]
    request = json.loads((directory / "request.json").read_bytes())
    result = json.loads((directory / "result.json").read_bytes())
    if attack == "request_date":
        request["request"]["start_date"] = "2026-07-14"
        request["request"]["end_date"] = "2026-07-14"
    elif attack == "scope":
        request["config"]["datasets"]["exchange_calendar"]["scope"] = ["SZ"]
    elif attack == "base":
        dataset_result = result["datasets"][0]
        version_id = dataset_result["version_id"]
        request["request"]["base_versions"] = {"exchange_calendar": version_id}
        base = {
            "version_id": version_id,
            "dataset": "exchange_calendar",
            "schema_version": "exchange_calendar.v1",
            "content_sha256": dataset_result["content_sha256"],
            "manifest_sha256": dataset_result["manifest_sha256"],
            "coverage": [
                {"scope": item, "start": "2026-07-15", "end": "2026-07-15"}
                for item in ("BJ", "SH", "SZ")
            ],
        }
        request["config"]["datasets"]["exchange_calendar"]["base_version"] = base
        request["config"]["datasets"]["exchange_calendar"]["known_coverage"] = base["coverage"]
    elif attack == "chunk":
        request["config"]["max_days_per_chunk"] = 1
    elif attack == "batch":
        result["datasets"][0]["batch_id"] = "data_batch:99999999-9999-4999-8999-999999999999"
    else:
        request["config"]["schema_versions"]["exchange_calendar"] = "forged.v1"
        request["version_manifest"]["schema_versions_sha256"] = hashlib.sha256(
            json.dumps(
                request["config"]["schema_versions"],
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def canonical_bytes(value: object) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()

    request["request_sha256"] = hashlib.sha256(canonical_bytes(request["request"])).hexdigest()
    request["config_sha256"] = hashlib.sha256(canonical_bytes(request["config"])).hexdigest()
    request_bytes = canonical_bytes(request)
    (directory / "request.json").write_bytes(request_bytes)
    result_bytes = canonical_bytes(result)
    (directory / "result.json").write_bytes(result_bytes)
    unsigned = {
        "schema_version": "eod-evidence-manifest.v1",
        "job_id": job_id,
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "authority_state": "PROVISIONAL_UNTIL_JOB_SUCCEEDED",
    }
    manifest_sha = hashlib.sha256(canonical_bytes(unsigned)).hexdigest()
    (directory / "manifest.json").write_bytes(
        canonical_bytes({**unsigned, "manifest_sha256": manifest_sha})
    )
    (directory / "COMPLETE").write_bytes(
        canonical_bytes(
            {
                "schema_version": "eod-evidence-complete.v1",
                "job_id": job_id,
                "manifest_sha256": manifest_sha,
            }
        )
    )

    with pytest.raises(EodEvidenceConflictError):
        verify_eod_success_evidence_deep(
            directory,
            artifacts_root=tmp_path / "artifacts" / "eod",
            data_root=tmp_path,
            uow_factory=DataSqliteUnitOfWorkFactory(
                create_data_engine(tmp_path / "metadata.db", busy_timeout_ms=250)
            ),
        )


@pytest.mark.parametrize(
    "field",
    ["adapter_version", "code_version", "rule_version", "references"],
)
def test_terminal_success_rejects_self_signed_command_not_matching_job(
    tmp_path: Path,
    field: str,
) -> None:
    gateway = CalendarGateway()
    jobs, command, worker = _runtime(tmp_path, gateway)
    job_id = _enqueue(command, key=f"terminal-binding-{field}")
    worker.run(job_id)
    job = jobs.get_job(job_id)
    directory = tmp_path / job.artifacts[0]
    request = json.loads((directory / "request.json").read_bytes())
    if field == "references":
        request["config"]["datasets"]["exchange_calendar"]["references"] = {
            "forged": {
                "dataset": "exchange_calendar",
                "version_id": "data_version:99999999-9999-4999-8999-999999999999",
                "content_sha256": "9" * 64,
            }
        }
    elif field in {"code_version", "rule_version"}:
        request["config"][field] = f"forged-{field}"
        request["version_manifest"][field] = f"forged-{field}"
    else:
        request["config"][field] = f"forged-{field}"
    _resign_success_directory(directory, request=request)

    if field != "references":
        with pytest.raises(EodEvidenceConflictError):
            verify_eod_success_evidence_deep(
                directory,
                artifacts_root=tmp_path / "artifacts" / "eod",
                data_root=tmp_path,
                uow_factory=DataSqliteUnitOfWorkFactory(
                    create_data_engine(tmp_path / "metadata.db", busy_timeout_ms=250)
                ),
            )
    with pytest.raises(EodEvidenceConflictError):
        verify_eod_terminal_success(
            directory,
            artifacts_root=tmp_path / "artifacts" / "eod",
            data_root=tmp_path,
            uow_factory=DataSqliteUnitOfWorkFactory(
                create_data_engine(tmp_path / "metadata.db", busy_timeout_ms=250)
            ),
            job=job,
        )


@pytest.mark.parametrize("field", ["code_version", "rule_version", "references"])
def test_terminal_failure_rejects_self_signed_command_not_matching_job(
    tmp_path: Path,
    field: str,
) -> None:
    gateway = CalendarGateway(complete=False)
    jobs, command, worker = _runtime(tmp_path, gateway)
    job_id = _enqueue(command, key=f"terminal-failure-binding-{field}")
    with pytest.raises(EodExecutionError):
        worker.run(job_id)
    job = jobs.get_job(job_id)
    directory = tmp_path / job.artifacts[0]
    request = json.loads((directory / "request.json").read_bytes())
    if field == "references":
        request["config"]["datasets"]["exchange_calendar"]["references"] = {
            "forged": {
                "dataset": "exchange_calendar",
                "version_id": "data_version:99999999-9999-4999-8999-999999999999",
                "content_sha256": "9" * 64,
            }
        }
    else:
        request["config"][field] = f"forged-{field}"
        request["version_manifest"][field] = f"forged-{field}"
    request["config_sha256"] = hashlib.sha256(_canonical_bytes(request["config"])).hexdigest()
    (directory / "request.json").write_bytes(_canonical_bytes(request))
    verify_eod_failure_evidence(
        directory,
        artifacts_root=tmp_path / "artifacts" / "eod",
    )

    with pytest.raises(EodEvidenceConflictError):
        verify_eod_terminal_failure(
            directory,
            artifacts_root=tmp_path / "artifacts" / "eod",
            data_root=tmp_path,
            uow_factory=_data_uow(tmp_path),
            job=job,
        )


def test_final_succeeded_commit_failure_is_recovered_without_republish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = CalendarGateway()
    jobs, command, worker = _runtime(tmp_path, gateway)
    job_id = _enqueue(command, key="final-commit-first")
    original_transition = jobs.transition
    armed = True

    def fail_final_transition(job_id_value, target, **kwargs):
        nonlocal armed
        if armed and target is JobStatus.SUCCEEDED:
            armed = False
            raise RuntimeError("synthetic final commit failure")
        return original_transition(job_id_value, target, **kwargs)

    monkeypatch.setattr(jobs, "transition", fail_final_transition)
    with pytest.raises(RuntimeError, match="final commit"):
        worker.run(job_id)

    first = jobs.get_job(job_id)
    assert first.status is JobStatus.RUNNING
    failure_directory = tmp_path / "artifacts" / "eod" / f"{job_id.replace(':', '_')}.incomplete"
    assert failure_directory.is_dir()
    original_evidence = {item.name: item.read_bytes() for item in failure_directory.iterdir()}
    original_artifacts = first.artifacts
    monkeypatch.setattr(jobs, "transition", original_transition)
    recovered_ids = jobs.recover_stale_running_job_ids(
        reason="expired after final commit failure",
        stale_before=UtcTimestamp(_NOW.value + timedelta(minutes=2)),
        at=UtcTimestamp(_NOW.value + timedelta(minutes=3)),
    )
    assert recovered_ids == (job_id,)
    handler = worker._registry.resolve("data.eod.v1")  # type: ignore[attr-defined]
    assert isinstance(handler, EodWorkerHandler)
    handler.recover_interrupted(jobs, job_id)
    handler.recover_interrupted(jobs, job_id)
    assert {
        item.name: item.read_bytes() for item in failure_directory.iterdir()
    } == original_evidence
    recovered_job = jobs.get_job(job_id)
    assert recovered_job.artifacts == original_artifacts

    retry_id = _enqueue(command, key="final-commit-retry")
    worker.run(retry_id)
    with sqlite3.connect(tmp_path / "metadata.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM ingestion_batches").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM dataset_versions").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM quality_runs").fetchone() == (1,)
    assert gateway.calls == 1


def test_formal_worker_run_recovers_existing_failure_then_runs_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gateway = CalendarGateway()
    runtime = build_eod_runtime(_settings(tmp_path), gateway=gateway)
    failed_id = runtime.eod_commands.enqueue(
        EodRunRequest(
            start_date=date(2026, 7, 15),
            end_date=date(2026, 7, 15),
            datasets=("exchange_calendar",),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        ),
        idempotency_key="formal-recovery-source",
        requester="pytest",
    ).job_id
    original_transition = runtime.jobs.transition

    def fail_final_transition(job_id_value, target, **kwargs):
        if target is JobStatus.SUCCEEDED:
            raise RuntimeError("synthetic final commit failure")
        return original_transition(job_id_value, target, **kwargs)

    monkeypatch.setattr(runtime.jobs, "transition", fail_final_transition)
    with pytest.raises(RuntimeError, match="final commit"):
        runtime.worker.run(failed_id)
    monkeypatch.setattr(runtime.jobs, "transition", original_transition)
    failed = runtime.jobs.get_job(failed_id)
    assert failed.heartbeat_at is not None

    target_id = runtime.eod_commands.enqueue(
        EodRunRequest(
            start_date=date(2026, 7, 15),
            end_date=date(2026, 7, 15),
            datasets=("exchange_calendar",),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        ),
        idempotency_key="formal-recovery-target",
        requester="pytest",
    ).job_id
    monkeypatch.setattr(runtime_module, "load_runtime_settings", lambda: runtime.settings)
    monkeypatch.setattr(runtime_module, "build_eod_runtime", lambda settings: runtime)
    monkeypatch.setattr(
        time_module,
        "utc_now",
        lambda: UtcTimestamp(failed.heartbeat_at.value + timedelta(minutes=3)),
    )

    assert eod_cli_main(["worker-run", "--job-id", target_id]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["job_id"] == target_id
    assert output["status"] == "SUCCEEDED"
    assert runtime.jobs.get_job(failed_id).status is JobStatus.INTERRUPTED
    assert runtime.jobs.get_job(target_id).status is JobStatus.SUCCEEDED
    database = runtime.settings.paths.data_root / "metadata" / "quantking.db"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ingestion_batches").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM dataset_versions").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM quality_runs").fetchone() == (1,)
    assert gateway.calls == 1


@pytest.mark.parametrize(
    "field",
    [
        "version_id",
        "batch_id",
        "quality_run_id",
        "content_sha256",
        "manifest_sha256",
        "parent_version_id",
    ],
)
def test_terminal_failure_rejects_forged_completed_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    gateway = CalendarGateway()
    jobs, command, worker = _runtime(tmp_path, gateway)
    job_id = _enqueue(command, key=f"completed-forgery-{field}")
    original_transition = jobs.transition

    def fail_final_transition(job_id_value, target, **kwargs):
        if target is JobStatus.SUCCEEDED:
            raise RuntimeError("synthetic final commit failure")
        return original_transition(job_id_value, target, **kwargs)

    monkeypatch.setattr(jobs, "transition", fail_final_transition)
    with pytest.raises(RuntimeError, match="final commit"):
        worker.run(job_id)
    monkeypatch.setattr(jobs, "transition", original_transition)
    jobs.recover_stale_running_jobs(
        reason="expired final commit",
        stale_before=UtcTimestamp(_NOW.value + timedelta(minutes=2)),
        at=UtcTimestamp(_NOW.value + timedelta(minutes=3)),
    )
    job = jobs.get_job(job_id)
    directory = tmp_path / "artifacts" / "eod" / f"{job_id.replace(':', '_')}.incomplete"
    failure = json.loads((directory / "failure.json").read_bytes())
    result = failure["completed_datasets"][0]
    replacements = {
        "version_id": "data_version:99999999-9999-4999-8999-999999999999",
        "batch_id": "data_batch:99999999-9999-4999-8999-999999999999",
        "quality_run_id": "data_quality:99999999-9999-4999-8999-999999999999",
        "content_sha256": "9" * 64,
        "manifest_sha256": "8" * 64,
        "parent_version_id": "data_version:88888888-8888-4888-8888-888888888888",
    }
    result[field] = replacements[field]
    (directory / "failure.json").write_bytes(_canonical_bytes(failure))

    with pytest.raises(EodEvidenceConflictError):
        verify_eod_terminal_failure(
            directory,
            artifacts_root=tmp_path / "artifacts" / "eod",
            data_root=tmp_path,
            uow_factory=_data_uow(tmp_path),
            job=job,
        )


@pytest.mark.parametrize("attack", ["missing", "extra", "tampered"])
def test_terminal_failure_rejects_checkpoint_inventory_attack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    gateway = CalendarGateway()
    jobs, command, worker = _runtime(tmp_path, gateway)
    job_id = _enqueue(command, key=f"checkpoint-attack-{attack}")
    original_transition = jobs.transition

    def fail_final_transition(job_id_value, target, **kwargs):
        if target is JobStatus.SUCCEEDED:
            raise RuntimeError("synthetic final commit failure")
        return original_transition(job_id_value, target, **kwargs)

    monkeypatch.setattr(jobs, "transition", fail_final_transition)
    with pytest.raises(RuntimeError, match="final commit"):
        worker.run(job_id)
    monkeypatch.setattr(jobs, "transition", original_transition)
    jobs.recover_stale_running_jobs(
        reason="expired final commit",
        stale_before=UtcTimestamp(_NOW.value + timedelta(minutes=2)),
        at=UtcTimestamp(_NOW.value + timedelta(minutes=3)),
    )
    job = jobs.get_job(job_id)
    directory = tmp_path / "artifacts" / "eod" / f"{job_id.replace(':', '_')}.incomplete"
    checkpoint_root = tmp_path / "artifacts" / "eod" / ".checkpoints" / job_id.replace(":", "_")
    checkpoint = checkpoint_root / "exchange_calendar.json"
    if attack == "missing":
        checkpoint.unlink()
    elif attack == "extra":
        (checkpoint_root / "extra.json").write_bytes(checkpoint.read_bytes())
    else:
        payload = json.loads(checkpoint.read_bytes())
        payload["dataset_result"]["content_sha256"] = "7" * 64
        checkpoint.write_bytes(_canonical_bytes(payload))

    with pytest.raises(EodEvidenceConflictError):
        verify_eod_terminal_failure(
            directory,
            artifacts_root=tmp_path / "artifacts" / "eod",
            data_root=tmp_path,
            uow_factory=_data_uow(tmp_path),
            job=job,
        )


def test_recovered_provisional_success_without_failure_is_strictly_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = CalendarGateway()
    jobs, command, worker = _runtime(tmp_path, gateway)
    job_id = _enqueue(command, key="hard-exit-first")
    handler = worker._registry.resolve("data.eod.v1")  # type: ignore[attr-defined]
    wrapper_registry = WorkerRegistry()
    wrapper_registry.register("data.eod.v1", lambda context: handler(context))
    wrapper_worker = Worker(jobs, wrapper_registry, worker_id="hard-exit-worker")
    original_transition = jobs.transition

    def fail_final_transition(job_id_value, target, **kwargs):
        if target is JobStatus.SUCCEEDED:
            raise RuntimeError("synthetic process exit at final commit")
        return original_transition(job_id_value, target, **kwargs)

    monkeypatch.setattr(jobs, "transition", fail_final_transition)
    with pytest.raises(RuntimeError, match="process exit"):
        wrapper_worker.run(job_id)
    assert not (tmp_path / "artifacts" / "eod" / f"{job_id.replace(':', '_')}.incomplete").exists()
    monkeypatch.setattr(jobs, "transition", original_transition)
    recovered_job_ids = jobs.recover_stale_running_job_ids(
        reason="hard-exit lease expired",
        stale_before=UtcTimestamp(_NOW.value + timedelta(minutes=2)),
        at=UtcTimestamp(_NOW.value + timedelta(minutes=3)),
    )
    assert recovered_job_ids == (job_id,)
    assert isinstance(handler, EodWorkerHandler)
    handler.recover_interrupted(jobs, job_id)
    recovered_before_retry = jobs.get_job(job_id)
    assert f"artifacts/eod/{job_id.replace(':', '_')}.incomplete" in (
        recovered_before_retry.artifacts
    )

    retry_id = _enqueue(command, key="hard-exit-retry")
    worker.run(retry_id)
    recovered = jobs.get_job(job_id)
    assert recovered.status is JobStatus.INTERRUPTED
    assert f"artifacts/eod/{job_id.replace(':', '_')}.incomplete" in recovered.artifacts
    with sqlite3.connect(tmp_path / "metadata.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM ingestion_batches").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM dataset_versions").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM quality_runs").fetchone() == (1,)
    assert gateway.calls == 1


def test_cancelled_eod_records_cancelled_failure_evidence(tmp_path: Path) -> None:
    gateway = CalendarGateway()
    jobs, command, worker = _runtime(tmp_path, gateway)
    job_id = _enqueue(command, key="cancelled-evidence")
    original_fetch = gateway.fetch

    def fetch_then_cancel(chunk: object) -> SupplierResponse:
        response = original_fetch(chunk)
        jobs.request_cancel(job_id, reason="synthetic cancellation")
        return response

    gateway.fetch = fetch_then_cancel  # type: ignore[method-assign]

    with pytest.raises(Exception, match="cancellation"):
        worker.run(job_id)

    job = jobs.get_job(job_id)
    verified = verify_eod_failure_evidence(
        tmp_path / job.artifacts[0],
        artifacts_root=tmp_path / "artifacts" / "eod",
    )
    assert job.status is JobStatus.CANCELLED
    assert verified.failure["terminal_status"] == "CANCELLED"
    assert verified.failure["error_code"] == "CANCELLED"
    verify_eod_terminal_failure(
        tmp_path / job.artifacts[0],
        artifacts_root=tmp_path / "artifacts" / "eod",
        data_root=tmp_path,
        uow_factory=_data_uow(tmp_path),
        job=job,
    )
