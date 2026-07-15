"""Distinct UTC timestamp and exchange trading-date value objects."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from quantking.common.errors import InvalidTimeError

Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class UtcTimestamp:
    """An aware datetime normalized to UTC."""

    value: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.value, datetime):
            raise InvalidTimeError("UTC timestamp must wrap a datetime")
        if self.value.tzinfo is None or self.value.utcoffset() is None:
            raise InvalidTimeError("UTC timestamp must be timezone-aware")
        object.__setattr__(self, "value", self.value.astimezone(UTC))

    @classmethod
    def from_iso(cls, raw: str) -> UtcTimestamp:
        """Parse an aware ISO-8601 value and normalize it to UTC."""
        if not isinstance(raw, str) or not raw.strip():
            raise InvalidTimeError("UTC timestamp text must be non-empty")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as error:
            raise InvalidTimeError("UTC timestamp text is not valid ISO-8601") from error
        return cls(parsed)

    def to_iso(self) -> str:
        """Render a canonical ISO-8601 UTC value with the ``Z`` suffix."""
        return self.value.isoformat().removesuffix("+00:00") + "Z"

    def __str__(self) -> str:
        return self.to_iso()


@dataclass(frozen=True, slots=True)
class TradingDate:
    """An exchange-local calendar date, deliberately not a timestamp."""

    value: date

    def __post_init__(self) -> None:
        if type(self.value) is not date:
            raise InvalidTimeError("trading date must wrap a date, not a datetime")

    @classmethod
    def from_iso(cls, raw: str) -> TradingDate:
        """Parse an ISO calendar date."""
        if not isinstance(raw, str) or not raw.strip():
            raise InvalidTimeError("trading date text must be non-empty")
        try:
            parsed = date.fromisoformat(raw)
        except ValueError as error:
            raise InvalidTimeError("trading date text is not a valid ISO date") from error
        return cls(parsed)

    def __str__(self) -> str:
        return self.value.isoformat()


def utc_now(*, clock: Clock | None = None) -> UtcTimestamp:
    """Read the current UTC time through an optionally injected clock."""
    current = clock() if clock is not None else datetime.now(UTC)
    return UtcTimestamp(current)
