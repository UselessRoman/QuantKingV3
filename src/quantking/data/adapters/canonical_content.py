"""Canonical logical-content fingerprinting shared by Stage 1 data flows."""

from __future__ import annotations

import hashlib
import io
from collections.abc import Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from quantking.data.adapters.canonical_schema import ensure_sorted, validate_table
from quantking.data.models import DatasetKind, EmptyResultInfo

_EMPTY_CONTENT_PAYLOAD = b"quantking.canonical-content.v1:empty"
EMPTY_CANONICAL_CONTENT_SHA256 = hashlib.sha256(_EMPTY_CONTENT_PAYLOAD).hexdigest()


def canonical_table(
    dataset: DatasetKind,
    tables: Sequence[pa.Table],
    *,
    empty_result: EmptyResultInfo | None = None,
    validate: bool = True,
) -> pa.Table | None:
    """Merge chunks and globally sort one logical Canonical table."""
    if not isinstance(dataset, DatasetKind):
        raise TypeError("dataset must be a DatasetKind")
    normalized = tuple(tables)
    if any(not isinstance(table, pa.Table) for table in normalized):
        raise TypeError("tables must contain pyarrow.Table values")
    if not normalized:
        if empty_result is None:
            raise ValueError("empty content requires an explicit legal empty result")
        return None
    combined = ensure_sorted(dataset.value, pa.concat_tables(list(normalized)))
    if validate:
        result = validate_table(dataset.value, combined, empty_result=empty_result)
        if not result.ok:
            first = result.issues[0]
            raise ValueError(f"canonical content validation failed: {first.rule}: {first.message}")
    return combined


def canonical_content_sha256(
    dataset: DatasetKind,
    tables: Sequence[pa.Table],
    *,
    empty_result: EmptyResultInfo | None = None,
    validate: bool = True,
) -> str:
    """Hash canonical logical rows independently of chunk and table ordering."""
    combined = canonical_table(
        dataset,
        tables,
        empty_result=empty_result,
        validate=validate,
    )
    if combined is None or combined.num_rows == 0:
        return EMPTY_CANONICAL_CONTENT_SHA256
    buffer = io.BytesIO()
    pq.write_table(combined, buffer, compression="snappy")  # type: ignore[no-untyped-call]
    return hashlib.sha256(buffer.getvalue()).hexdigest()


__all__ = (
    "EMPTY_CANONICAL_CONTENT_SHA256",
    "canonical_content_sha256",
    "canonical_table",
)
