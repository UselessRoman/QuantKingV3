"""PyArrow implementation of the explicit SourceOverride transformation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pyarrow as pa

from quantking.data.adapters.canonical_schema import ensure_sorted, validate_table
from quantking.data.models import DatasetKind
from quantking.data.schemas import descriptor_for


@dataclass(frozen=True, slots=True)
class OverrideApplicationResult:
    table: pa.Table
    matched_rows: int
    changed_cells: int


def apply_override_to_table(
    *,
    dataset: DatasetKind,
    primary_table: pa.Table,
    fallback_table: pa.Table,
    scope_codes: frozenset[str],
    scope_dates: frozenset[date],
    fields: frozenset[str],
) -> OverrideApplicationResult:
    """Replace only approved cells from fallback into the primary table."""
    if primary_table.schema != fallback_table.schema:
        raise ValueError("primary and fallback tables must have identical schemas")
    descriptor = descriptor_for(dataset.value)
    primary = primary_table.to_pylist()
    fallback = fallback_table.to_pylist()
    key_fields = descriptor.primary_key
    primary_keys = [_key(row, key_fields) for row in primary]
    fallback_map = {_key(row, key_fields): row for row in fallback}
    if len(fallback_map) != len(fallback) or len(set(primary_keys)) != len(primary):
        raise ValueError("override inputs must have unique primary keys")
    changed = 0
    matched = 0
    output: list[dict[str, object]] = []
    date_field = "trade_date" if "trade_date" in descriptor.field_names else "effective_from"
    for row in primary:
        key = _key(row, key_fields)
        if str(row["code"]) not in scope_codes or row[date_field] not in scope_dates:
            output.append(dict(row))
            continue
        source = fallback_map.get(key)
        if source is None:
            raise ValueError(f"fallback is missing scoped primary row {key!r}")
        matched += 1
        merged = dict(row)
        for field in fields:
            if field in key_fields:
                raise ValueError("primary key fields cannot be overridden")
            if merged[field] != source[field]:
                changed += 1
            merged[field] = source[field]
        output.append(merged)
    candidate = ensure_sorted(
        dataset.value, pa.Table.from_pylist(output, schema=primary_table.schema)
    )
    validation = validate_table(dataset.value, candidate)
    if not validation.ok:
        raise ValueError(
            f"override result failed canonical validation: {validation.issues[0].message}"
        )
    return OverrideApplicationResult(table=candidate, matched_rows=matched, changed_cells=changed)


def _key(row: dict[str, object], fields: tuple[str, ...]) -> tuple[object, ...]:
    values = tuple(row[field] for field in fields)
    if any(value is None for value in values):
        raise ValueError("primary key values cannot be null")
    return values


__all__ = ("OverrideApplicationResult", "apply_override_to_table")
