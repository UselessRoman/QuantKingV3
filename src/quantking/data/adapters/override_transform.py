"""PyArrow implementation of the explicit SourceOverride transformation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

import pyarrow as pa

from quantking.data.adapters.canonical_schema import canonical_schema, ensure_sorted, validate_table
from quantking.data.models import DatasetKind
from quantking.data.schemas import descriptor_for


@dataclass(frozen=True, slots=True)
class OverrideApplicationResult:
    table: pa.Table
    matched_rows: int
    changed_cells: int
    inserted_rows: int = 0


def apply_override_to_table(
    *,
    dataset: DatasetKind,
    primary_table: pa.Table,
    fallback_table: pa.Table,
    field_replacements: tuple[Mapping[str, str], ...] = (),
    fallback_only_keys: tuple[Mapping[str, str], ...] = (),
) -> OverrideApplicationResult:
    """Apply only explicitly approved cells and fallback-only rows.

    Args:
        dataset: The dataset kind being transformed.
        primary_table: The primary source table (immutable).
        fallback_table: The fallback source table.
        field_replacements: Complete primary key plus field for each cell.
        fallback_only_keys: Complete primary key for each approved row insert.

    Returns:
        OverrideApplicationResult with transformed table, matched_rows, and
        changed_cells count.

    Raises:
        ValueError: If schemas differ, primary keys are not unique, primary
            key fields are selected for replacement, or the exact approved
            scope is not fully consumed.
    """
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

    if not field_replacements and not fallback_only_keys:
        raise ValueError("override requires at least one exact difference unit")

    changed = 0
    matched_keys: set[tuple[object, ...]] = set()
    inserted = 0
    output: list[dict[str, object]] = []
    primary_key_set = set(primary_keys)
    requested_fields: dict[tuple[str, ...], set[str]] = {}
    for unit in field_replacements:
        if set(unit) != {*key_fields, "field"}:
            raise ValueError("field replacement must contain the complete primary key and field")
        key = tuple(str(unit[name]) for name in key_fields)
        field = str(unit["field"])
        if field in key_fields or field not in descriptor.field_names:
            raise ValueError("field replacement contains an invalid field")
        requested_fields.setdefault(key, set()).add(field)
    requested_rows: set[tuple[str, ...]] = set()
    for unit in fallback_only_keys:
        if set(unit) != set(key_fields):
            raise ValueError("row insert must contain the complete primary key")
        requested_rows.add(tuple(str(unit[name]) for name in key_fields))
    consumed_fields: set[tuple[tuple[str, ...], str]] = set()
    consumed_rows: set[tuple[str, ...]] = set()

    # Process all primary rows.
    for row in primary:
        row_key = _key(row, key_fields)
        string_key = tuple(_string_key_value(row[name]) for name in key_fields)
        replacement_fields = requested_fields.get(string_key)
        if not replacement_fields:
            output.append(dict(row))
            continue
        source = fallback_map.get(row_key)
        if source is None:
            raise ValueError(f"fallback is missing approved field-replacement row {row_key!r}")
        matched_keys.add(row_key)
        merged = dict(row)
        for field in replacement_fields:
            if merged[field] != source[field]:
                changed += 1
            merged[field] = source[field]
            consumed_fields.add((string_key, field))
        output.append(merged)

    for fallback_row in fallback:
        row_key = _key(fallback_row, key_fields)
        string_key = tuple(_string_key_value(fallback_row[name]) for name in key_fields)
        if string_key in requested_rows:
            if row_key in primary_key_set:
                raise ValueError("approved fallback-only row already exists in primary")
            matched_keys.add(row_key)
            consumed_rows.add(string_key)
            inserted += 1
            output.append(dict(fallback_row))

    expected_fields = {
        (key, field) for key, unit_fields in requested_fields.items() for field in unit_fields
    }
    if not expected_fields and not requested_rows:
        raise ValueError("override scope matched zero exact difference units")
    if consumed_fields != expected_fields or consumed_rows != requested_rows:
        raise ValueError("override exact difference scope was only partially consumed")

    candidate = ensure_sorted(
        dataset.value, pa.Table.from_pylist(output, schema=canonical_schema(dataset.value))
    )
    validation = validate_table(dataset.value, candidate)
    if not validation.ok:
        raise ValueError(
            f"override result failed canonical validation: {validation.issues[0].message}"
        )
    return OverrideApplicationResult(
        table=candidate,
        matched_rows=len(matched_keys),
        changed_cells=changed,
        inserted_rows=inserted,
    )


def _key(row: dict[str, object], fields: tuple[str, ...]) -> tuple[object, ...]:
    values = tuple(row[field] for field in fields)
    if any(value is None for value in values):
        raise ValueError("primary key values cannot be null")
    return values


def _string_key_value(value: object) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


__all__ = ("OverrideApplicationResult", "apply_override_to_table")
