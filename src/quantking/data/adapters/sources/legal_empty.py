"""Legal empty-result classification against the bound published calendar.

A supplier zero-row outcome is legal only when the frozen, published
exchange_calendar (bound by version id and content SHA) proves that every
requested security has no expected trading session in the requested range
after applying the frozen listing/suspension exceptions. The calendar itself
must cover every natural day of the frozen request for every involved
exchange: a truncated calendar is a blocking reference defect, never an
implicit holiday. Weekday heuristics, request ranges, vendor emptiness or
fixed text never substitute for the versioned calendar.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

import pyarrow as pa

from quantking.common.time import TradingDate
from quantking.data.adapters.canonical_schema import validate_table
from quantking.data.models import (
    DatasetKind,
    EmptyResultInfo,
    LegalEmptyClassification,
)

_ZERO_ROW_REASON = "frozen calendar proves no expected trading session for the requested scope"


class CalendarReferenceIncompleteError(ValueError):
    """The bound calendar misses records for the frozen request coverage.

    Calendar gaps must fail closed: missing dates or missing exchanges can
    never be interpreted as closed sessions, so this error is raised before
    any network call and blocks the whole EOD job.
    """


@dataclass(frozen=True, slots=True)
class ExpectedSessionResolver:
    """Classify whole-range zero-row supplier outcomes per requested chunk.

    The calendar table must already be loaded through the published-version
    path (PUBLISHED status, dataset type, version id and content SHA all
    verified before any network call). Construction additionally verifies
    that every involved exchange has exactly one record for every natural day
    of the frozen coverage window, and fails closed otherwise.
    """

    calendar_table: pa.Table
    expected_securities: tuple[str, ...]
    security_exceptions: Mapping[str, tuple[tuple[TradingDate, TradingDate], ...]]
    coverage_start: date
    coverage_end: date
    _sessions: dict[str, set[date]] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        if not isinstance(self.calendar_table, pa.Table):
            raise TypeError("calendar_table must be a pyarrow Table")
        validation = validate_table(DatasetKind.EXCHANGE_CALENDAR.value, self.calendar_table)
        if not validation.ok:
            first = validation.issues[0]
            raise ValueError(
                "calendar_table is not a canonical exchange_calendar: "
                f"{first.rule}: {first.message}"
            )
        if (
            not isinstance(self.expected_securities, tuple)
            or not self.expected_securities
            or tuple(sorted(set(self.expected_securities))) != self.expected_securities
        ):
            raise ValueError("expected_securities must be a sorted unique non-empty tuple")
        if not isinstance(self.security_exceptions, Mapping):
            raise TypeError("security_exceptions must be a mapping")
        unknown = set(self.security_exceptions) - set(self.expected_securities)
        if unknown:
            raise ValueError("security_exceptions reference securities outside the scope")
        if not isinstance(self.coverage_start, date) or not isinstance(self.coverage_end, date):
            raise TypeError("coverage bounds must be dates")
        if self.coverage_end < self.coverage_start:
            raise ValueError("coverage_end must not precede coverage_start")
        self._verify_complete_coverage()
        self._build_sessions()

    def _verify_complete_coverage(self) -> None:
        """Require exactly one record per involved exchange per natural day."""
        records: dict[tuple[str, date], int] = {}
        for row in self.calendar_table.to_pylist():
            exchange = row["exchange"]
            trade_date = row["trade_date"]
            if not isinstance(exchange, str) or not isinstance(trade_date, date):
                raise ValueError("calendar exchange/trade_date types are invalid")
            key = (exchange, trade_date)
            records[key] = records.get(key, 0) + 1
        exchanges = sorted({security.rsplit(".", 1)[-1] for security in self.expected_securities})
        missing: list[str] = []
        day = self.coverage_start
        while day <= self.coverage_end:
            for exchange in exchanges:
                if records.get((exchange, day), 0) != 1:
                    missing.append(f"{exchange}:{day.isoformat()}")
            day += timedelta(days=1)
        if missing:
            raise CalendarReferenceIncompleteError(
                "calendar reference is incomplete for the frozen coverage: "
                + ",".join(missing[:10])
                + ("" if len(missing) <= 10 else f",...({len(missing)} total)")
            )

    def _build_sessions(self) -> None:
        sessions: dict[str, set[date]] = {}
        for row in self.calendar_table.to_pylist():
            exchange = row["exchange"]
            trade_date = row["trade_date"]
            if row["is_open"]:
                sessions.setdefault(exchange, set()).add(trade_date)
        object.__setattr__(self, "_sessions", sessions)

    def classify(
        self,
        *,
        symbols: tuple[str, ...],
        start_date: date,
        end_date: date,
    ) -> EmptyResultInfo | None:
        """Return a legal empty classification only when no symbol has an expected session."""
        if not isinstance(symbols, tuple) or not symbols or tuple(sorted(set(symbols))) != symbols:
            raise ValueError("symbols must be a sorted unique non-empty tuple")
        if any(symbol not in self.expected_securities for symbol in symbols):
            raise ValueError("symbols must belong to the frozen scope")
        if not isinstance(start_date, date) or not isinstance(end_date, date):
            raise TypeError("request bounds must be dates")
        if start_date > end_date:
            raise ValueError("request start must not follow end")
        for symbol in symbols:
            exchange = symbol.rsplit(".", 1)[-1]
            sessions = self._sessions.get(exchange, set())
            exceptions = self.security_exceptions.get(symbol, ())
            for trade_date in sessions:
                if not start_date <= trade_date <= end_date:
                    continue
                if any(start.value <= trade_date <= end.value for start, end in exceptions):
                    continue
                return None
        return EmptyResultInfo(
            classification=LegalEmptyClassification.NO_TRADING_SESSION,
            reason=_ZERO_ROW_REASON,
        )


__all__ = ["CalendarReferenceIncompleteError", "ExpectedSessionResolver"]
