"""Pure S1-08 first-release planning with no persistence or provider imports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from quantking.common.config import S108_FIRST_RELEASE_SYMBOLS, AppSettings
from quantking.data.comparison.thresholds import THRESHOLD_RULE_VERSION
from quantking.data.eod.service import EodExecutionConfig
from quantking.data.models import DatasetKind, DataUpstream

S108_TARGET_START = date(2026, 6, 11)
S108_TARGET_END = date(2026, 8, 9)
S107K3_CODE_VERSION = "s1-07k3.v1"
S107K3_IDEMPOTENCY_KEY = "s1-07k3-first-four-source-release-20260611-20260809-v1"
S108_CODE_VERSION = "s1-08.v1"
S108_IDEMPOTENCY_KEY = "s1-08-first-release-20260611-20260809-v1"
_HTTP_PER_ENDPOINT = {
    DataUpstream.EASTMONEY: 1,
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


def _chunks(config: EodExecutionConfig) -> tuple[ReleaseChunk, ...]:
    chunks: list[ReleaseChunk] = []
    for symbol in S108_FIRST_RELEASE_SYMBOLS:
        start = S108_TARGET_START
        while start <= S108_TARGET_END:
            end = min(
                start + timedelta(days=config.max_days_per_chunk - 1),
                S108_TARGET_END,
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
    """Return the exact fixed release plan without creating a file or importing AKShare."""
    config = EodExecutionConfig.production(code_version=code_version, settings=settings)
    chunks = _chunks(config)
    primary_count = len(chunks)
    c_count = len(chunks)
    d_count = len(chunks)
    http_limit = (
        primary_count * _HTTP_PER_ENDPOINT[DataUpstream.EASTMONEY]
        + c_count * _HTTP_PER_ENDPOINT[DataUpstream.TENCENT]
        + d_count * _HTTP_PER_ENDPOINT[DataUpstream.SINA]
    )
    baostock_protocol_limit = primary_count * 3
    if (
        primary_count != 4
        or c_count != 4
        or d_count != 4
        or primary_count * 2 + c_count + d_count != config.endpoint_invocation_limit
        or http_limit != config.estimated_http_request_limit
        or baostock_protocol_limit != config.estimated_baostock_protocol_message_limit
    ):
        raise ValueError("S1-07K3 release plan no longer matches the frozen network budget")
    return {
        "schema_version": "s1-07k3-dry-run.v1",
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
        "max_d_chunks": [item.to_dict() for item in chunks],
        "network_budget": {
            "a_baostock_endpoint_invocations": primary_count,
            "b_eastmoney_endpoint_invocations": primary_count,
            "estimated_max_baostock_protocol_messages": baostock_protocol_limit,
            "estimated_max_akshare_http_requests": http_limit,
            "max_c_endpoint_invocations": c_count,
            "max_d_endpoint_invocations": d_count,
            "max_endpoint_invocations": config.endpoint_invocation_limit,
            "application_retries": 0,
        },
        "versions": {
            "code": config.code_version,
            "adapter": config.adapter_version,
            "quality_rules": config.rule_version,
            "baostock": config.baostock_version,
            "baostock_unit_policy": config.baostock_unit_policy_version,
            "source_route": config.source_route_version,
            "legacy_import_manifest_sha256": config.legacy_import_manifest_sha256,
            "threshold_rules": THRESHOLD_RULE_VERSION,
            "resolution_rules": config.resolution_rule_version,
        },
        "publish_intent": "PUBLISH_AFTER_QUALITY",
        "idempotency_key": S107K3_IDEMPOTENCY_KEY,
        "data_root": str(settings.paths.data_root),
    }


__all__ = [
    "S107K3_CODE_VERSION",
    "S107K3_IDEMPOTENCY_KEY",
    "S108_CODE_VERSION",
    "S108_IDEMPOTENCY_KEY",
    "S108_TARGET_END",
    "S108_TARGET_START",
    "ReleaseChunk",
    "build_s108_dry_run",
]
