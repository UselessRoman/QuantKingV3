"""Frozen, manifest-driven release planning contract for S1-08R2.

The manifest binds the complete release scope — security set, windows,
per-security coverage segments with internal gaps and explicit exemptions,
versioned trading calendar, three-role source plan, deterministic chunks,
endpoint/HTTP/protocol budgets, lineage caps, publish intent, the exact
would-write directory set and the ADR-027 legacy rejection disposition —
into one canonical-JSON, SHA-addressed, versioned contract.

Every declared value is additionally bounded by independent safety hard
caps (:data:`RELEASE_SAFETY_CAPS`). A manifest can plan below the caps but
can never raise them: over-cap manifests, missing fields, duplicated or
misordered securities, wrong SHAs or coverage inconsistent with the chunk
inventory fail closed at parse/build time — before any provider call or
formal file write.

This module is pure computation: no provider, filesystem, database or
wall-clock dependency.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import MappingProxyType

RELEASE_MANIFEST_SCHEMA_VERSION = "s1-08r2-release-manifest.v1"
RELEASE_DRY_RUN_SCHEMA_VERSION = "s1-08r2-dry-run.v1"
RELEASE_MANIFEST_DATASET = "daily_bar_unadjusted"
RELEASE_MANIFEST_CANONICAL_SCHEMA = "daily_bar_unadjusted.v1"
RELEASE_PUBLISH_INTENT = "PUBLISH_AFTER_QUALITY"

# ADR-027 (2026-08-17): the legacy XTQuant daily-bar migration is rejected.
# The manifest binds the frozen rejection disposition and the two stable
# evidence SHAs that closed the evaluation. XTQUANT_LEGACY never enters the
# Raw, Resolution, Canonical or Warehouse lineage of any release.
ADR_027_ID = "ADR-027"
K4D_INVENTORY_EVIDENCE_SHA256 = "140978ea3e3f4d9139c15d641c92564af8f58928bf77017117393ce988a04ce2"
K4D_PILOT_ATTEMPT2_DIAGNOSTIC_SHA256 = (
    "b75f27b4096571f04eb790e8cd78f1da1faf174187dc44ab0ca09d20fe1c02e1"
)

# Independent safety hard caps. Manifest-declared budgets and lineage caps
# are validated against these bounds at build and parse time; a manifest
# cannot raise its own limits.
RELEASE_SAFETY_CAPS: Mapping[str, int] = MappingProxyType(
    {
        "symbols": 64,
        "chunks_per_role": 512,
        "endpoint_invocations_per_role": 512,
        "endpoint_invocations_total": 1536,
        "akshare_http_requests": 2560,
        "baostock_protocol_messages": 1536,
        "raw_batches": 1536,
        "difference_reports": 1536,
        "resolution_runs": 8,
        "quality_runs": 8,
        "max_days_per_chunk": 31,
        "lookback_days": 15,
    }
)

_AKSHARE_HTTP_PER_ENDPOINT = {"TENCENT": 3, "SINA": 2}
_BAOSTOCK_PROTOCOL_MESSAGES_PER_ENDPOINT = 3
_EXEMPTION_REASONS = frozenset(
    {
        "SUSPENSION",
        "PRE_LISTING",
        "DELISTED",
        "NO_TRADING_SESSION",
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_SYMBOL = re.compile(r"\d{6}\.(?:SZ|SH|BJ)\Z")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ReleaseManifestError(ValueError):
    """Stable fail-closed manifest contract violation."""


def _require_int(value: object, *, name: str, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReleaseManifestError(f"{name} must be an integer")
    if value < minimum:
        raise ReleaseManifestError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ReleaseManifestError(f"{name} must not exceed {maximum}")
    return value


def _require_version(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _SAFE_VERSION.fullmatch(value) is None:
        raise ReleaseManifestError(f"{name} must be a safe stable identifier")
    return value


def _require_sha(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ReleaseManifestError(f"{name} must be lowercase SHA-256")
    return value


def _require_date(value: object, *, name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ReleaseManifestError(f"{name} must be a date")
    return value


@dataclass(frozen=True, slots=True)
class SecurityCoveragePlan:
    """Per-security coverage segments, internal gaps and explicit exemptions."""

    symbol: str
    segments: tuple[tuple[date, date], ...]
    internal_gaps: tuple[tuple[date, date], ...] = ()
    exemptions: tuple[tuple[date, date, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or _SAFE_SYMBOL.fullmatch(self.symbol) is None:
            raise ReleaseManifestError("coverage symbol must be canonical")
        self._validate_ranges(self.segments, label="segments")
        self._validate_ranges(self.internal_gaps, label="internal_gaps")
        normalized: list[tuple[date, date, str]] = []
        for item in self.exemptions:
            if not isinstance(item, tuple) or len(item) != 3:
                raise ReleaseManifestError("exemptions must be (start, end, reason) tuples")
            start, end, reason = item
            if (
                not isinstance(start, date)
                or not isinstance(end, date)
                or not isinstance(reason, str)
                or reason not in _EXEMPTION_REASONS
            ):
                raise ReleaseManifestError("exemption entries are invalid")
            if end < start:
                raise ReleaseManifestError("exemption end must not precede start")
            normalized.append((start, end, reason))
        ordered = tuple(sorted(normalized, key=lambda item: (item[0], item[1], item[2])))
        if ordered != tuple(normalized):
            raise ReleaseManifestError("exemptions must be sorted")
        if self.segments:
            first = self.segments[0][0]
            last = self.segments[-1][1]
            for start, end in self.internal_gaps:
                if start < first or end > last:
                    raise ReleaseManifestError("internal gaps must lie within the coverage span")
                if any(
                    start <= segment_end and end >= segment_start
                    for segment_start, segment_end in self.segments
                ):
                    raise ReleaseManifestError("internal gaps must not overlap coverage segments")

    @staticmethod
    def _validate_ranges(ranges: tuple[tuple[date, date], ...], *, label: str) -> None:
        if not isinstance(ranges, tuple):
            raise ReleaseManifestError(f"{label} must be a tuple")
        previous_end: date | None = None
        for item in ranges:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ReleaseManifestError(f"{label} entries must be date pairs")
            start, end = item
            if not isinstance(start, date) or not isinstance(end, date):
                raise ReleaseManifestError(f"{label} entries must be dates")
            if end < start:
                raise ReleaseManifestError(f"{label} end must not precede start")
            if previous_end is not None and start <= previous_end:
                raise ReleaseManifestError(f"{label} must be sorted and non-overlapping")
            previous_end = end

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "segments": [[start.isoformat(), end.isoformat()] for start, end in self.segments],
            "internal_gaps": [
                [start.isoformat(), end.isoformat()] for start, end in self.internal_gaps
            ],
            "exemptions": [
                [start.isoformat(), end.isoformat(), reason]
                for start, end, reason in self.exemptions
            ],
        }


@dataclass(frozen=True, slots=True)
class ReleaseChunk:
    """One deterministic planned supplier request."""

    sequence: int
    symbol: str
    target_start: date
    target_end: date
    upstream_start: date
    upstream_end: date

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "symbol": self.symbol,
            "target_start": self.target_start.isoformat(),
            "target_end": self.target_end.isoformat(),
            "upstream_start": self.upstream_start.isoformat(),
            "upstream_end": self.upstream_end.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class LegacyDisposition:
    """ADR-027 frozen rejection binding for XTQUANT_LEGACY."""

    status: str = "REJECTED"
    adr: str = ADR_027_ID
    evidence_sha256: tuple[str, ...] = (
        K4D_INVENTORY_EVIDENCE_SHA256,
        K4D_PILOT_ATTEMPT2_DIAGNOSTIC_SHA256,
    )
    included_batches: int = 0
    included_lineage: int = 0
    published_count: int = 0

    def __post_init__(self) -> None:
        if self.status != "REJECTED" or self.adr != ADR_027_ID:
            raise ReleaseManifestError("legacy disposition must remain the ADR-027 rejection")
        for value in self.evidence_sha256:
            _require_sha(value, name="legacy evidence SHA")
        for name in ("included_batches", "included_lineage", "published_count"):
            if getattr(self, name) != 0:
                raise ReleaseManifestError("legacy disposition counts must be zero")

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "adr": self.adr,
            "evidence_sha256": list(self.evidence_sha256),
            "included_batches": self.included_batches,
            "included_lineage": self.included_lineage,
            "published_count": self.published_count,
            "import_manifest_sha256": None,
        }


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    """The complete frozen release contract addressable by SHA-256."""

    schema_version: str
    symbols: tuple[str, ...]
    symbols_sha256: str
    target_start: date
    target_end: date
    lookback_days: int
    calendar_version_id: str
    calendar_sha256: str
    security_coverage: tuple[SecurityCoveragePlan, ...]
    source_route_version: str
    adapter_version: str
    code_version: str
    quality_rule_version: str
    threshold_rule_version: str
    resolution_rule_version: str
    unit_policy_versions: Mapping[str, str]
    max_days_per_chunk: int
    a_chunks: tuple[ReleaseChunk, ...]
    b_chunks: tuple[ReleaseChunk, ...]
    c_max_chunks: tuple[ReleaseChunk, ...]
    legacy: LegacyDisposition = field(default_factory=LegacyDisposition)
    publish_intent: str = RELEASE_PUBLISH_INTENT

    def __post_init__(self) -> None:
        if self.schema_version != RELEASE_MANIFEST_SCHEMA_VERSION:
            raise ReleaseManifestError("release manifest schema version is invalid")
        caps = RELEASE_SAFETY_CAPS
        if not isinstance(self.symbols, tuple) or not self.symbols:
            raise ReleaseManifestError("symbols must be a non-empty tuple")
        if len(set(self.symbols)) != len(self.symbols) or tuple(sorted(self.symbols)) != (
            self.symbols
        ):
            raise ReleaseManifestError("symbols must be sorted, unique and canonical")
        if len(self.symbols) > caps["symbols"]:
            raise ReleaseManifestError("symbols exceed the safety cap")
        for symbol in self.symbols:
            if _SAFE_SYMBOL.fullmatch(symbol) is None:
                raise ReleaseManifestError("symbols must use canonical security codes")
        expected_symbols_sha = hashlib.sha256(
            json.dumps(list(self.symbols), separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self.symbols_sha256 != expected_symbols_sha:
            raise ReleaseManifestError("symbols_sha256 does not match the symbol set")
        _require_date(self.target_start, name="target_start")
        _require_date(self.target_end, name="target_end")
        if self.target_end < self.target_start:
            raise ReleaseManifestError("target end must not precede start")
        _require_int(
            self.lookback_days,
            name="lookback_days",
            minimum=0,
            maximum=caps["lookback_days"],
        )
        _require_version(self.calendar_version_id, name="calendar_version_id")
        _require_sha(self.calendar_sha256, name="calendar_sha256")
        if not isinstance(self.security_coverage, tuple):
            raise ReleaseManifestError("security_coverage must be a tuple")
        for plan in self.security_coverage:
            if not isinstance(plan, SecurityCoveragePlan):
                raise ReleaseManifestError("security_coverage entries are invalid")
            if plan.symbol not in self.symbols:
                raise ReleaseManifestError("coverage symbol must be part of the symbol set")
            for start, _end in plan.segments:
                if start > self.target_end:
                    raise ReleaseManifestError("coverage segments must not follow the target end")
            for _start, end in plan.segments:
                if end < self.target_start - timedelta(days=self.lookback_days):
                    raise ReleaseManifestError("coverage segments precede the upstream window")
        if tuple(sorted(plan.symbol for plan in self.security_coverage)) != tuple(
            plan.symbol for plan in self.security_coverage
        ):
            raise ReleaseManifestError("security_coverage must be sorted by symbol")
        _require_version(self.source_route_version, name="source_route_version")
        _require_version(self.adapter_version, name="adapter_version")
        _require_version(self.code_version, name="code_version")
        _require_version(self.quality_rule_version, name="quality_rule_version")
        _require_version(self.threshold_rule_version, name="threshold_rule_version")
        _require_version(self.resolution_rule_version, name="resolution_rule_version")
        if not isinstance(self.unit_policy_versions, Mapping):
            raise ReleaseManifestError("unit_policy_versions must be a mapping")
        _require_int(
            self.max_days_per_chunk,
            name="max_days_per_chunk",
            minimum=1,
            maximum=caps["max_days_per_chunk"],
        )
        if self.publish_intent != RELEASE_PUBLISH_INTENT:
            raise ReleaseManifestError("publish intent must be PUBLISH_AFTER_QUALITY")
        if not isinstance(self.legacy, LegacyDisposition):
            raise ReleaseManifestError("legacy disposition is invalid")
        expected_a = _plan_chunks(
            symbols=self.symbols,
            target_start=self.target_start,
            target_end=self.target_end,
            lookback_days=self.lookback_days,
            max_days_per_chunk=self.max_days_per_chunk,
        )
        if self.a_chunks != expected_a or self.b_chunks != expected_a:
            raise ReleaseManifestError("A/B chunk plans must equal the deterministic inventory")
        if self.c_max_chunks != expected_a:
            raise ReleaseManifestError(
                "C maximum chunk plan must equal the deterministic inventory"
            )
        if len(self.a_chunks) > caps["chunks_per_role"]:
            raise ReleaseManifestError("chunk count exceeds the safety cap")

    # -- derived values ------------------------------------------------------

    @property
    def upstream_start(self) -> date:
        return self.target_start - timedelta(days=self.lookback_days)

    @property
    def budgets(self) -> dict[str, int]:
        primary = len(self.a_chunks)
        c_max = len(self.c_max_chunks)
        caps = RELEASE_SAFETY_CAPS
        budgets = {
            "a_baostock_endpoints": primary,
            "b_tencent_endpoints": primary,
            "c_sina_max_endpoints": c_max,
            "endpoint_total": primary * 2 + c_max,
            "akshare_http_max": primary * _AKSHARE_HTTP_PER_ENDPOINT["TENCENT"]
            + c_max * _AKSHARE_HTTP_PER_ENDPOINT["SINA"],
            "baostock_protocol_max": primary * _BAOSTOCK_PROTOCOL_MESSAGES_PER_ENDPOINT,
        }
        limits = {
            "a_baostock_endpoints": caps["endpoint_invocations_per_role"],
            "b_tencent_endpoints": caps["endpoint_invocations_per_role"],
            "c_sina_max_endpoints": caps["endpoint_invocations_per_role"],
            "endpoint_total": caps["endpoint_invocations_total"],
            "akshare_http_max": caps["akshare_http_requests"],
            "baostock_protocol_max": caps["baostock_protocol_messages"],
        }
        for key, value in budgets.items():
            _require_int(value, name=key, minimum=0, maximum=limits[key])
        return budgets

    @property
    def lineage_caps(self) -> dict[str, int]:
        caps = RELEASE_SAFETY_CAPS
        primary = len(self.a_chunks)
        # Per-source quality runs (A/B plus C when its exception path fired)
        # plus the final gated run before publish.
        planned_quality_runs = 1 + (2 if primary else 0) + (1 if len(self.c_max_chunks) else 0)
        return {
            "raw_batches": primary * 2 + len(self.c_max_chunks),
            "difference_reports": primary,
            "resolution_runs": 1,
            "quality_runs": planned_quality_runs,
            "published_versions": 1,
            "raw_batches_cap": caps["raw_batches"],
            "difference_reports_cap": caps["difference_reports"],
            "resolution_runs_cap": caps["resolution_runs"],
            "quality_runs_cap": caps["quality_runs"],
        }

    @property
    def would_write_directories(self) -> tuple[str, ...]:
        return (
            "warehouse/daily_bar_unadjusted/<version_id>",
            "warehouse/daily_bar_unadjusted/.locks",
            "raw/BAOSTOCK/daily_bar_unadjusted/<batch_id>",
            "raw/AKSHARE_TENCENT/daily_bar_unadjusted/<batch_id>",
            "raw/AKSHARE_SINA/daily_bar_unadjusted/<batch_id>",
            "artifacts/quality",
            "artifacts/resolution",
            "artifacts/differences",
        )

    def to_canonical_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def manifest_sha256(self) -> str:
        return hashlib.sha256(self.to_canonical_json().encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "dataset": RELEASE_MANIFEST_DATASET,
            "canonical_schema": RELEASE_MANIFEST_CANONICAL_SCHEMA,
            "symbols": list(self.symbols),
            "symbols_sha256": self.symbols_sha256,
            "target_window": {
                "start": self.target_start.isoformat(),
                "end": self.target_end.isoformat(),
            },
            "upstream_window": {
                "start": self.upstream_start.isoformat(),
                "end": self.target_end.isoformat(),
            },
            "lookback_days": self.lookback_days,
            "calendar": {
                "version_id": self.calendar_version_id,
                "sha256": self.calendar_sha256,
            },
            "security_coverage": [plan.to_dict() for plan in self.security_coverage],
            "sources": {
                "roles": {
                    "A": "BAOSTOCK/BAOSTOCK",
                    "B": "AKSHARE/TENCENT",
                    "C": "AKSHARE/SINA",
                },
                "routing": "source-resolution.daily-bar-unadjusted.k4.v1",
                "source_route_version": self.source_route_version,
                "adapter_version": self.adapter_version,
                "unit_policy_versions": dict(sorted(self.unit_policy_versions.items())),
                "comparison_threshold_version": self.threshold_rule_version,
                "resolution_rule_version": self.resolution_rule_version,
                "quality_rule_version": self.quality_rule_version,
            },
            "code_version": self.code_version,
            "chunk_policy": {
                "max_days_per_chunk": self.max_days_per_chunk,
                "max_symbols_per_chunk": 1,
            },
            "a_chunks": [chunk.to_dict() for chunk in self.a_chunks],
            "b_chunks": [chunk.to_dict() for chunk in self.b_chunks],
            "c_max_chunks": [chunk.to_dict() for chunk in self.c_max_chunks],
            "budgets": dict(self.budgets),
            "lineage_caps": dict(self.lineage_caps),
            "publish_intent": self.publish_intent,
            "would_write_directories": list(self.would_write_directories),
            "legacy": self.legacy.to_dict(),
            "safety_caps": dict(RELEASE_SAFETY_CAPS),
        }

    @classmethod
    def from_dict(cls, value: object) -> ReleaseManifest:
        if not isinstance(value, Mapping):
            raise ReleaseManifestError("release manifest must be a JSON object")
        try:
            coverage = tuple(
                SecurityCoveragePlan(
                    symbol=item["symbol"],
                    segments=tuple(
                        (
                            date.fromisoformat(pair[0]),
                            date.fromisoformat(pair[1]),
                        )
                        for pair in item["segments"]
                    ),
                    internal_gaps=tuple(
                        (
                            date.fromisoformat(pair[0]),
                            date.fromisoformat(pair[1]),
                        )
                        for pair in item.get("internal_gaps", [])
                    ),
                    exemptions=tuple(
                        (
                            date.fromisoformat(item2[0]),
                            date.fromisoformat(item2[1]),
                            item2[2],
                        )
                        for item2 in item.get("exemptions", [])
                    ),
                )
                for item in value["security_coverage"]
            )

            def parse_chunks(raw: object) -> tuple[ReleaseChunk, ...]:
                if not isinstance(raw, list):
                    raise ReleaseManifestError("chunk plans must be arrays")
                chunks: list[ReleaseChunk] = []
                for item in raw:
                    if not isinstance(item, Mapping) or set(item) != {
                        "sequence",
                        "symbol",
                        "target_start",
                        "target_end",
                        "upstream_start",
                        "upstream_end",
                    }:
                        raise ReleaseManifestError("chunk fields are invalid")
                    chunks.append(
                        ReleaseChunk(
                            sequence=item["sequence"],
                            symbol=item["symbol"],
                            target_start=date.fromisoformat(item["target_start"]),
                            target_end=date.fromisoformat(item["target_end"]),
                            upstream_start=date.fromisoformat(item["upstream_start"]),
                            upstream_end=date.fromisoformat(item["upstream_end"]),
                        )
                    )
                return tuple(chunks)

            manifest = cls(
                schema_version=value["schema_version"],
                symbols=tuple(value["symbols"]),
                symbols_sha256=value["symbols_sha256"],
                target_start=date.fromisoformat(value["target_window"]["start"]),
                target_end=date.fromisoformat(value["target_window"]["end"]),
                lookback_days=value["lookback_days"],
                calendar_version_id=value["calendar"]["version_id"],
                calendar_sha256=value["calendar"]["sha256"],
                security_coverage=coverage,
                source_route_version=value["sources"]["source_route_version"],
                adapter_version=value["sources"]["adapter_version"],
                code_version=value["code_version"],
                quality_rule_version=value["sources"]["quality_rule_version"],
                threshold_rule_version=value["sources"]["comparison_threshold_version"],
                resolution_rule_version=value["sources"]["resolution_rule_version"],
                unit_policy_versions=dict(value["sources"]["unit_policy_versions"]),
                max_days_per_chunk=value["chunk_policy"]["max_days_per_chunk"],
                a_chunks=parse_chunks(value["a_chunks"]),
                b_chunks=parse_chunks(value["b_chunks"]),
                c_max_chunks=parse_chunks(value["c_max_chunks"]),
                publish_intent=value["publish_intent"],
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, ReleaseManifestError):
                raise
            raise ReleaseManifestError("release manifest fields are invalid") from error
        if manifest.to_dict() != dict(value):
            raise ReleaseManifestError("release manifest is not in canonical form")
        return manifest


def _runs(values: set[date]) -> tuple[tuple[date, date], ...]:
    if not values:
        return ()
    ordered = sorted(values)
    result: list[tuple[date, date]] = []
    start = previous = ordered[0]
    for current in ordered[1:]:
        if current != previous + timedelta(days=1):
            result.append((start, previous))
            start = current
        previous = current
    result.append((start, previous))
    return tuple(result)


def _chunks_for_run(start: date, end: date, size: int) -> tuple[tuple[date, date], ...]:
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=size - 1))
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return tuple(chunks)


def _plan_chunks(
    *,
    symbols: tuple[str, ...],
    target_start: date,
    target_end: date,
    lookback_days: int,
    max_days_per_chunk: int,
) -> tuple[ReleaseChunk, ...]:
    chunks: list[ReleaseChunk] = []
    sequence = 0
    for symbol in symbols:
        for chunk_start, chunk_end in _chunks_for_run(target_start, target_end, max_days_per_chunk):
            chunks.append(
                ReleaseChunk(
                    sequence=sequence,
                    symbol=symbol,
                    target_start=chunk_start,
                    target_end=chunk_end,
                    upstream_start=chunk_start - timedelta(days=lookback_days),
                    upstream_end=chunk_end,
                )
            )
            sequence += 1
    return tuple(chunks)


def symbols_sha256(symbols: tuple[str, ...]) -> str:
    return hashlib.sha256(
        json.dumps(list(symbols), separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_release_manifest(
    *,
    symbols: tuple[str, ...],
    target_start: date,
    target_end: date,
    lookback_days: int,
    calendar_version_id: str,
    calendar_sha256: str,
    known_coverage: tuple[tuple[str, date, date], ...] = (),
    exemptions: tuple[tuple[str, date, date, str], ...] = (),
    source_route_version: str,
    adapter_version: str,
    code_version: str,
    quality_rule_version: str,
    threshold_rule_version: str,
    resolution_rule_version: str,
    unit_policy_versions: Mapping[str, str],
    max_days_per_chunk: int = 31,
    legacy: LegacyDisposition | None = None,
) -> ReleaseManifest:
    """Build the deterministic frozen release manifest from explicit inputs."""
    normalized_symbols = tuple(sorted(set(symbols)))
    coverage: dict[str, set[date]] = {symbol: set() for symbol in normalized_symbols}
    for symbol, start, end in known_coverage:
        if symbol not in coverage:
            raise ReleaseManifestError("known coverage symbol is outside the scope")
        if not isinstance(start, date) or not isinstance(end, date) or end < start:
            raise ReleaseManifestError("known coverage windows are invalid")
        coverage[symbol].update(
            start + timedelta(days=offset) for offset in range((end - start).days + 1)
        )
    exemption_map: dict[str, list[tuple[date, date, str]]] = {}
    for symbol, start, end, reason in exemptions:
        if symbol not in coverage:
            raise ReleaseManifestError("exemption symbol is outside the scope")
        if reason not in _EXEMPTION_REASONS:
            raise ReleaseManifestError("exemption reason is not a known legal reason")
        exemption_map.setdefault(symbol, []).append((start, end, reason))
    plans: list[SecurityCoveragePlan] = []
    for symbol in normalized_symbols:
        segments = _runs(coverage[symbol])
        gaps: list[tuple[date, date]] = []
        if segments:
            span_start, span_end = segments[0][0], segments[-1][1]
            covered: set[date] = set()
            for start, end in segments:
                covered.update(
                    start + timedelta(days=offset) for offset in range((end - start).days + 1)
                )
            missing = {
                span_start + timedelta(days=offset)
                for offset in range((span_end - span_start).days + 1)
            } - covered
            gaps = list(_runs(missing))
        plans.append(
            SecurityCoveragePlan(
                symbol=symbol,
                segments=segments,
                internal_gaps=tuple(gaps),
                exemptions=tuple(sorted(exemption_map.get(symbol, ()))),
            )
        )
    return ReleaseManifest(
        schema_version=RELEASE_MANIFEST_SCHEMA_VERSION,
        symbols=normalized_symbols,
        symbols_sha256=symbols_sha256(normalized_symbols),
        target_start=target_start,
        target_end=target_end,
        lookback_days=lookback_days,
        calendar_version_id=calendar_version_id,
        calendar_sha256=calendar_sha256,
        security_coverage=tuple(plans),
        source_route_version=source_route_version,
        adapter_version=adapter_version,
        code_version=code_version,
        quality_rule_version=quality_rule_version,
        threshold_rule_version=threshold_rule_version,
        resolution_rule_version=resolution_rule_version,
        unit_policy_versions=dict(unit_policy_versions),
        max_days_per_chunk=max_days_per_chunk,
        a_chunks=_plan_chunks(
            symbols=normalized_symbols,
            target_start=target_start,
            target_end=target_end,
            lookback_days=lookback_days,
            max_days_per_chunk=max_days_per_chunk,
        ),
        b_chunks=_plan_chunks(
            symbols=normalized_symbols,
            target_start=target_start,
            target_end=target_end,
            lookback_days=lookback_days,
            max_days_per_chunk=max_days_per_chunk,
        ),
        c_max_chunks=_plan_chunks(
            symbols=normalized_symbols,
            target_start=target_start,
            target_end=target_end,
            lookback_days=lookback_days,
            max_days_per_chunk=max_days_per_chunk,
        ),
        legacy=legacy or LegacyDisposition(),
    )


def build_zero_network_dry_run(
    manifest: ReleaseManifest,
    *,
    data_root_label: str,
) -> dict[str, object]:
    """Derive the complete R3 execution plan without any network or data-root I/O.

    The output is deterministic canonical JSON: identical manifests produce
    byte-identical plans. It is explicitly *not* an authorization — R3 real
    execution requires a new user authorization.
    """
    if not isinstance(manifest, ReleaseManifest):
        raise ReleaseManifestError("dry-run requires a ReleaseManifest")
    plan = {
        "schema_version": RELEASE_DRY_RUN_SCHEMA_VERSION,
        "release_manifest_sha256": manifest.manifest_sha256(),
        "dataset": RELEASE_MANIFEST_DATASET,
        "canonical_schema": RELEASE_MANIFEST_CANONICAL_SCHEMA,
        "symbols": list(manifest.symbols),
        "symbols_sha256": manifest.symbols_sha256,
        "target_window": {
            "start": manifest.target_start.isoformat(),
            "end": manifest.target_end.isoformat(),
        },
        "upstream_window": {
            "start": manifest.upstream_start.isoformat(),
            "end": manifest.target_end.isoformat(),
        },
        "lookback_days": manifest.lookback_days,
        "calendar": {
            "version_id": manifest.calendar_version_id,
            "sha256": manifest.calendar_sha256,
        },
        "security_coverage": [plan_.to_dict() for plan_ in manifest.security_coverage],
        "chunk_policy": {
            "max_days_per_chunk": manifest.max_days_per_chunk,
            "max_symbols_per_chunk": 1,
        },
        "a_chunks": [chunk.to_dict() for chunk in manifest.a_chunks],
        "b_chunks": [chunk.to_dict() for chunk in manifest.b_chunks],
        "c_max_chunks": [chunk.to_dict() for chunk in manifest.c_max_chunks],
        "network_budget": {
            **dict(manifest.budgets),
            "application_retries": 0,
            "fail_fast": True,
        },
        "lineage_caps": dict(manifest.lineage_caps),
        "would_write_directories": list(manifest.would_write_directories),
        "data_root_label": data_root_label,
        "legacy_disposition": manifest.legacy.to_dict(),
        "publish_intent": manifest.publish_intent,
        "requires_new_real_authorization": True,
        "network_invocations": 0,
        "reads_legacy_root": False,
        "writes_data_root": False,
    }
    return plan


__all__ = [
    "ADR_027_ID",
    "K4D_INVENTORY_EVIDENCE_SHA256",
    "K4D_PILOT_ATTEMPT2_DIAGNOSTIC_SHA256",
    "RELEASE_DRY_RUN_SCHEMA_VERSION",
    "RELEASE_MANIFEST_CANONICAL_SCHEMA",
    "RELEASE_MANIFEST_DATASET",
    "RELEASE_MANIFEST_SCHEMA_VERSION",
    "RELEASE_PUBLISH_INTENT",
    "RELEASE_SAFETY_CAPS",
    "LegacyDisposition",
    "ReleaseChunk",
    "ReleaseManifest",
    "ReleaseManifestError",
    "SecurityCoveragePlan",
    "build_release_manifest",
    "build_zero_network_dry_run",
    "symbols_sha256",
]
