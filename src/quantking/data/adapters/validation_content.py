"""PyArrow boundary helpers for source-validation evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from quantking.data.adapters.canonical_content import canonical_content_sha256
from quantking.data.adapters.canonical_schema import canonical_schema, validate_table
from quantking.data.models import DatasetKind


@dataclass(frozen=True, slots=True)
class VerifiedCanonicalEvidence:
    content_sha256: str
    rows: tuple[dict[str, object], ...]


def verify_canonical_evidence(*, path: Path, dataset: DatasetKind) -> VerifiedCanonicalEvidence:
    table = pq.read_table(path)  # type: ignore[no-untyped-call]
    result = validate_table(dataset.value, table)
    if not result.ok:
        raise ValueError("Canonical schema, primary key or sort verification failed")
    return VerifiedCanonicalEvidence(
        content_sha256=canonical_content_sha256(dataset, [table]),
        rows=tuple(table.to_pylist()),
    )


def verify_quality_row_evidence(path: Path) -> None:
    pq.read_table(path)  # type: ignore[no-untyped-call]


def build_fake_exchange_calendar_parquet(
    rows: tuple[tuple[str, date, bool], ...],
) -> tuple[bytes, str, int]:
    """Create strict Canonical bytes for offline Fake-runner tests."""
    table = build_fake_exchange_calendar_table(rows)
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)  # type: ignore[no-untyped-call]
    return (
        bytes(sink.getvalue()),
        canonical_content_sha256(DatasetKind.EXCHANGE_CALENDAR, [table]),
        table.num_rows,
    )


def build_fake_exchange_calendar_table(rows: tuple[tuple[str, date, bool], ...]) -> pa.Table:
    """Create the strict table used by isolated validation contract fixtures."""
    dataset = DatasetKind.EXCHANGE_CALENDAR
    return pa.Table.from_arrays(
        [
            pa.array([row[0] for row in rows], type=pa.string()),
            pa.array([row[1] for row in rows], type=pa.date32()),
            pa.array([row[2] for row in rows], type=pa.bool_()),
        ],
        schema=canonical_schema(dataset.value),
    )


def build_fake_trading_status_parquet(
    rows: tuple[
        tuple[str, date, bool, bool, bool, bool, bool, float | None, float | None, str | None], ...
    ],
) -> tuple[bytes, str, int]:
    """Create canonical synthetic trading-status bytes for the C0B Fake runner."""
    table = build_fake_trading_status_table(rows)
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)  # type: ignore[no-untyped-call]
    return (
        bytes(sink.getvalue()),
        canonical_content_sha256(DatasetKind.TRADING_STATUS, [table]),
        table.num_rows,
    )


def build_fake_trading_status_table(
    rows: tuple[
        tuple[str, date, bool, bool, bool, bool, bool, float | None, float | None, str | None], ...
    ],
) -> pa.Table:
    """Create a schema-valid, permanently synthetic trading-status table."""
    dataset = DatasetKind.TRADING_STATUS
    return pa.Table.from_arrays(
        [
            pa.array([row[0] for row in rows], type=pa.string()),
            pa.array([row[1] for row in rows], type=pa.date32()),
            pa.array([row[2] for row in rows], type=pa.bool_()),
            pa.array([row[3] for row in rows], type=pa.bool_()),
            pa.array([row[4] for row in rows], type=pa.bool_()),
            pa.array([row[5] for row in rows], type=pa.bool_()),
            pa.array([row[6] for row in rows], type=pa.bool_()),
            pa.array([row[7] for row in rows], type=pa.float64()),
            pa.array([row[8] for row in rows], type=pa.float64()),
            pa.array([row[9] for row in rows], type=pa.string()),
        ],
        schema=canonical_schema(dataset.value),
    )


__all__ = [
    "VerifiedCanonicalEvidence",
    "build_fake_exchange_calendar_parquet",
    "build_fake_exchange_calendar_table",
    "build_fake_trading_status_parquet",
    "build_fake_trading_status_table",
    "verify_canonical_evidence",
    "verify_quality_row_evidence",
]
