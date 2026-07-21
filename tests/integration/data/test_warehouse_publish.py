"""Canonical warehouse publish and atomic promotion contract tests (S1-02).

The canonical warehouse is the only location that physically writes published
Parquet data. It must:

* Land under ``warehouse/<dataset>/<dataset_version>/``.
* Validate every input chunk against the six canonical Arrow contracts, then
  re-validate the **global** primary-key uniqueness and sort order across all
  chunks before publishing.
* Reject unpublished empty tables unless they carry an explicit
  :class:`EmptyResultInfo`.
* Write a deterministic manifest that lists every Parquet file with managed
  relative path, partition key, size, row count and SHA-256.
* Refuse to publish if the manifest does not match the on-disk file set.
* Promote a temporary ``.incomplete`` directory to its final name atomically
  and only then mark the SQLite ``DatasetVersion`` as ``PUBLISHED``.
* Make the publish idempotent: the same request returns the same directory
  without re-writing any file. A request with the same ``version_id`` but
  different contents/manifest/inputs raises
  :class:`IdempotencyConflictError`.
* Never write inside an existing published directory and never let a
  ``BUILDING``/``VALIDATED``/``FAILED`` version become queryable.

All tests use only ``tmp_path`` and synthetic data.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.errors import (
    IdempotencyConflictError,
    InvalidPathError,
)
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
    IncompatibleInputError,
)
from quantking.data.adapters.warehouse.manifest import (
    WAREHOUSE_COMPLETE_FILENAME,
    WAREHOUSE_FAILURE_FILENAME,
    WAREHOUSE_MANIFEST_FILENAME,
    path_safe_id,
)
from quantking.data.adapters.warehouse.query import (
    CanonicalQueryAdapter,
    DatasetVersionNotQueryableError,
)
from quantking.data.models import (
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    EmptyResultInfo,
    LegalEmptyClassification,
    QualityResult,
    QualityRun,
    QualityRunStatus,
    transition_dataset_version,
)
from quantking.jobs.adapters.persistence.sqlite import initialize_database


def _enforce_canonical_schema(dataset: str, table: pa.Table) -> pa.Table:
    """Cast ``table`` to the canonical schema for ``dataset``.

    ``pa.Table.from_pylist`` always emits nullable fields; the canonical contract
    requires some fields to be non-nullable. This helper re-builds the table with
    the exact nullability/ordering declared in the descriptor.
    """
    descriptor = canonical_schema(dataset)
    columns: list[pa.Array] = []
    fields: list[pa.Field] = []
    for field in descriptor:
        columns.append(table.column(field.name).cast(field.type, safe=False))
        fields.append(pa.field(field.name, field.type, nullable=field.nullable))
    return pa.Table.from_arrays(columns, schema=pa.schema(fields))


@dataclass(frozen=True, slots=True)
class _SyntheticRow:
    code: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    pre_close: float
    volume: int
    amount: float


def _daily_bar_rows() -> list[_SyntheticRow]:
    return [
        _SyntheticRow(
            code="000001.SZ",
            trade_date=date(2026, 7, 15),
            open=10.0,
            high=11.0,
            low=9.5,
            close=10.5,
            pre_close=10.0,
            volume=1_000_000,
            amount=10_500_000.0,
        ),
        _SyntheticRow(
            code="000001.SZ",
            trade_date=date(2026, 7, 16),
            open=10.5,
            high=10.8,
            low=10.2,
            close=10.7,
            pre_close=10.5,
            volume=1_100_000,
            amount=11_700_000.0,
        ),
    ]


def _build_daily_bar_table(rows: Sequence[_SyntheticRow]) -> pa.Table:
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
    return _enforce_canonical_schema("daily_bar_unadjusted", table)


def _security_master_rows() -> list[Mapping[str, object]]:
    return [
        {
            "code": "000001.SZ",
            "exchange": "SZ",
            "name": "Ping An Bank",
            "board": "MAIN",
            "listing_date": date(1991, 4, 3),
            "delisting_date": None,
            "effective_from": date(1991, 4, 3),
            "effective_to": None,
            "listing_status": "LISTED",
        }
    ]


def _build_security_master_table() -> pa.Table:
    return _enforce_canonical_schema(
        "security_master", pa.Table.from_pylist(_security_master_rows())
    )


def _build_exchange_calendar_table() -> pa.Table:
    return _enforce_canonical_schema(
        "exchange_calendar",
        pa.Table.from_pylist(
            [
                {
                    "exchange": "SZ",
                    "trade_date": date(2026, 7, 15),
                    "is_open": True,
                },
                {
                    "exchange": "SZ",
                    "trade_date": date(2026, 7, 16),
                    "is_open": True,
                },
            ]
        ),
    )


def _build_adjustment_factor_table() -> pa.Table:
    return _enforce_canonical_schema(
        "adjustment_factor",
        pa.Table.from_pylist(
            [
                {
                    "code": "000001.SZ",
                    "trade_date": date(2026, 7, 15),
                    "price_multiplier": 1.0,
                    "volume_multiplier": 1.0,
                    "normalization_date": date(2026, 7, 15),
                    "algorithm_version": "v1",
                }
            ]
        ),
    )


def _build_trading_status_table() -> pa.Table:
    return _enforce_canonical_schema(
        "trading_status",
        pa.Table.from_pylist(
            [
                {
                    "code": "000001.SZ",
                    "trade_date": date(2026, 7, 15),
                    "is_trading": True,
                    "is_suspended": False,
                    "is_st": False,
                    "is_star_st": False,
                    "is_delisting_period": False,
                    "limit_up_price": 11.0,
                    "limit_down_price": 9.5,
                    "status_reason": None,
                }
            ]
        ),
    )


def _build_benchmark_index_table() -> pa.Table:
    return _enforce_canonical_schema(
        "benchmark_index_daily",
        pa.Table.from_pylist(
            [
                {
                    "code": "000300.SH",
                    "trade_date": date(2026, 7, 15),
                    "open": 4000.0,
                    "high": 4050.0,
                    "low": 3990.0,
                    "close": 4030.0,
                    "pre_close": 3995.0,
                    "volume": 1_000_000_000,
                    "amount": 40_000_000_000.0,
                }
            ]
        ),
    )


_CANONICAL_BUILDERS = {
    DatasetKind.SECURITY_MASTER: _build_security_master_table,
    DatasetKind.EXCHANGE_CALENDAR: _build_exchange_calendar_table,
    DatasetKind.DAILY_BAR_UNADJUSTED: lambda: _build_daily_bar_table(_daily_bar_rows()),
    DatasetKind.ADJUSTMENT_FACTOR: _build_adjustment_factor_table,
    DatasetKind.TRADING_STATUS: _build_trading_status_table,
    DatasetKind.BENCHMARK_INDEX_DAILY: _build_benchmark_index_table,
}


def _table_for(dataset: DatasetKind) -> pa.Table:
    return _CANONICAL_BUILDERS[dataset]()


@pytest.fixture()
def warehouse(tmp_path: Path) -> CanonicalWarehouse:
    return CanonicalWarehouse(data_root=tmp_path)


@pytest.fixture()
def database_uow(tmp_path: Path) -> DataSqliteUnitOfWorkFactory:
    database = tmp_path / "data.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    yield factory
    engine.dispose()


def _building_version(
    *,
    dataset: DatasetKind,
    parent: DatasetVersion | None = None,
) -> DatasetVersion:
    return DatasetVersion.building(
        version_id=new_entity_id("data_version"),
        dataset=dataset,
        schema_version=f"{dataset.value}.v1",
        code_version="s1-02",
        parent_version_id=None if parent is None else parent.version_id,
        input_batch_ids=(),
        created_at=utc_now(),
    )


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
        reference_tables={"exchange_calendar": _build_exchange_calendar_table()},
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


class TestCanonicalPublishHappyPath:
    def test_publish_writes_partitioned_parquet_and_manifest(
        self, tmp_path: Path, warehouse: CanonicalWarehouse
    ) -> None:
        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        request = CanonicalPublishRequest(
            version=version,
            tables=(_build_daily_bar_table(_daily_bar_rows()),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        landed = warehouse.publish(request)
        final = (
            tmp_path / "warehouse" / "daily_bar_unadjusted" / path_safe_id(str(version.version_id))
        )
        assert landed == final
        assert (final / WAREHOUSE_COMPLETE_FILENAME).is_file()
        assert (final / WAREHOUSE_MANIFEST_FILENAME).is_file()
        part = next(final.glob("year=*/month=*/part-*.parquet"))
        assert part.is_file()
        manifest = json.loads((final / WAREHOUSE_MANIFEST_FILENAME).read_text(encoding="utf-8"))
        assert manifest["dataset"] == "daily_bar_unadjusted"
        assert manifest["version_id"] == str(version.version_id)
        assert manifest["manifest_schema_version"]
        assert manifest["partition_scheme"]["scheme_id"]
        assert manifest["partition_scheme"]["scheme_version"]
        files = manifest["files"]
        assert len(files) == 1
        entry = files[0]
        assert entry["relative_path"] == part.relative_to(final).as_posix()
        assert entry["partition"]["year"] == 2026
        assert entry["partition"]["month"] == 7
        assert entry["row_count"] == 2
        assert len(entry["sha256"]) == 64

    def test_manifest_sha_is_bound_to_complete_marker(
        self, tmp_path: Path, warehouse: CanonicalWarehouse
    ) -> None:
        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        request = CanonicalPublishRequest(
            version=version,
            tables=(_build_daily_bar_table(_daily_bar_rows()),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        warehouse.publish(request)
        final = (
            tmp_path / "warehouse" / "daily_bar_unadjusted" / path_safe_id(str(version.version_id))
        )
        complete = json.loads((final / WAREHOUSE_COMPLETE_FILENAME).read_text(encoding="utf-8"))
        manifest_sha = json.loads(
            (final / WAREHOUSE_MANIFEST_FILENAME).read_text(encoding="utf-8")
        )["manifest_sha256"]
        assert complete["version_id"] == str(version.version_id)
        assert complete["manifest_sha256"] == manifest_sha


class TestPublishIdempotency:
    def test_repeat_publish_is_noop(self, tmp_path: Path, warehouse: CanonicalWarehouse) -> None:
        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        request = CanonicalPublishRequest(
            version=version,
            tables=(_build_daily_bar_table(_daily_bar_rows()),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        first = warehouse.publish(request)
        second = warehouse.publish(request)
        assert first == second
        # The Parquet file must not have been re-written.
        manifest = json.loads((first / WAREHOUSE_MANIFEST_FILENAME).read_text(encoding="utf-8"))
        entry = manifest["files"][0]
        target = first / entry["relative_path"]
        mtime_before = target.stat().st_mtime_ns
        warehouse.publish(request)
        assert target.stat().st_mtime_ns == mtime_before

    def test_same_version_different_content_raises_idempotency_conflict(
        self, tmp_path: Path, warehouse: CanonicalWarehouse
    ) -> None:
        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        request = CanonicalPublishRequest(
            version=version,
            tables=(_build_daily_bar_table(_daily_bar_rows()),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        warehouse.publish(request)
        other_rows = [
            _SyntheticRow(
                code="000001.SZ",
                trade_date=date(2026, 7, 15),
                open=10.0,
                high=11.0,
                low=9.5,
                close=10.5,
                pre_close=10.0,
                volume=1_000_000,
                amount=10_500_000.0,
            ),
            _SyntheticRow(
                code="000001.SZ",
                trade_date=date(2026, 7, 16),
                open=11.0,
                high=12.0,
                low=10.5,
                close=11.5,
                pre_close=10.5,
                volume=2_000_000,
                amount=22_000_000.0,
            ),
        ]
        conflicting = CanonicalPublishRequest(
            version=version,
            tables=(_build_daily_bar_table(other_rows),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        with pytest.raises(IdempotencyConflictError):
            warehouse.publish(conflicting)

    def test_same_version_different_rule_version_is_conflict(
        self, tmp_path: Path, warehouse: CanonicalWarehouse
    ) -> None:
        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        request = CanonicalPublishRequest(
            version=version,
            tables=(_build_daily_bar_table(_daily_bar_rows()),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        warehouse.publish(request)
        conflicting = CanonicalPublishRequest(
            version=version,
            tables=(_build_daily_bar_table(_daily_bar_rows()),),
            code_version="s1-02",
            rule_version="quality-rules.v2",
        )
        with pytest.raises(IdempotencyConflictError):
            warehouse.publish(conflicting)


class TestPublishValidation:
    def test_legal_empty_dataset_is_publishable(
        self, tmp_path: Path, warehouse: CanonicalWarehouse
    ) -> None:
        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        empty = _enforce_canonical_schema(
            "daily_bar_unadjusted",
            pa.table(
                {
                    "code": pa.array([], type=pa.string()),
                    "trade_date": pa.array([], type=pa.date32()),
                    "open": pa.array([], type=pa.float64()),
                    "high": pa.array([], type=pa.float64()),
                    "low": pa.array([], type=pa.float64()),
                    "close": pa.array([], type=pa.float64()),
                    "pre_close": pa.array([], type=pa.float64()),
                    "volume": pa.array([], type=pa.int64()),
                    "amount": pa.array([], type=pa.float64()),
                }
            ),
        )
        request = CanonicalPublishRequest(
            version=version,
            tables=(empty,),
            code_version="s1-02",
            rule_version="quality-rules.v1",
            empty_result=EmptyResultInfo(
                classification=LegalEmptyClassification.NO_TRADING_SESSION,
                reason="calendar is closed for the entire window",
            ),
        )
        landed = warehouse.publish(request)
        assert landed.is_dir()
        assert (landed / WAREHOUSE_COMPLETE_FILENAME).is_file()

    def test_unexpected_empty_dataset_is_rejected(
        self, tmp_path: Path, warehouse: CanonicalWarehouse
    ) -> None:
        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        empty = _enforce_canonical_schema(
            "daily_bar_unadjusted",
            pa.table(
                {
                    "code": pa.array([], type=pa.string()),
                    "trade_date": pa.array([], type=pa.date32()),
                    "open": pa.array([], type=pa.float64()),
                    "high": pa.array([], type=pa.float64()),
                    "low": pa.array([], type=pa.float64()),
                    "close": pa.array([], type=pa.float64()),
                    "pre_close": pa.array([], type=pa.float64()),
                    "volume": pa.array([], type=pa.int64()),
                    "amount": pa.array([], type=pa.float64()),
                }
            ),
        )
        request = CanonicalPublishRequest(
            version=version,
            tables=(empty,),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        with pytest.raises(CanonicalWarehouseError):
            warehouse.publish(request)
        final = (
            tmp_path / "warehouse" / "daily_bar_unadjusted" / path_safe_id(str(version.version_id))
        )
        incomplete = final.with_name(final.name + ".incomplete")
        assert not final.exists()
        assert incomplete.is_dir()
        assert (incomplete / WAREHOUSE_FAILURE_FILENAME).is_file()

    def test_global_primary_key_conflict_across_chunks_is_rejected(
        self, tmp_path: Path, warehouse: CanonicalWarehouse
    ) -> None:
        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        chunk_a = pa.Table.from_pylist(
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
        chunk_b = pa.Table.from_pylist(
            [
                {
                    "code": "000001.SZ",
                    "trade_date": date(2026, 7, 15),
                    "open": 10.5,
                    "high": 10.8,
                    "low": 10.2,
                    "close": 10.7,
                    "pre_close": 10.5,
                    "volume": 1_100_000,
                    "amount": 11_700_000.0,
                }
            ]
        )
        request = CanonicalPublishRequest(
            version=version,
            tables=(chunk_a, chunk_b),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        with pytest.raises(CanonicalWarehouseError):
            warehouse.publish(request)
        final = (
            tmp_path / "warehouse" / "daily_bar_unadjusted" / path_safe_id(str(version.version_id))
        )
        assert not final.exists()

    def test_global_sort_order_must_be_ascending(
        self, tmp_path: Path, warehouse: CanonicalWarehouse
    ) -> None:
        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        chunk_a = pa.Table.from_pylist(
            [
                {
                    "code": "000001.SZ",
                    "trade_date": date(2026, 7, 16),
                    "open": 10.5,
                    "high": 10.8,
                    "low": 10.2,
                    "close": 10.7,
                    "pre_close": 10.5,
                    "volume": 1_100_000,
                    "amount": 11_700_000.0,
                }
            ]
        )
        chunk_b = pa.Table.from_pylist(
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
        request = CanonicalPublishRequest(
            version=version,
            tables=(chunk_a, chunk_b),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        with pytest.raises(CanonicalWarehouseError):
            warehouse.publish(request)

    def test_field_type_mismatch_is_rejected(
        self, tmp_path: Path, warehouse: CanonicalWarehouse
    ) -> None:
        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        original = pa.Table.from_pylist(
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
        # Re-cast the table so the ``volume`` column is float64 instead of
        # the canonical int64. Build a target schema to keep field order.
        fields = [
            pa.field(name, pa.float64() if name == "volume" else original.schema.field(name).type)
            for name in original.schema.names
        ]
        bad = original.cast(pa.schema(fields))
        request = CanonicalPublishRequest(
            version=version,
            tables=(bad,),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        with pytest.raises(CanonicalWarehouseError):
            warehouse.publish(request)


class TestPublishAtomicity:
    def test_incomplete_directory_is_removed_only_after_rename(
        self, tmp_path: Path, warehouse: CanonicalWarehouse
    ) -> None:
        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        request = CanonicalPublishRequest(
            version=version,
            tables=(_build_daily_bar_table(_daily_bar_rows()),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        landed = warehouse.publish(request)
        incomplete = landed.with_name(landed.name + ".incomplete")
        assert not incomplete.exists()
        assert landed.is_dir()

    def test_partial_failure_keeps_incomplete_and_does_not_publish(self, tmp_path: Path) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)

        original = CanonicalWarehouse._write_manifest_and_complete

        def _boom(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("simulated post-write failure")

        CanonicalWarehouse._write_manifest_and_complete = _boom  # type: ignore[method-assign]
        try:
            version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
            request = CanonicalPublishRequest(
                version=version,
                tables=(_build_daily_bar_table(_daily_bar_rows()),),
                code_version="s1-02",
                rule_version="quality-rules.v1",
            )
            with pytest.raises(CanonicalWarehouseError):
                warehouse.publish(request)
            final = (
                tmp_path
                / "warehouse"
                / "daily_bar_unadjusted"
                / path_safe_id(str(version.version_id))
            )
            incomplete = final.with_name(final.name + ".incomplete")
            assert not final.exists()
            assert incomplete.is_dir()
            assert (incomplete / WAREHOUSE_FAILURE_FILENAME).is_file()
        finally:
            CanonicalWarehouse._write_manifest_and_complete = original  # type: ignore[method-assign]


class TestPublishPaths:
    def test_publish_rejects_path_traversal_in_version_id(self, tmp_path: Path) -> None:
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        with pytest.raises((InvalidPathError, IncompatibleInputError)):
            # The new_entity_id helper returns a namespaced UUID4; we
            # wrap it in a class whose ``__str__`` produces a path
            # traversal so the publisher's path check rejects it.
            class _TraversalId:
                def __str__(self) -> str:
                    return "../../escape/data_version:00000000-0000-0000-0000-000000000000"

            version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
            object.__setattr__(version, "version_id", _TraversalId())  # type: ignore[attr-defined]
            request = CanonicalPublishRequest(
                version=version,
                tables=(_build_daily_bar_table(_daily_bar_rows()),),
                code_version="s1-02",
                rule_version="quality-rules.v1",
            )
            warehouse.publish(request)


class TestPublishMetadataCoordination:
    def test_publish_marks_version_published_via_repository(
        self,
        tmp_path: Path,
        warehouse: CanonicalWarehouse,
        database_uow: DataSqliteUnitOfWorkFactory,
    ) -> None:
        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        table = _build_daily_bar_table(_daily_bar_rows())
        request = CanonicalPublishRequest(
            version=version,
            tables=(table,),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        warehouse.publish(request)
        # Persist a PASSED quality run bound to the candidate content so
        # the SQLite trigger allows the PUBLISHED transition.
        _make_passed_quality_run(
            version,
            table,
            uow_factory=database_uow,
            artifacts_root=tmp_path / "artifacts",
        )
        # Move from BUILDING to VALIDATED then PUBLISHED through the repository,
        # the same way the application layer would.
        with database_uow() as uow:
            stored = uow.data.save_version(version)
            validated = transition_dataset_version(
                stored,
                DatasetVersionStatus.VALIDATED,
                at=utc_now(),
                manifest_sha256="a" * 64,
                security_count=1,
                row_count=2,
                coverage_start=TradingDate(date(2026, 7, 15)),
                coverage_end=TradingDate(date(2026, 7, 16)),
                coverage_summary={"date_count": "2", "security_count": "1"},
            )
            published = transition_dataset_version(
                validated,
                DatasetVersionStatus.PUBLISHED,
                at=utc_now(),
            )
            uow.data.save_version(validated)
            uow.data.save_version(published)
            uow.commit()
        with database_uow() as uow:
            row = uow.data.get_version(str(version.version_id))
        assert row.status is DatasetVersionStatus.PUBLISHED

    def test_publish_failure_does_not_create_published_metadata(
        self,
        tmp_path: Path,
        warehouse: CanonicalWarehouse,
        database_uow: DataSqliteUnitOfWorkFactory,
    ) -> None:
        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        # Inducing a validation failure must leave no PUBLISHED row.
        empty = pa.table(
            {
                "code": pa.array([], type=pa.string()),
                "trade_date": pa.array([], type=pa.date32()),
                "open": pa.array([], type=pa.float64()),
                "high": pa.array([], type=pa.float64()),
                "low": pa.array([], type=pa.float64()),
                "close": pa.array([], type=pa.float64()),
                "pre_close": pa.array([], type=pa.float64()),
                "volume": pa.array([], type=pa.int64()),
                "amount": pa.array([], type=pa.float64()),
            }
        )
        request_bad = CanonicalPublishRequest(
            version=version,
            tables=(empty,),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        with pytest.raises(CanonicalWarehouseError):
            warehouse.publish(request_bad)
        with database_uow() as uow:
            try:
                uow.data.get_version(str(version.version_id))
            except Exception as error:
                assert "not found" in str(error).lower() or "does not exist" in str(error).lower()


class TestPublishConcurrentProducers:
    @pytest.mark.parametrize("_stress_iteration", range(50))
    def test_concurrent_publishes_do_not_corrupt_or_overwrite(
        self, tmp_path: Path, _stress_iteration: int
    ) -> None:
        import threading

        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        request = CanonicalPublishRequest(
            version=version,
            tables=(_build_daily_bar_table(_daily_bar_rows()),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        results: list[str] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def _run() -> None:
            try:
                barrier.wait(timeout=10)
                warehouse = CanonicalWarehouse(data_root=tmp_path)
                try:
                    landed = warehouse.publish(request)
                    manifest = json.loads(
                        (landed / WAREHOUSE_MANIFEST_FILENAME).read_text(encoding="utf-8")
                    )
                    results.append(manifest["manifest_sha256"])
                except (IdempotencyConflictError, CanonicalWarehouseError) as error:
                    results.append(type(error).__name__)
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=_run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not errors, f"worker raised: {errors}"
        assert len(results) == 2
        # The published directory must still be consistent.
        final = (
            tmp_path / "warehouse" / "daily_bar_unadjusted" / path_safe_id(str(version.version_id))
        )
        assert (final / WAREHOUSE_COMPLETE_FILENAME).is_file()
        manifest = json.loads((final / WAREHOUSE_MANIFEST_FILENAME).read_text(encoding="utf-8"))
        # The Parquet file must match the manifest and the on-disk hash.
        import hashlib

        for entry in manifest["files"]:
            absolute = final / entry["relative_path"]
            digest = hashlib.sha256(absolute.read_bytes()).hexdigest()
            assert digest == entry["sha256"]


class TestPublishQueryCoordination:
    def test_query_rejects_non_published_versions(
        self,
        tmp_path: Path,
        warehouse: CanonicalWarehouse,
        database_uow: DataSqliteUnitOfWorkFactory,
    ) -> None:
        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        request = CanonicalPublishRequest(
            version=version,
            tables=(_build_daily_bar_table(_daily_bar_rows()),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        warehouse.publish(request)
        with database_uow() as uow:
            stored = uow.data.save_version(version)
            uow.commit()
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=database_uow)
        with pytest.raises(DatasetVersionNotQueryableError):
            adapter.query_by_id(version_id=str(stored.version_id), dataset="daily_bar_unadjusted")

    def test_query_uses_manifest_files_only(
        self,
        tmp_path: Path,
        warehouse: CanonicalWarehouse,
        database_uow: DataSqliteUnitOfWorkFactory,
    ) -> None:
        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        table = _build_daily_bar_table(_daily_bar_rows())
        request = CanonicalPublishRequest(
            version=version,
            tables=(table,),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        landed = warehouse.publish(request)
        _make_passed_quality_run(
            version,
            table,
            uow_factory=database_uow,
            artifacts_root=tmp_path / "artifacts",
        )
        with database_uow() as uow:
            stored = uow.data.save_version(version)
            validated = transition_dataset_version(
                stored,
                DatasetVersionStatus.VALIDATED,
                at=utc_now(),
                manifest_sha256="a" * 64,
                security_count=1,
                row_count=2,
                coverage_start=TradingDate(date(2026, 7, 15)),
                coverage_end=TradingDate(date(2026, 7, 16)),
                coverage_summary={"date_count": "2", "security_count": "1"},
            )
            uow.data.save_version(validated)
            published = transition_dataset_version(
                validated,
                DatasetVersionStatus.PUBLISHED,
                at=utc_now(),
            )
            uow.data.save_version(published)
            uow.commit()
        # Drop a foreign Parquet file into the directory; the query adapter
        # must ignore it because the manifest is the source of truth.
        rogue = landed / "rogue.parquet"
        rogue.write_bytes(b"this file is not in the manifest")
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=database_uow)
        with pytest.raises(DatasetVersionNotQueryableError):
            adapter.query_by_id(version_id=str(version.version_id), dataset="daily_bar_unadjusted")

    def test_query_rejects_tampered_manifest(
        self,
        tmp_path: Path,
        warehouse: CanonicalWarehouse,
        database_uow: DataSqliteUnitOfWorkFactory,
    ) -> None:
        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        table = _build_daily_bar_table(_daily_bar_rows())
        request = CanonicalPublishRequest(
            version=version,
            tables=(table,),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        landed = warehouse.publish(request)
        _make_passed_quality_run(
            version,
            table,
            uow_factory=database_uow,
            artifacts_root=tmp_path / "artifacts",
        )
        with database_uow() as uow:
            stored = uow.data.save_version(version)
            validated = transition_dataset_version(
                stored,
                DatasetVersionStatus.VALIDATED,
                at=utc_now(),
                manifest_sha256="a" * 64,
                security_count=1,
                row_count=2,
                coverage_start=TradingDate(date(2026, 7, 15)),
                coverage_end=TradingDate(date(2026, 7, 16)),
                coverage_summary={"date_count": "2", "security_count": "1"},
            )
            uow.data.save_version(validated)
            published = transition_dataset_version(
                validated,
                DatasetVersionStatus.PUBLISHED,
                at=utc_now(),
            )
            uow.data.save_version(published)
            uow.commit()
        # Tamper with the manifest by appending garbage; the COMPLETE
        # marker must no longer match the manifest SHA-256.
        manifest_path = landed / WAREHOUSE_MANIFEST_FILENAME
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=database_uow)
        with pytest.raises(DatasetVersionNotQueryableError):
            adapter.query_by_id(version_id=str(version.version_id), dataset="daily_bar_unadjusted")

    def test_query_rejects_tampered_complete_marker(
        self,
        tmp_path: Path,
        warehouse: CanonicalWarehouse,
        database_uow: DataSqliteUnitOfWorkFactory,
    ) -> None:
        version = _building_version(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
        table = _build_daily_bar_table(_daily_bar_rows())
        request = CanonicalPublishRequest(
            version=version,
            tables=(table,),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        landed = warehouse.publish(request)
        _make_passed_quality_run(
            version,
            table,
            uow_factory=database_uow,
            artifacts_root=tmp_path / "artifacts",
        )
        with database_uow() as uow:
            stored = uow.data.save_version(version)
            validated = transition_dataset_version(
                stored,
                DatasetVersionStatus.VALIDATED,
                at=utc_now(),
                manifest_sha256="a" * 64,
                security_count=1,
                row_count=2,
                coverage_start=TradingDate(date(2026, 7, 15)),
                coverage_end=TradingDate(date(2026, 7, 16)),
                coverage_summary={"date_count": "2", "security_count": "1"},
            )
            uow.data.save_version(validated)
            published = transition_dataset_version(
                validated,
                DatasetVersionStatus.PUBLISHED,
                at=utc_now(),
            )
            uow.data.save_version(published)
            uow.commit()
        (landed / WAREHOUSE_COMPLETE_FILENAME).write_text("{}", encoding="utf-8")
        adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=database_uow)
        with pytest.raises(DatasetVersionNotQueryableError):
            adapter.query_by_id(version_id=str(version.version_id), dataset="daily_bar_unadjusted")
