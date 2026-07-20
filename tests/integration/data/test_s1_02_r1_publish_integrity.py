"""S1-02-R1 red tests: publish integrity and unified verifier.

These tests target the S1-02-R1 requirements about idempotent republish
safety, manifest/COMPLETE tampering, manifest row_count validation and
duplicate manifest paths (section 四, 五, 八 items 3-5, 12-13). They MUST
fail against the S1-02 baseline (``3235aba``) and pass after R1 lands:

3. After a Raw file changes on disk, the same publish request must NOT
   idempotently succeed.
4. After a Canonical Parquet file changes on disk, the same publish
   request must NOT idempotently succeed.
5. manifest/COMPLETE tampering, missing fields or wrong types must be
   rejected by both the publisher reconcile and the query adapter.
12. manifest ``row_count`` not matching the actual Parquet row count
   must be rejected.
13. Duplicate manifest ``relative_path`` entries and unexpected extra
   files must be rejected.

All tests use only ``tmp_path`` and synthetic data.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest
from pyarrow import parquet as pa_parquet

from quantking.common.errors import IdempotencyConflictError
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
from quantking.data.adapters.warehouse.errors import (
    CanonicalWarehouseError,
    ManifestIntegrityError,
)
from quantking.data.adapters.warehouse.manifest import (
    WAREHOUSE_COMPLETE_FILENAME,
    WAREHOUSE_FAILURE_FILENAME,
    WAREHOUSE_MANIFEST_FILENAME,
    compute_sha256,
)
from quantking.data.adapters.warehouse.query import (
    CanonicalQueryAdapter,
    CanonicalQueryRequest,
    DatasetVersionNotQueryableError,
)
from quantking.data.adapters.warehouse.raw_store import (
    RawBatchPublisher,
    RawBatchPublishRequest,
    RawManifestFile,
)
from quantking.data.adapters.warehouse.verifier import verify_canonical_directory
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file(name: str, payload: bytes) -> RawManifestFile:
    return RawManifestFile(relative_path=name, payload=payload, row_count=0)


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


def _resign_manifest(landed: Path, payload: dict[str, object]) -> None:
    without_sha = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    new_sha = hashlib.sha256(
        json.dumps(
            without_sha,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    payload["manifest_sha256"] = new_sha
    (landed / WAREHOUSE_MANIFEST_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    complete_path = landed / WAREHOUSE_COMPLETE_FILENAME
    complete = json.loads(complete_path.read_text(encoding="utf-8"))
    complete["manifest_sha256"] = new_sha
    complete_path.write_text(
        json.dumps(complete, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )


@pytest.fixture()
def uow_factory(tmp_path: Path) -> DataSqliteUnitOfWorkFactory:
    database = tmp_path / "data.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    yield factory
    engine.dispose()


def _calendar_table() -> pa.Table:
    """Build an exchange_calendar reference table covering the test rows."""
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
) -> Path:
    landed = warehouse.publish(
        CanonicalPublishRequest(
            version=version,
            tables=(_canonical_daily_bar_table(),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
    )
    # Persist a PASSED quality run bound to the candidate content so
    # the SQLite trigger allows the PUBLISHED transition.
    _make_passed_quality_run(
        version,
        _canonical_daily_bar_table(),
        uow_factory=uow_factory,
        artifacts_root=warehouse.data_root / "artifacts",
    )
    with uow_factory() as uow:
        stored = uow.data.save_version(version)
        manifest = json.loads((landed / WAREHOUSE_MANIFEST_FILENAME).read_text(encoding="utf-8"))
        validated = transition_dataset_version(
            stored,
            DatasetVersionStatus.VALIDATED,
            at=utc_now(),
            manifest_sha256=manifest["manifest_sha256"],
            security_count=1,
            row_count=1,
            coverage_start=TradingDate(date(2026, 7, 15)),
            coverage_end=TradingDate(date(2026, 7, 15)),
            coverage_summary={"date_count": "1", "security_count": "1"},
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
# Red test 3: Raw file change must not idempotently succeed
# ---------------------------------------------------------------------------


class TestRawFileChangeBreaksIdempotency:
    """Red test 3: file mutation on disk must break idempotent reconcile."""

    def test_mutated_payload_raises_conflict(self, tmp_path: Path) -> None:
        publisher = RawBatchPublisher(data_root=tmp_path)
        request = RawBatchPublishRequest(
            source="xtquant",
            dataset="daily_bar_unadjusted",
            batch_id="b-2026-07-16-r1-001",
            adapter_version="xtquant-probe-1",
            request_params={"code": "000001.SZ"},
            files=(_file("rows/000001.SZ.parquet", b"AAA"),),
        )
        publisher.publish(request)
        final = tmp_path / "raw" / "xtquant" / "daily_bar_unadjusted" / request.batch_id
        # Mutate the on-disk payload after the successful publish. The
        # next idempotent reconcile must detect the byte-level drift and
        # raise IdempotencyConflictError rather than silently succeeding.
        target = final / "rows" / "000001.SZ.parquet"
        target.write_bytes(b"BBB")
        with pytest.raises(IdempotencyConflictError):
            publisher.publish(request)


# ---------------------------------------------------------------------------
# Red test 4: Canonical Parquet change must not idempotently succeed
# ---------------------------------------------------------------------------


class TestCanonicalParquetChangeBreaksIdempotency:
    """Red test 4: Parquet mutation on disk must break idempotent reconcile."""

    def test_mutated_parquet_raises_conflict(self, tmp_path: Path) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        request = CanonicalPublishRequest(
            version=version,
            tables=(_canonical_daily_bar_table(),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        landed = warehouse.publish(request)
        # Mutate the Parquet file on disk. The next idempotent reconcile
        # must compare actual file SHA-256 against the manifest and reject.
        part = next(landed.glob("year=*/month=*/part-*.parquet"))
        original = part.read_bytes()
        # Flip a single byte in the middle of the Parquet payload. Even
        # if the file remains a valid Parquet file, the SHA must differ.
        mutated = bytearray(original)
        mutated[len(mutated) // 2] ^= 0xFF
        part.write_bytes(bytes(mutated))
        with pytest.raises(IdempotencyConflictError):
            warehouse.publish(request)


# ---------------------------------------------------------------------------
# Red test 5: manifest/COMPLETE tampering rejected by reconcile AND query
# ---------------------------------------------------------------------------


class TestManifestCompleteTamperingRejected:
    """Red test 5: tampered manifest/COMPLETE must be rejected everywhere."""

    def test_reconcile_rejects_tampered_manifest_bytes(self, tmp_path: Path) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        request = CanonicalPublishRequest(
            version=version,
            tables=(_canonical_daily_bar_table(),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        landed = warehouse.publish(request)
        # Append a single space to the manifest bytes. The reconcile
        # must reject the non-canonical bytes rather than re-using the
        # directory as an idempotent success.
        manifest_path = landed / WAREHOUSE_MANIFEST_FILENAME
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
        with pytest.raises((IdempotencyConflictError, Exception)):
            warehouse.publish(request)

    def test_reconcile_rejects_missing_manifest_sha_field(self, tmp_path: Path) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        request = CanonicalPublishRequest(
            version=version,
            tables=(_canonical_daily_bar_table(),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        landed = warehouse.publish(request)
        manifest_path = landed / WAREHOUSE_MANIFEST_FILENAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload.pop("manifest_sha256", None)
        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        with pytest.raises((IdempotencyConflictError, Exception)):
            warehouse.publish(request)

    def test_reconcile_rejects_wrong_type_manifest_sha(self, tmp_path: Path) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        request = CanonicalPublishRequest(
            version=version,
            tables=(_canonical_daily_bar_table(),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        landed = warehouse.publish(request)
        manifest_path = landed / WAREHOUSE_MANIFEST_FILENAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Replace the SHA string with an integer; the type check must reject.
        payload["manifest_sha256"] = 12345
        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        with pytest.raises((IdempotencyConflictError, Exception)):
            warehouse.publish(request)

    def test_reconcile_rejects_tampered_complete_marker(self, tmp_path: Path) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        request = CanonicalPublishRequest(
            version=version,
            tables=(_canonical_daily_bar_table(),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        landed = warehouse.publish(request)
        # Overwrite COMPLETE with a payload whose manifest_sha256 differs
        # from the on-disk manifest. The reconcile must reject.
        complete_path = landed / WAREHOUSE_COMPLETE_FILENAME
        complete_path.write_text(
            json.dumps(
                {
                    "version_id": str(version.version_id),
                    "manifest_sha256": "0" * 64,
                    "completed_at": "2026-07-16T00:00:00Z",
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        with pytest.raises((IdempotencyConflictError, Exception)):
            warehouse.publish(request)

    def test_query_rejects_complete_with_missing_manifest_sha(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        landed = _publish_and_mark(warehouse, uow_factory, version)
        complete_path = landed / WAREHOUSE_COMPLETE_FILENAME
        payload = json.loads(complete_path.read_text(encoding="utf-8"))
        # Remove the manifest_sha256 field entirely. The query adapter
        # must treat a missing SHA as an integrity failure rather than
        # silently accepting the marker.
        payload.pop("manifest_sha256", None)
        complete_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(DatasetVersionNotQueryableError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id=str(version.version_id),
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                )
            )

    def test_query_rejects_complete_with_wrong_type_manifest_sha(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        landed = _publish_and_mark(warehouse, uow_factory, version)
        complete_path = landed / WAREHOUSE_COMPLETE_FILENAME
        payload = json.loads(complete_path.read_text(encoding="utf-8"))
        payload["manifest_sha256"] = 12345
        complete_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
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
# Red test 12: manifest row_count != actual Parquet row count rejected
# ---------------------------------------------------------------------------


class TestManifestRowCountMismatch:
    """Red test 12: manifest row_count must match the actual Parquet rows."""

    def test_query_rejects_wrong_row_count(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        landed = _publish_and_mark(warehouse, uow_factory, version)
        manifest_path = landed / WAREHOUSE_MANIFEST_FILENAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Bump the file row_count from 1 to 999. The actual Parquet
        # file only has 1 row, so the verifier must reject.
        payload["files"][0]["row_count"] = 999
        # Re-compute the manifest_sha256 because we changed the payload.
        without_sha = {k: v for k, v in payload.items() if k != "manifest_sha256"}
        new_sha = hashlib.sha256(
            json.dumps(
                without_sha, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        ).hexdigest()
        payload["manifest_sha256"] = new_sha
        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        # Also update the COMPLETE marker so the SHA binding check does
        # not pre-empt the row_count check.
        complete_path = landed / WAREHOUSE_COMPLETE_FILENAME
        complete_payload = json.loads(complete_path.read_text(encoding="utf-8"))
        complete_payload["manifest_sha256"] = new_sha
        complete_path.write_text(
            json.dumps(
                complete_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
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
# Red test 13: duplicate manifest paths and extra files rejected
# ---------------------------------------------------------------------------


class TestDuplicateManifestPathsAndExtraFiles:
    """Red test 13: duplicate paths in manifest and extra on-disk files rejected."""

    def test_query_rejects_duplicate_manifest_paths(
        self,
        tmp_path: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        landed = _publish_and_mark(warehouse, uow_factory, version)
        manifest_path = landed / WAREHOUSE_MANIFEST_FILENAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Duplicate the first file entry in the manifest. The verifier
        # must reject the duplicate relative_path.
        payload["files"] = [payload["files"][0], payload["files"][0]]
        without_sha = {k: v for k, v in payload.items() if k != "manifest_sha256"}
        new_sha = hashlib.sha256(
            json.dumps(
                without_sha, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        ).hexdigest()
        payload["manifest_sha256"] = new_sha
        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        complete_path = landed / WAREHOUSE_COMPLETE_FILENAME
        complete_payload = json.loads(complete_path.read_text(encoding="utf-8"))
        complete_payload["manifest_sha256"] = new_sha
        complete_path.write_text(
            json.dumps(
                complete_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
        with pytest.raises(DatasetVersionNotQueryableError):
            adapter.query(
                CanonicalQueryRequest(
                    version_id=str(version.version_id),
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
                )
            )

    def test_reconcile_rejects_extra_file_on_disk(self, tmp_path: Path) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        request = CanonicalPublishRequest(
            version=version,
            tables=(_canonical_daily_bar_table(),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        landed = warehouse.publish(request)
        # Drop a foreign Parquet file inside the published directory.
        # The reconcile must reject because the on-disk file set no
        # longer matches the manifest.
        (landed / "rogue.parquet").write_bytes(b"not in manifest")
        with pytest.raises((IdempotencyConflictError, Exception)):
            warehouse.publish(request)

    def test_verifier_rejects_two_files_for_same_partition(self, tmp_path: Path) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        landed = warehouse.publish(
            CanonicalPublishRequest(
                version=version,
                tables=(_canonical_daily_bar_table(),),
                code_version="s1-02",
                rule_version="quality-rules.v1",
            )
        )
        manifest_path = landed / WAREHOUSE_MANIFEST_FILENAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        original_entry = dict(payload["files"][0])
        original_path = landed / str(original_entry["relative_path"])
        duplicate_relative = "year=2026/month=07/part-99999.parquet"
        duplicate_path = landed / duplicate_relative
        duplicate_path.write_bytes(original_path.read_bytes())
        duplicate_entry = dict(original_entry)
        duplicate_entry["relative_path"] = duplicate_relative
        payload["files"].append(duplicate_entry)
        _resign_manifest(landed, payload)
        with pytest.raises(ManifestIntegrityError):
            verify_canonical_directory(landed, data_root=tmp_path)


class TestStrictCanonicalManifestVerification:
    def _publish(self, tmp_path: Path) -> Path:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        return warehouse.publish(
            CanonicalPublishRequest(
                version=_building_version(),
                tables=(_canonical_daily_bar_table(),),
                code_version="s1-02",
                rule_version="quality-rules.v1",
            )
        )

    def test_verifier_rejects_full_arrow_schema_mismatch(self, tmp_path: Path) -> None:
        landed = self._publish(tmp_path)
        manifest_path = landed / WAREHOUSE_MANIFEST_FILENAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = payload["files"][0]
        parquet_path = landed / str(entry["relative_path"])
        table = pa_parquet.ParquetFile(parquet_path).read()
        arrays: list[pa.ChunkedArray] = []
        fields: list[pa.Field] = []
        for field in table.schema:
            column = table.column(field.name)
            if field.name == "open":
                column = column.cast(pa.string())
                fields.append(pa.field(field.name, pa.string(), nullable=field.nullable))
            else:
                fields.append(field)
            arrays.append(column)
        wrong_schema = pa.schema(fields)
        wrong_table = pa.Table.from_arrays(arrays, schema=wrong_schema)
        pa_parquet.write_table(wrong_table, parquet_path)
        raw = parquet_path.read_bytes()
        entry["size"] = len(raw)
        entry["sha256"] = compute_sha256(raw)
        _resign_manifest(landed, payload)
        with pytest.raises(ManifestIntegrityError):
            verify_canonical_directory(landed, data_root=tmp_path)

    def test_verifier_rejects_manifest_missing_required_field(self, tmp_path: Path) -> None:
        landed = self._publish(tmp_path)
        manifest_path = landed / WAREHOUSE_MANIFEST_FILENAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload.pop("parent_version_id")
        _resign_manifest(landed, payload)
        with pytest.raises(ManifestIntegrityError):
            verify_canonical_directory(landed, data_root=tmp_path)

    def test_verifier_rejects_summary_row_count_drift(self, tmp_path: Path) -> None:
        landed = self._publish(tmp_path)
        manifest_path = landed / WAREHOUSE_MANIFEST_FILENAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["coverage_summary"]["row_count"] = "999"
        _resign_manifest(landed, payload)
        with pytest.raises(ManifestIntegrityError):
            verify_canonical_directory(landed, data_root=tmp_path)

    def test_verifier_rejects_failure_marker_in_published_directory(self, tmp_path: Path) -> None:
        landed = self._publish(tmp_path)
        (landed / WAREHOUSE_FAILURE_FILENAME).write_text("{}", encoding="utf-8")
        with pytest.raises(ManifestIntegrityError):
            verify_canonical_directory(landed, data_root=tmp_path)

    def test_reconcile_rejects_parent_version_drift(self, tmp_path: Path) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        original = _building_version()
        request = CanonicalPublishRequest(
            version=original,
            tables=(_canonical_daily_bar_table(),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        warehouse.publish(request)
        changed_lineage = DatasetVersion.building(
            version_id=original.version_id,
            dataset=original.dataset,
            schema_version=original.schema_version,
            code_version=original.code_version,
            rule_version=original.rule_version,
            parent_version_id=new_entity_id("data_version"),
            input_batch_ids=(),
            created_at=original.created_at,
        )
        with pytest.raises(IdempotencyConflictError):
            warehouse.publish(
                CanonicalPublishRequest(
                    version=changed_lineage,
                    tables=(_canonical_daily_bar_table(),),
                    code_version="s1-02",
                    rule_version="quality-rules.v1",
                )
            )

    def test_expected_coverage_summary_must_match_exactly(self, tmp_path: Path) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        with pytest.raises(CanonicalWarehouseError):
            warehouse.publish(
                CanonicalPublishRequest(
                    version=_building_version(),
                    tables=(_canonical_daily_bar_table(),),
                    code_version="s1-02",
                    rule_version="quality-rules.v1",
                    coverage_summary={"unexpected": "value"},
                )
            )


__all__ = [
    "TestCanonicalParquetChangeBreaksIdempotency",
    "TestDuplicateManifestPathsAndExtraFiles",
    "TestManifestCompleteTamperingRejected",
    "TestManifestRowCountMismatch",
    "TestRawFileChangeBreaksIdempotency",
]
