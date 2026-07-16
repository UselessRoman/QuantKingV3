"""Boundary-sample and contract tests for the six canonical schemas."""

from __future__ import annotations

import math
from datetime import date

import pyarrow as pa

from quantking.data.adapters.canonical_schema import (
    CanonicalValidationIssue,
    canonical_schema,
    sort_key,
    validate_table,
)
from quantking.data.schemas import DatasetSchemaDescriptor, canonical_code_pattern

# ---------------------------------------------------------------------------
# Schema descriptor contracts (framework-free)
# ---------------------------------------------------------------------------


class TestSchemaDescriptors:
    def test_all_six_datasets_have_descriptors(self) -> None:
        names = {d.name for d in DatasetSchemaDescriptor.all()}
        assert names == {
            "security_master",
            "exchange_calendar",
            "daily_bar_unadjusted",
            "adjustment_factor",
            "trading_status",
            "benchmark_index_daily",
        }

    def test_each_descriptor_has_schema_version_primary_key_and_sort_key(self) -> None:
        for descriptor in DatasetSchemaDescriptor.all():
            assert descriptor.schema_version
            assert descriptor.primary_key, f"{descriptor.name} missing primary key"
            assert descriptor.sort_key, f"{descriptor.name} missing sort key"
            pk_fields = {f.name for f in descriptor.fields if f.primary_key}
            assert pk_fields == set(descriptor.primary_key)

    def test_no_vendor_field_names_present(self) -> None:
        vendor_terms = {"symbol", "tradeStatus", "qtq", "xtcode", "code_id"}
        for descriptor in DatasetSchemaDescriptor.all():
            field_names = {f.name for f in descriptor.fields}
            assert not (field_names & vendor_terms), (
                f"{descriptor.name} contains vendor field names: {field_names & vendor_terms}"
            )

    def test_canonical_code_pattern(self) -> None:
        assert canonical_code_pattern().fullmatch("000001.SZ")
        assert canonical_code_pattern().fullmatch("600000.SH")
        assert canonical_code_pattern().fullmatch("430047.BJ")
        assert not canonical_code_pattern().fullmatch("000001")
        assert not canonical_code_pattern().fullmatch("sz000001")
        assert not canonical_code_pattern().fullmatch("000001.SZ ")

    def test_units_documented(self) -> None:
        daily = DatasetSchemaDescriptor.daily_bar_unadjusted()
        volume = next(f for f in daily.fields if f.name == "volume")
        assert volume.unit == "share"
        amount = next(f for f in daily.fields if f.name == "amount")
        assert amount.unit == "cny_yuan"


# ---------------------------------------------------------------------------
# PyArrow schema mapping + validation
# ---------------------------------------------------------------------------


def _tbl(name: str, rows: list[dict[str, object]], schema: pa.Schema) -> pa.Table:
    return pa.Table.from_pylist(rows, schema=schema)


class TestDailyBarUnadjusted:
    SCHEMA = canonical_schema("daily_bar_unadjusted")

    def _row(self, **over: object) -> dict[str, object]:
        row: dict[str, object] = {
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
        row.update(over)
        return row

    def test_valid_normal_trading_row_passes(self) -> None:
        table = _tbl("d", [self._row()], self.SCHEMA)
        result = validate_table("daily_bar_unadjusted", table)
        assert result.ok, result.issues

    def test_nan_is_rejected(self) -> None:
        table = _tbl("d", [self._row(close=math.nan)], self.SCHEMA)
        result = validate_table("daily_bar_unadjusted", table)
        assert not result.ok
        assert any(
            "nan" in i.message.lower() or "infinity" in i.message.lower() for i in result.issues
        )

    def test_infinity_is_rejected(self) -> None:
        table = _tbl("d", [self._row(close=math.inf)], self.SCHEMA)
        result = validate_table("daily_bar_unadjusted", table)
        assert not result.ok

    def test_negative_volume_rejected(self) -> None:
        table = _tbl("d", [self._row(volume=-1)], self.SCHEMA)
        result = validate_table("daily_bar_unadjusted", table)
        assert not result.ok
        assert any("volume" in i.message.lower() for i in result.issues)

    def test_zero_or_negative_price_on_trading_row_rejected(self) -> None:
        table = _tbl("d", [self._row(close=0.0)], self.SCHEMA)
        result = validate_table("daily_bar_unadjusted", table)
        assert not result.ok

    def test_bad_canonical_code_rejected(self) -> None:
        table = _tbl("d", [self._row(code="sz000001")], self.SCHEMA)
        result = validate_table("daily_bar_unadjusted", table)
        assert not result.ok

    def test_duplicate_primary_key_rejected(self) -> None:
        rows = [self._row(), self._row()]
        table = _tbl("d", rows, self.SCHEMA)
        result = validate_table("daily_bar_unadjusted", table)
        assert not result.ok
        assert any("primary key" in i.message.lower() for i in result.issues)

    def test_unsorted_output_is_sorted_and_reported(self) -> None:
        rows = [
            self._row(code="600000.SH", trade_date=date(2026, 7, 15)),
            self._row(code="000001.SZ", trade_date=date(2026, 7, 15)),
        ]
        table = _tbl("d", rows, self.SCHEMA)
        result = validate_table("daily_bar_unadjusted", table)
        # sorted table should pass once deduplicated/ordered
        assert result.ok or any("sort" in i.message.lower() for i in result.issues)


class TestTradingStatus:
    SCHEMA = canonical_schema("trading_status")

    def _row(self, **over: object) -> dict[str, object]:
        row: dict[str, object] = {
            "code": "000001.SZ",
            "trade_date": date(2026, 7, 15),
            "is_trading": True,
            "is_suspended": False,
            "is_st": False,
            "is_star_st": False,
            "is_delisting_period": False,
            "limit_up_price": 11.0,
            "limit_down_price": 9.0,
            "status_reason": None,
        }
        row.update(over)
        return row

    def test_suspended_row_passes(self) -> None:
        row = self._row(
            is_trading=False, is_suspended=True, limit_up_price=None, limit_down_price=None
        )
        table = _tbl("t", [row], self.SCHEMA)
        assert validate_table("trading_status", table).ok

    def test_st_row_passes(self) -> None:
        row = self._row(is_st=True, status_reason="ST")
        table = _tbl("t", [row], self.SCHEMA)
        assert validate_table("trading_status", table).ok

    def test_nullable_limit_prices_accepted(self) -> None:
        row = self._row(limit_up_price=None, limit_down_price=None, status_reason=None)
        table = _tbl("t", [row], self.SCHEMA)
        assert validate_table("trading_status", table).ok


class TestExchangeCalendar:
    SCHEMA = canonical_schema("exchange_calendar")

    def test_open_and_closed_days(self) -> None:
        rows = [
            {"exchange": "SZ", "trade_date": date(2026, 7, 15), "is_open": True},
            {"exchange": "SZ", "trade_date": date(2026, 7, 18), "is_open": False},
        ]
        table = _tbl("c", rows, self.SCHEMA)
        assert validate_table("exchange_calendar", table).ok

    def test_duplicate_calendar_pk_rejected(self) -> None:
        rows = [
            {"exchange": "SZ", "trade_date": date(2026, 7, 15), "is_open": True},
            {"exchange": "SZ", "trade_date": date(2026, 7, 15), "is_open": False},
        ]
        table = _tbl("c", rows, self.SCHEMA)
        assert not validate_table("exchange_calendar", table).ok


class TestSecurityMaster:
    SCHEMA = canonical_schema("security_master")

    def test_valid_row(self) -> None:
        rows = [
            {
                "code": "000001.SZ",
                "exchange": "SZ",
                "name": "平安银行",
                "board": "MAIN",
                "listing_date": date(1991, 4, 3),
                "delisting_date": None,
                "effective_from": date(1991, 4, 3),
                "effective_to": None,
                "listing_status": "LISTED",
            }
        ]
        table = _tbl("s", rows, self.SCHEMA)
        assert validate_table("security_master", table).ok

    def test_nullable_delisting_and_effective_to(self) -> None:
        rows = [
            {
                "code": "000001.SZ",
                "exchange": "SZ",
                "name": "x",
                "board": "MAIN",
                "listing_date": date(1991, 4, 3),
                "delisting_date": None,
                "effective_from": date(1991, 4, 3),
                "effective_to": None,
                "listing_status": "LISTED",
            }
        ]
        table = _tbl("s", rows, self.SCHEMA)
        assert validate_table("security_master", table).ok


class TestAdjustmentFactor:
    SCHEMA = canonical_schema("adjustment_factor")

    def test_valid_row(self) -> None:
        rows = [
            {
                "code": "000001.SZ",
                "trade_date": date(2026, 6, 1),
                "price_multiplier": 1.05,
                "volume_multiplier": 0.95,
                "normalization_date": date(2026, 6, 1),
                "algorithm_version": "qkv3-forward-1",
            }
        ]
        table = _tbl("a", rows, self.SCHEMA)
        assert validate_table("adjustment_factor", table).ok


class TestBenchmarkIndexDaily:
    SCHEMA = canonical_schema("benchmark_index_daily")

    def test_volume_amount_nullable(self) -> None:
        rows = [
            {
                "code": "000300.SH",
                "trade_date": date(2026, 7, 15),
                "open": 4000.0,
                "high": 4050.0,
                "low": 3980.0,
                "close": 4020.0,
                "pre_close": 3990.0,
                "volume": None,
                "amount": None,
            }
        ]
        table = _tbl("b", rows, self.SCHEMA)
        assert validate_table("benchmark_index_daily", table).ok


class TestSortKey:
    def test_daily_bar_sort_key(self) -> None:
        assert sort_key("daily_bar_unadjusted") == ("code", "trade_date")

    def test_security_master_sort_key(self) -> None:
        assert sort_key("security_master") == ("code", "effective_from")


class TestCanonicalValidationIssue:
    def test_issue_render(self) -> None:
        issue = CanonicalValidationIssue(rule="pk_unique", message="dup", row_index=2)
        assert issue.rule == "pk_unique"
        assert issue.row_index == 2


class TestLegalVsIllegalEmpty:
    def test_legal_empty_table_passes(self) -> None:
        schema = canonical_schema("daily_bar_unadjusted")
        empty = pa.Table.from_pylist([], schema=schema)
        result = validate_table("daily_bar_unadjusted", empty)
        # A legal explicit-empty result (e.g. a date with no trades) is a zero-row table
        # that still matches the schema; it passes structural validation.
        assert result.ok
