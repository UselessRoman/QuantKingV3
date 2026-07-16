"""Strict PyArrow mapping and validation for the six canonical schemas.

This is the only module in the data domain that imports ``pyarrow`` (allowed by
the architecture boundary checker because it lives under ``data/adapters``). The
framework-free descriptors in ``quantking.data.schemas`` remain the single source
of truth; this module turns them into executable ``pyarrow.Schema`` objects and
validates tables against the canonical contract: field types, nullability,
primary-key uniqueness, stable sort order, no NaN/inf, price/volume/amount
invariants, and canonical code form.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import pyarrow as pa

from quantking.data.models import EmptyResultInfo
from quantking.data.schemas import (
    DatasetSchemaDescriptor,
    FieldType,
    canonical_code_pattern,
    descriptor_for,
)

CANONICAL_SCHEMA_VERSION = "stage1-canonical-v1"

_PYARROW_TYPE: dict[FieldType, pa.DataType] = {
    FieldType.CODE: pa.string(),
    FieldType.EXCHANGE: pa.string(),
    FieldType.DATE: pa.date32(),
    FieldType.BOOL: pa.bool_(),
    FieldType.INT64: pa.int64(),
    FieldType.FLOAT64: pa.float64(),
    FieldType.TEXT: pa.string(),
    FieldType.LISTING_STATUS: pa.string(),
    FieldType.BOARD: pa.string(),
}


@dataclass(frozen=True, slots=True)
class CanonicalValidationIssue:
    """One validation failure against a canonical schema."""

    rule: str
    message: str
    row_index: int | None = None


@dataclass(frozen=True, slots=True)
class CanonicalValidationResult:
    """Outcome of validating one table against its canonical schema."""

    dataset: str
    ok: bool
    issues: tuple[CanonicalValidationIssue, ...]


def canonical_schema(dataset: str) -> pa.Schema:
    """Build the executable PyArrow schema for one canonical dataset."""
    descriptor = descriptor_for(dataset)
    fields: list[pa.Field] = []
    for field in descriptor.fields:
        fields.append(
            pa.field(field.name, _PYARROW_TYPE[field.field_type], nullable=field.nullable)
        )
    return pa.schema(fields)


def sort_key(dataset: str) -> tuple[str, ...]:
    """Return the canonical sort key for a dataset."""
    return descriptor_for(dataset).sort_key


def validate_table(
    dataset: str,
    table: pa.Table,
    *,
    empty_result: EmptyResultInfo | None = None,
) -> CanonicalValidationResult:
    """Validate a PyArrow table against the canonical contract for ``dataset``.

    The table is checked for schema field presence, nullability, primary-key
    uniqueness, canonical code form, no NaN/inf, and price/volume/amount value
    invariants. A zero-row table is successful only when the caller supplies an
    explicit :class:`EmptyResultInfo`; transport, permission, parse and
    unexpected-empty failures are batch outcomes and must never be represented
    by a successful empty table.
    """
    descriptor = descriptor_for(dataset)
    issues: list[CanonicalValidationIssue] = _check_schema(descriptor, table)
    if not issues and table.num_rows == 0:
        if empty_result is None:
            issues.append(
                CanonicalValidationIssue(
                    "unexpected_empty",
                    "zero-row table requires an explicit legal empty classification and reason",
                )
            )
        elif not isinstance(empty_result, EmptyResultInfo):
            issues.append(
                CanonicalValidationIssue(
                    "empty_result_type", "empty_result must be an EmptyResultInfo"
                )
            )
    elif not issues:
        if empty_result is not None:
            issues.append(
                CanonicalValidationIssue(
                    "unexpected_empty_metadata",
                    "non-empty table cannot carry a legal empty result",
                )
            )
        issues.extend(_check_primary_key_unique(descriptor, table))
        issues.extend(_check_sort_order(descriptor, table))
        issues.extend(_check_canonical_codes(descriptor, table))
        issues.extend(_check_exchange_consistency(descriptor, table))
        issues.extend(_check_value_invariants(descriptor, table))
        issues.extend(_check_ohlc_relationships(descriptor, table))
        issues.extend(_check_trading_status_consistency(descriptor, table))
        issues.extend(_check_no_nan_inf(descriptor, table))
    return CanonicalValidationResult(
        dataset=dataset,
        ok=not issues,
        issues=tuple(issues),
    )


def ensure_sorted(dataset: str, table: pa.Table) -> pa.Table:
    """Return the table sorted by the canonical sort key."""
    key = sort_key(dataset)
    indices = pa.sort_indices(table, sort_keys=[(col, "ascending") for col in key])
    return table.take(indices)


def _check_schema(
    descriptor: DatasetSchemaDescriptor, table: pa.Table
) -> list[CanonicalValidationIssue]:
    issues: list[CanonicalValidationIssue] = []
    schema = table.schema
    expected_schema = canonical_schema(descriptor.name)
    if tuple(schema.names) != tuple(expected_schema.names):
        issues.append(
            CanonicalValidationIssue(
                "field_order",
                f"field order must be {expected_schema.names!r}, got {schema.names!r}",
            )
        )
    expected = {field.name: field for field in descriptor.fields}
    actual_names = set(schema.names)
    for name, field in expected.items():
        if name not in actual_names:
            issues.append(
                CanonicalValidationIssue("missing_field", f"required field {name!r} is missing")
            )
            continue
        column = schema.field(name)
        expected_type = _PYARROW_TYPE[field.field_type]
        if not column.type.equals(expected_type):
            issues.append(
                CanonicalValidationIssue(
                    "field_type",
                    f"field {name!r} must have Arrow type {expected_type}, got {column.type}",
                )
            )
        if column.nullable and not field.nullable:
            issues.append(
                CanonicalValidationIssue(
                    "nullability", f"field {name!r} must be non-nullable but is nullable"
                )
            )
        if not column.nullable and field.nullable:
            issues.append(
                CanonicalValidationIssue(
                    "nullability", f"field {name!r} must be nullable but is non-nullable"
                )
            )
    extra = actual_names - set(expected)
    if extra:
        issues.append(
            CanonicalValidationIssue("unexpected_field", f"unexpected fields: {sorted(extra)}")
        )
    # nulls in non-nullable columns
    for name, field in expected.items():
        if name not in actual_names or field.nullable:
            continue
        col = table.column(name)
        if col.null_count > 0:
            issues.append(
                CanonicalValidationIssue("non_nullable_null", f"field {name!r} contains nulls")
            )
    return issues


def _check_sort_order(
    descriptor: DatasetSchemaDescriptor, table: pa.Table
) -> list[CanonicalValidationIssue]:
    columns = [table.column(name).to_pylist() for name in descriptor.sort_key]
    previous: tuple[object, ...] | None = None
    for index in range(table.num_rows):
        current = tuple(column[index] for column in columns)
        if previous is not None and current < previous:
            return [
                CanonicalValidationIssue(
                    "sort_order",
                    f"rows must be ascending by {descriptor.sort_key}",
                    row_index=index,
                )
            ]
        previous = current
    return []


def _check_primary_key_unique(
    descriptor: DatasetSchemaDescriptor, table: pa.Table
) -> list[CanonicalValidationIssue]:
    issues: list[CanonicalValidationIssue] = []
    pk = descriptor.primary_key
    seen: set[tuple[object, ...]] = set()
    columns = [table.column(name).to_pylist() for name in pk]
    for index in range(table.num_rows):
        key = tuple(column[index] for column in columns)
        if key in seen:
            issues.append(
                CanonicalValidationIssue(
                    "pk_unique",
                    f"duplicate primary key {pk}={key}",
                    row_index=index,
                )
            )
        seen.add(key)
    return issues


def _check_canonical_codes(
    descriptor: DatasetSchemaDescriptor, table: pa.Table
) -> list[CanonicalValidationIssue]:
    issues: list[CanonicalValidationIssue] = []
    pattern = canonical_code_pattern()
    code_fields = [f for f in descriptor.fields if f.field_type is FieldType.CODE]
    for field in code_fields:
        values = table.column(field.name).to_pylist()
        for index, value in enumerate(values):
            if value is None:
                if not field.nullable:
                    issues.append(
                        CanonicalValidationIssue("code_null", "code is null", row_index=index)
                    )
                continue
            if not pattern.fullmatch(str(value)):
                issues.append(
                    CanonicalValidationIssue(
                        "code_format",
                        f"code {value!r} is not canonical (expected 000001.SZ form)",
                        row_index=index,
                    )
                )
    return issues


def _check_exchange_consistency(
    descriptor: DatasetSchemaDescriptor, table: pa.Table
) -> list[CanonicalValidationIssue]:
    issues: list[CanonicalValidationIssue] = []
    allowed = {"SZ", "SH", "BJ"}
    if "exchange" in table.column_names:
        exchanges = table.column("exchange").to_pylist()
        for index, exchange in enumerate(exchanges):
            if exchange not in allowed:
                issues.append(
                    CanonicalValidationIssue(
                        "exchange_value",
                        f"exchange must be one of {sorted(allowed)}, got {exchange!r}",
                        row_index=index,
                    )
                )
    if descriptor.name == "security_master":
        codes = table.column("code").to_pylist()
        exchanges = table.column("exchange").to_pylist()
        for index, (code, exchange) in enumerate(zip(codes, exchanges, strict=True)):
            suffix = str(code).rsplit(".", 1)[-1]
            if suffix != exchange:
                issues.append(
                    CanonicalValidationIssue(
                        "exchange_code_suffix",
                        f"exchange {exchange!r} does not match code suffix {suffix!r}",
                        row_index=index,
                    )
                )
    return issues


def _check_value_invariants(
    descriptor: DatasetSchemaDescriptor, table: pa.Table
) -> list[CanonicalValidationIssue]:
    issues: list[CanonicalValidationIssue] = []
    for field in descriptor.fields:
        if field.field_type not in (FieldType.FLOAT64, FieldType.INT64):
            continue
        values = table.column(field.name).to_pylist()
        for index, value in enumerate(values):
            if value is None:
                continue
            if field.positive_required and not (isinstance(value, (int, float)) and value > 0):
                issues.append(
                    CanonicalValidationIssue(
                        "positive_required",
                        f"field {field.name!r} must be positive, got {value!r}",
                        row_index=index,
                    )
                )
            if field.non_negative_required and not (isinstance(value, (int, float)) and value >= 0):
                issues.append(
                    CanonicalValidationIssue(
                        "non_negative_required",
                        f"field {field.name!r} must be non-negative, got {value!r}",
                        row_index=index,
                    )
                )
    return issues


def _check_ohlc_relationships(
    descriptor: DatasetSchemaDescriptor, table: pa.Table
) -> list[CanonicalValidationIssue]:
    if descriptor.name not in {"daily_bar_unadjusted", "benchmark_index_daily"}:
        return []
    issues: list[CanonicalValidationIssue] = []
    values = {name: table.column(name).to_pylist() for name in ("open", "high", "low", "close")}
    for index in range(table.num_rows):
        open_price = values["open"][index]
        high = values["high"][index]
        low = values["low"][index]
        close = values["close"][index]
        if any(value is None for value in (open_price, high, low, close)):
            continue
        if high < max(open_price, close, low):
            issues.append(
                CanonicalValidationIssue(
                    "ohlc_high",
                    "high must be greater than or equal to open, close and low",
                    row_index=index,
                )
            )
        if low > min(open_price, close, high):
            issues.append(
                CanonicalValidationIssue(
                    "ohlc_low",
                    "low must be less than or equal to open, close and high",
                    row_index=index,
                )
            )
    return issues


def _check_trading_status_consistency(
    descriptor: DatasetSchemaDescriptor, table: pa.Table
) -> list[CanonicalValidationIssue]:
    if descriptor.name != "trading_status":
        return []
    issues: list[CanonicalValidationIssue] = []
    rows = table.to_pylist()
    for index, row in enumerate(rows):
        if row["is_trading"] and row["is_suspended"]:
            issues.append(
                CanonicalValidationIssue(
                    "trading_suspension_conflict",
                    "a row cannot be both trading and suspended",
                    row_index=index,
                )
            )
        if row["is_star_st"] and not row["is_st"]:
            issues.append(
                CanonicalValidationIssue(
                    "star_st_consistency",
                    "*ST status requires is_st=True",
                    row_index=index,
                )
            )
        if row["is_suspended"] and (
            row["limit_up_price"] is not None or row["limit_down_price"] is not None
        ):
            issues.append(
                CanonicalValidationIssue(
                    "suspended_limit_price",
                    "suspended rows must not carry limit prices",
                    row_index=index,
                )
            )
        if (
            not row["is_trading"]
            and not row["is_suspended"]
            and not (row["is_delisting_period"] or row["status_reason"])
        ):
            issues.append(
                CanonicalValidationIssue(
                    "non_trading_reason",
                    "non-trading non-suspended rows require a status reason",
                    row_index=index,
                )
            )
        up = row["limit_up_price"]
        down = row["limit_down_price"]
        if up is not None and down is not None and up < down:
            issues.append(
                CanonicalValidationIssue(
                    "limit_price_order",
                    "limit_up_price must not be below limit_down_price",
                    row_index=index,
                )
            )
    return issues


def _check_no_nan_inf(
    descriptor: DatasetSchemaDescriptor, table: pa.Table
) -> list[CanonicalValidationIssue]:
    issues: list[CanonicalValidationIssue] = []
    for field in descriptor.fields:
        if field.field_type is not FieldType.FLOAT64:
            continue
        values = table.column(field.name).to_pylist()
        for index, value in enumerate(values):
            if value is None:
                continue
            if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                issues.append(
                    CanonicalValidationIssue(
                        "no_nan_inf",
                        f"field {field.name!r} must not be NaN or infinity, got {value!r}",
                        row_index=index,
                    )
                )
    return issues


__all__: Sequence[str] = (
    "CANONICAL_SCHEMA_VERSION",
    "CanonicalValidationIssue",
    "CanonicalValidationResult",
    "canonical_schema",
    "ensure_sorted",
    "sort_key",
    "validate_table",
)
