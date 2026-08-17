"""Stage 1 metadata repository, lineage, audit, and retry integration tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect

from quantking.common.errors import InvalidDataTransitionError
from quantking.common.ids import EntityId, new_entity_id
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.comparison.verifier import VerifiedDifferenceReport
from quantking.data.models import (
    BatchFailureInfo,
    BatchFailureKind,
    DataProvider,
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    DataUpstream,
    DifferenceReport,
    IngestionBatch,
    IngestionBatchStatus,
    ManifestFileEntry,
    QualityResult,
    QualityRun,
    QualityRunStatus,
    transition_batch,
    transition_dataset_version,
)
from quantking.jobs.adapters.persistence.sqlite import initialize_database

_NOW = UtcTimestamp(datetime(2026, 7, 16, 1, 0, tzinfo=UTC))
_DECIDED = UtcTimestamp(datetime(2026, 7, 16, 2, 0, tzinfo=UTC))
_LATER = UtcTimestamp(datetime(2026, 7, 16, 3, 0, tzinfo=UTC))
_TRADE_DATE = TradingDate(date(2026, 7, 16))
_SHA = "a" * 64


@pytest.fixture()
def factory(tmp_path: Path) -> DataSqliteUnitOfWorkFactory:
    database = tmp_path / "data.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    yield DataSqliteUnitOfWorkFactory(engine)
    engine.dispose()


def _running_batch(
    *,
    source: DataUpstream = DataUpstream.SINA,
    dataset: DatasetKind = DatasetKind.DAILY_BAR_UNADJUSTED,
    batch_id: EntityId | None = None,
) -> IngestionBatch:
    return IngestionBatch.running(
        batch_id=batch_id or new_entity_id("data_batch"),
        provider=DataProvider.AKSHARE,
        upstream=source,
        endpoint={
            DataUpstream.TENCENT: "stock_zh_a_hist_tx",
            DataUpstream.SINA: "stock_zh_a_daily",
        }[source],
        library_version="test",
        dataset=dataset,
        adapter_version=f"{source.value.lower()}-1",
        request_params={
            "provider": "AKSHARE",
            "upstream": source.value,
            "endpoint": {
                DataUpstream.TENCENT: "stock_zh_a_hist_tx",
                DataUpstream.SINA: "stock_zh_a_daily",
            }[source],
            "akshare_version": "test",
            "code": "000001.SZ",
            "start": "2026-01-01",
        },
        started_at=_NOW,
    )


def _succeeded_batch(**kwargs: object) -> IngestionBatch:
    running = _running_batch(**kwargs)  # type: ignore[arg-type]
    return transition_batch(
        running,
        IngestionBatchStatus.SUCCEEDED,
        at=_DECIDED,
        row_count=100,
        security_count=1,
        manifest_files=(
            ManifestFileEntry(
                relative_path=f"raw/{running.upstream.value.lower()}/part.parquet",
                size=10,
                sha256=_SHA,
            ),
        ),
    )


def _building_version(
    batch_ids: tuple[EntityId, ...],
    *,
    dataset: DatasetKind = DatasetKind.DAILY_BAR_UNADJUSTED,
    parent_version_id: EntityId | None = None,
    created_at: UtcTimestamp = _NOW,
) -> DatasetVersion:
    return DatasetVersion.building(
        version_id=new_entity_id("data_version"),
        dataset=dataset,
        schema_version=f"{dataset.value}.v1",
        code_version="abc123",
        parent_version_id=parent_version_id,
        input_batch_ids=batch_ids,
        created_at=created_at,
    )


def _validated(version: DatasetVersion) -> DatasetVersion:
    return transition_dataset_version(
        version,
        DatasetVersionStatus.VALIDATED,
        at=_DECIDED,
        manifest_sha256=_SHA,
        security_count=1,
        row_count=100,
        coverage_start=_TRADE_DATE,
        coverage_end=_TRADE_DATE,
        coverage_summary={"date_count": "1", "security_count": "1"},
    )


def _validated_for_run(version: DatasetVersion, run: QualityRun) -> DatasetVersion:
    return transition_dataset_version(
        version,
        DatasetVersionStatus.VALIDATED,
        at=_DECIDED,
        manifest_sha256=_SHA,
        security_count=1,
        row_count=100,
        coverage_start=_TRADE_DATE,
        coverage_end=_TRADE_DATE,
        coverage_summary={"date_count": "1", "security_count": "1"},
        quality_run_id=run.run_id,
        content_sha256=run.content_sha256,
    )


def _published(version: DatasetVersion, run: QualityRun) -> DatasetVersion:
    return transition_dataset_version(
        _validated_for_run(version, run), DatasetVersionStatus.PUBLISHED, at=_LATER
    )


def _verified(report: DifferenceReport) -> VerifiedDifferenceReport:
    return VerifiedDifferenceReport(
        report_id=report.report_id,
        created_at=report.created_at,
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
        primary_source=report.primary_source.value,
        fallback_source=report.fallback_source.value,
        report_relative_path=report.report_relative_path,
        manifest_sha256="e" * 64,
        schema_version="daily_bar_unadjusted.v1",
        primary_version_id=report.primary_version_id,
        fallback_version_id=report.fallback_version_id,
        primary_batch_id=report.primary_batch_id,
        fallback_batch_id=report.fallback_batch_id,
        primary_content_sha256=report.primary_content_sha256,
        fallback_content_sha256=report.fallback_content_sha256,
        primary_quality_run_id=report.primary_quality_run_id,
        fallback_quality_run_id=report.fallback_quality_run_id,
        sample_spec_sha256=report.sample_spec_sha256,
        threshold_rule_version=report.threshold_rule_version,
        report_sha256=report.report_sha256,
        total_compared=report.total_compared,
        total_differences=report.total_differences,
        field_differences=dict(report.field_differences),
        row_differences=[],
        units=(),
        fallback_only_rows=frozenset(),
    )


class TestBatchRepository:
    def test_create_and_get_running_batch(self, factory: DataSqliteUnitOfWorkFactory) -> None:
        batch = _running_batch()
        with factory() as uow:
            uow.data.save_batch(batch)
            uow.commit()
        with factory() as uow:
            loaded = uow.data.get_batch(str(batch.batch_id))
        assert loaded == batch

    def test_terminal_batch_allows_identical_noop_but_rejects_content_change(
        self, factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        batch = _succeeded_batch()
        with factory() as uow:
            uow.data.save_batch(batch)
            uow.commit()
        with factory() as uow:
            assert uow.data.save_batch(batch) == batch
            uow.commit()
        changed = replace(batch, row_count=101)
        with factory() as uow, pytest.raises(InvalidDataTransitionError, match="content"):
            uow.data.save_batch(changed)

    def test_failed_request_can_retry_with_new_batch_id_and_succeed(
        self, factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        first = _running_batch()
        failed = transition_batch(
            first,
            IngestionBatchStatus.FAILED,
            at=_DECIDED,
            failure=BatchFailureInfo(
                error_type=BatchFailureKind.NETWORK,
                message="supplier unavailable",
                retryable=True,
            ),
        )
        retry = _succeeded_batch()
        assert retry.request_params_json == first.request_params_json
        assert retry.batch_id != first.batch_id
        with factory() as uow:
            uow.data.save_batch(failed)
            uow.data.save_batch(retry)
            uow.commit()
        with factory() as uow:
            assert uow.data.get_batch(str(first.batch_id)).status is IngestionBatchStatus.FAILED
            assert uow.data.get_batch(str(retry.batch_id)).status is IngestionBatchStatus.SUCCEEDED


class TestDatasetVersionLineageAndCoverage:
    def test_parent_and_input_batches_are_fk_backed_and_coverage_round_trips(
        self, factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        first_batch = _succeeded_batch()
        second_batch = _succeeded_batch()
        parent = _building_version((first_batch.batch_id,))
        child = _validated(
            _building_version(
                (first_batch.batch_id, second_batch.batch_id),
                parent_version_id=parent.version_id,
            )
        )
        with factory() as uow:
            uow.data.save_batch(first_batch)
            uow.data.save_batch(second_batch)
            uow.data.save_version(parent)
            uow.data.save_version(child)
            uow.commit()
        with factory() as uow:
            loaded = uow.data.get_version(str(child.version_id))
            rows = (
                uow.data._session.connection()
                .exec_driver_sql(
                    "SELECT batch_id, ordinal FROM dataset_version_batches "
                    "WHERE version_id=? ORDER BY ordinal",
                    (str(child.version_id),),
                )
                .fetchall()
            )
        assert loaded.input_batch_ids == (first_batch.batch_id, second_batch.batch_id)
        assert loaded.parent_version_id == parent.version_id
        assert loaded.coverage_start == _TRADE_DATE
        assert loaded.coverage_summary["date_count"] == "1"
        assert [row[0] for row in rows] == [str(first_batch.batch_id), str(second_batch.batch_id)]

    def test_missing_or_cross_dataset_lineage_is_rejected(
        self, factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        calendar_batch = _succeeded_batch(dataset=DatasetKind.EXCHANGE_CALENDAR)
        wrong = _building_version((calendar_batch.batch_id,))
        with factory() as uow:
            uow.data.save_batch(calendar_batch)
            with pytest.raises(InvalidDataTransitionError, match="input batch"):
                uow.data.save_version(wrong)


class TestQualityAndDifferencePersistence:
    def test_quality_run_results_save_read_and_query(
        self, factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        batch = _succeeded_batch()
        version = _building_version((batch.batch_id,))
        run = QualityRun(
            run_id=new_entity_id("data_quality"),
            version_id=version.version_id,
            status=QualityRunStatus.PASSED,
            rule_version="quality-rules.v1",
            schema_version="daily_bar_unadjusted.v1",
            content_sha256="a" * 64,
            started_at=_NOW,
            ended_at=_DECIDED,
            results=(
                QualityResult(
                    rule_code="schema_exact",
                    passed=True,
                    affected_count=0,
                    summary="exact schema",
                    report_relative_path=None,
                ),
                QualityResult(
                    rule_code="pk_unique",
                    passed=True,
                    affected_count=0,
                    summary="unique",
                    report_relative_path="quality/pk.json",
                ),
            ),
            summary_report_relative_path="quality/summary.json",
            failure=None,
        )
        with factory() as uow:
            uow.data.save_batch(batch)
            uow.data.save_version(version)
            uow.data.save_quality_run(run)
            uow.commit()
        with factory() as uow:
            loaded = uow.data.get_quality_run(str(run.run_id))
            queried = uow.data.list_quality_runs(
                version_id=str(version.version_id), status=QualityRunStatus.PASSED
            )
        assert loaded == run
        assert queried == [run]

    def test_difference_report_save_read_and_query(
        self, factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        primary_batch = _succeeded_batch(source=DataUpstream.SINA)
        fallback_batch = _succeeded_batch(source=DataUpstream.TENCENT)
        primary_version = _building_version((primary_batch.batch_id,))
        fallback_version = _building_version((fallback_batch.batch_id,))
        primary_quality_run = QualityRun(
            run_id=new_entity_id("data_quality"),
            version_id=primary_version.version_id,
            status=QualityRunStatus.PASSED,
            rule_version="quality-rules.v1",
            schema_version="daily_bar_unadjusted.v1",
            content_sha256="a" * 64,
            started_at=_NOW,
            ended_at=_DECIDED,
            results=(
                QualityResult(
                    rule_code="schema_exact",
                    passed=True,
                    affected_count=0,
                    summary="exact schema",
                    report_relative_path=None,
                ),
            ),
            summary_report_relative_path=None,
            failure=None,
        )
        fallback_quality_run = QualityRun(
            run_id=new_entity_id("data_quality"),
            version_id=fallback_version.version_id,
            status=QualityRunStatus.PASSED,
            rule_version="quality-rules.v1",
            schema_version="daily_bar_unadjusted.v1",
            content_sha256="b" * 64,
            started_at=_NOW,
            ended_at=_DECIDED,
            results=(
                QualityResult(
                    rule_code="schema_exact",
                    passed=True,
                    affected_count=0,
                    summary="exact schema",
                    report_relative_path=None,
                ),
            ),
            summary_report_relative_path=None,
            failure=None,
        )
        report = DifferenceReport(
            report_id=new_entity_id("data_diff"),
            version_id=primary_version.version_id,
            primary_source=DataUpstream.SINA,
            fallback_source=DataUpstream.TENCENT,
            created_at=_DECIDED,
            field_differences={"amount": "2", "close": "1"},
            total_compared=100,
            total_differences=3,
            report_relative_path="reports/diff.json",
            primary_batch_id=primary_batch.batch_id,
            fallback_batch_id=fallback_batch.batch_id,
            primary_version_id=primary_version.version_id,
            fallback_version_id=fallback_version.version_id,
            primary_content_sha256="a" * 64,
            fallback_content_sha256="b" * 64,
            primary_quality_run_id=primary_quality_run.run_id,
            fallback_quality_run_id=fallback_quality_run.run_id,
            sample_spec_sha256="c" * 64,
            threshold_rule_version="comparison-thresholds.v1",
            report_sha256="d" * 64,
        )
        with factory() as uow:
            uow.data.save_batch(primary_batch)
            uow.data.save_batch(fallback_batch)
            uow.data.save_version(primary_version)
            uow.data.save_version(fallback_version)
            uow.data.save_quality_run(primary_quality_run)
            uow.data.save_quality_run(fallback_quality_run)
            uow.data.save_version(_validated_for_run(primary_version, primary_quality_run))
            uow.data.save_version(_published(primary_version, primary_quality_run))
            uow.data.save_version(_validated_for_run(fallback_version, fallback_quality_run))
            uow.data.save_version(_published(fallback_version, fallback_quality_run))
            uow.data.save_difference_report(_verified(report))
            uow.commit()
        with factory() as uow:
            assert uow.data.get_difference_report(str(report.report_id)) == report
            assert uow.data.list_difference_reports(
                version_id=str(primary_version.version_id), primary_source=DataUpstream.SINA
            ) == [report]


class TestNoParquetInSqlite:
    def test_no_blob_or_parquet_columns(self, tmp_path: Path) -> None:
        database = tmp_path / "cols.db"
        initialize_database(database)
        engine = create_data_engine(database, busy_timeout_ms=250)
        inspector = inspect(engine)
        for table in {
            "ingestion_batches",
            "dataset_versions",
            "dataset_version_batches",
            "quality_runs",
            "quality_results",
            "difference_reports",
            "source_overrides",
        }:
            for column in inspector.get_columns(table):
                assert "BLOB" not in str(column["type"]).upper()
        engine.dispose()


class TestOrmDomainSeparation:
    def test_domain_models_do_not_import_sqlalchemy(self) -> None:
        import quantking.data.models as data_models

        source = Path(data_models.__file__).read_text(encoding="utf-8")
        assert "sqlalchemy" not in source
        assert "from alembic" not in source

    def test_orm_rows_are_not_reexported_as_domain(self) -> None:
        import quantking.data as data_pkg

        for name in ("IngestionBatchRow", "DatasetVersionRow", "Base"):
            assert not hasattr(data_pkg, name)
