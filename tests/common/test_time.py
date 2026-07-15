from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from quantking.common.errors import InvalidTimeError
from quantking.common.time import TradingDate, UtcTimestamp, utc_now


def test_utc_timestamp_rejects_naive_datetime() -> None:
    with pytest.raises(InvalidTimeError):
        UtcTimestamp(datetime(2026, 7, 15, 9, 30))


def test_utc_timestamp_normalizes_offset_and_round_trips_z_iso() -> None:
    timestamp = UtcTimestamp(
        datetime(2026, 7, 15, 17, 30, 12, 345678, tzinfo=timezone(timedelta(hours=8)))
    )

    assert timestamp.value == datetime(2026, 7, 15, 9, 30, 12, 345678, tzinfo=UTC)
    assert timestamp.to_iso() == "2026-07-15T09:30:12.345678Z"
    assert UtcTimestamp.from_iso(timestamp.to_iso()) == timestamp


def test_utc_now_uses_injected_clock() -> None:
    expected = datetime(2026, 7, 15, 9, 30, tzinfo=UTC)

    assert utc_now(clock=lambda: expected) == UtcTimestamp(expected)


def test_trading_date_accepts_date_but_rejects_datetime() -> None:
    trading_date = TradingDate(date(2026, 7, 15))

    assert str(trading_date) == "2026-07-15"
    assert TradingDate.from_iso("2026-07-15") == trading_date
    with pytest.raises(InvalidTimeError):
        TradingDate(datetime(2026, 7, 15, tzinfo=UTC))


def test_trading_date_and_timestamp_are_not_interchangeable() -> None:
    trading_date = TradingDate(date(2026, 7, 15))
    timestamp = UtcTimestamp(datetime(2026, 7, 15, tzinfo=UTC))

    assert trading_date != timestamp
    assert trading_date != date(2026, 7, 15)
    assert timestamp != timestamp.value
