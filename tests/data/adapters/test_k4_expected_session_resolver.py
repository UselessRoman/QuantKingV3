from __future__ import annotations

from datetime import date, timedelta

import pyarrow as pa
import pytest

from quantking.common.time import TradingDate
from quantking.data.adapters.canonical_schema import canonical_schema, ensure_sorted
from quantking.data.adapters.sources.legal_empty import (
    CalendarReferenceIncompleteError,
    K4ExpectedSessionResolver,
)
from quantking.data.models import DatasetKind, LegalEmptyClassification

_START = date(2026, 8, 5)
_END = date(2026, 8, 7)


def _calendar(
    open_sessions: set[tuple[str, date]],
    *,
    skip: set[tuple[str, date]] | None = None,
    exchanges: tuple[str, ...] = ("SZ", "SH"),
) -> pa.Table:
    skip = skip or set()
    rows: list[dict[str, object]] = []
    for exchange in exchanges:
        day = _START
        while day <= _END:
            if (exchange, day) not in skip:
                rows.append(
                    {
                        "exchange": exchange,
                        "trade_date": day,
                        "is_open": (exchange, day) in open_sessions,
                    }
                )
            day += timedelta(days=1)
    return ensure_sorted(
        DatasetKind.EXCHANGE_CALENDAR.value,
        pa.Table.from_pylist(rows, schema=canonical_schema(DatasetKind.EXCHANGE_CALENDAR.value)),
    )


def _resolver(
    open_sessions: set[tuple[str, date]],
    *,
    securities: tuple[str, ...] = ("000001.SZ", "600000.SH"),
    exceptions: dict[str, tuple[tuple[TradingDate, TradingDate], ...]] | None = None,
    calendar_table: pa.Table | None = None,
) -> K4ExpectedSessionResolver:
    return K4ExpectedSessionResolver(
        calendar_table=(_calendar(open_sessions) if calendar_table is None else calendar_table),
        expected_securities=securities,
        security_exceptions=exceptions or {},
        coverage_start=_START,
        coverage_end=_END,
    )


def test_every_symbol_without_expected_sessions_yields_legal_empty() -> None:
    resolver = _resolver(set())
    result = resolver.classify(
        symbols=("000001.SZ", "600000.SH"),
        start_date=_START,
        end_date=_END,
    )
    assert result is not None
    assert result.classification is LegalEmptyClassification.NO_TRADING_SESSION


def test_any_expected_session_yields_none() -> None:
    resolver = _resolver({("SZ", date(2026, 8, 6))})
    result = resolver.classify(
        symbols=("000001.SZ", "600000.SH"),
        start_date=_START,
        end_date=_END,
    )
    assert result is None


def test_exception_covers_the_only_session_and_restores_legal_empty() -> None:
    resolver = _resolver(
        {("SH", date(2026, 8, 6))},
        exceptions={"600000.SH": ((TradingDate(date(2026, 8, 6)), TradingDate(date(2026, 8, 6))),)},
    )
    result = resolver.classify(
        symbols=("000001.SZ", "600000.SH"),
        start_date=_START,
        end_date=_END,
    )
    assert result is not None
    # A partial exception never restores legality.
    partial = _resolver(
        {("SH", date(2026, 8, 6)), ("SH", date(2026, 8, 7))},
        exceptions={"600000.SH": ((TradingDate(date(2026, 8, 6)), TradingDate(date(2026, 8, 6))),)},
    )
    assert (
        partial.classify(
            symbols=("000001.SZ", "600000.SH"),
            start_date=_START,
            end_date=_END,
        )
        is None
    )


def test_out_of_range_sessions_do_not_block_legal_empty() -> None:
    resolver = _resolver({("SZ", date(2026, 8, 7))})
    result = resolver.classify(
        symbols=("000001.SZ", "600000.SH"),
        start_date=date(2026, 8, 5),
        end_date=date(2026, 8, 6),
    )
    assert result is not None


def test_unknown_symbol_or_non_canonical_calendar_is_rejected() -> None:
    resolver = _resolver(set())
    with pytest.raises(ValueError, match="frozen scope"):
        resolver.classify(
            symbols=("300750.SZ",),
            start_date=_START,
            end_date=_END,
        )
    with pytest.raises(ValueError, match="canonical"):
        K4ExpectedSessionResolver(
            calendar_table=pa.table({"exchange": ["SZ"]}),
            expected_securities=("000001.SZ",),
            security_exceptions={},
            coverage_start=_START,
            coverage_end=_END,
        )
    with pytest.raises(ValueError, match="outside the scope"):
        K4ExpectedSessionResolver(
            calendar_table=_calendar(set()),
            expected_securities=("000001.SZ",),
            security_exceptions={
                "600000.SH": ((TradingDate(date(2026, 8, 6)), TradingDate(date(2026, 8, 6))),)
            },
            coverage_start=_START,
            coverage_end=_END,
        )


# ---------------------------------------------------------------------------
# Calendar completeness: missing records must never read as closed sessions.
# ---------------------------------------------------------------------------


def test_missing_request_date_is_rejected_as_incomplete() -> None:
    truncated = _calendar(set(), skip={("SZ", date(2026, 8, 6))})
    with pytest.raises(CalendarReferenceIncompleteError, match="SZ:2026-08-06"):
        _resolver(set(), calendar_table=truncated)


def test_missing_exchange_is_rejected_as_incomplete() -> None:
    truncated = _calendar(set(), exchanges=("SZ",))  # SH missing entirely
    with pytest.raises(CalendarReferenceIncompleteError, match="SH:"):
        _resolver(set(), calendar_table=truncated)


def test_complete_all_closed_calendar_allows_legal_empty() -> None:
    # Every natural day present and explicitly is_open=False -> legal empty.
    resolver = _resolver(set())
    result = resolver.classify(
        symbols=("000001.SZ", "600000.SH"),
        start_date=_START,
        end_date=_END,
    )
    assert result is not None
    assert result.classification is LegalEmptyClassification.NO_TRADING_SESSION


def test_security_exceptions_cannot_mask_calendar_gaps() -> None:
    truncated = _calendar(
        {("SH", date(2026, 8, 6))},
        skip={("SZ", date(2026, 8, 7))},
    )
    with pytest.raises(CalendarReferenceIncompleteError, match="SZ:2026-08-07"):
        _resolver(
            set(),
            calendar_table=truncated,
            exceptions={
                "000001.SZ": ((TradingDate(date(2026, 8, 7)), TradingDate(date(2026, 8, 7))),)
            },
        )
