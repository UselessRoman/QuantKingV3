"""S1-02-R1 red tests: query credential binding and contract.

These tests target the S1-02-R1 requirements about disk-manifest-vs-SQLite
credential comparison and query contract fixes (section 五, 六, 八 items
6, 7, 10, 11). They MUST fail against the S1-02 baseline (``3235aba``)
and pass after R1 lands:

6. SQLite ``manifest_sha256`` differs from disk manifest -> not queryable.
7. Any of schema/code/rule/parent_version/input_batches/coverage/counts
   differs between SQLite and the on-disk manifest -> not queryable.
10. A legally published empty version queried by id returns a zero-row
    ``pyarrow.Table`` whose schema matches the requested fields exactly.
11. ``fields=("open",)`` (or any legal projection that omits the sort
    key columns) must succeed and return only the requested columns in
    canonical sort order.

All tests use only ``tmp_path`` and synthetic data.
"""

from __future__ import annotations

import json
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
)
from quantking.data.models import (
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    EmptyResultInfo,
    LegalEmptyClassification,
    transition_dataset_version,
)
from quantking.jobs.adapters.persistence.sqlite import initialize_database

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _canonical_daily_bar_table() -> pa.Table:
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


def _empty_daily_bar_table() -> pa.Table:
    descriptor = canonical_schema("daily_bar_unadjusted")
    arrays = [pa.array([], type=field.type) for field in descriptor]
    return pa.Table.from_arrays(arrays, schema=pa.schema(descriptor))


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


def _publish_and_mark(
    warehouse: CanonicalWarehouse,
    uow_factory: DataSqliteUnitOfWorkFactory,
    version: DatasetVersion,
    *,
    manifest_sha_override: str | None = None,
    security_count: int = 1,
    row_count: int = 1,
    coverage_start: date | None = date(2026, 7, 15),
    coverage_end: date | None = date(2026, 7, 15),
    coverage_summary: dict[str, str] | None = None,
    schema_version: str | None = None,
    code_version: str | None = None,
    rule_version: str | None = None,
    parent_version_id: object | None = None,
    input_batch_ids: tuple[object, ...] = (),
) -> Path:
    if coverage_summary is None:
        coverage_summary = {"date_count": "1", "security_count": "1"}
    landed = warehouse.publish(
        CanonicalPublishRequest(
            version=version,
            tables=(_canonical_daily_bar_table(),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
    )
    manifest = json.loads((landed / WAREHOUSE_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    sha = manifest["manifest_sha256"] if manifest_sha_override is None else manifest_sha_override
    with uow_factory() as uow:
        stored = uow.data.save_version(version)
        # Override stored version fields to simulate credential drift.
        if (
            schema_version is not None
            or code_version is not None
            or rule_version is not None
            or parent_version_id is not None
            or input_batch_ids != ()
        ):
            object.__setattr__(
                stored,
                "schema_version",
                schema_version if schema_version is not None else stored.schema_version,
            )
            object.__setattr__(
                stored,
                "code_version",
                code_version if code_version is not None else stored.code_version,
            )
            object.__setattr__(
                stored,
                "rule_version",
                rule_version if rule_version is not None else "quality-rules.v1",
            )
            object.__setattr__(
                stored,
                "parent_version_id",
                parent_version_id if parent_version_id is not None else stored.parent_version_id,
            )
            object.__setattr__(
                stored,
                "input_batch_ids",
                input_batch_ids if input_batch_ids != () else stored.input_batch_ids,
            )
        validated = transition_dataset_version(
            stored,
            DatasetVersionStatus.VALIDATED,
            at=utc_now(),
            manifest_sha256=sha,
            security_count=security_count,
            row_count=row_count,
            coverage_start=(TradingDate(coverage_start) if coverage_start is not None else None),
            coverage_end=(TradingDate(coverage_end) if coverage_end is not None else None),
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


# ---------------------------------------------------------------------------
# Red test 6: SQLite manifest_sha256 differs from disk -> not queryable
# ---------------------------------------------------------------------------


class TestSqliteManifestShaMismatch:
    """Red test 6: SQLite-side manifest_sha256 must equal the disk manifest SHA."""

    def test_query_rejects_sqlite_manifest_sha_mismatch(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        _publish_and_mark(
            warehouse,
            uow_factory,
            version,
            manifest_sha_override="a" * 64,
        )
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(DatasetVersionNotQueryableError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id=str(version.version_id),
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                )
            )


# ---------------------------------------------------------------------------
# Red test 7: schema/code/rule/parent/inputs/coverage/count mismatch
# ---------------------------------------------------------------------------


class TestCredentialBindingMismatches:
    """Red test 7: every credential field must match between SQLite and manifest."""

    def test_schema_version_mismatch_rejected(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        _publish_and_mark(
            warehouse,
            uow_factory,
            version,
            schema_version="daily_bar_unadjusted.v2",
        )
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(DatasetVersionNotQueryableError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id=str(version.version_id),
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                )
            )

    def test_code_version_mismatch_rejected(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        _publish_and_mark(
            warehouse,
            uow_factory,
            version,
            code_version="s1-02-different",
        )
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(DatasetVersionNotQueryableError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id=str(version.version_id),
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                )
            )

    def test_rule_version_mismatch_rejected(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        _publish_and_mark(
            warehouse,
            uow_factory,
            version,
            rule_version="quality-rules.v2",
        )
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(DatasetVersionNotQueryableError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id=str(version.version_id),
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                )
            )

    def test_row_count_mismatch_rejected(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        _publish_and_mark(
            warehouse,
            uow_factory,
            version,
            row_count=999,
        )
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(DatasetVersionNotQueryableError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id=str(version.version_id),
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                )
            )

    def test_security_count_mismatch_rejected(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        _publish_and_mark(
            warehouse,
            uow_factory,
            version,
            security_count=999,
        )
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(DatasetVersionNotQueryableError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id=str(version.version_id),
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                )
            )

    def test_coverage_start_mismatch_rejected(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        _publish_and_mark(
            warehouse,
            uow_factory,
            version,
            coverage_start=date(2026, 7, 1),
        )
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(DatasetVersionNotQueryableError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id=str(version.version_id),
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                )
            )

    def test_coverage_summary_mismatch_rejected(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        _publish_and_mark(
            warehouse,
            uow_factory,
            version,
            coverage_summary={"date_count": "999", "security_count": "1"},
        )
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(DatasetVersionNotQueryableError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id=str(version.version_id),
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                )
            )

    def test_input_batch_ids_mismatch_rejected(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        from quantking.common.ids import EntityId
        from quantking.data.adapters.persistence.models import (
            DatasetVersionBatchRow,
        )
        from quantking.data.models import (
            DataSource,
            IngestionBatch,
            IngestionBatchStatus,
            transition_batch,
        )

        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        # Publish with empty input_batch_ids; the manifest will carry
        # input_batch_ids=[]. Then we inject a fake batch association
        # directly into SQLite to simulate credential drift: the
        # SQLite row will report input_batch_ids=(fake_batch,) while
        # the manifest still says [].
        _publish_and_mark(warehouse, uow_factory, version)
        fake_batch_id = EntityId.parse("data_batch:a3b4c5d6-e7f8-4a2b-9c1d-2e3f4a5b6c7d")
        # Create the ingestion batch row so the foreign key on
        # DatasetVersionBatchRow is satisfied.
        batch = IngestionBatch.running(
            batch_id=fake_batch_id,
            source=DataSource.XTQUANT,
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            adapter_version="xtquant-probe-1",
            request_params={"code": "000001.SZ"},
            started_at=utc_now(),
        )
        batch = transition_batch(
            batch,
            IngestionBatchStatus.SUCCEEDED,
            at=utc_now(),
            row_count=1,
            security_count=1,
            manifest_files=(),
        )
        with uow_factory() as uow:
            uow.data.save_batch(batch)
            # Inject a fake DatasetVersionBatchRow so the SQLite row
            # reports input_batch_ids=(fake_batch_id,) while the
            # manifest still says [].
            uow.data._session.add(
                DatasetVersionBatchRow(
                    version_id=str(version.version_id),
                    batch_id=str(fake_batch_id),
                    ordinal=0,
                )
            )
            uow.commit()
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(DatasetVersionNotQueryableError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id=str(version.version_id),
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                )
            )


# ---------------------------------------------------------------------------
# Red test 10: legal empty version query returns zero-row exact schema
# ---------------------------------------------------------------------------


class TestLegalEmptyVersionQuery:
    """Red test 10: a legally published empty version must return a zero-row table."""

    def test_empty_version_query_returns_zero_row_table_with_full_schema(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        landed = warehouse.publish(
            CanonicalPublishRequest(
                version=version,
                tables=(_empty_daily_bar_table(),),
                code_version="s1-02",
                rule_version="quality-rules.v1",
                empty_result=EmptyResultInfo(
                    classification=LegalEmptyClassification.NO_TRADING_SESSION,
                    reason="calendar is closed for the entire window",
                ),
            )
        )
        manifest = json.loads((landed / WAREHOUSE_MANIFEST_FILENAME).read_text(encoding="utf-8"))
        # The manifest for an empty version has coverage_start/end = None
        # (no rows to compute dates from). The SQLite row must match.
        manifest_coverage_summary = dict(manifest["coverage_summary"])
        with uow_factory() as uow:
            stored = uow.data.save_version(version)
            validated = transition_dataset_version(
                stored,
                DatasetVersionStatus.VALIDATED,
                at=utc_now(),
                manifest_sha256=manifest["manifest_sha256"],
                security_count=0,
                row_count=0,
                coverage_start=None,
                coverage_end=None,
                coverage_summary=manifest_coverage_summary,
            )
            uow.data.save_version(validated)
            published = transition_dataset_version(
                validated,
                DatasetVersionStatus.PUBLISHED,
                at=utc_now(),
            )
            uow.data.save_version(published)
            uow.commit()
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        result = adapter.query(
            CanonicalQueryRequest(
                version_id=str(version.version_id),
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
            )
        )
        assert isinstance(result, pa.Table)
        assert result.num_rows == 0
        # The schema must match the canonical daily_bar_unadjusted descriptor
        # exactly (all fields, in order, with the right types).
        descriptor = canonical_schema("daily_bar_unadjusted")
        assert tuple(result.schema.names) == tuple(field.name for field in descriptor)
        for actual, expected in zip(result.schema, descriptor, strict=True):
            assert actual.type == expected.type


# ---------------------------------------------------------------------------
# Red test 11: fields projection without sort keys succeeds
# ---------------------------------------------------------------------------


class TestFieldProjectionWithoutSortKeys:
    """Red test 11: legal projection that omits sort keys must succeed."""

    def test_fields_open_only_succeeds(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        _publish_and_mark(warehouse, uow_factory, version)
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        result = adapter.query(
            CanonicalQueryRequest(
                version_id=str(version.version_id),
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                fields=("open",),
            )
        )
        # Only the ``open`` column may appear in the result.
        assert tuple(result.schema.names) == ("open",)
        assert result.num_rows == 1
        assert result.column("open").to_pylist() == [10.0]

    def test_fields_close_only_succeeds(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        _publish_and_mark(warehouse, uow_factory, version)
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        result = adapter.query(
            CanonicalQueryRequest(
                version_id=str(version.version_id),
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                fields=("close",),
            )
        )
        assert tuple(result.schema.names) == ("close",)
        assert result.num_rows == 1
        assert result.column("close").to_pylist() == [10.5]


__all__ = [
    "TestCredentialBindingMismatches",
    "TestFieldProjectionWithoutSortKeys",
    "TestLegalEmptyVersionQuery",
    "TestSqliteManifestShaMismatch",
]
