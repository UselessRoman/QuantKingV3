"""Pure S1-08/K4C first-release planning with no persistence or provider imports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from quantking.common.config import S108_FIRST_RELEASE_SYMBOLS, AppSettings
from quantking.data.adapters.sources.akshare import AKSHARE_SESSION_TIMEOUT_SECONDS
from quantking.data.adapters.sources.baostock import BAOSTOCK_SESSION_TIMEOUT_SECONDS
from quantking.data.comparison.thresholds import K4_THRESHOLD_RULE_VERSION
from quantking.data.eod.service import EodExecutionConfig
from quantking.data.models import DatasetKind, DataUpstream

S108_TARGET_START = date(2026, 6, 11)
S108_TARGET_END = date(2026, 8, 9)
S107K3_CODE_VERSION = "s1-07k3.v1"
S107K3_IDEMPOTENCY_KEY = "s1-07k3-first-four-source-release-20260611-20260809-v1"
S107K4_CODE_VERSION = "s1-07k4.v1"
S107K4_IDEMPOTENCY_KEY = "s1-07k4-first-release-20260611-20260809-v1"
S108_CODE_VERSION = "s1-08.v1"
S108_IDEMPOTENCY_KEY = "s1-08-first-release-20260611-20260809-v1"

# K4C representative sample: covers Shenzhen mainboard, Shanghai mainboard,
# ChiNext, STAR market and corporate-action/liquidity/amount-boundary cases.
# The real probe measures normal, ex-rights, suspension, low-liquidity,
# volume/amount boundary and missing/empty upstream evidence per symbol.
K4C_PROBE_CODE_VERSION = "s1-07k4c-representative-probe.v1"
K4C_PROBE_IDEMPOTENCY_KEY = "s1-07k4c-representative-probe-20260611-20260809-v1"
K4C_REPRESENTATIVE_SYMBOLS = (
    "000001.SZ",
    "600000.SH",
    "000858.SZ",
    "300750.SZ",
    "688981.SH",
    "600519.SH",
)
K4C_SAMPLE_COVERAGE = {
    "000001.SZ": "shenzhen_mainboard_baseline",
    "600000.SH": "shanghai_mainboard_baseline",
    "000858.SZ": "corporate_action_history",
    "300750.SZ": "chinext_segment",
    "688981.SH": "star_market_segment",
    "600519.SH": "high_price_volume_amount_boundaries",
}
K4C_MEASUREMENT_OBJECTIVES = (
    "normal_session",
    "ex_rights_session",
    "suspension_session",
    "low_liquidity_session",
    "volume_amount_boundary",
    "missing_or_empty_upstream",
    "shenzhen_code_segments",
    "shanghai_code_segments",
)
_HTTP_PER_ENDPOINT = {
    DataUpstream.TENCENT: 3,
    DataUpstream.SINA: 2,
}


@dataclass(frozen=True, slots=True)
class ReleaseChunk:
    symbol: str
    target_start: date
    target_end: date
    upstream_start: date
    upstream_end: date

    def to_dict(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "target_start": self.target_start.isoformat(),
            "target_end": self.target_end.isoformat(),
            "upstream_start": self.upstream_start.isoformat(),
            "upstream_end": self.upstream_end.isoformat(),
        }


def _chunks(
    config: EodExecutionConfig,
    *,
    symbols: tuple[str, ...],
    target_start: date,
    target_end: date,
) -> tuple[ReleaseChunk, ...]:
    chunks: list[ReleaseChunk] = []
    for symbol in symbols:
        start = target_start
        while start <= target_end:
            end = min(
                start + timedelta(days=config.max_days_per_chunk - 1),
                target_end,
            )
            chunks.append(
                ReleaseChunk(
                    symbol=symbol,
                    target_start=start,
                    target_end=end,
                    upstream_start=start - timedelta(days=config.adapter_context_lookback_days),
                    upstream_end=end,
                )
            )
            start = end + timedelta(days=1)
    return tuple(chunks)


def build_s108_dry_run(settings: AppSettings, *, code_version: str) -> dict[str, object]:
    """Return the exact fixed three-role release plan without a file or AKShare import."""
    config = EodExecutionConfig.production(code_version=code_version, settings=settings)
    chunks = _chunks(
        config,
        symbols=tuple(S108_FIRST_RELEASE_SYMBOLS),
        target_start=S108_TARGET_START,
        target_end=S108_TARGET_END,
    )
    primary_count = len(chunks)
    c_count = len(chunks)
    http_limit = (
        primary_count * _HTTP_PER_ENDPOINT[DataUpstream.TENCENT]
        + c_count * _HTTP_PER_ENDPOINT[DataUpstream.SINA]
    )
    baostock_protocol_limit = primary_count * 3
    if (
        primary_count != 4
        or c_count != 4
        or primary_count * 2 + c_count != config.endpoint_invocation_limit
        or http_limit != config.estimated_http_request_limit
        or baostock_protocol_limit != config.estimated_baostock_protocol_message_limit
    ):
        raise ValueError("S1-07K4 release plan no longer matches the frozen network budget")
    return {
        "schema_version": "s1-07k4-dry-run.v1",
        "dataset": DatasetKind.DAILY_BAR_UNADJUSTED.value,
        "schema": "daily_bar_unadjusted.v1",
        "scope": list(settings.eod_symbols),
        "target_window": {
            "start": S108_TARGET_START.isoformat(),
            "end": S108_TARGET_END.isoformat(),
        },
        "upstream_window": {
            "start": (
                S108_TARGET_START - timedelta(days=config.adapter_context_lookback_days)
            ).isoformat(),
            "end": S108_TARGET_END.isoformat(),
        },
        "planner_lookback_days": config.planner_lookback_days,
        "adapter_context_lookback_days": config.adapter_context_lookback_days,
        "chunk_policy": {
            "max_days_per_chunk": config.max_days_per_chunk,
            "max_symbols_per_chunk": config.max_items_per_chunk,
        },
        "a_chunks": [item.to_dict() for item in chunks],
        "b_chunks": [item.to_dict() for item in chunks],
        "max_c_chunks": [item.to_dict() for item in chunks],
        "network_budget": {
            "a_baostock_endpoint_invocations": primary_count,
            "b_tencent_endpoint_invocations": primary_count,
            "estimated_max_baostock_protocol_messages": baostock_protocol_limit,
            "estimated_max_akshare_http_requests": http_limit,
            "max_c_endpoint_invocations": c_count,
            "max_endpoint_invocations": config.endpoint_invocation_limit,
            "application_retries": 0,
        },
        "versions": {
            "code": config.code_version,
            "adapter": config.adapter_version,
            "quality_rules": config.rule_version,
            "baostock": config.baostock_version,
            "baostock_unit_policy": config.baostock_unit_policy_version,
            "akshare_unit_policy": config.akshare_unit_policy_version,
            "source_route": config.source_route_version,
            "legacy_import_manifest_sha256": config.legacy_import_manifest_sha256,
            "threshold_rules": K4_THRESHOLD_RULE_VERSION,
            "resolution_rules": config.resolution_rule_version,
        },
        "security_exceptions": {
            code: [[start, end] for start, end in ranges]
            for code, ranges in config.datasets[
                DatasetKind.DAILY_BAR_UNADJUSTED.value
            ].security_exceptions.items()
        },
        "publish_intent": "PUBLISH_AFTER_QUALITY",
        "idempotency_key": S107K4_IDEMPOTENCY_KEY,
        "data_root": str(settings.paths.data_root),
    }


def build_k4c_representative_dry_run(
    settings: AppSettings,
    *,
    code_version: str = K4C_PROBE_CODE_VERSION,
) -> dict[str, object]:
    """Return the frozen K4C representative three-source probe plan (zero network).

    The plan fixes the representative security set, the target window and the
    15-day lookback, per-source endpoint upper bounds, per-source HTTP upper
    bounds, the hard session timeouts, zero retry and fail-fast semantics. It
    deliberately performs no provider call and cannot be converted into one
    under the same authorization.
    """
    config = EodExecutionConfig.production(code_version=code_version, settings=settings)
    chunks = _chunks(
        config,
        symbols=K4C_REPRESENTATIVE_SYMBOLS,
        target_start=S108_TARGET_START,
        target_end=S108_TARGET_END,
    )
    primary_count = len(chunks)
    c_count = len(chunks)
    http_limit = (
        primary_count * _HTTP_PER_ENDPOINT[DataUpstream.TENCENT]
        + c_count * _HTTP_PER_ENDPOINT[DataUpstream.SINA]
    )
    baostock_protocol_limit = primary_count * 3
    return {
        "schema_version": "s1-07k4c-representative-probe-dry-run.v1",
        "dataset": DatasetKind.DAILY_BAR_UNADJUSTED.value,
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
        "lookback_days": config.adapter_context_lookback_days,
        "chunk_policy": {
            "max_days_per_chunk": config.max_days_per_chunk,
            "max_symbols_per_chunk": config.max_items_per_chunk,
        },
        "a_baostock_chunks": [item.to_dict() for item in chunks],
        "b_tencent_chunks": [item.to_dict() for item in chunks],
        "max_c_sina_spans": [item.to_dict() for item in chunks],
        "network_budget": {
            "a_baostock_endpoint_invocations": primary_count,
            "b_tencent_endpoint_invocations": primary_count,
            "max_c_sina_endpoint_invocations": c_count,
            "max_endpoint_invocations": primary_count * 2 + c_count,
            "estimated_max_baostock_protocol_messages": baostock_protocol_limit,
            "estimated_max_akshare_http_requests": http_limit,
            "application_retries": 0,
            "fail_fast": True,
        },
        "hard_timeouts_seconds": {
            "baostock_session": BAOSTOCK_SESSION_TIMEOUT_SECONDS,
            "akshare_session": AKSHARE_SESSION_TIMEOUT_SECONDS,
        },
        "versions": {
            "code": code_version,
            "adapter": config.adapter_version,
            "quality_rules": config.rule_version,
            "baostock": config.baostock_version,
            "baostock_unit_policy": config.baostock_unit_policy_version,
            "akshare_unit_policy": config.akshare_unit_policy_version,
            "source_route": config.source_route_version,
            "threshold_rules": K4_THRESHOLD_RULE_VERSION,
            "resolution_rules": config.resolution_rule_version,
        },
        "security_exceptions": {
            code: [[start, end] for start, end in ranges]
            for code, ranges in config.datasets[
                DatasetKind.DAILY_BAR_UNADJUSTED.value
            ].security_exceptions.items()
        },
        "publish_intent": "NONE",
        "idempotency_key": K4C_PROBE_IDEMPOTENCY_KEY,
        "requires_real_network_authorization": True,
    }


__all__ = [
    "K4C_MEASUREMENT_OBJECTIVES",
    "K4C_PROBE_CODE_VERSION",
    "K4C_PROBE_IDEMPOTENCY_KEY",
    "K4C_REPRESENTATIVE_SYMBOLS",
    "K4C_SAMPLE_COVERAGE",
    "S107K3_CODE_VERSION",
    "S107K3_IDEMPOTENCY_KEY",
    "S107K4_CODE_VERSION",
    "S107K4_IDEMPOTENCY_KEY",
    "S108_CODE_VERSION",
    "S108_IDEMPOTENCY_KEY",
    "S108_TARGET_END",
    "S108_TARGET_START",
    "ReleaseChunk",
    "build_k4c_representative_dry_run",
    "build_s108_dry_run",
]
