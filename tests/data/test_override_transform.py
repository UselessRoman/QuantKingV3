"""Scope and field isolation tests for SourceOverride transformation."""

from __future__ import annotations

from datetime import date

import pyarrow as pa

from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.adapters.override_transform import apply_override_to_table
from quantking.data.models import DatasetKind


def _table(rows: list[dict[str, object]]) -> pa.Table:
    raw = pa.Table.from_pylist(rows)
    schema = canonical_schema("daily_bar_unadjusted")
    return pa.Table.from_arrays(
        [raw.column(field.name).cast(field.type, safe=False) for field in schema],
        schema=schema,
    )


def _row(code: str, day: date, close: float, open_price: float = 10.0) -> dict[str, object]:
    return {
        "code": code,
        "trade_date": day,
        "open": open_price,
        "high": max(open_price, close) + 1.0,
        "low": min(open_price, close) - 1.0,
        "close": close,
        "pre_close": open_price,
        "volume": 100,
        "amount": 1000.0,
    }


def test_override_changes_only_approved_cells_and_scope() -> None:
    day = date(2026, 7, 15)
    other_day = date(2026, 7, 16)
    primary = _table([_row("000001.SZ", day, 10.0), _row("000001.SZ", other_day, 11.0)])
    fallback = _table(
        [_row("000001.SZ", day, 10.5, 20.0), _row("000001.SZ", other_day, 13.0, 20.0)]
    )

    result = apply_override_to_table(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        primary_table=primary,
        fallback_table=fallback,
        scope_codes=frozenset({"000001.SZ"}),
        scope_dates=frozenset({day}),
        fields=frozenset({"close"}),
    )
    rows = result.table.to_pylist()
    assert rows[0]["close"] == 10.5
    assert rows[0]["open"] == 10.0
    assert rows[1] == primary.to_pylist()[1]
    assert result.matched_rows == 1
    assert result.changed_cells == 1
