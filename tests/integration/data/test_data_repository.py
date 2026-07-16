"""Repository, immutability, audit, and ORM/domain separation tests for Stage 1."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.models import (
    BatchFailureInfo,
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    DataSource,
    IngestionBatch,
    IngestionBatchStatus,
    ManifestFileEntry,
    SourceOverride,
    SourceOverrideStatus,
    transition_batch,
    transition_dataset_version,
    transition_override,
)
from quantking.jobs.adapters.persistence.sqlite import initialize_database

_NOW = UtcTimestamp(datetime(2026, 7, 16, 1, 0, tzinfo=UTC))
_LATER = UtcTimestamp(datetime(2026, 7, 16, 2, 0, tzinfo=UTC))
_TRADE_DATE = TradingDate(date(2026, 7, 16))
_SHA = "a" * 64


@pytest.fixture()
def factory(tmp_path: Path) -> DataSqliteUnitOfWorkFactory:
    database = tmp_path / "data.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    yield DataSqliteUnitOfWorkFactory(engine)
    engine.dispose()


def _running_batch() -> IngestionBatch:
    return IngestionBatch.running(
        batch_id=new_entity_id("data_batch"),
        source=DataSource.XTQUANT,
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        adapter_version="xtquant-probe-1",
        request_params={"code": "000001.SZ", "start": "2026-01-01"},
        started_at=_NOW,
    )


def _building_version(batch_id: object) -> DatasetVersion:
    return DatasetVersion.building(
        version_id=new_entity_id("data_version"),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        schema_version="daily_bar_unadjusted.v1",
        code_version="abc123",
        parent_version_id=None,
        input_batch_ids=(batch_id,)
        if isinstance(batch_id, type(_running_batch().batch_id))
        else (),
        created_at=_NOW,
    )


class TestBatchRepository:
    def test_create_and_get_running_batch(self, factory: DataSqliteUnitOfWorkFactory) -> None:
        batch = _running_batch()
        with factory() as uow:
            saved = uow.data.save_batch(batch)
            uow.commit()
        assert saved.batch_id == batch.batch_id
        with factory() as uow:
            loaded = uow.data.get_batch(str(batch.batch_id))
            uow.rollback()
        assert loaded.status is IngestionBatchStatus.RUNNING
        assert loaded.source is DataSource.XTQUANT

    def test_transition_batch_to_succeeded_persists(
        self, factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        batch = _running_batch()
        with factory() as uow:
            uow.data.save_batch(batch)
            uow.commit()
        succeeded = transition_batch(
            batch,
            IngestionBatchStatus.SUCCEEDED,
            at=_LATER,
            row_count=100,
            security_count=1,
            manifest_files=(
                ManifestFileEntry(relative_path="raw/x/d.parquet", size=10, sha256=_SHA),
            ),
        )
        with factory() as uow:
            uow.data.save_batch(succeeded)
            uow.commit()
        with factory() as uow:
            loaded = uow.data.get_batch(str(batch.batch_id))
            uow.rollback()
        assert loaded.status is IngestionBatchStatus.SUCCEEDED
        assert loaded.row_count == 100
        assert len(loaded.manifest_files) == 1


class TestDatasetVersionImmutability:
    def _make_published(self, factory: DataSqliteUnitOfWorkFactory) -> DatasetVersion:
        batch = _running_batch()
        version = DatasetVersion.building(
            version_id=new_entity_id("data_version"),
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            code_version="abc123",
            parent_version_id=None,
            input_batch_ids=(batch.batch_id,),
            created_at=_NOW,
        )
        with factory() as uow:
            uow.data.save_batch(batch)
            uow.data.save_version(version)
            uow.commit()
        validated = transition_dataset_version(
            version,
            DatasetVersionStatus.VALIDATED,
            at=_NOW,
            manifest_sha256=_SHA,
            security_count=10,
            row_count=1000,
        )
        with factory() as uow:
            uow.data.save_version(validated)
            uow.commit()
        published = transition_dataset_version(
            validated,
            DatasetVersionStatus.PUBLISHED,
            at=_LATER,
        )
        with factory() as uow:
            uow.data.save_version(published)
            uow.commit()
        return published

    def test_published_version_cannot_be_updated_at_db_level(
        self, factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        published = self._make_published(factory)
        # Attempt a direct UPDATE on a PUBLISHED row via raw sqlite -> blocked by trigger.
        with factory() as uow:
            engine = uow.data._session.bind
            with engine.connect() as conn, pytest.raises(IntegrityError):
                conn.exec_driver_sql(
                    "UPDATE dataset_versions SET status='BUILDING' WHERE version_id=?",
                    (str(published.version_id),),
                )
                conn.commit()
            uow.rollback()

    def test_service_layer_rejects_mutation_of_published(
        self, factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        published = self._make_published(factory)
        from quantking.common.errors import DatasetVersionImmutableError

        with factory() as uow:
            with pytest.raises(DatasetVersionImmutableError):
                uow.data.save_version(published)
            uow.rollback()

    def test_failed_version_cannot_be_made_successful_via_retry(
        self, factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        from quantking.common.errors import InvalidDataTransitionError

        version = DatasetVersion.building(
            version_id=new_entity_id("data_version"),
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            code_version="abc123",
            parent_version_id=None,
            input_batch_ids=(),
            created_at=_NOW,
        )
        with factory() as uow:
            uow.data.save_version(version)
            uow.commit()
        failed = transition_dataset_version(
            version,
            DatasetVersionStatus.FAILED,
            at=_LATER,
            failure=BatchFailureInfo(error_type="Q", message="bad", retryable=False),
        )
        with factory() as uow:
            uow.data.save_version(failed)
            uow.commit()
        # Retry must NOT reuse the failed id; attempting to transition it raises.
        with pytest.raises(InvalidDataTransitionError):
            transition_dataset_version(
                failed, DatasetVersionStatus.VALIDATED, at=_LATER, manifest_sha256=_SHA
            )


class TestSourceOverrideAndAudit:
    def test_approve_and_apply_override_appends_audit(
        self, factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        override = SourceOverride.pending(
            override_id=new_entity_id("data_override"),
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            primary_source=DataSource.XTQUANT,
            fallback_source=DataSource.BAOSTOCK,
            scope_codes=("000001.SZ",),
            scope_dates=(_TRADE_DATE,),
            fields=("close",),
            reason="missing bar",
            created_at=_NOW,
        )
        with factory() as uow:
            uow.data.save_override(override)
            uow.commit()
        approved = transition_override(
            override,
            SourceOverrideStatus.APPROVED,
            at=_LATER,
            actor="analyst",
        )
        with factory() as uow:
            uow.data.save_override(approved)
            uow.data.append_audit(
                actor="analyst",
                entity=str(override.override_id),
                action="override_approved",
                correlation_id=str(override.override_id),
                occurred_at=_LATER,
                details={"actor": "analyst"},
            )
            uow.commit()
        new_version_id = new_entity_id("data_version")
        applied = transition_override(
            approved,
            SourceOverrideStatus.APPLIED,
            at=_LATER,
            actor="analyst",
            applied_version_id=new_version_id,
        )
        with factory() as uow:
            uow.data.save_override(applied)
            uow.data.append_audit(
                actor="analyst",
                entity=str(override.override_id),
                action="override_applied",
                correlation_id=str(override.override_id),
                occurred_at=_LATER,
                details={"new_version": str(new_version_id)},
            )
            uow.commit()
        with factory() as uow:
            loaded = uow.data.get_override(str(override.override_id))
            audits = uow.data.list_audits(action="override_approved", limit=10)
            uow.rollback()
        assert loaded.status is SourceOverrideStatus.APPLIED
        assert loaded.applied_version_id == new_version_id
        assert any(a.action == "override_approved" for a in audits)

    def test_applied_override_immutable_at_db_level(
        self, factory: DataSqliteUnitOfWorkFactory
    ) -> None:
        override = SourceOverride.pending(
            override_id=new_entity_id("data_override"),
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            primary_source=DataSource.XTQUANT,
            fallback_source=DataSource.BAOSTOCK,
            scope_codes=("000001.SZ",),
            scope_dates=(_TRADE_DATE,),
            fields=("close",),
            reason="missing",
            created_at=_NOW,
        )
        approved = transition_override(
            override,
            SourceOverrideStatus.APPROVED,
            at=_NOW,
            actor="analyst",
        )
        applied = transition_override(
            approved,
            SourceOverrideStatus.APPLIED,
            at=_LATER,
            actor="analyst",
            applied_version_id=new_entity_id("data_version"),
        )
        with factory() as uow:
            uow.data.save_override(override)
            uow.data.save_override(approved)
            uow.data.save_override(applied)
            uow.commit()
        with factory() as uow:
            engine = uow.data._session.bind
            with engine.connect() as conn, pytest.raises(IntegrityError):
                conn.exec_driver_sql(
                    "UPDATE source_overrides SET status='PENDING' WHERE override_id=?",
                    (str(override.override_id),),
                )
                conn.commit()
            uow.rollback()


class TestNoParquetInSqlite:
    def test_no_blob_or_parquet_columns(self, tmp_path: Path) -> None:
        database = tmp_path / "cols.db"
        initialize_database(database)
        engine = create_data_engine(database, busy_timeout_ms=250)
        inspector = inspect(engine)
        data_tables = {
            "ingestion_batches",
            "dataset_versions",
            "quality_runs",
            "difference_reports",
            "source_overrides",
        }
        for table in data_tables:
            columns = {col["name"]: col["type"] for col in inspector.get_columns(table)}
            for name, col_type in columns.items():
                rendered = str(col_type).upper()
                assert "BLOB" not in rendered, f"{table}.{name} is BLOB (no Parquet row data)"
        engine.dispose()


class TestOrmDomainSeparation:
    def test_domain_models_do_not_import_sqlalchemy(self) -> None:
        import quantking.data.models as data_models

        module = data_models
        # The domain module must not import sqlalchemy or alembic.
        source = open(module.__file__, encoding="utf-8").read()  # noqa: SIM115
        assert "sqlalchemy" not in source
        assert "import alembic" not in source
        assert "from alembic" not in source

    def test_orm_rows_are_not_reexported_as_domain(self) -> None:
        # Domain package __init__ must not expose ORM row classes.
        import quantking.data as data_pkg

        for name in ("IngestionBatchRow", "DatasetVersionRow", "Base"):
            assert not hasattr(data_pkg, name), f"ORM row {name} leaked into domain package"
