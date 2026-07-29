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
    dataset = DatasetKind.EXCHANGE_CALENDAR
    table = pa.Table.from_arrays(
        [
            pa.array([row[0] for row in rows], type=pa.string()),
            pa.array([row[1] for row in rows], type=pa.date32()),
            pa.array([row[2] for row in rows], type=pa.bool_()),
        ],
        schema=canonical_schema(dataset.value),
    )
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)  # type: ignore[no-untyped-call]
    return bytes(sink.getvalue()), canonical_content_sha256(dataset, [table]), table.num_rows


__all__ = [
    "VerifiedCanonicalEvidence",
    "build_fake_exchange_calendar_parquet",
    "verify_canonical_evidence",
    "verify_quality_row_evidence",
]
