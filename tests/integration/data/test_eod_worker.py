from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import UtcTimestamp
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
from quantking.data.adapters.warehouse.raw_store import RawBatchPublisher
from quantking.data.eod.artifacts import EodEvidenceWriter, verify_eod_success_evidence
from quantking.data.eod.contracts import EodDatasetConfig, EodPublishIntent, EodRunRequest
from quantking.data.eod.service import EodCommandService, EodExecutionConfig
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

_NOW = UtcTimestamp(datetime(2026, 7, 15, 18, 0, tzinfo=UTC))


class CalendarGateway:
    def __init__(self, *, complete: bool = True) -> None:
        self.calls = 0
        self.complete = complete

    def fetch(self, chunk: object) -> SupplierResponse:
        self.calls += 1
        records = tuple(
            {
                "market": exchange,
                "calendar_date": "2026-07-15",
                "open_flag": True,
            }
            for exchange in ("BJ", "SH", "SZ")
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
    )
    registry = WorkerRegistry()
    register_eod_handler(registry, handler)
    command = EodCommandService(
        jobs,
        config=execution_config or EodExecutionConfig.production(code_version="test-code"),
    )
    return jobs, command, Worker(jobs, registry, worker_id="worker-1")


def _enqueue(
    command: EodCommandService,
    *,
    datasets=("exchange_calendar",),
    key: str = "eod-worker-test",
) -> str:
    return command.enqueue(
        EodRunRequest(
            start_date=_NOW.value.date(),
            end_date=_NOW.value.date(),
            datasets=datasets,
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
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
    result = evidence.result["datasets"][0]  # type: ignore[index]
    assert result["status"] == "PUBLISHED"
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
