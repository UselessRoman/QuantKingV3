"""Red integration tests for DifferenceReport artifact directory (S1-04B).

The DifferenceReport writer must produce a managed, immutable, SHA-bound
directory under ``artifacts/data-diff/<report_id>/`` that mirrors the
Raw / Canonical / Quality artifact conventions. The SQLite repository
must verify that both-side batches, sources, datasets and quality run
IDs exist and match before persisting a DifferenceReport.

Tests use only ``tmp_path`` and synthetic data.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import utc_now
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.models import (
    DatasetKind,
    DatasetVersion,
    DataSource,
    IngestionBatch,
    IngestionBatchStatus,
    ManifestFileEntry,
    QualityResult,
    QualityRun,
    QualityRunStatus,
)
from quantking.jobs.adapters.persistence.sqlite import initialize_database

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _daily_bar_table(rows: list[dict[str, object]] | None = None) -> pa.Table:
    rows = (
        rows
        if rows is not None
        else [
            {
                "code": "000001.SZ",
                "trade_date": date(2026, 7, 15),
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "pre_close": 10.0,
                "volume": 1_000_000,
                "amount": 10_500_000.0,
            }
        ]
    )
    table = pa.Table.from_pylist(rows)
    descriptor = canonical_schema("daily_bar_unadjusted")
    columns: list[pa.Array] = []
    fields: list[pa.Field] = []
    for field in descriptor:
        columns.append(table.column(field.name).cast(field.type, safe=False))
        fields.append(pa.field(field.name, field.type, nullable=field.nullable))
    return pa.Table.from_arrays(columns, schema=pa.schema(fields))


@pytest.fixture()
def uow_factory(tmp_path: Path) -> DataSqliteUnitOfWorkFactory:
    database = tmp_path / "data.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    yield factory
    engine.dispose()


def _succeeded_batch(*, source: DataSource, batch_id=None) -> IngestionBatch:
    return IngestionBatch(
        batch_id=batch_id or new_entity_id("data_batch"),
        source=source,
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        status=IngestionBatchStatus.SUCCEEDED,
        adapter_version="adapter.v1",
        request_params={"code": "000001.SZ"},
        request_params_json='{"code":"000001.SZ"}',
        started_at=utc_now(),
        ended_at=utc_now(),
        row_count=1,
        security_count=1,
        manifest_files=(
            ManifestFileEntry(
                relative_path="data.parquet",
                size=100,
                sha256="a" * 64,
            ),
        ),
        failure=None,
        job_run_id=None,
        empty_result=None,
    )


def _building_version(parent_id=None) -> DatasetVersion:
    return DatasetVersion.building(
        version_id=new_entity_id("data_version"),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-04",
        parent_version_id=parent_id,
        input_batch_ids=(),
        created_at=utc_now(),
    )


# ---------------------------------------------------------------------------
# Artifact directory
# ---------------------------------------------------------------------------


class TestDifferenceReportArtifactDirectory:
    def test_writer_creates_managed_directory_with_manifest_and_complete(
        self, tmp_path: Path
    ) -> None:
        from quantking.data.comparison.artifacts import DifferenceReportArtifactWriter
        from quantking.data.comparison.comparator import (
            CrossSourceComparator,
            CrossSourceComparisonRequest,
        )
        from quantking.data.comparison.thresholds import default_thresholds

        primary = _daily_bar_table()
        fallback = _daily_bar_table()
        request = CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            primary_source="XTQUANT",
            fallback_source="BAOSTOCK",
            primary_version_id=new_entity_id("data_version"),
            fallback_version_id=new_entity_id("data_version"),
            primary_batch_id=new_entity_id("data_batch"),
            fallback_batch_id=new_entity_id("data_batch"),
            primary_content_sha256="a" * 64,
            fallback_content_sha256="b" * 64,
            primary_quality_run_id=new_entity_id("data_quality"),
            fallback_quality_run_id=new_entity_id("data_quality"),
            primary_table=primary,
            fallback_table=fallback,
            thresholds=default_thresholds(),
            sample_spec=(),
            created_at=utc_now(),
        )
        outcome = CrossSourceComparator().compare(request=request)
        writer = DifferenceReportArtifactWriter(artifacts_root=tmp_path)
        writer.write(outcome=outcome)
        directory = tmp_path / "data-diff" / str(outcome.report_id).replace(":", "--")
        assert (directory / "manifest.json").is_file()
        assert (directory / "COMPLETE").is_file()
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        assert "manifest_sha256" in manifest
        complete = json.loads((directory / "COMPLETE").read_text(encoding="utf-8"))
        assert complete["manifest_sha256"] == manifest["manifest_sha256"]

    def test_idempotent_rewrite_returns_same_directory(self, tmp_path: Path) -> None:
        from quantking.data.comparison.artifacts import DifferenceReportArtifactWriter
        from quantking.data.comparison.comparator import (
            CrossSourceComparator,
            CrossSourceComparisonRequest,
        )
        from quantking.data.comparison.thresholds import default_thresholds

        primary = _daily_bar_table()
        fallback = _daily_bar_table()
        request = CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            primary_source="XTQUANT",
            fallback_source="BAOSTOCK",
            primary_version_id=new_entity_id("data_version"),
            fallback_version_id=new_entity_id("data_version"),
            primary_batch_id=new_entity_id("data_batch"),
            fallback_batch_id=new_entity_id("data_batch"),
            primary_content_sha256="a" * 64,
            fallback_content_sha256="b" * 64,
            primary_quality_run_id=new_entity_id("data_quality"),
            fallback_quality_run_id=new_entity_id("data_quality"),
            primary_table=primary,
            fallback_table=fallback,
            thresholds=default_thresholds(),
            sample_spec=(),
            created_at=utc_now(),
        )
        outcome = CrossSourceComparator().compare(request=request)
        writer = DifferenceReportArtifactWriter(artifacts_root=tmp_path)
        writer.write(outcome=outcome)
        directory = tmp_path / "data-diff" / str(outcome.report_id).replace(":", "--")
        manifest_bytes = (directory / "manifest.json").read_bytes()
        # Idempotent rewrite must succeed and produce identical bytes.
        writer.write(outcome=outcome)
        assert (directory / "manifest.json").read_bytes() == manifest_bytes

    def test_writer_rejects_overwrite_with_different_input(self, tmp_path: Path) -> None:
        from dataclasses import replace

        from quantking.data.comparison.artifacts import (
            DifferenceReportArtifactWriter,
            DifferenceReportConflictError,
        )
        from quantking.data.comparison.comparator import (
            CrossSourceComparator,
            CrossSourceComparisonRequest,
        )
        from quantking.data.comparison.thresholds import default_thresholds

        primary = _daily_bar_table()
        fallback = _daily_bar_table()
        request = CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            primary_source="XTQUANT",
            fallback_source="BAOSTOCK",
            primary_version_id=new_entity_id("data_version"),
            fallback_version_id=new_entity_id("data_version"),
            primary_batch_id=new_entity_id("data_batch"),
            fallback_batch_id=new_entity_id("data_batch"),
            primary_content_sha256="a" * 64,
            fallback_content_sha256="b" * 64,
            primary_quality_run_id=new_entity_id("data_quality"),
            fallback_quality_run_id=new_entity_id("data_quality"),
            primary_table=primary,
            fallback_table=fallback,
            thresholds=default_thresholds(),
            sample_spec=(),
            created_at=utc_now(),
        )
        outcome = CrossSourceComparator().compare(request=request)
        writer = DifferenceReportArtifactWriter(artifacts_root=tmp_path)
        writer.write(outcome=outcome)
        # Same report_id but different outcome content must conflict.
        conflict = replace(outcome, total_differences=outcome.total_differences + 1)
        with pytest.raises(DifferenceReportConflictError):
            writer.write(outcome=conflict)

    def test_writer_rejects_non_absolute_root(self) -> None:
        from quantking.data.comparison.artifacts import DifferenceReportArtifactWriter

        with pytest.raises(ValueError):
            DifferenceReportArtifactWriter(artifacts_root=Path("relative"))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Repository binding
# ---------------------------------------------------------------------------


class TestDifferenceReportRepositoryBinding:
    def test_repository_rejects_report_with_mismatched_batch(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        from quantking.common.errors import InvalidDataTransitionError
        from quantking.data.models import DifferenceReport

        # Build primary and fallback batches, versions and quality runs so
        # the DifferenceReport domain model can be constructed with valid
        # bindings. The primary_batch_id in the report will deliberately
        # reference a batch that does NOT exist in the database.
        primary_batch = _succeeded_batch(source=DataSource.XTQUANT)
        fallback_batch = _succeeded_batch(source=DataSource.BAOSTOCK)
        primary_version = _building_version()
        fallback_version = _building_version()
        primary_quality_run = QualityRun(
            run_id=new_entity_id("data_quality"),
            version_id=primary_version.version_id,
            status=QualityRunStatus.PASSED,
            rule_version="quality-rules.v1",
            schema_version="daily_bar_unadjusted.v1",
            content_sha256="a" * 64,
            started_at=utc_now(),
            ended_at=utc_now(),
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
            started_at=utc_now(),
            ended_at=utc_now(),
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
        with uow_factory() as uow:
            uow.data.save_batch(primary_batch)
            uow.data.save_batch(fallback_batch)
            uow.data.save_version(primary_version)
            uow.data.save_version(fallback_version)
            uow.data.save_quality_run(primary_quality_run)
            uow.data.save_quality_run(fallback_quality_run)
            uow.commit()
        # Build a DifferenceReport whose primary_batch_id does NOT exist
        # in the database. The repository must reject it.
        report = DifferenceReport(
            report_id=new_entity_id("data_diff"),
            version_id=primary_version.version_id,
            primary_source=DataSource.XTQUANT,
            fallback_source=DataSource.BAOSTOCK,
            created_at=utc_now(),
            field_differences={},
            total_compared=1,
            total_differences=0,
            report_relative_path="data-diff/test/manifest.json",
            primary_batch_id=new_entity_id("data_batch"),  # does not exist
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
        with uow_factory() as uow:
            with pytest.raises(InvalidDataTransitionError):
                uow.data.save_difference_report(report)
            uow.rollback()


__all__ = [
    "TestDifferenceReportArtifactDirectory",
    "TestDifferenceReportRepositoryBinding",
]
