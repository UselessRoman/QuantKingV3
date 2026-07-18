"""S1-02-R1 red tests: application-level publish coordinator and failure evidence.

These tests target the S1-02-R1 requirements about the unique Canonical
publish coordination service and failure evidence preservation (section
三, 七, 八 items 8, 9, 14). They MUST fail against the S1-02 baseline
(``3235aba``) and pass after R1 lands:

8. The publish coordinator service must write a ``DatasetVersion`` whose
   ``manifest_sha256`` and coverage metadata equal the actual on-disk
   manifest. Callers must not be able to forge these values.
9. If the SQLite commit fails after the file publish (between
   BUILDING->VALIDATED or VALIDATED->PUBLISHED), a re-coordination with
   the exact same request must succeed without rewriting the published
   files.
14. A failed canonical publish must leave a ``.incomplete`` directory
    that contains ``input_listing.json``, a sanitized ``failure.json``
    (no absolute paths, no raw exception text), and the final published
    directory must NOT be deleted by a recovery path.

All tests use only ``tmp_path`` and synthetic data.
"""

from __future__ import annotations

import json
import re
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
    CanonicalWarehouseError,
)
from quantking.data.adapters.warehouse.manifest import (
    WAREHOUSE_FAILURE_FILENAME,
    WAREHOUSE_MANIFEST_FILENAME,
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


def _request(version: DatasetVersion) -> CanonicalPublishRequest:
    return CanonicalPublishRequest(
        version=version,
        tables=(_canonical_daily_bar_table(),),
        code_version="s1-02",
        rule_version="quality-rules.v1",
    )


# ---------------------------------------------------------------------------
# Red test 8: coordinator writes SHA and coverage equal to actual manifest
# ---------------------------------------------------------------------------


class TestPublishCoordinatorWritesActualManifestCredentials:
    """Red test 8: coordinator must source SHA/coverage from the manifest itself."""

    def test_coordinator_publishes_version_with_actual_manifest_sha(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        # Lazy import: the coordinator module does not exist in the S1-02
        # baseline; importing here makes the test RED until R1 lands.
        from quantking.data.adapters.warehouse.publish_coordinator import (
            CanonicalPublishCoordinator,
            CanonicalPublishCoordinatorRequest,
        )

        warehouse = CanonicalWarehouse(data_root=tmp_path)
        coordinator = CanonicalPublishCoordinator(warehouse=warehouse, uow_factory=uow_factory)
        version = _building_version()
        result = coordinator.coordinate(
            CanonicalPublishCoordinatorRequest(
                version=version,
                tables=(_canonical_daily_bar_table(),),
                code_version="s1-02",
                rule_version="quality-rules.v1",
            )
        )
        # The returned DatasetVersion must be PUBLISHED and carry the
        # exact manifest_sha256 of the on-disk manifest.
        assert result.version.status is DatasetVersionStatus.PUBLISHED
        landed = result.directory
        manifest = json.loads((landed / WAREHOUSE_MANIFEST_FILENAME).read_text(encoding="utf-8"))
        assert result.version.manifest_sha256 == manifest["manifest_sha256"]
        # The coordinator must source security_count and row_count from
        # the actual manifest's coverage_summary, not from caller input.
        assert result.version.security_count == 1
        assert result.version.row_count == 1

    def test_coordinator_rejects_caller_supplied_manifest_sha(
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
        # Callers must NOT be allowed to forge the manifest SHA; the
        # coordinator request type therefore does not accept the field.
        with pytest.raises((TypeError, ValueError)):
            coordinator.coordinate(
                CanonicalPublishCoordinatorRequest(
                    version=version,
                    tables=(_canonical_daily_bar_table(),),
                    code_version="s1-02",
                    rule_version="quality-rules.v1",
                    manifest_sha256="0" * 64,  # type: ignore[call-arg]
                )
            )

    @pytest.mark.parametrize(
        ("version_kwargs", "request_kwargs"),
        [
            ({"schema_version": "daily_bar_unadjusted.v999"}, {}),
            ({"code_version": "version-code"}, {"code_version": "request-code"}),
            ({"rule_version": "version-rules"}, {"rule_version": "request-rules"}),
        ],
    )
    def test_coordinator_rejects_version_credential_mismatch_before_file_publish(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
        version_kwargs: dict[str, str],
        request_kwargs: dict[str, str],
    ) -> None:
        from quantking.data.adapters.warehouse.publish_coordinator import (
            CanonicalPublishCoordinator,
            CanonicalPublishCoordinatorRequest,
        )

        version = DatasetVersion.building(
            version_id=new_entity_id("data_version"),
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version=version_kwargs.get("schema_version", "daily_bar_unadjusted.v1"),
            code_version=version_kwargs.get("code_version", "s1-02"),
            rule_version=version_kwargs.get("rule_version", "quality-rules.v1"),
            parent_version_id=None,
            input_batch_ids=(),
            created_at=utc_now(),
        )
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        coordinator = CanonicalPublishCoordinator(warehouse=warehouse, uow_factory=uow_factory)
        request = CanonicalPublishCoordinatorRequest(
            version=version,
            tables=(_canonical_daily_bar_table(),),
            code_version=request_kwargs.get("code_version", version.code_version),
            rule_version=request_kwargs.get("rule_version", version.rule_version),
        )
        with pytest.raises(CanonicalWarehouseError):
            coordinator.coordinate(request)
        assert not (tmp_path / "warehouse").exists()

    def test_coordinator_publishes_legal_empty_version_end_to_end(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        from quantking.data.adapters.warehouse.publish_coordinator import (
            CanonicalPublishCoordinator,
            CanonicalPublishCoordinatorRequest,
        )

        schema = canonical_schema("daily_bar_unadjusted")
        empty = pa.Table.from_arrays(
            [pa.array([], type=field.type) for field in schema],
            schema=schema,
        )
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        coordinator = CanonicalPublishCoordinator(warehouse=warehouse, uow_factory=uow_factory)
        result = coordinator.coordinate(
            CanonicalPublishCoordinatorRequest(
                version=_building_version(),
                tables=(empty,),
                code_version="s1-02",
                rule_version="quality-rules.v1",
                empty_result=EmptyResultInfo(
                    classification=LegalEmptyClassification.NO_TRADING_SESSION,
                    reason="calendar is closed for the requested window",
                ),
            )
        )
        assert result.version.status is DatasetVersionStatus.PUBLISHED
        assert result.version.row_count == 0
        assert result.version.security_count == 0


# ---------------------------------------------------------------------------
# Red test 9: recovery when SQLite commit fails post file-publish
# ---------------------------------------------------------------------------


class TestCoordinatorRecoveryAfterCommitFailure:
    """Red test 9: re-coordination after a post-publish SQLite failure must not overwrite files."""

    def test_recovery_after_validated_commit_failure(
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
        # Step 1: succeed the file publish manually.
        landed = warehouse.publish(_request(version))
        manifest_path = landed / WAREHOUSE_MANIFEST_FILENAME
        original_manifest_bytes = manifest_path.read_bytes()
        # Step 2: simulate a SQLite commit failure after the file was
        # already published (no PUBLISHED row exists yet). The
        # coordinator must observe the existing directory and complete
        # the metadata transitions without rewriting any file.
        coordinator = CanonicalPublishCoordinator(warehouse=warehouse, uow_factory=uow_factory)
        result = coordinator.coordinate(
            CanonicalPublishCoordinatorRequest(
                version=version,
                tables=(_canonical_daily_bar_table(),),
                code_version="s1-02",
                rule_version="quality-rules.v1",
            )
        )
        assert result.version.status is DatasetVersionStatus.PUBLISHED
        # The on-disk manifest bytes must be unchanged (no rewrite).
        assert manifest_path.read_bytes() == original_manifest_bytes

    def test_recovery_after_published_commit_failure(
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
        landed = warehouse.publish(_request(version))
        manifest_path = landed / WAREHOUSE_MANIFEST_FILENAME
        original_manifest_bytes = manifest_path.read_bytes()
        # Simulate: VALIDATED was committed but PUBLISHED commit failed.
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        with uow_factory() as uow:
            stored = uow.data.save_version(version)
            validated = transition_dataset_version(
                stored,
                DatasetVersionStatus.VALIDATED,
                at=utc_now(),
                manifest_sha256=manifest["manifest_sha256"],
                security_count=1,
                row_count=1,
                coverage_start=TradingDate(date(2026, 7, 15)),
                coverage_end=TradingDate(date(2026, 7, 15)),
                coverage_summary=dict(manifest["coverage_summary"]),
            )
            uow.data.save_version(validated)
            uow.commit()
        # Re-coordinate; the coordinator must move VALIDATED -> PUBLISHED
        # without rewriting files.
        coordinator = CanonicalPublishCoordinator(warehouse=warehouse, uow_factory=uow_factory)
        result = coordinator.coordinate(
            CanonicalPublishCoordinatorRequest(
                version=version,
                tables=(_canonical_daily_bar_table(),),
                code_version="s1-02",
                rule_version="quality-rules.v1",
            )
        )
        assert result.version.status is DatasetVersionStatus.PUBLISHED
        assert manifest_path.read_bytes() == original_manifest_bytes


# ---------------------------------------------------------------------------
# Red test 14: failure evidence preservation
# ---------------------------------------------------------------------------


_ABSOLUTE_PATH_RE = re.compile(r"[A-Za-z]:[\\/]|^/")


class TestFailureEvidencePreservation:
    """Red test 14: failed canonical publish must preserve sanitized evidence."""

    def test_failure_directory_contains_input_listing_and_sanitized_failure(
        self, tmp_path: Path
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        # Force a publish failure by injecting an exception via the
        # manifest-writing step. The failure evidence must preserve
        # ``input_listing.json`` and a sanitized ``failure.json``.
        original = CanonicalWarehouse._write_manifest_and_complete

        def _boom(self: CanonicalWarehouse, *args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated catastrophic failure with sensitive path C:\\secret\\key")

        CanonicalWarehouse._write_manifest_and_complete = _boom  # type: ignore[method-assign]
        try:
            with pytest.raises(CanonicalWarehouseError):
                warehouse.publish(_request(version))
        finally:
            CanonicalWarehouse._write_manifest_and_complete = original  # type: ignore[method-assign]
        from quantking.data.adapters.warehouse.manifest import path_safe_id

        final = (
            tmp_path / "warehouse" / "daily_bar_unadjusted" / path_safe_id(str(version.version_id))
        )
        incomplete = final.with_name(final.name + ".incomplete")
        # The incomplete directory must exist.
        assert incomplete.is_dir()
        # The input listing must be preserved for forensic review.
        assert (incomplete / "input_listing.json").is_file()
        # The failure.json must be preserved and sanitized.
        failure_path = incomplete / WAREHOUSE_FAILURE_FILENAME
        assert failure_path.is_file()
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        # The failure record must carry the version_id, dataset, code/rule
        # version, schema version, input batches and failure time.
        assert failure["version_id"] == str(version.version_id)
        assert failure["dataset"] == "daily_bar_unadjusted"
        assert failure["failed_at"]
        # Sanitization: no absolute path may appear in any value.
        for key, value in failure.items():
            if isinstance(value, str):
                assert not _ABSOLUTE_PATH_RE.search(value), (
                    f"failure.{key} leaks an absolute path: {value!r}"
                )
                # The raw exception text must NOT be propagated verbatim.
                assert "C:\\secret\\key" not in value
        # The final published directory must NOT exist (no recovery overwrite).
        assert not final.exists()

    def test_failure_directory_not_deleted_after_failure(self, tmp_path: Path) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        original = CanonicalWarehouse._write_manifest_and_complete

        def _boom(self: CanonicalWarehouse, *args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated mid-publish failure")

        CanonicalWarehouse._write_manifest_and_complete = _boom  # type: ignore[method-assign]
        try:
            with pytest.raises(CanonicalWarehouseError):
                warehouse.publish(_request(version))
        finally:
            CanonicalWarehouse._write_manifest_and_complete = original  # type: ignore[method-assign]
        from quantking.data.adapters.warehouse.manifest import path_safe_id

        final = (
            tmp_path / "warehouse" / "daily_bar_unadjusted" / path_safe_id(str(version.version_id))
        )
        incomplete = final.with_name(final.name + ".incomplete")
        # The incomplete directory must still exist (no rmtree recovery).
        assert incomplete.is_dir()
        assert (incomplete / WAREHOUSE_FAILURE_FILENAME).is_file()


__all__ = [
    "TestCoordinatorRecoveryAfterCommitFailure",
    "TestFailureEvidencePreservation",
    "TestPublishCoordinatorWritesActualManifestCredentials",
]
