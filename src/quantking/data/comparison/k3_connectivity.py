"""Bounded S1-07K3B AKShare upstream connectivity probe (offline-safe contracts)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from types import MappingProxyType

from quantking.data.models import DataProvider, DataUpstream

K3_CONNECTIVITY_PROBE_MARKER = "S1-07K3B-AKSHARE-CONNECTIVITY-PROBE-V1"
K3_CONNECTIVITY_SYMBOL = "000001.SZ"
K3_CONNECTIVITY_TARGET_DATE = date(2026, 6, 11)
K3_CONNECTIVITY_LOOKBACK_DAYS = 15
K3_CONNECTIVITY_UPSTREAMS = (
    DataUpstream.EASTMONEY,
    DataUpstream.TENCENT,
    DataUpstream.SINA,
)
K3_CONNECTIVITY_MAX_INVOCATIONS = len(K3_CONNECTIVITY_UPSTREAMS)


@dataclass(frozen=True, slots=True)
class K3ConnectivityInvocation:
    sequence: int
    provider: DataProvider
    upstream: DataUpstream
    symbol: str
    target_date: date
    upstream_start: date
    upstream_end: date

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool):
            raise TypeError("sequence must be an integer")
        if self.provider is not DataProvider.AKSHARE:
            raise ValueError("connectivity probe invocations must use AKSHARE")
        if self.upstream not in K3_CONNECTIVITY_UPSTREAMS:
            raise ValueError("connectivity probe upstream is not frozen")
        if not isinstance(self.symbol, str) or self.symbol != K3_CONNECTIVITY_SYMBOL:
            raise ValueError("connectivity probe symbol is not frozen")

    def to_payload(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "provider": self.provider.value,
            "upstream": self.upstream.value,
            "symbol": self.symbol,
            "target_date": self.target_date.isoformat(),
            "upstream_start": self.upstream_start.isoformat(),
            "upstream_end": self.upstream_end.isoformat(),
        }


def fixed_k3_connectivity_plan() -> tuple[K3ConnectivityInvocation, ...]:
    result = tuple(
        K3ConnectivityInvocation(
            sequence=index,
            provider=DataProvider.AKSHARE,
            upstream=upstream,
            symbol=K3_CONNECTIVITY_SYMBOL,
            target_date=K3_CONNECTIVITY_TARGET_DATE,
            upstream_start=K3_CONNECTIVITY_TARGET_DATE
            - timedelta(days=K3_CONNECTIVITY_LOOKBACK_DAYS),
            upstream_end=K3_CONNECTIVITY_TARGET_DATE,
        )
        for index, upstream in enumerate(K3_CONNECTIVITY_UPSTREAMS, start=1)
    )
    if len(result) != K3_CONNECTIVITY_MAX_INVOCATIONS:
        raise RuntimeError("K3 connectivity probe budget plan is inconsistent")
    return result


def k3_connectivity_dry_run_payload() -> MappingProxyType[str, object]:
    plan = fixed_k3_connectivity_plan()
    return MappingProxyType(
        {
            "schema_version": "s1-07k3b-akshare-connectivity-plan.v1",
            "mode": "dry-run",
            "marker": K3_CONNECTIVITY_PROBE_MARKER,
            "network_calls_made": 0,
            "application_retries": 0,
            "failure_policy": "RUN_ALL_BOUNDED_NO_RETRY",
            "writes_formal_data_root": False,
            "target_date": K3_CONNECTIVITY_TARGET_DATE.isoformat(),
            "lookback_days": K3_CONNECTIVITY_LOOKBACK_DAYS,
            "market_data_endpoint_invocations": {
                "EASTMONEY": 1,
                "TENCENT": 1,
                "SINA": 1,
                "total": K3_CONNECTIVITY_MAX_INVOCATIONS,
            },
            "akshare_locked_source_http_upper_bound": {
                "EASTMONEY": 1,
                "TENCENT": 3,
                "SINA": 2,
                "total": 6,
            },
            "invocations": [item.to_payload() for item in plan],
        }
    )


@dataclass(frozen=True, slots=True)
class K3ConnectivityOutcome:
    upstream: DataUpstream
    succeeded: bool
    stable_code: str | None
    message: str | None
    complete: bool | None
    canonical_row_count: int | None
    library_version: str | None
    volume_unit: str | None
    amount_unit: str | None

    def __post_init__(self) -> None:
        if self.upstream not in K3_CONNECTIVITY_UPSTREAMS:
            raise ValueError("connectivity outcome upstream is not frozen")
        if self.succeeded == (self.stable_code is not None):
            raise ValueError("stable_code must be present exactly for failed outcomes")
        for field in (
            "complete",
            "canonical_row_count",
            "library_version",
            "volume_unit",
            "amount_unit",
        ):
            value = getattr(self, field)
            if value is None and self.succeeded:
                raise ValueError(f"successful outcome requires {field}")
            if value is not None and not self.succeeded:
                raise ValueError(f"failed outcome must not carry {field}")

    def to_payload(self) -> dict[str, object]:
        return {
            "upstream": self.upstream.value,
            "outcome": "SUCCEEDED" if self.succeeded else "FAILED",
            "stable_code": self.stable_code,
            "message": self.message,
            "complete": self.complete,
            "canonical_row_count": self.canonical_row_count,
            "library_version": self.library_version,
            "volume_unit": self.volume_unit,
            "amount_unit": self.amount_unit,
        }


def summarize_k3_connectivity(
    outcomes: tuple[K3ConnectivityOutcome, ...],
) -> MappingProxyType[str, object]:
    """Validate the bounded per-upstream result set and return stable evidence."""
    if len(outcomes) != K3_CONNECTIVITY_MAX_INVOCATIONS:
        raise ValueError("connectivity summary requires exactly three outcomes")
    if tuple(item.upstream for item in outcomes) != K3_CONNECTIVITY_UPSTREAMS:
        raise ValueError("connectivity outcomes must be EASTMONEY/TENCENT/SINA in order")
    return MappingProxyType(
        {
            "schema_version": "s1-07k3b-akshare-connectivity-evidence.v1",
            "marker": K3_CONNECTIVITY_PROBE_MARKER,
            "application_retries": 0,
            "failure_policy": "RUN_ALL_BOUNDED_NO_RETRY",
            "endpoint_invocations": K3_CONNECTIVITY_MAX_INVOCATIONS,
            "outcomes": [item.to_payload() for item in outcomes],
            "eastmoney_http_reachable": outcomes[0].succeeded,
            "tencent_http_reachable": outcomes[1].succeeded,
            "sina_http_reachable": outcomes[2].succeeded,
        }
    )


__all__ = [
    "K3_CONNECTIVITY_LOOKBACK_DAYS",
    "K3_CONNECTIVITY_MAX_INVOCATIONS",
    "K3_CONNECTIVITY_PROBE_MARKER",
    "K3_CONNECTIVITY_SYMBOL",
    "K3_CONNECTIVITY_TARGET_DATE",
    "K3_CONNECTIVITY_UPSTREAMS",
    "K3ConnectivityInvocation",
    "K3ConnectivityOutcome",
    "fixed_k3_connectivity_plan",
    "k3_connectivity_dry_run_payload",
    "summarize_k3_connectivity",
]
