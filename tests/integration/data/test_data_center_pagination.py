"""Database keyset paging used by the S1-06 data-center service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.assessment.models import (
    AssessmentStatus,
    EvidenceKind,
    SourceCandidateAssessment,
)
from quantking.data.center import DataCenterQueryService
from quantking.data.models import (
    BatchFailureInfo,
    BatchFailureKind,
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    DataSource,
    IngestionBatch,
    IngestionBatchStatus,
    QualityRun,
    QualityRunStatus,
    transition_batch,
    transition_dataset_version,
)
from quantking.data.validation.testing import verified_synthetic_assessment
from quantking.jobs.adapters.persistence.sqlite import (
    SqliteUnitOfWorkFactory,
    initialize_database,
)
from quantking.jobs.models import FailureInfo, JobStatus
from quantking.jobs.services import CreateJob, JobService


class _UnusedPort:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"unexpected port call: {name}")


class _NoJobs:
    def get_job(self, job_id: str) -> object:
        raise AssertionError(f"unexpected job lookup: {job_id}")

    def list_jobs_page(self, **filters: object) -> list[object]:
        del filters
        return []

    def count_jobs(self, **filters: object) -> int:
        del filters
        return 0


def _batch(index: int, *, at: datetime) -> IngestionBatch:
    return IngestionBatch.running(
        batch_id=new_entity_id("data_batch"),
        source=DataSource.XTQUANT,
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        adapter_version="s1-06-test",
        request_params={"index": str(index)},
        started_at=UtcTimestamp(at),
    )


def test_database_keyset_page_is_stable_across_concurrent_insert(tmp_path: Path) -> None:
    database = tmp_path / "data-center.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    base = datetime(2026, 7, 28, tzinfo=UTC)
    originals = [_batch(index, at=base + timedelta(seconds=index)) for index in range(5)]
    with factory() as uow:
        for item in originals:
            uow.data.save_batch(item)
        uow.commit()

    with factory() as uow:
        first = uow.data.list_batches_page(
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            source=DataSource.XTQUANT,
            status=IngestionBatchStatus.RUNNING,
            started_from=None,
            started_to=None,
            before=None,
            limit=2,
        )
    assert len(first) == 2
    boundary = (first[-1].started_at.to_iso(), str(first[-1].batch_id))

    with factory() as uow:
        uow.data.save_batch(_batch(99, at=base + timedelta(minutes=1)))
        uow.commit()

    seen = {str(item.batch_id) for item in first}
    before = boundary
    for _ in range(3):
        with factory() as uow:
            page = uow.data.list_batches_page(
                dataset=DatasetKind.EXCHANGE_CALENDAR,
                source=DataSource.XTQUANT,
                status=IngestionBatchStatus.RUNNING,
                started_from=None,
                started_to=None,
                before=before,
                limit=2,
            )
        assert not (seen & {str(item.batch_id) for item in page})
        seen.update(str(item.batch_id) for item in page)
        if not page:
            break
        before = (page[-1].started_at.to_iso(), str(page[-1].batch_id))
    else:
        raise AssertionError("keyset pagination did not terminate")

    assert seen == {str(item.batch_id) for item in originals}
    engine.dispose()


def test_latest_source_assessment_is_selected_in_sql_by_time_and_stable_id(
    tmp_path: Path,
) -> None:
    database = tmp_path / "source-assessment.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    base = datetime(2026, 7, 28, tzinfo=UTC)
    assessments = [
        SourceCandidateAssessment(
            assessment_id=new_entity_id("data_assessment"),
            source="BAOSTOCK",
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            evidence_kind=EvidenceKind.SYNTHETIC,
            semantic_contract_verified=True,
            schema_unit_verified=True,
            coverage_adequate=True,
            quality_passed=True,
            fixed_sample_differences_adequate=True,
            stability_runs=index,
            status=AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION,
            blocking_reason="external validation required",
            assessment_config_version="assessment.v1",
            sample_spec_version="s1-07.fixed-sample.v1",
            rule_version="rules.v1",
            evidence_relative_path=f"assessments/{index}.json",
            evidence_sha256=f"{index + 1:x}".rjust(64, "0"),
            created_at=UtcTimestamp(base + timedelta(seconds=index)),
        )
        for index in range(2)
    ]
    verified = tuple(verified_synthetic_assessment(item) for item in assessments)
    with factory() as uow:
        for evidence in verified:
            uow.data.save_source_candidate_assessment(evidence)
        uow.commit()

    with factory() as uow:
        latest = uow.data.find_latest_source_candidate_assessment(
            source="BAOSTOCK",
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        )

    assert latest == verified[-1].assessment
    engine.dispose()


@pytest.mark.parametrize("failure_count", [0, 101, 150])
def test_summary_failure_count_is_exact_at_page_boundaries(
    tmp_path: Path,
    failure_count: int,
) -> None:
    database = tmp_path / f"failure-summary-{failure_count}.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    base = datetime(2026, 7, 28, tzinfo=UTC)
    failures = [
        transition_batch(
            _batch(index, at=base + timedelta(seconds=index * 2)),
            IngestionBatchStatus.FAILED,
            at=UtcTimestamp(base + timedelta(seconds=index * 2 + 1)),
            failure=BatchFailureInfo(
                error_type=BatchFailureKind.INTERNAL,
                message="sanitized failure",
                retryable=False,
            ),
        )
        for index in range(failure_count)
    ]
    with factory() as uow:
        for failure in failures:
            uow.data.save_batch(failure)
        uow.commit()
    service = DataCenterQueryService(
        data_uow_factory=factory,
        preview=_UnusedPort(),
        difference_verifier=_UnusedPort(),
        jobs=_NoJobs(),
    )

    assert service.summary()["failures"] == failure_count
    seen: set[str] = set()
    cursor: str | None = None
    for _ in range(3):
        page = service.list_failures(cursor=cursor, limit=100)
        seen.update(str(item["entity_id"]) for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    else:
        raise AssertionError("failure pagination did not terminate")

    assert len(seen) == failure_count
    engine.dispose()


def test_summary_and_failure_pages_share_the_same_mixed_entity_scope(tmp_path: Path) -> None:
    database = tmp_path / "mixed-failure-summary.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    data_factory = DataSqliteUnitOfWorkFactory(engine)
    job_service = JobService(SqliteUnitOfWorkFactory(engine))
    base = datetime(2026, 7, 28, tzinfo=UTC)
    failure_info = BatchFailureInfo(
        error_type=BatchFailureKind.INTERNAL,
        message="sanitized failure",
        retryable=False,
    )
    batch = transition_batch(
        _batch(1, at=base),
        IngestionBatchStatus.FAILED,
        at=UtcTimestamp(base + timedelta(seconds=1)),
        failure=failure_info,
    )
    building = DatasetVersion.building(
        version_id=new_entity_id("data_version"),
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        schema_version="exchange-calendar.v1",
        code_version="s1-06-r2-test",
        parent_version_id=None,
        input_batch_ids=(batch.batch_id,),
        created_at=UtcTimestamp(base + timedelta(seconds=2)),
    )
    version = transition_dataset_version(
        building,
        DatasetVersionStatus.FAILED,
        at=UtcTimestamp(base + timedelta(seconds=3)),
        failure=failure_info,
    )
    quality = QualityRun(
        run_id=new_entity_id("data_quality"),
        version_id=version.version_id,
        status=QualityRunStatus.FAILED,
        rule_version=version.rule_version,
        schema_version=version.schema_version,
        content_sha256="a" * 64,
        started_at=UtcTimestamp(base + timedelta(seconds=4)),
        ended_at=UtcTimestamp(base + timedelta(seconds=5)),
        results=(),
        summary_report_relative_path=None,
        failure=failure_info,
    )
    with data_factory() as uow:
        uow.data.save_batch(batch)
        uow.data.save_version(version)
        uow.data.save_quality_run(quality)
        uow.commit()
    job = job_service.create_job(
        CreateJob(
            job_type="data.eod.v1",
            idempotency_key="s1-06-r2-mixed-failure",
            input_manifest={"dataset": DatasetKind.EXCHANGE_CALENDAR.value},
            config_manifest={"mode": "PUBLISH_AFTER_QUALITY"},
            version_manifest={"exchange_calendar": "root"},
            requester="pytest",
            log_id="s1-06-r2",
        )
    )
    job_service.transition(
        job.job_id,
        JobStatus.RUNNING,
        worker_id="pytest-worker",
    )
    job_service.transition(
        job.job_id,
        JobStatus.FAILED,
        failure=FailureInfo(error_type="SyntheticError", message="sanitized", retryable=False),
    )
    service = DataCenterQueryService(
        data_uow_factory=data_factory,
        preview=_UnusedPort(),
        difference_verifier=_UnusedPort(),
        jobs=job_service,
    )

    page = service.list_failures(limit=100)

    assert service.summary()["failures"] == 4
    assert len(page.items) == 4
    assert {item["kind"] for item in page.items} == {"BATCH", "VERSION", "QUALITY", "EOD"}
    assert page.next_cursor is None
    engine.dispose()
