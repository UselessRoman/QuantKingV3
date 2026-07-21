"""PyArrow implementation of the explicit SourceOverride transformation."""

from __future__ import annotations

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


def apply_override_to_table(
    *,
    dataset: DatasetKind,
    primary_table: pa.Table,
    fallback_table: pa.Table,
    scope_codes: frozenset[str],
    scope_dates: frozenset[date],
    fields: frozenset[str],
    allow_fallback_only: bool = False,
) -> OverrideApplicationResult:
    """Replace only approved cells from fallback into the primary table.

    Args:
        dataset: The dataset kind being transformed.
        primary_table: The primary source table (immutable).
        fallback_table: The fallback source table.
        scope_codes: Security codes approved for override.
        scope_dates: Trade dates approved for override.
        fields: Field names approved for override.
        allow_fallback_only: If True, allows adding FALLBACK_ONLY rows
            (rows that exist in fallback but not in primary) to the result.
            If False (default), raises ValueError if any scoped row is missing
            from primary.

    Returns:
        OverrideApplicationResult with transformed table, matched_rows, and
        changed_cells count.

    Raises:
        ValueError: If schemas differ, primary keys are not unique, primary
            key fields are in the override field list, a scoped row is missing
            from fallback, a scoped row is missing from primary when
            allow_fallback_only=False, or approved scope is not fully consumed.
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

    # Validate that no primary key fields are in the override field list.
    for field in fields:
        if field in key_fields:
            raise ValueError(f"primary key field {field!r} cannot be overridden")

    changed = 0
    matched = 0
    output: list[dict[str, object]] = []
    date_field = "trade_date" if "trade_date" in descriptor.field_names else "effective_from"

    # Track which scoped (code, date) pairs were actually consumed.
    scoped_pairs = {(code, date) for code in scope_codes for date in scope_dates}
    consumed_pairs: set[tuple[str, date]] = set()

    # Build primary key set for faster lookup.
    primary_key_set = set(_key(row, key_fields) for row in primary)

    # Process all primary rows.
    for row in primary:
        key = _key(row, key_fields)
        code = str(row["code"])
        trade_date = row[date_field]
        pair = (code, trade_date)

        if code not in scope_codes or trade_date not in scope_dates:
            # Out of scope: keep primary row unchanged.
            output.append(dict(row))
            continue

        # In scope: must find fallback row.
        source = fallback_map.get(key)
        if source is None:
            raise ValueError(
                f"fallback is missing scoped primary row {key!r}; "
                f"cannot apply override without fallback data"
            )

        matched += 1
        consumed_pairs.add(pair)
        merged = dict(row)
        for field in fields:
            if merged[field] != source[field]:
                changed += 1
            merged[field] = source[field]
        output.append(merged)

    # If allow_fallback_only, add FALLBACK_ONLY rows that are in scope.
    if allow_fallback_only:
        for fallback_row in fallback:
            key = _key(fallback_row, key_fields)
            code = str(fallback_row["code"])
            trade_date = fallback_row[date_field]
            pair = (code, trade_date)

            if code in scope_codes and trade_date in scope_dates and key not in primary_key_set:
                # FALLBACK_ONLY: add it.
                matched += 1
                consumed_pairs.add(pair)
                output.append(dict(fallback_row))

    # Verify all approved scope was consumed (zero-match is an error).
    if matched == 0:
        raise ValueError("override scope matched zero rows; scope may be invalid")

    unconsumed = scoped_pairs - consumed_pairs
    if unconsumed:
        # Some (code, date) pairs in the approved scope were not found in
        # either primary or fallback (or allow_fallback_only=False blocked them).
        # This is a partial match, which is not allowed.
        sample = sorted(unconsumed)[:5]
        raise ValueError(
            f"override scope is not fully consumed; {len(unconsumed)} "
            f"(code, date) pairs were not found in available data. "
            f"Sample: {sample}"
        )

    candidate = ensure_sorted(
        dataset.value, pa.Table.from_pylist(output, schema=canonical_schema(dataset.value))
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
