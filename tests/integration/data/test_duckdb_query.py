"""DuckDB-based read-only query adapter tests (Stage 1 S1-02).

The query adapter must:

* Accept a concrete ``version_id`` only; refuse ``None``, ``"latest"`` and
  filesystem paths.
* Verify that the dataset version row in SQLite is ``PUBLISHED``.
* Re-validate the on-disk file set against the manifest and refuse to query
  if any expected file is missing, any extra file is present, any size or
  SHA-256 differs, or the ``COMPLETE`` marker is tampered with.
* Open a fresh in-memory DuckDB connection, register the Parquet files
  listed in the manifest, and execute a parameterised, schema-allowlist
  query.
* Return a ``pyarrow.Table`` ordered by the canonical sort key.
* Refuse ad-hoc SQL or unknown field names; refuse arbitrary ``Path``
  arguments.

All tests use only ``tmp_path`` and synthetic data.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.errors import InvalidPathError
from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate, utc_now
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.adapters.warehouse.canonical_warehouse import (
    CanonicalPublishRequest,
    CanonicalWarehouse,
)
from quantking.data.adapters.warehouse.manifest import (
    WAREHOUSE_MANIFEST_FILENAME,
)
from quantking.data.adapters.warehouse.query import (
    CanonicalQueryAdapter,
    CanonicalQueryRequest,
    DatasetVersionNotQueryableError,
    QueryContractError,
)
from quantking.data.models import (
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    QualityResult,
    QualityRun,
    QualityRunStatus,
    transition_dataset_version,
)
from quantking.jobs.adapters.persistence.sqlite import initialize_database


@dataclass(frozen=True, slots=True)
class _Row:
    code: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    pre_close: float
    volume: int
    amount: float


def _rows() -> list[_Row]:
    return [
        _Row("000001.SZ", date(2026, 7, 14), 9.9, 10.4, 9.8, 10.2, 9.9, 900_000, 9_000_000.0),
        _Row("000001.SZ", date(2026, 7, 15), 10.2, 10.6, 10.1, 10.5, 10.2, 1_000_000, 10_500_000.0),
        _Row("000001.SZ", date(2026, 7, 16), 10.5, 10.8, 10.2, 10.7, 10.5, 1_100_000, 11_700_000.0),
        _Row("600000.SH", date(2026, 7, 15), 8.0, 8.2, 7.9, 8.1, 8.0, 500_000, 4_000_000.0),
        _Row("600000.SH", date(2026, 7, 16), 8.1, 8.3, 8.0, 8.2, 8.1, 520_000, 4_200_000.0),
    ]


def _canonical_table(rows: Sequence[_Row]) -> pa.Table:
    table = pa.Table.from_pylist(
        [
            {
                "code": row.code,
                "trade_date": row.trade_date,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "pre_close": row.pre_close,
                "volume": row.volume,
                "amount": row.amount,
            }
            for row in rows
        ]
    )
    # ``pa.Table.from_pylist`` emits nullable fields; the canonical contract
    # requires the daily_bar_unadjusted columns to be non-nullable, so we
    # rebuild the table against the exact descriptor before publishing.
    descriptor = canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value)
    columns: list[pa.Array] = []
    fields: list[pa.Field] = []
    for field in descriptor:
        columns.append(table.column(field.name).cast(field.type, safe=False))
        fields.append(pa.field(field.name, field.type, nullable=field.nullable))
    return pa.Table.from_arrays(columns, schema=pa.schema(fields))


def _table() -> pa.Table:
    return _canonical_table(_rows())


def _building_version() -> DatasetVersion:
    return DatasetVersion.building(
        version_id=new_entity_id("data_version"),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-02",
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


def _publish(
    warehouse: CanonicalWarehouse,
    version: DatasetVersion,
    table: pa.Table | None = None,
) -> Path:
    return warehouse.publish(
        CanonicalPublishRequest(
            version=version,
            tables=(table if table is not None else _table(),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
    )


def _calendar_table() -> pa.Table:
    """Build an exchange_calendar reference table covering the test rows."""
    table = pa.Table.from_pylist(
        [
            {"exchange": "SZ", "trade_date": date(2026, 7, 14), "is_open": True},
            {"exchange": "SZ", "trade_date": date(2026, 7, 15), "is_open": True},
            {"exchange": "SZ", "trade_date": date(2026, 7, 16), "is_open": True},
            {"exchange": "SH", "trade_date": date(2026, 7, 15), "is_open": True},
            {"exchange": "SH", "trade_date": date(2026, 7, 16), "is_open": True},
        ]
    )
    descriptor = canonical_schema("exchange_calendar")
    columns: list[pa.Array] = []
    fields: list[pa.Field] = []
    for field in descriptor:
        columns.append(table.column(field.name).cast(field.type, safe=False))
        fields.append(pa.field(field.name, field.type, nullable=field.nullable))
    return pa.Table.from_arrays(columns, schema=pa.schema(fields))


def _compute_content_sha(table: pa.Table) -> str:
    """Compute a deterministic SHA-256 of the publishable data."""
    buffer = io.BytesIO()
    pa.parquet.write_table(table, buffer, compression="snappy")  # type: ignore[no-untyped-call]
    return hashlib.sha256(buffer.getvalue()).hexdigest()


def _make_passed_quality_run(
    version: DatasetVersion,
    table: pa.Table,
    *,
    uow_factory: DataSqliteUnitOfWorkFactory,
    artifacts_root: Path,
) -> QualityRun:
    """Build, persist and artifact-write a PASSED QualityRun for the version."""
    from quantking.data.quality.artifacts import QualityArtifactWriter
    from quantking.data.quality.engine import QualityEngine
    from quantking.data.quality.models import (
        QualityRequest,
        ReferenceDataBinding,
        RuleOutcomeStatus,
    )
    from quantking.data.quality.rules import default_rule_set

    request = QualityRequest(
        dataset=version.dataset,
        version_id=version.version_id,
        rule_version=version.rule_version,
        schema_version=version.schema_version,
        code_version=version.code_version,
        content_sha256=_compute_content_sha(table),
        input_batch_ids=version.input_batch_ids,
        expected_securities=tuple(sorted(set(table.column("code").to_pylist()))),
        expected_exchanges=("SZ", "SH"),
        coverage_start=TradingDate(date(2026, 7, 14)),
        coverage_end=TradingDate(date(2026, 7, 16)),
        reference_data={
            "exchange_calendar": ReferenceDataBinding(
                dataset=DatasetKind.EXCHANGE_CALENDAR,
                version_id=new_entity_id("data_version"),
                content_sha256="b" * 64,
            )
        },
        empty_result=None,
    )
    engine = QualityEngine(rule_set=default_rule_set())
    outcome = engine.execute(
        request=request,
        table=table,
        reference_tables={"exchange_calendar": _calendar_table()},
    )
    assert outcome.status is QualityRunStatus.PASSED
    writer = QualityArtifactWriter(artifacts_root=artifacts_root)
    writer.write(outcome=outcome, row_evidence_tables={})
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


def _publish_and_mark(
    warehouse: CanonicalWarehouse,
    uow_factory: DataSqliteUnitOfWorkFactory,
    version: DatasetVersion,
    table: pa.Table | None = None,
) -> Path:
    landed = _publish(warehouse, version, table=table)
    publishable = table if table is not None else _table()
    # Persist a PASSED quality run bound to the candidate content so
    # the SQLite trigger allows the PUBLISHED transition.
    _make_passed_quality_run(
        version,
        publishable,
        uow_factory=uow_factory,
        artifacts_root=warehouse.data_root / "artifacts",
    )
    # Read the actual manifest SHA and coverage from the on-disk
    # manifest so the SQLite DatasetVersion credentials match the
    # disk exactly. The publish coordinator would do the same.
    manifest_path = landed / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_sha = manifest["manifest_sha256"]
    coverage_summary = dict(manifest["coverage_summary"])
    coverage_start_value = manifest.get("coverage_start")
    coverage_end_value = manifest.get("coverage_end")
    with uow_factory() as uow:
        stored = uow.data.save_version(version)
        validated = transition_dataset_version(
            stored,
            DatasetVersionStatus.VALIDATED,
            at=utc_now(),
            manifest_sha256=manifest_sha,
            security_count=int(coverage_summary.get("security_count", "0")),
            row_count=int(coverage_summary.get("row_count", str(publishable.num_rows))),
            coverage_start=(
                TradingDate(date.fromisoformat(coverage_start_value))
                if coverage_start_value
                else None
            ),
            coverage_end=(
                TradingDate(date.fromisoformat(coverage_end_value)) if coverage_end_value else None
            ),
            coverage_summary=coverage_summary,
        )
        uow.data.save_version(validated)
        published = transition_dataset_version(
            validated,
            DatasetVersionStatus.PUBLISHED,
            at=utc_now(),
        )
        uow.data.save_version(published)
        uow.commit()
    return landed


class TestQueryAdapterContract:
    def test_query_returns_manifest_files_only(
        self, tmp_path: Path, uow_factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        _publish_and_mark(warehouse, uow_factory, version)
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        result = adapter.query(
            CanonicalQueryRequest(
                version_id=str(version.version_id),
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
            )
        )
        assert isinstance(result, pa.Table)
        assert result.num_rows == len(_rows())
        codes = sorted(result.column("code").to_pylist())
        assert codes == ["000001.SZ", "000001.SZ", "000001.SZ", "600000.SH", "600000.SH"]
        # The result must be sorted by the canonical sort key.
        sort_columns = list(
            zip(
                result.column("code").to_pylist(),
                result.column("trade_date").to_pylist(),
                strict=True,
            )
        )
        assert sort_columns == sorted(sort_columns)

    def test_query_rejects_unknown_field(
        self, tmp_path: Path, uow_factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        _publish_and_mark(warehouse, uow_factory, version)
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(QueryContractError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id=str(version.version_id),
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                    fields=("open", "vendor_field"),
                )
            )

    def test_query_rejects_arbitrary_sql(
        self, tmp_path: Path, uow_factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        _publish_and_mark(warehouse, uow_factory, version)
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(QueryContractError):
            adapter.execute_raw_sql(
                version_id=str(version.version_id),
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                sql="SELECT 1",
            )

    def test_query_rejects_latest_alias(
        self, tmp_path: Path, uow_factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(QueryContractError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id="latest",
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                )
            )

    def test_query_rejects_directory_path(
        self, tmp_path: Path, uow_factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(QueryContractError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id=str(tmp_path),
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                )
            )

    def test_query_rejects_path_within_root(
        self, tmp_path: Path, uow_factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(QueryContractError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id="warehouse/daily_bar_unadjusted/anything",
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                )
            )

    def test_query_rejects_building_version(
        self, tmp_path: Path, uow_factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        _publish(warehouse, version)
        # Persist a BUILDING version explicitly; the query must refuse it.
        with uow_factory() as uow:
            uow.data.save_version(version)
            uow.commit()
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(DatasetVersionNotQueryableError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id=str(version.version_id),
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                )
            )

    def test_query_rejects_failed_version(
        self, tmp_path: Path, uow_factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        _publish(warehouse, version)
        from quantking.data.models import (
            BatchFailureInfo,
            BatchFailureKind,
            transition_dataset_version,
        )

        with uow_factory() as uow:
            stored = uow.data.save_version(version)
            failed = transition_dataset_version(
                stored,
                DatasetVersionStatus.FAILED,
                at=utc_now(),
                failure=BatchFailureInfo(
                    error_type=BatchFailureKind.QUALITY,
                    message="synthetic",
                    retryable=False,
                ),
            )
            uow.data.save_version(failed)
            uow.commit()
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(DatasetVersionNotQueryableError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id=str(version.version_id),
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                )
            )


class TestQueryAdapterFiltering:
    def test_filter_by_codes_and_dates(
        self, tmp_path: Path, uow_factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        _publish_and_mark(warehouse, uow_factory, version)
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        result = adapter.query(
            CanonicalQueryRequest(
                version_id=str(version.version_id),
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                fields=("code", "trade_date", "close"),
                codes=("000001.SZ",),
                start_date=TradingDate(date(2026, 7, 15)),
                end_date=TradingDate(date(2026, 7, 15)),
            )
        )
        rows = result.to_pylist()
        assert rows == [{"code": "000001.SZ", "trade_date": date(2026, 7, 15), "close": 10.5}]

    def test_invalid_field_combination_with_dates(
        self, tmp_path: Path, uow_factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        _publish_and_mark(warehouse, uow_factory, version)
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(QueryContractError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id=str(version.version_id),
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                    start_date=TradingDate(date(2026, 7, 15)),
                    end_date=TradingDate(date(2026, 7, 10)),  # end < start
                )
            )


class TestQueryAdapterFreeze:
    def test_old_version_unchanged_after_republish(
        self, tmp_path: Path, uow_factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        first_version = _building_version()
        first_landed = _publish_and_mark(warehouse, uow_factory, first_version)
        first_manifest = json.loads(
            (first_landed / WAREHOUSE_MANIFEST_FILENAME).read_text(encoding="utf-8")
        )
        first_sha = first_manifest["manifest_sha256"]
        # Now publish a new version with different content.
        new_rows = [
            _Row("000001.SZ", date(2026, 7, 14), 9.9, 10.4, 9.8, 10.2, 9.9, 900_000, 9_000_000.0),
            _Row(
                "000001.SZ",
                date(2026, 7, 15),
                99.0,
                99.5,
                98.5,
                99.2,
                9.9,
                1_000_000,
                99_000_000.0,
            ),
            _Row(
                "000001.SZ",
                date(2026, 7, 16),
                99.2,
                99.8,
                99.0,
                99.6,
                99.2,
                1_100_000,
                109_000_000.0,
            ),
        ]
        new_table = _canonical_table(new_rows)
        second_version = DatasetVersion.building(
            version_id=new_entity_id("data_version"),
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            code_version="s1-02",
            parent_version_id=first_version.version_id,
            input_batch_ids=(),
            created_at=utc_now(),
        )
        _publish_and_mark(warehouse, uow_factory, second_version, table=new_table)
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        first_result = adapter.query(
            CanonicalQueryRequest(
                version_id=str(first_version.version_id),
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
            )
        )
        # The first version must still return the original rows.
        closes = first_result.column("close").to_pylist()
        assert 10.2 in closes
        assert 10.5 in closes
        assert 99.2 not in closes
        # The directory itself must be byte-identical to its manifest.
        for entry in first_manifest["files"]:
            absolute = first_landed / entry["relative_path"]
            assert hashlib.sha256(absolute.read_bytes()).hexdigest() == entry["sha256"]
        # The manifest SHA-256 of the first version must not have changed.
        reloaded = json.loads(
            (first_landed / WAREHOUSE_MANIFEST_FILENAME).read_text(encoding="utf-8")
        )
        assert reloaded["manifest_sha256"] == first_sha


class TestQueryAdapterRootValidation:
    def test_adapter_rejects_relative_data_root(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidPathError):
            CanonicalQueryAdapter(data_root=Path("relative"), uow_factory=None)  # type: ignore[arg-type]
