"""Red tests for S1-04-R2 override transformation fixes.

Tests cover:
* Support for FALLBACK_ONLY rows (fallback has row, primary doesn't).
* Rejection of zero-match scope (no rows found in either table).
* Rejection of partial-match scope (some approved pairs not found).
* Prohibition of primary-row deletion (only cell replacement allowed).
* Full consumption of approved scope (every code/date pair must be used).
"""

from __future__ import annotations

from datetime import date

import pyarrow as pa
import pytest

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


class TestOverrideTransformationR2Fixes:
    def test_supports_explicit_fallback_only_rows(self) -> None:
        """Fallback has an explicitly approved row that primary does not."""
        day = date(2026, 7, 15)
        primary = _table([_row("000001.SZ", day, 10.0)])
        # Fallback has the same row plus an extra one.
        fallback = _table([_row("000001.SZ", day, 10.5), _row("000002.SZ", day, 20.0)])

        result = apply_override_to_table(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            primary_table=primary,
            fallback_table=fallback,
            field_replacements=(
                {"code": "000001.SZ", "trade_date": day.isoformat(), "field": "close"},
            ),
            fallback_only_keys=({"code": "000002.SZ", "trade_date": day.isoformat()},),
        )
        rows = result.table.to_pylist()
        assert len(rows) == 2
        assert rows[0]["code"] == "000001.SZ"
        assert rows[0]["close"] == 10.5  # overridden
        assert rows[1]["code"] == "000002.SZ"
        assert rows[1]["close"] == 20.0  # added from fallback
        assert result.matched_rows == 2

    def test_does_not_insert_unapproved_fallback_only_rows(self) -> None:
        """Rows absent from the explicit approval remain outside the result."""
        day = date(2026, 7, 15)
        primary = _table([_row("000001.SZ", day, 10.0)])
        fallback = _table([_row("000001.SZ", day, 10.5), _row("000002.SZ", day, 20.0)])

        result = apply_override_to_table(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            primary_table=primary,
            fallback_table=fallback,
            field_replacements=(
                {"code": "000001.SZ", "trade_date": day.isoformat(), "field": "close"},
            ),
        )
        assert [row["code"] for row in result.table.to_pylist()] == ["000001.SZ"]

    def test_rejects_zero_match_scope(self) -> None:
        """Approved scope matches no rows in either table."""
        day = date(2026, 7, 15)
        primary = _table([_row("000001.SZ", day, 10.0)])
        fallback = _table([_row("000001.SZ", day, 10.5)])

        with pytest.raises(ValueError, match="at least one exact difference unit"):
            apply_override_to_table(
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                primary_table=primary,
                fallback_table=fallback,
            )

    def test_rejects_partial_match_scope(self) -> None:
        """Some approved (code, date) pairs are not found in available data."""
        day1 = date(2026, 7, 15)
        day2 = date(2026, 7, 16)
        primary = _table([_row("000001.SZ", day1, 10.0)])
        fallback = _table([_row("000001.SZ", day1, 10.5)])

        # Approve two days but only one exists.
        with pytest.raises(ValueError, match="only partially consumed"):
            apply_override_to_table(
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                primary_table=primary,
                fallback_table=fallback,
                field_replacements=(
                    {
                        "code": "000001.SZ",
                        "trade_date": day1.isoformat(),
                        "field": "close",
                    },
                    {
                        "code": "000001.SZ",
                        "trade_date": day2.isoformat(),
                        "field": "close",
                    },
                ),
            )

    def test_prohibits_primary_key_field_override(self) -> None:
        """Primary key fields cannot be in the override field list."""
        day = date(2026, 7, 15)
        primary = _table([_row("000001.SZ", day, 10.0)])
        fallback = _table([_row("000001.SZ", day, 10.5)])

        with pytest.raises(ValueError, match="invalid field"):
            apply_override_to_table(
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                primary_table=primary,
                fallback_table=fallback,
                field_replacements=(
                    {"code": "000001.SZ", "trade_date": day.isoformat(), "field": "code"},
                ),
            )

    def test_full_scope_consumption_with_multiple_codes_and_dates(self) -> None:
        """All (code, date) pairs in scope must be found and consumed."""
        day1 = date(2026, 7, 15)
        day2 = date(2026, 7, 16)
        primary = _table(
            [
                _row("000001.SZ", day1, 10.0),
                _row("000001.SZ", day2, 11.0),
                _row("000002.SZ", day1, 20.0),
                _row("000002.SZ", day2, 21.0),
            ]
        )
        fallback = _table(
            [
                _row("000001.SZ", day1, 10.5),
                _row("000001.SZ", day2, 11.5),
                _row("000002.SZ", day1, 20.5),
                _row("000002.SZ", day2, 21.5),
            ]
        )

        result = apply_override_to_table(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            primary_table=primary,
            fallback_table=fallback,
            field_replacements=tuple(
                {
                    "code": code,
                    "trade_date": trading_day.isoformat(),
                    "field": "close",
                }
                for code in ("000001.SZ", "000002.SZ")
                for trading_day in (day1, day2)
            ),
        )
        rows = result.table.to_pylist()
        assert len(rows) == 4
        assert result.matched_rows == 4
        # All close values should be from fallback.
        assert rows[0]["close"] == 10.5
        assert rows[1]["close"] == 11.5
        assert rows[2]["close"] == 20.5
        assert rows[3]["close"] == 21.5


__all__ = ["TestOverrideTransformationR2Fixes"]
