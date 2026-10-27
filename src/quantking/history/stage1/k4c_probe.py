"""Frozen K4C representative three-source probe plan and evidence summary.

The probe is the authorization-gated successor to the retired K3 four-source
probes: A (BaoStock), B (AKShare/Tencent) and C (AKShare/Sina) are measured
for a fixed representative security set covering normal, ex-rights,
suspension, low-liquidity, volume/amount-boundary, missing/empty-upstream and
Shanghai/Shenzhen code-segment cases. ``k4c_probe_dry_run_payload`` performs
no network access; real execution requires the separate authorization marker
consumed by ``tests/compatibility/test_k4c_representative_real.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from types import MappingProxyType

from quantking.data.adapters.sources.akshare import AKSHARE_SESSION_TIMEOUT_SECONDS
from quantking.data.adapters.sources.baostock import BAOSTOCK_SESSION_TIMEOUT_SECONDS
from quantking.data.adapters.sources.units import (
    AKSHARE_UNIT_POLICY_VERSION,
    BAOSTOCK_UNIT_POLICY_VERSION,
)
from quantking.data.comparison.thresholds import (
    canonical_decimal,
    decimal_difference,
    decimal_from_number,
)
from quantking.data.eod.release import (
    K4C_MEASUREMENT_OBJECTIVES,
    K4C_REPRESENTATIVE_SYMBOLS,
    K4C_SAMPLE_COVERAGE,
    S108_TARGET_END,
    S108_TARGET_START,
)
from quantking.data.models import DataProvider, DataUpstream

K4C_CROSS_FIELDS = ("open", "high", "low", "close", "pre_close", "volume", "amount")
K4C_EXACT_FIELDS = ("open", "high", "low", "close", "pre_close")
K4C_DIFFERENCE_FIELDS = ("volume", "amount")
K4C_MAX_REPORTED_DIVERGENCES = 20

K4C_PROBE_MARKER = "K4C-REPRESENTATIVE-2026-08-14-v1"
K4C_LOOKBACK_DAYS = 15
K4C_MAX_DAYS_PER_CHUNK = 31
K4C_MAX_SYMBOLS_PER_CHUNK = 1
_HTTP_PER_ENDPOINT = {
    DataUpstream.TENCENT: 3,
    DataUpstream.SINA: 2,
}
_ROLE_BY_UPSTREAM = {
    DataUpstream.BAOSTOCK: "A",
    DataUpstream.TENCENT: "B",
    DataUpstream.SINA: "C",
}
_ENDPOINT_BY_UPSTREAM = {
    DataUpstream.BAOSTOCK: "query_history_k_data_plus",
    DataUpstream.TENCENT: "stock_zh_a_hist_tx",
    DataUpstream.SINA: "stock_zh_a_daily",
}


@dataclass(frozen=True, slots=True)
class K4CProbeInvocation:
    """One frozen per-symbol/per-source probe invocation."""

    role: str
    provider: DataProvider
    upstream: DataUpstream
    symbol: str
    target_start: date
    target_end: date
    upstream_start: date
    upstream_end: date

    @property
    def endpoint(self) -> str:
        return _ENDPOINT_BY_UPSTREAM[self.upstream]

    def to_dict(self) -> dict[str, str]:
        return {
            "role": self.role,
            "provider": self.provider.value,
            "upstream": self.upstream.value,
            "endpoint": self.endpoint,
            "symbol": self.symbol,
            "target_start": self.target_start.isoformat(),
            "target_end": self.target_end.isoformat(),
            "upstream_start": self.upstream_start.isoformat(),
            "upstream_end": self.upstream_end.isoformat(),
        }


def _chunks() -> tuple[tuple[date, date], ...]:
    chunks: list[tuple[date, date]] = []
    start = S108_TARGET_START
    while start <= S108_TARGET_END:
        end = min(start + timedelta(days=K4C_MAX_DAYS_PER_CHUNK - 1), S108_TARGET_END)
        chunks.append((start, end))
        start = end + timedelta(days=1)
    return tuple(chunks)


def fixed_k4c_probe_plan() -> tuple[K4CProbeInvocation, ...]:
    """Return the full frozen A/B/C measurement plan (C is a maximum bound)."""
    invocations: list[K4CProbeInvocation] = []
    for symbol in K4C_REPRESENTATIVE_SYMBOLS:
        for upstream in (DataUpstream.BAOSTOCK, DataUpstream.TENCENT, DataUpstream.SINA):
            for target_start, target_end in _chunks():
                invocations.append(
                    K4CProbeInvocation(
                        role=_ROLE_BY_UPSTREAM[upstream],
                        provider=(
                            DataProvider.BAOSTOCK
                            if upstream is DataUpstream.BAOSTOCK
                            else DataProvider.AKSHARE
                        ),
                        upstream=upstream,
                        symbol=symbol,
                        target_start=target_start,
                        target_end=target_end,
                        upstream_start=target_start - timedelta(days=K4C_LOOKBACK_DAYS),
                        upstream_end=target_end,
                    )
                )
    return tuple(invocations)


def k4c_probe_dry_run_payload() -> Mapping[str, object]:
    """Return the frozen zero-network K4C representative probe contract."""
    plan = fixed_k4c_probe_plan()
    per_upstream: dict[str, int] = {}
    for invocation in plan:
        per_upstream[invocation.upstream.value] = per_upstream.get(invocation.upstream.value, 0) + 1
    a_count = per_upstream.get(DataUpstream.BAOSTOCK.value, 0)
    b_count = per_upstream.get(DataUpstream.TENCENT.value, 0)
    c_count = per_upstream.get(DataUpstream.SINA.value, 0)
    http_upper_bound = (
        b_count * _HTTP_PER_ENDPOINT[DataUpstream.TENCENT]
        + c_count * _HTTP_PER_ENDPOINT[DataUpstream.SINA]
    )
    return MappingProxyType(
        {
            "schema_version": "s1-07k4c-probe-dry-run.v1",
            "authorization_marker": K4C_PROBE_MARKER,
            "dataset": "daily_bar_unadjusted",
            "schema": "daily_bar_unadjusted.v1",
            "sample": {
                "symbols": list(K4C_REPRESENTATIVE_SYMBOLS),
                "coverage": dict(K4C_SAMPLE_COVERAGE),
                "measurement_objectives": list(K4C_MEASUREMENT_OBJECTIVES),
            },
            "target_window": {
                "start": S108_TARGET_START.isoformat(),
                "end": S108_TARGET_END.isoformat(),
            },
            "lookback_days": K4C_LOOKBACK_DAYS,
            "chunk_policy": {
                "max_days_per_chunk": K4C_MAX_DAYS_PER_CHUNK,
                "max_symbols_per_chunk": K4C_MAX_SYMBOLS_PER_CHUNK,
            },
            "network_budget": {
                "a_baostock_endpoint_invocations": a_count,
                "b_tencent_endpoint_invocations": b_count,
                "max_c_sina_endpoint_invocations": c_count,
                "max_endpoint_invocations": a_count + b_count + c_count,
                "estimated_max_baostock_protocol_messages": a_count * 3,
                "estimated_max_akshare_http_requests": http_upper_bound,
                "application_retries": 0,
                "fail_fast": True,
            },
            "hard_timeouts_seconds": {
                "baostock_session": BAOSTOCK_SESSION_TIMEOUT_SECONDS,
                "akshare_session": AKSHARE_SESSION_TIMEOUT_SECONDS,
            },
            "unit_policies": {
                "baostock": BAOSTOCK_UNIT_POLICY_VERSION,
                "akshare": AKSHARE_UNIT_POLICY_VERSION,
            },
            "invocations": [item.to_dict() for item in plan],
            "requires_real_network_authorization": True,
        }
    )


@dataclass(frozen=True, slots=True)
class _ProbeSlot:
    ok: int = 0
    failed: int = 0
    rows: int = 0
    invocations: int = 0


def _row_key(symbol: str, trade_date: str) -> tuple[str, str]:
    return (symbol, trade_date)


def _canonical_number(value: object, *, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be numeric")
    return canonical_decimal(decimal_from_number(value, field=field))


def k4c_cross_source_measurement(
    rows_by_role: Mapping[str, Mapping[tuple[str, str], Mapping[str, object]]],
) -> dict[str, object]:
    """Measure held-row agreement across probe roles for the K4C sample.

    A ``(symbol, trade_date)`` key participates when at least one role
    actually holds a row; absence never counts as agreement. OHLC/`pre_close`
    are measured for exact Decimal equality; `volume`/`amount` additionally
    record the maximum observed absolute and relative Decimal differences.
    The result is bounded evidence: per-field divergences are capped at
    ``K4C_MAX_REPORTED_DIVERGENCES``.
    """
    if not isinstance(rows_by_role, Mapping):
        raise TypeError("rows_by_role must be a mapping")
    if any(
        not isinstance(role, str) or len(role) != 1 or not role.isalpha() for role in rows_by_role
    ):
        raise ValueError("rows_by_role keys must be single-letter roles")
    if len(set(rows_by_role)) != len(rows_by_role):
        raise ValueError("rows_by_role roles must be unique")
    if any(not isinstance(rows, Mapping) for rows in rows_by_role.values()):
        raise TypeError("rows_by_role values must be row mappings")
    roles = tuple(sorted(rows_by_role))
    keys = sorted(
        {key for rows in rows_by_role.values() for key in rows},
        key=lambda key: (key[0], key[1]),
    )
    held_histogram: dict[str, int] = {}
    for symbol, trade_date in keys:
        held_count = sum(1 for rows in rows_by_role.values() if (symbol, trade_date) in rows)
        held_histogram[str(held_count)] = held_histogram.get(str(held_count), 0) + 1
    field_evidence: dict[str, dict[str, object]] = {}
    for field in K4C_CROSS_FIELDS:
        compared = 0
        exact = 0
        divergences: list[dict[str, object]] = []
        max_absolute: tuple[str, str] | None = None
        max_relative: tuple[str, str] | None = None
        for symbol, trade_date in keys:
            held: dict[str, str] = {}
            for role in roles:
                row = rows_by_role[role].get((symbol, trade_date))
                if row is not None and field in row:
                    held[role] = _canonical_number(row[field], field=field)
            if len(held) < 2:
                continue
            compared += 1
            values = tuple(held.values())
            if len(set(values)) == 1:
                exact += 1
                continue
            divergence: dict[str, object] = {
                "symbol": symbol,
                "trade_date": trade_date,
                "values": held,
            }
            if field in K4C_DIFFERENCE_FIELDS:
                row_max_absolute: tuple[str, str] | None = None
                row_max_relative: tuple[str, str] | None = None
                for first in range(len(values)):
                    for second in range(first + 1, len(values)):
                        absolute, relative = decimal_difference(
                            Decimal(values[first]), Decimal(values[second])
                        )
                        absolute_text = canonical_decimal(absolute)
                        relative_text = canonical_decimal(relative)
                        if row_max_absolute is None or Decimal(absolute_text) > Decimal(
                            row_max_absolute[1]
                        ):
                            row_max_absolute = (f"{symbol}/{trade_date}", absolute_text)
                        if row_max_relative is None or Decimal(relative_text) > Decimal(
                            row_max_relative[1]
                        ):
                            row_max_relative = (f"{symbol}/{trade_date}", relative_text)
                assert row_max_absolute is not None
                assert row_max_relative is not None
                divergence["absolute_difference"] = row_max_absolute[1]
                divergence["relative_difference"] = row_max_relative[1]
                if max_absolute is None or Decimal(row_max_absolute[1]) > Decimal(max_absolute[1]):
                    max_absolute = row_max_absolute
                if max_relative is None or Decimal(row_max_relative[1]) > Decimal(max_relative[1]):
                    max_relative = row_max_relative
            if len(divergences) < K4C_MAX_REPORTED_DIVERGENCES:
                divergences.append(divergence)
        evidence: dict[str, object] = {
            "compared_rows": compared,
            "exact_rows": exact,
            "divergent_rows": compared - exact,
            "divergences": divergences,
        }
        if field in K4C_DIFFERENCE_FIELDS:
            evidence["max_absolute_difference"] = None if max_absolute is None else max_absolute[1]
            evidence["max_relative_difference"] = None if max_relative is None else max_relative[1]
            evidence["max_absolute_difference_at"] = (
                None if max_absolute is None else max_absolute[0]
            )
            evidence["max_relative_difference_at"] = (
                None if max_relative is None else max_relative[0]
            )
        field_evidence[field] = evidence
    return {
        "schema_version": "s1-07k4c-measurement.v1",
        "held_rows_total": len(keys),
        "rows_held_by_role_count": held_histogram,
        "fields": field_evidence,
    }


def summarize_k4c_probe(
    results: Mapping[K4CProbeInvocation, Mapping[str, object]],
) -> dict[str, object]:
    """Summarize measured probe outcomes into bounded evidence categories.

    Every invocation outcome carries ``status`` (``OK``/stable error code),
    ``row_count`` and the measured field/unit facts. The summary never
    interprets a failed or missing invocation as an agreeing value.
    """
    if not isinstance(results, Mapping):
        raise TypeError("results must be a mapping")
    by_upstream: dict[str, _ProbeSlot] = {}
    missing: list[str] = []
    for invocation in fixed_k4c_probe_plan():
        outcome = results.get(invocation)
        if outcome is None:
            missing.append(invocation.to_dict()["symbol"])
            continue
        slot = by_upstream.setdefault(invocation.upstream.value, _ProbeSlot())
        row_count = outcome.get("row_count")
        by_upstream[invocation.upstream.value] = _ProbeSlot(
            ok=slot.ok + (1 if outcome.get("status") == "OK" else 0),
            failed=slot.failed + (0 if outcome.get("status") == "OK" else 1),
            rows=slot.rows + (int(row_count) if isinstance(row_count, int) else 0),
            invocations=slot.invocations + 1,
        )
    per_upstream: dict[str, dict[str, object]] = {}
    for upstream, slot in sorted(by_upstream.items()):
        per_upstream[upstream] = {
            "ok": slot.ok,
            "failed": slot.failed,
            "rows": slot.rows,
            "invocations": slot.invocations,
        }
    return {
        "schema_version": "s1-07k4c-probe-summary.v1",
        "marker": K4C_PROBE_MARKER,
        "symbols": len(K4C_REPRESENTATIVE_SYMBOLS),
        "per_upstream": per_upstream,
        "missing_invocations": sorted(set(missing)),
        "any_failure_means_stop": True,
    }


__all__ = [
    "K4C_CROSS_FIELDS",
    "K4C_DIFFERENCE_FIELDS",
    "K4C_EXACT_FIELDS",
    "K4C_MAX_REPORTED_DIVERGENCES",
    "K4C_PROBE_MARKER",
    "K4CProbeInvocation",
    "fixed_k4c_probe_plan",
    "k4c_cross_source_measurement",
    "k4c_probe_dry_run_payload",
    "summarize_k4c_probe",
]
