"""Dataset-aware fixed-sample coverage evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta

from quantking.data.models import DatasetKind, EmptyResultInfo

_DATED = frozenset(
    {
        DatasetKind.DAILY_BAR_UNADJUSTED,
        DatasetKind.ADJUSTMENT_FACTOR,
        DatasetKind.TRADING_STATUS,
        DatasetKind.BENCHMARK_INDEX_DAILY,
    }
)


def _date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _natural_days(start: date, end: date) -> frozenset[date]:
    return frozenset(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def evaluate_fixed_sample_coverage(
    *,
    dataset: DatasetKind,
    rows: Sequence[Mapping[str, object]],
    cases: Sequence[Mapping[str, object]],
    calendar_rows: Sequence[Mapping[str, object]],
    empty_result: EmptyResultInfo | None = None,
) -> bool:
    """Return true only when every controlled case is completely represented."""
    if not cases:
        return False
    if not rows:
        return False
    for case in cases:
        start = _date(case["start_date"])
        end = _date(case["end_date"])
        exchange = str(case["exchange"])
        code = case.get("code")
        if dataset is DatasetKind.EXCHANGE_CALENDAR:
            expected = _natural_days(start, end)
            actual = {_date(row["trade_date"]) for row in rows if row.get("exchange") == exchange}
            if not expected.issubset(actual):
                return False
            continue
        if dataset is DatasetKind.SECURITY_MASTER:
            if not isinstance(code, str) or code.rsplit(".", 1)[-1] != exchange:
                return False
            intersects = any(
                row.get("code") == code
                and row.get("exchange") == exchange
                and _date(row["effective_from"]) <= end
                and (row.get("effective_to") is None or _date(row["effective_to"]) >= start)
                for row in rows
            )
            if not intersects:
                return False
            continue
        if dataset in _DATED:
            if not isinstance(code, str) or code.rsplit(".", 1)[-1] != exchange:
                return False
            reference_dates = {
                _date(row["trade_date"])
                for row in calendar_rows
                if row.get("exchange") == exchange and start <= _date(row["trade_date"]) <= end
            }
            if reference_dates != _natural_days(start, end):
                return False
            calendar = {
                _date(row["trade_date"])
                for row in calendar_rows
                if row.get("exchange") == exchange
                and row.get("is_open") is True
                and start <= _date(row["trade_date"]) <= end
            }
            if not calendar:
                return False
            actual = {_date(row["trade_date"]) for row in rows if row.get("code") == code}
            if not calendar.issubset(actual):
                return False
            continue
        return False
    return True


__all__ = ["evaluate_fixed_sample_coverage"]
