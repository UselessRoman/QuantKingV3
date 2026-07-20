"""Red tests for the quality-gated Canonical publish coordinator (S1-04A).

These tests verify that no ``DatasetVersion`` may transition to
``PUBLISHED`` without a matching PASSED ``QualityRun`` bound by
``version_id``, ``rule_version`` and content SHA-256. They also verify
that direct repository calls or direct SQL cannot bypass the gate.

All tests use only ``tmp_path`` and synthetic data.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate, utc_now
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.adapters.warehouse.canonical_warehouse import (
    CanonicalWarehouse,
    CanonicalWarehouseError,
)
from quantking.data.models import (
    BatchFailureInfo,
    BatchFailureKind,
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    QualityRunStatus,
)
from quantking.jobs.adapters.persistence.sqlite import initialize_database


def _daily_bar_table() -> pa.Table:
    table = pa.Table.from_pylist(
        [
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
    descriptor = canonical_schema("daily_bar_unadjusted")
    columns: list[pa.Array] = []
    fields: list[pa.Field] = []
    for field in descriptor:
        columns.append(table.column(field.name).cast(field.type, safe=False))
        fields.append(pa.field(field.name, field.type, nullable=field.nullable))
    return pa.Table.from_arrays(columns, schema=pa.schema(fields))


def _calendar_table() -> pa.Table:
    table = pa.Table.from_pylist(
        [{"exchange": "SZ", "trade_date": date(2026, 7, 15), "is_open": True}]
    )
    descriptor = canonical_schema("exchange_calendar")
    columns: list[pa.Array] = []
    fields: list[pa.Field] = []
    for field in descriptor:
        columns.append(table.column(field.name).cast(field.type, safe=False))
        fields.append(pa.field(field.name, field.type, nullable=field.nullable))
    return pa.Table.from_arrays(columns, schema=pa.schema(fields))


def _building_version() -> DatasetVersion:
    return DatasetVersion.building(
        version_id=new_entity_id("data_version"),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-04",
        parent_version_id=None,
        input_batch_ids=(),
        created_at=utc_now(),
    )


@pytest.fixture()
def uow_factory(tmp_path: Path) -> DataSqliteUnitOfWorkFactory:
    database = tmp_path / "data.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    yield factory
    engine.dispose()


def _compute_content_sha(table: pa.Table) -> str:
    """Compute a deterministic SHA-256 of the publishable data."""
    import io

    from pyarrow import parquet as pa_parquet

    buffer = io.BytesIO()
    pa_parquet.write_table(table, buffer, compression="snappy")  # type: ignore[no-untyped-call]
    import hashlib

    return hashlib.sha256(buffer.getvalue()).hexdigest()


def _make_passed_quality_run(
    version: DatasetVersion,
    table: pa.Table,
    *,
    uow_factory: DataSqliteUnitOfWorkFactory,
    artifacts_root: Path,
):
    """Build, persist and artifact-write a PASSED QualityRun for the version."""
    from quantking.data.quality.artifacts import QualityArtifactWriter
    from quantking.data.quality.engine import QualityEngine
    from quantking.data.quality.models import QualityRequest, ReferenceDataBinding

    request = QualityRequest(
        dataset=version.dataset,
        version_id=version.version_id,
        rule_version=version.rule_version,
        schema_version=version.schema_version,
        code_version=version.code_version,
        content_sha256=_compute_content_sha(table),
        input_batch_ids=version.input_batch_ids,
        expected_securities=("000001.SZ",),
        expected_exchanges=("SZ",),
        coverage_start=TradingDate(date(2026, 7, 15)),
        coverage_end=TradingDate(date(2026, 7, 15)),
        reference_data={
            "exchange_calendar": ReferenceDataBinding(
                dataset=DatasetKind.EXCHANGE_CALENDAR,
                version_id=new_entity_id("data_version"),
                content_sha256="b" * 64,
            )
        },
        empty_result=None,
    )
    engine = QualityEngine(
        rule_set=__import__(
            "quantking.data.quality.rules", fromlist=["default_rule_set"]
        ).default_rule_set()
    )
    outcome = engine.execute(
        request=request,
        table=table,
        reference_tables={"exchange_calendar": _calendar_table()},
    )
    assert outcome.status is QualityRunStatus.PASSED
    writer = QualityArtifactWriter(artifacts_root=artifacts_root)
    writer.write(outcome=outcome, row_evidence_tables={})
    # Persist the QualityRun in SQLite through the repository. SKIPPED
    # rules are not applicable to this dataset and are filtered out of
    # the SQLite summary; the on-disk artifact manifest still records
    # them with their full RuleOutcomeStatus.
    from quantking.data.models import QualityResult, QualityRun
    from quantking.data.quality.models import RuleOutcomeStatus

    results = tuple(
        QualityResult(
            rule_code=result.rule_code,
            passed=result.status is RuleOutcomeStatus.PASSED,
            affected_count=result.affected_count,
            summary=result.summary,
            report_relative_path=result.row_evidence_relative_path,
        )
        for result in outcome.results
        if result.status is not RuleOutcomeStatus.SKIPPED
    )
    run = QualityRun(
        run_id=outcome.run_id,
        version_id=version.version_id,
        status=QualityRunStatus.PASSED,
        rule_version=version.rule_version,
        schema_version=version.schema_version,
        content_sha256=_compute_content_sha(table),
        started_at=outcome.started_at,
        ended_at=outcome.ended_at,  # type: ignore[arg-type]
        results=results,
        summary_report_relative_path=None,
        failure=None,
    )
    with uow_factory() as uow:
        uow.data.save_version(version)
        uow.data.save_quality_run(run)
        uow.commit()
    return run


# ---------------------------------------------------------------------------
# No quality run, no publish
# ---------------------------------------------------------------------------


class TestQualityGateBlocksPublishWithoutPassedRun:
    def test_coordinator_rejects_publish_when_no_quality_run_provided(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        from quantking.data.adapters.warehouse.publish_coordinator import (
            CanonicalPublishCoordinator,
            CanonicalPublishCoordinatorRequest,
        )

        warehouse = CanonicalWarehouse(data_root=tmp_path)
        coordinator = CanonicalPublishCoordinator(warehouse=warehouse, uow_factory=uow_factory)
        version = _building_version()
        with pytest.raises(TypeError):
            coordinator.coordinate(
                CanonicalPublishCoordinatorRequest(
                    version=version,
                    tables=(_daily_bar_table(),),
                    code_version="s1-04",
                    rule_version="quality-rules.v1",
                )
            )

    def test_coordinator_rejects_publish_when_quality_run_failed(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        from quantking.data.adapters.warehouse.publish_coordinator import (
            CanonicalPublishCoordinator,
            CanonicalPublishCoordinatorRequest,
        )
        from quantking.data.models import QualityResult, QualityRun

        warehouse = CanonicalWarehouse(data_root=tmp_path)
        coordinator = CanonicalPublishCoordinator(warehouse=warehouse, uow_factory=uow_factory)
        version = _building_version()
        run = QualityRun(
            run_id=new_entity_id("data_quality"),
            version_id=version.version_id,
            status=QualityRunStatus.FAILED,
            rule_version=version.rule_version,
            schema_version=version.schema_version,
            content_sha256=_compute_content_sha(_daily_bar_table()),
            started_at=utc_now(),
            ended_at=utc_now(),
            results=(
                QualityResult(
                    rule_code="ohlc_consistency",
                    passed=False,
                    affected_count=1,
                    summary="high < open",
                    report_relative_path=None,
                ),
            ),
            summary_report_relative_path=None,
            failure=BatchFailureInfo(
                error_type=BatchFailureKind.QUALITY,
                message="ohlc violation",
                retryable=False,
            ),
        )
        with uow_factory() as uow:
            uow.data.save_version(version)
            uow.data.save_quality_run(run)
            uow.commit()
        with pytest.raises(CanonicalWarehouseError):
            coordinator.coordinate(
                CanonicalPublishCoordinatorRequest(
                    version=version,
                    tables=(_daily_bar_table(),),
                    code_version="s1-04",
                    rule_version="quality-rules.v1",
                    quality_run_id=run.run_id,
                )
            )

    def test_coordinator_rejects_publish_when_quality_run_skipped(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        from quantking.data.adapters.warehouse.publish_coordinator import (
            CanonicalPublishCoordinator,
            CanonicalPublishCoordinatorRequest,
        )
        from quantking.data.models import QualityRun

        warehouse = CanonicalWarehouse(data_root=tmp_path)
        coordinator = CanonicalPublishCoordinator(warehouse=warehouse, uow_factory=uow_factory)
        version = _building_version()
        run = QualityRun(
            run_id=new_entity_id("data_quality"),
            version_id=version.version_id,
            status=QualityRunStatus.SKIPPED,
            rule_version=version.rule_version,
            schema_version=version.schema_version,
            content_sha256=_compute_content_sha(_daily_bar_table()),
            started_at=utc_now(),
            ended_at=utc_now(),
            results=(),
            summary_report_relative_path=None,
            failure=None,
        )
        with uow_factory() as uow:
            uow.data.save_version(version)
            uow.data.save_quality_run(run)
            uow.commit()
        with pytest.raises(CanonicalWarehouseError):
            coordinator.coordinate(
                CanonicalPublishCoordinatorRequest(
                    version=version,
                    tables=(_daily_bar_table(),),
                    code_version="s1-04",
                    rule_version="quality-rules.v1",
                    quality_run_id=run.run_id,
                )
            )


# ---------------------------------------------------------------------------
# Credential binding
# ---------------------------------------------------------------------------


class TestQualityGateCredentialBinding:
    def test_coordinator_rejects_run_for_different_version(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        from quantking.data.adapters.warehouse.publish_coordinator import (
            CanonicalPublishCoordinator,
            CanonicalPublishCoordinatorRequest,
        )

        warehouse = CanonicalWarehouse(data_root=tmp_path)
        artifacts_root = tmp_path / "artifacts"
        version = _building_version()
        table = _daily_bar_table()
        run = _make_passed_quality_run(
            version, table, uow_factory=uow_factory, artifacts_root=artifacts_root
        )
        # Build a *different* version with a new version_id.
        other_version = _building_version()
        with uow_factory() as uow:
            uow.data.save_version(other_version)
            uow.commit()
        coordinator = CanonicalPublishCoordinator(warehouse=warehouse, uow_factory=uow_factory)
        with pytest.raises(CanonicalWarehouseError):
            coordinator.coordinate(
                CanonicalPublishCoordinatorRequest(
                    version=other_version,
                    tables=(table,),
                    code_version="s1-04",
                    rule_version="quality-rules.v1",
                    quality_run_id=run.run_id,
                )
            )

    def test_coordinator_rejects_run_with_different_rule_version(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        from quantking.data.adapters.warehouse.publish_coordinator import (
            CanonicalPublishCoordinator,
            CanonicalPublishCoordinatorRequest,
        )

        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        table = _daily_bar_table()
        # Save a PASSED quality run with a DIFFERENT rule_version.
        from quantking.data.models import QualityResult, QualityRun

        run = QualityRun(
            run_id=new_entity_id("data_quality"),
            version_id=version.version_id,
            status=QualityRunStatus.PASSED,
            rule_version="quality-rules.v999",  # mismatched
            schema_version=version.schema_version,
            content_sha256=_compute_content_sha(table),
            started_at=utc_now(),
            ended_at=utc_now(),
            results=(
                QualityResult(
                    rule_code="schema_conformance",
                    passed=True,
                    affected_count=0,
                    summary="ok",
                    report_relative_path=None,
                ),
            ),
            summary_report_relative_path=None,
            failure=None,
        )
        with uow_factory() as uow:
            uow.data.save_version(version)
            uow.data.save_quality_run(run)
            uow.commit()
        coordinator = CanonicalPublishCoordinator(warehouse=warehouse, uow_factory=uow_factory)
        with pytest.raises(CanonicalWarehouseError):
            coordinator.coordinate(
                CanonicalPublishCoordinatorRequest(
                    version=version,
                    tables=(table,),
                    code_version="s1-04",
                    rule_version="quality-rules.v1",
                    quality_run_id=run.run_id,
                )
            )

    def test_coordinator_rejects_run_with_mismatched_content_sha(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        from quantking.data.adapters.warehouse.publish_coordinator import (
            CanonicalPublishCoordinator,
            CanonicalPublishCoordinatorRequest,
        )
        from quantking.data.quality.artifacts import QualityArtifactWriter
        from quantking.data.quality.engine import QualityEngine
        from quantking.data.quality.models import QualityRequest, ReferenceDataBinding

        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        table = _daily_bar_table()
        # Run quality against a *different* content_sha (simulating tampering
        # after the run).
        request = QualityRequest(
            dataset=version.dataset,
            version_id=version.version_id,
            rule_version=version.rule_version,
            schema_version=version.schema_version,
            code_version=version.code_version,
            content_sha256="0" * 64,  # mismatched content SHA
            input_batch_ids=version.input_batch_ids,
            expected_securities=("000001.SZ",),
            expected_exchanges=("SZ",),
            coverage_start=TradingDate(date(2026, 7, 15)),
            coverage_end=TradingDate(date(2026, 7, 15)),
            reference_data={
                "exchange_calendar": ReferenceDataBinding(
                    dataset=DatasetKind.EXCHANGE_CALENDAR,
                    version_id=new_entity_id("data_version"),
                    content_sha256="b" * 64,
                )
            },
            empty_result=None,
        )
        from quantking.data.quality.rules import default_rule_set

        engine = QualityEngine(rule_set=default_rule_set())
        outcome = engine.execute(
            request=request,
            table=table,
            reference_tables={"exchange_calendar": _calendar_table()},
        )
        writer = QualityArtifactWriter(artifacts_root=tmp_path / "artifacts")
        writer.write(outcome=outcome, row_evidence_tables={})
        from quantking.data.models import QualityResult, QualityRun
        from quantking.data.quality.models import RuleOutcomeStatus

        run = QualityRun(
            run_id=outcome.run_id,
            version_id=version.version_id,
            status=QualityRunStatus.PASSED,
            rule_version=version.rule_version,
            schema_version=version.schema_version,
            content_sha256="0" * 64,  # mismatched content SHA
            started_at=outcome.started_at,
            ended_at=outcome.ended_at,  # type: ignore[arg-type]
            results=tuple(
                QualityResult(
                    rule_code=result.rule_code,
                    passed=result.status is RuleOutcomeStatus.PASSED,
                    affected_count=result.affected_count,
                    summary=result.summary,
                    report_relative_path=result.row_evidence_relative_path,
                )
                for result in outcome.results
                if result.status is not RuleOutcomeStatus.SKIPPED
            ),
            summary_report_relative_path=None,
            failure=None,
        )
        with uow_factory() as uow:
            uow.data.save_version(version)
            uow.data.save_quality_run(run)
            uow.commit()
        coordinator = CanonicalPublishCoordinator(warehouse=warehouse, uow_factory=uow_factory)
        with pytest.raises(CanonicalWarehouseError):
            coordinator.coordinate(
                CanonicalPublishCoordinatorRequest(
                    version=version,
                    tables=(table,),
                    code_version="s1-04",
                    rule_version="quality-rules.v1",
                    quality_run_id=run.run_id,
                )
            )

    def test_coordinator_publishes_when_quality_run_matches(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        from quantking.data.adapters.warehouse.publish_coordinator import (
            CanonicalPublishCoordinator,
            CanonicalPublishCoordinatorRequest,
        )

        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        table = _daily_bar_table()
        run = _make_passed_quality_run(
            version,
            table,
            uow_factory=uow_factory,
            artifacts_root=tmp_path / "artifacts",
        )
        coordinator = CanonicalPublishCoordinator(warehouse=warehouse, uow_factory=uow_factory)
        result = coordinator.coordinate(
            CanonicalPublishCoordinatorRequest(
                version=version,
                tables=(table,),
                code_version="s1-04",
                rule_version="quality-rules.v1",
                quality_run_id=run.run_id,
            )
        )
        assert result.version.status is DatasetVersionStatus.PUBLISHED


# ---------------------------------------------------------------------------
# Failure semantics
# ---------------------------------------------------------------------------


class TestQualityGateFailureSemantics:
    def test_quality_failure_transitions_version_to_failed(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        from quantking.data.adapters.warehouse.publish_coordinator import (
            CanonicalPublishCoordinator,
            CanonicalPublishCoordinatorRequest,
        )
        from quantking.data.models import QualityResult, QualityRun

        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        run = QualityRun(
            run_id=new_entity_id("data_quality"),
            version_id=version.version_id,
            status=QualityRunStatus.FAILED,
            rule_version=version.rule_version,
            schema_version=version.schema_version,
            content_sha256=_compute_content_sha(_daily_bar_table()),
            started_at=utc_now(),
            ended_at=utc_now(),
            results=(
                QualityResult(
                    rule_code="ohlc_consistency",
                    passed=False,
                    affected_count=1,
                    summary="high < open",
                    report_relative_path=None,
                ),
            ),
            summary_report_relative_path=None,
            failure=BatchFailureInfo(
                error_type=BatchFailureKind.QUALITY,
                message="ohlc violation",
                retryable=False,
            ),
        )
        with uow_factory() as uow:
            uow.data.save_version(version)
            uow.data.save_quality_run(run)
            uow.commit()
        coordinator = CanonicalPublishCoordinator(warehouse=warehouse, uow_factory=uow_factory)
        with pytest.raises(CanonicalWarehouseError):
            coordinator.coordinate(
                CanonicalPublishCoordinatorRequest(
                    version=version,
                    tables=(_daily_bar_table(),),
                    code_version="s1-04",
                    rule_version="quality-rules.v1",
                    quality_run_id=run.run_id,
                )
            )
        # The version must now be FAILED in SQLite; no PUBLISHED row may exist.
        with uow_factory() as uow:
            stored = uow.data.get_version(str(version.version_id))
            assert stored.status is DatasetVersionStatus.FAILED
            assert stored.failure is not None
            assert stored.failure.error_type is BatchFailureKind.QUALITY

    def test_failed_version_cannot_be_repaired_in_place(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        from quantking.data.adapters.warehouse.publish_coordinator import (
            CanonicalPublishCoordinator,
            CanonicalPublishCoordinatorRequest,
        )
        from quantking.data.models import QualityResult, QualityRun

        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        run = QualityRun(
            run_id=new_entity_id("data_quality"),
            version_id=version.version_id,
            status=QualityRunStatus.FAILED,
            rule_version=version.rule_version,
            schema_version=version.schema_version,
            content_sha256=_compute_content_sha(_daily_bar_table()),
            started_at=utc_now(),
            ended_at=utc_now(),
            results=(
                QualityResult(
                    rule_code="ohlc_consistency",
                    passed=False,
                    affected_count=1,
                    summary="high < open",
                    report_relative_path=None,
                ),
            ),
            summary_report_relative_path=None,
            failure=BatchFailureInfo(
                error_type=BatchFailureKind.QUALITY,
                message="ohlc violation",
                retryable=False,
            ),
        )
        with uow_factory() as uow:
            uow.data.save_version(version)
            uow.data.save_quality_run(run)
            uow.commit()
        coordinator = CanonicalPublishCoordinator(warehouse=warehouse, uow_factory=uow_factory)
        with pytest.raises(CanonicalWarehouseError):
            coordinator.coordinate(
                CanonicalPublishCoordinatorRequest(
                    version=version,
                    tables=(_daily_bar_table(),),
                    code_version="s1-04",
                    rule_version="quality-rules.v1",
                    quality_run_id=run.run_id,
                )
            )
        # Trying to re-coordinate the SAME version_id (now FAILED) must
        # fail; retry must use a new version_id.
        with pytest.raises(CanonicalWarehouseError):
            coordinator.coordinate(
                CanonicalPublishCoordinatorRequest(
                    version=version,
                    tables=(_daily_bar_table(),),
                    code_version="s1-04",
                    rule_version="quality-rules.v1",
                    quality_run_id=run.run_id,
                )
            )


# ---------------------------------------------------------------------------
# Direct SQL cannot bypass the gate
# ---------------------------------------------------------------------------


class TestQualityGateDatabaseEnforcement:
    def test_direct_sql_cannot_mark_published_without_quality_run(
        self,
        tmp_path: Path,
    ) -> None:
        database = tmp_path / "data.db"
        initialize_database(database)
        # Insert a BUILDING version directly via SQL (bypassing the coordinator).
        version_id = "data_version:11111111-1111-4111-8111-111111111111"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO dataset_versions (version_id, dataset, status, "
                "schema_version, code_version, rule_version, parent_version_id, "
                "input_batch_ids_json, created_at, validated_at, published_at, "
                "manifest_sha256, security_count, row_count, coverage_start, "
                "coverage_end, coverage_summary_json, failure_json) "
                "VALUES (?, ?, 'BUILDING', ?, ?, ?, NULL, '[]', ?, NULL, NULL, "
                "NULL, NULL, NULL, NULL, NULL, '{}', NULL)",
                (
                    version_id,
                    "daily_bar_unadjusted",
                    "daily_bar_unadjusted.v1",
                    "s1-04",
                    "quality-rules.v1",
                    "2026-07-20T00:00:00Z",
                ),
            )
            connection.commit()
            # Attempting to transition directly to PUBLISHED must be blocked
            # by a SQLite trigger/constraint because no PASSED QualityRun
            # exists for this version_id.
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE dataset_versions SET status='PUBLISHED', "
                    "published_at='2026-07-20T00:00:00Z' WHERE version_id=?",
                    (version_id,),
                )

    def test_direct_sql_cannot_mark_published_when_quality_run_failed(
        self,
        tmp_path: Path,
    ) -> None:
        database = tmp_path / "data.db"
        initialize_database(database)
        version_id = "data_version:22222222-2222-4222-8222-222222222222"
        run_id = "data_quality:33333333-3333-4333-8333-333333333333"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO dataset_versions (version_id, dataset, status, "
                "schema_version, code_version, rule_version, parent_version_id, "
                "input_batch_ids_json, created_at, validated_at, published_at, "
                "manifest_sha256, security_count, row_count, coverage_start, "
                "coverage_end, coverage_summary_json, failure_json) "
                "VALUES (?, ?, 'VALIDATED', ?, ?, ?, NULL, '[]', ?, ?, NULL, "
                "?, 1, 1, '2026-07-15', '2026-07-15', "
                '\'{"date_count":"1","row_count":"1",'
                '"security_count":"1","first_date":"2026-07-15",'
                '"last_date":"2026-07-15"}\', NULL)',
                (
                    version_id,
                    "daily_bar_unadjusted",
                    "daily_bar_unadjusted.v1",
                    "s1-04",
                    "quality-rules.v1",
                    "2026-07-20T00:00:00Z",
                    "2026-07-20T00:01:00Z",
                    "a" * 64,
                ),
            )
            # Insert a FAILED quality run.
            connection.execute(
                "INSERT INTO quality_runs (run_id, version_id, status, rule_version, "
                "schema_version, content_sha256, "
                "started_at, ended_at, results_json, summary_report_relative_path, "
                "failure_json) VALUES (?, ?, 'FAILED', ?, ?, ?, ?, ?, '[]', NULL, "
                '\'{"error_type":"QUALITY_FAILURE","message":"bad","retryable":false}\')',
                (
                    run_id,
                    version_id,
                    "quality-rules.v1",
                    "daily_bar_unadjusted.v1",
                    "a" * 64,
                    "2026-07-20T00:00:30Z",
                    "2026-07-20T00:00:40Z",
                ),
            )
            connection.commit()
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE dataset_versions SET status='PUBLISHED', "
                    "published_at='2026-07-20T00:02:00Z' WHERE version_id=?",
                    (version_id,),
                )


__all__ = [
    "TestQualityGateBlocksPublishWithoutPassedRun",
    "TestQualityGateCredentialBinding",
    "TestQualityGateDatabaseEnforcement",
    "TestQualityGateFailureSemantics",
]
