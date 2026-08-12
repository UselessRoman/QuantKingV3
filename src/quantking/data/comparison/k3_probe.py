"""Bounded K3 four-source evidence probe plan; dry-run is always offline."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations
from types import MappingProxyType

from quantking.data.comparison.thresholds import canonical_decimal, decimal_from_number
from quantking.data.models import DataProvider, DataUpstream

K3_PROBE_MARKER = "S1-07K3B-FOUR-SOURCE-REAL-PROBE-V1"
K3_PROBE_TARGET_START = date(2026, 6, 11)
K3_PROBE_TARGET_END = date(2026, 8, 9)
K3_PROBE_LOOKBACK_DAYS = 15
K3_PROBE_SYMBOLS = ("000001.SZ", "600000.SH")
K3_PROBE_SOURCES = (
    ("A", DataProvider.BAOSTOCK, DataUpstream.BAOSTOCK),
    ("B", DataProvider.AKSHARE, DataUpstream.EASTMONEY),
    ("C", DataProvider.AKSHARE, DataUpstream.TENCENT),
    ("D", DataProvider.AKSHARE, DataUpstream.SINA),
)
K3_PROBE_MAX_INVOCATIONS = len(K3_PROBE_SYMBOLS) * len(K3_PROBE_SOURCES)
_NUMERIC_FIELDS = ("open", "high", "low", "close", "pre_close", "volume", "amount")


@dataclass(frozen=True, slots=True)
class K3ProbeInvocation:
    sequence: int
    role: str
    provider: DataProvider
    upstream: DataUpstream
    symbol: str
    target_start: date
    target_end: date
    upstream_start: date
    upstream_end: date

    def to_payload(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "role": self.role,
            "provider": self.provider.value,
            "upstream": self.upstream.value,
            "symbol": self.symbol,
            "target_start": self.target_start.isoformat(),
            "target_end": self.target_end.isoformat(),
            "upstream_start": self.upstream_start.isoformat(),
            "upstream_end": self.upstream_end.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class K3ProbeSourceResult:
    role: str
    provider: DataProvider
    upstream: DataUpstream
    library_versions: tuple[str, ...]
    contract_versions: tuple[str, ...]
    volume_units: tuple[str, ...]
    amount_units: tuple[str, ...]
    unadjusted: bool
    complete: bool
    upstream_row_count: int
    records: tuple[Mapping[str, object], ...]
    official_preclose: Mapping[tuple[str, str], Decimal] | None = None

    def __post_init__(self) -> None:
        expected = next(
            (item for item in K3_PROBE_SOURCES if item[0] == self.role),
            None,
        )
        if expected != (self.role, self.provider, self.upstream):
            raise ValueError("probe source role/provider/upstream is invalid")
        for field in (
            "library_versions",
            "contract_versions",
            "volume_units",
            "amount_units",
        ):
            values = getattr(self, field)
            if (
                not isinstance(values, tuple)
                or not values
                or any(not isinstance(item, str) or not item for item in values)
                or tuple(sorted(set(values))) != values
            ):
                raise ValueError(f"probe source {field} must be sorted and non-empty")
        if not self.unadjusted or not self.complete:
            raise ValueError("probe source must be complete and unadjusted")
        if self.upstream_row_count < len(self.records):
            raise ValueError("upstream row count cannot be below Canonical count")


def fixed_k3_probe_plan() -> tuple[K3ProbeInvocation, ...]:
    invocations: list[K3ProbeInvocation] = []
    for symbol in K3_PROBE_SYMBOLS:
        for role, provider, upstream in K3_PROBE_SOURCES:
            invocations.append(
                K3ProbeInvocation(
                    sequence=len(invocations) + 1,
                    role=role,
                    provider=provider,
                    upstream=upstream,
                    symbol=symbol,
                    target_start=K3_PROBE_TARGET_START,
                    target_end=K3_PROBE_TARGET_END,
                    upstream_start=K3_PROBE_TARGET_START - timedelta(days=K3_PROBE_LOOKBACK_DAYS),
                    upstream_end=K3_PROBE_TARGET_END,
                )
            )
    result = tuple(invocations)
    if len(result) != K3_PROBE_MAX_INVOCATIONS:
        raise RuntimeError("K3 probe budget plan is inconsistent")
    return result


def k3_probe_dry_run_payload() -> MappingProxyType[str, object]:
    plan = fixed_k3_probe_plan()
    return MappingProxyType(
        {
            "schema_version": "s1-07k3b-four-source-probe-plan.v1",
            "mode": "dry-run",
            "marker": K3_PROBE_MARKER,
            "network_calls_made": 0,
            "application_retries": 0,
            "lookback_days": K3_PROBE_LOOKBACK_DAYS,
            "market_data_endpoint_invocations": {
                "BAOSTOCK": 2,
                "EASTMONEY": 2,
                "TENCENT": 2,
                "SINA": 2,
                "total": K3_PROBE_MAX_INVOCATIONS,
            },
            "baostock_protocol_message_upper_bound": {
                "login": 2,
                "query": 2,
                "logout_attempt": 2,
                "total": 6,
            },
            "akshare_locked_source_http_upper_bound": {
                "EASTMONEY": 2,
                "TENCENT": 6,
                "SINA": 4,
                "total": 12,
            },
            "failure_policy": "STOP_ON_FIRST_FAILURE",
            "writes_formal_data_root": False,
            "invocations": [item.to_payload() for item in plan],
            "threshold_candidate_active": False,
            "representative_evidence_sufficient": False,
        }
    )


def _record_index(
    result: K3ProbeSourceResult,
) -> dict[tuple[str, str], Mapping[str, object]]:
    indexed: dict[tuple[str, str], Mapping[str, object]] = {}
    for record in result.records:
        code = record.get("code")
        trade_date = record.get("trade_date")
        if not isinstance(code, str) or not isinstance(trade_date, date):
            raise ValueError("probe Canonical record key is invalid")
        key = (code, trade_date.isoformat())
        if key in indexed:
            raise ValueError("probe Canonical records contain duplicate keys")
        indexed[key] = record
    return indexed


def _numeric_value(record: Mapping[str, object], field: str) -> int | float | Decimal:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError(f"probe Canonical {field} must be numeric")
    return value


def summarize_k3_probe(
    results: tuple[K3ProbeSourceResult, ...],
) -> MappingProxyType[str, object]:
    """Build bounded decimal evidence without returning tables or provider payloads."""
    if tuple(item.role for item in results) != ("A", "B", "C", "D"):
        raise ValueError("probe results must contain A/B/C/D in fixed order")
    indexes = {item.role: _record_index(item) for item in results}
    union = set().union(*(set(index) for index in indexes.values()))
    sources: dict[str, object] = {}
    for result in results:
        missing = sorted(union - set(indexes[result.role]))
        sources[result.role] = {
            "provider": result.provider.value,
            "upstream": result.upstream.value,
            "library_versions": list(result.library_versions),
            "contract_versions": list(result.contract_versions),
            "volume_units": list(result.volume_units),
            "amount_units": list(result.amount_units),
            "unadjusted": result.unadjusted,
            "complete": result.complete,
            "upstream_row_count": result.upstream_row_count,
            "canonical_row_count": len(result.records),
            "missing_keys": [list(key) for key in missing],
        }
    pair_summaries: dict[str, object] = {}
    for left_role, right_role in combinations(("A", "B", "C", "D"), 2):
        common = sorted(set(indexes[left_role]) & set(indexes[right_role]))
        fields: dict[str, object] = {}
        for field in _NUMERIC_FIELDS:
            maximum = Decimal("0")
            maximum_key: tuple[str, str] | None = None
            zero_count = 0
            nonzero_count = 0
            for key in common:
                left = decimal_from_number(
                    _numeric_value(indexes[left_role][key], field), field=field
                )
                right = decimal_from_number(
                    _numeric_value(indexes[right_role][key], field), field=field
                )
                difference = abs(left - right)
                if difference == 0:
                    zero_count += 1
                else:
                    nonzero_count += 1
                if difference > maximum:
                    maximum = difference
                    maximum_key = key
            fields[field] = {
                "max_absolute_difference": canonical_decimal(maximum),
                "max_key": None if maximum_key is None else list(maximum_key),
                "zero_difference_count": zero_count,
                "nonzero_difference_count": nonzero_count,
            }
        pair_summaries[f"{left_role}-{right_role}"] = {
            "common_key_count": len(common),
            "fields": fields,
        }
    bao = results[0]
    official = bao.official_preclose or {}
    bao_index = indexes["A"]
    semantic_nonzero = 0
    semantic_max = Decimal("0")
    for key, official_value in official.items():
        record = bao_index.get(key)
        if record is None:
            continue
        derived = decimal_from_number(_numeric_value(record, "pre_close"), field="pre_close")
        difference = abs(official_value - derived)
        if difference != 0:
            semantic_nonzero += 1
        semantic_max = max(semantic_max, difference)
    return MappingProxyType(
        {
            "schema_version": "s1-07k3b-four-source-evidence.v1",
            "marker": K3_PROBE_MARKER,
            "application_retries": 0,
            "endpoint_invocations": K3_PROBE_MAX_INVOCATIONS,
            "sources": sources,
            "pairs": pair_summaries,
            "baostock_official_preclose_semantics": {
                "compared_count": len(set(official) & set(bao_index)),
                "different_from_derived_count": semantic_nonzero,
                "max_absolute_difference": canonical_decimal(semantic_max),
                "official_preclose_used_for_canonical": False,
            },
            "representative_evidence_sufficient": False,
            "threshold_candidate_active": False,
        }
    )


__all__ = [
    "K3_PROBE_LOOKBACK_DAYS",
    "K3_PROBE_MARKER",
    "K3_PROBE_MAX_INVOCATIONS",
    "K3_PROBE_SOURCES",
    "K3_PROBE_SYMBOLS",
    "K3_PROBE_TARGET_END",
    "K3_PROBE_TARGET_START",
    "K3ProbeInvocation",
    "K3ProbeSourceResult",
    "fixed_k3_probe_plan",
    "k3_probe_dry_run_payload",
    "summarize_k3_probe",
]
