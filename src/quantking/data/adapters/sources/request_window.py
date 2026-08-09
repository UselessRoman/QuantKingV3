"""Fixed K1 target and upstream request-window binding."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

AKSHARE_LOOKBACK_DAYS = 15


@dataclass(frozen=True, slots=True)
class AkshareRequestWindow:
    """Bind the user-visible target range to its bounded upstream context range."""

    target_start: date
    target_end: date
    upstream_start: date
    upstream_end: date
    lookback_days: int

    def __post_init__(self) -> None:
        for field_name in ("target_start", "target_end", "upstream_start", "upstream_end"):
            if not isinstance(getattr(self, field_name), date):
                raise TypeError(f"{field_name} must be a date")
        if self.target_start > self.target_end:
            raise ValueError("target_start must not exceed target_end")
        if self.lookback_days != AKSHARE_LOOKBACK_DAYS:
            raise ValueError("lookback_days must equal the fixed K1 lookback")
        if self.upstream_start != self.target_start - timedelta(days=self.lookback_days):
            raise ValueError("upstream_start must equal target_start minus lookback_days")
        if self.upstream_end != self.target_end:
            raise ValueError("upstream_end must equal target_end")

    def to_payload(self) -> dict[str, object]:
        return {
            "target_start": self.target_start.isoformat(),
            "target_end": self.target_end.isoformat(),
            "upstream_start": self.upstream_start.isoformat(),
            "upstream_end": self.upstream_end.isoformat(),
            "lookback_days": self.lookback_days,
        }

    @classmethod
    def from_payload(cls, value: object) -> AkshareRequestWindow:
        if not isinstance(value, dict) or set(value) != {
            "target_start",
            "target_end",
            "upstream_start",
            "upstream_end",
            "lookback_days",
        }:
            raise ValueError("invalid request window payload")
        lookback_days = value["lookback_days"]
        if isinstance(lookback_days, bool) or not isinstance(lookback_days, int):
            raise ValueError("lookback_days must be an integer")
        try:
            return cls(
                target_start=datetime.strptime(str(value["target_start"]), "%Y-%m-%d").date(),
                target_end=datetime.strptime(str(value["target_end"]), "%Y-%m-%d").date(),
                upstream_start=datetime.strptime(str(value["upstream_start"]), "%Y-%m-%d").date(),
                upstream_end=datetime.strptime(str(value["upstream_end"]), "%Y-%m-%d").date(),
                lookback_days=lookback_days,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("invalid request window payload") from error


def fixed_akshare_request_window(target_start: date, target_end: date) -> AkshareRequestWindow:
    if not isinstance(target_start, date) or not isinstance(target_end, date):
        raise TypeError("target range must contain dates")
    return AkshareRequestWindow(
        target_start=target_start,
        target_end=target_end,
        upstream_start=target_start - timedelta(days=AKSHARE_LOOKBACK_DAYS),
        upstream_end=target_end,
        lookback_days=AKSHARE_LOOKBACK_DAYS,
    )


__all__ = [
    "AKSHARE_LOOKBACK_DAYS",
    "AkshareRequestWindow",
    "fixed_akshare_request_window",
]
