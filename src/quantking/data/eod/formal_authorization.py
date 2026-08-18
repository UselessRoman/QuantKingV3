"""S1-08R3 formal authorization envelope, attempt ledger and inventory plan.

This module adds the R3-only authorization identity on top of the accepted
R2 release manifest contract (``s1-08r2-release-manifest.v1``).  It does not
modify any accepted R2 semantics: the envelope references the frozen R2
manifest SHA and repeats the derived budgets/lineage caps only so an executor
can fail closed before the first provider call when the two disagree.

Everything here is a pure domain contract except:

- :func:`compute_code_tree_sha256` (reads the repository source tree);
- :class:`FormalAttemptLedger` (creates one exclusive attempt header
  directory under an explicitly provided root before the first call).

The zero-I/O inventory plan builder performs no filesystem access at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import NoReturn

from quantking.data.adapters.sources.akshare import AKSHARE_SESSION_TIMEOUT_SECONDS
from quantking.data.adapters.sources.baostock import BAOSTOCK_SESSION_TIMEOUT_SECONDS
from quantking.data.eod.release_manifest import (
    ADR_027_ID,
    K4D_INVENTORY_EVIDENCE_SHA256,
    K4D_PILOT_ATTEMPT2_DIAGNOSTIC_SHA256,
    RELEASE_SAFETY_CAPS,
    ReleaseManifest,
    symbols_sha256,
)

FORMAL_ENVELOPE_SCHEMA_VERSION = "s1-08r3-authorization-envelope.v1"
FORMAL_INVENTORY_PLAN_SCHEMA_VERSION = "s1-08r3-inventory-plan.v1"
FORMAL_ATTEMPT_HEADER_SCHEMA = "s1-08r3-attempt-header.v1"

# ADR-027 frozen closeout facts. These are literals of history, never inputs.
LEGACY_DISCOVERED_FILES = 5_208
LEGACY_DISCOVERED_BYTES = 527_708_270
LEGACY_READABLE_FILES = 5_208
LEGACY_READ_ERRORS = 0
LEGACY_REJECTED_FILES = 5_208

LEGACY_HARD_TIMEOUTS: dict[str, float] = {
    "baostock_session_seconds": float(BAOSTOCK_SESSION_TIMEOUT_SECONDS),
    "akshare_session_seconds": float(AKSHARE_SESSION_TIMEOUT_SECONDS),
}

_EXCLUSION_REASONS = frozenset(
    {
        "USER_EXCLUDED_FROM_SCOPE",
        "NO_VERIFIED_BASE_COVERAGE",
        "SUSPENSION_WINDOW_OUT_OF_SCOPE",
        "PRE_LISTING_OUT_OF_SCOPE",
    }
)
_UNIVERSE_SOURCES = frozenset({"config-eod-symbols", "release-authority-decision"})
_SAFE_MARKER = re.compile(r"[A-Z0-9][A-Z0-9._-]{5,127}\Z")
_SAFE_AUTH_ID = re.compile(r"S1-08R3-[A-Za-z0-9][A-Za-z0-9-]{5,63}\Z")
_SAFE_IDEMPOTENCY = re.compile(r"s1-08r3-[a-z0-9][a-z0-9.-]{5,127}\Z")
_SAFE_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_GIT_HEAD = re.compile(r"[0-9a-f]{7,40}\Z")
_SAFE_RELATIVE_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/<>-]{0,200}\Z")

# Default bounded write boundary inside the formal data root. Each entry is a
# relative directory (dynamic IDs use <placeholder> tokens) plus the maximum
# number of entries the formal run may create under it.
DEFAULT_ALLOWED_WRITE_PATHS: tuple[tuple[str, int], ...] = (
    ("metadata/quantking.db", 1),
    ("metadata/quantking.db-journal", 1),
    ("logs/jobs/<job_id>.jsonl", 1),
    ("raw/<upstream>/daily_bar_unadjusted/<batch_id>", 24),
    ("artifacts/quality/<quality_run_id>", 8),
    ("artifacts/difference/<report_id>", 24),
    ("artifacts/resolution/<resolution_run_id>", 1),
    ("warehouse/daily_bar_unadjusted/.locks/<lock_name>", 1),
    ("warehouse/daily_bar_unadjusted/<version_id>", 1),
    ("warehouse/daily_bar_unadjusted/<version_id>.incomplete", 1),
)
DEFAULT_READONLY_BOUNDARIES: tuple[str, ...] = (
    "verifier: sqlite mode=ro immutable=1 with query_only and sidecar refusal",
    "data-center: bounded explicit-version queries only",
    "double-idempotency probes: zero provider invocations",
)
# ADR-028 personal-local trust model: the authorization token binds the
# approved *content* (envelope SHA), not an identity. Calendar sources that
# may back a formal release, and the publicly declared standard data root.
ALLOWED_CALENDAR_SOURCES = frozenset(
    {"published-warehouse-exchange-calendar", "baostock-query-trade-dates"}
)
DECLARED_FORMAL_DATA_ROOT_LABEL = "E:/QuantKingV3Data"


class FormalAuthorizationError(ValueError):
    """Stable fail-closed violation of the R3 formal authorization contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise FormalAuthorizationError(code, message)


def _require_sha(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _SAFE_SHA.fullmatch(value) is None:
        _fail("ENVELOPE_FIELD_INVALID", f"{name} must be a lowercase SHA-256")
    return value


def _require_pattern(value: object, pattern: re.Pattern[str], *, name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        _fail("ENVELOPE_FIELD_INVALID", f"{name} is not a safe identifier")
    return value


def _require_date(value: object, *, name: str) -> date:
    if not isinstance(value, date):
        _fail("ENVELOPE_FIELD_INVALID", f"{name} must be a date")
    return value


def excluded_symbols_sha256(
    excluded: tuple[tuple[str, str], ...],
) -> str:
    payload = json.dumps(
        [{"symbol": symbol, "reason_code": reason} for symbol, reason in excluded],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def code_tree_entries(root: Path) -> tuple[tuple[str, str], ...]:
    """Deterministic (relative posix path, file sha256) inventory of a tree.

    Only ``*.py`` files are included; ``__pycache__`` and other generated
    directories are ignored. The result is sorted by path so the digest of a
    tree is stable across machines.
    """
    if not isinstance(root, Path):
        raise TypeError("root must be a Path")
    if not root.is_dir():
        raise FormalAuthorizationError("CODE_TREE_ROOT_INVALID", "code tree root is missing")
    entries: list[tuple[str, str]] = []
    for current, directories, files in os.walk(root):
        directories[:] = sorted(name for name in directories if not name.startswith("__pycache__"))
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append((relative, digest))
    return tuple(sorted(entries))


def compute_code_tree_sha256(root: Path) -> str:
    """Canonical SHA-256 over the sorted (path, file sha) inventory."""
    entries = code_tree_entries(root)
    payload = json.dumps(
        [[path, digest] for path, digest in entries],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class UniverseDeclaration:
    """Where the candidate universe came from and its frozen identity."""

    source: str
    version: str
    sha256: str
    total: int

    def __post_init__(self) -> None:
        if self.source not in _UNIVERSE_SOURCES:
            _fail("ENVELOPE_FIELD_INVALID", "universe source is not known")
        if not isinstance(self.version, str) or not self.version:
            _fail("ENVELOPE_FIELD_INVALID", "universe version must be non-empty")
        _require_sha(self.sha256, name="universe.sha256")
        if isinstance(self.total, bool) or not isinstance(self.total, int) or self.total < 1:
            _fail("ENVELOPE_FIELD_INVALID", "universe total must be a positive integer")

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "version": self.version,
            "sha256": self.sha256,
            "total": self.total,
        }


@dataclass(frozen=True, slots=True)
class AllowedWritePath:
    """One bounded relative write target inside the formal data root."""

    relative_path: str
    maximum_entries: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.relative_path, str)
            or _SAFE_RELATIVE_PATH.fullmatch(self.relative_path) is None
            or self.relative_path.startswith("/")
            or ".." in self.relative_path.split("/")
            or "\\" in self.relative_path
        ):
            _fail("ENVELOPE_FIELD_INVALID", "allowed write path is not relative and safe")
        if (
            isinstance(self.maximum_entries, bool)
            or not isinstance(self.maximum_entries, int)
            or self.maximum_entries < 1
        ):
            _fail("ENVELOPE_FIELD_INVALID", "allowed write maximum must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "maximum_entries": self.maximum_entries,
        }


def _legacy_disposition_dict() -> dict[str, object]:
    return {
        "decision": "ADR-027",
        "decision_id": ADR_027_ID,
        "legacy_import_manifest_id": None,
        "legacy_import_manifest_sha256": None,
        "inventory_evidence_sha256": K4D_INVENTORY_EVIDENCE_SHA256,
        "pilot_attempt2_diagnostic_sha256": K4D_PILOT_ATTEMPT2_DIAGNOSTIC_SHA256,
        "discovered_files": LEGACY_DISCOVERED_FILES,
        "discovered_bytes": LEGACY_DISCOVERED_BYTES,
        "readable_files": LEGACY_READABLE_FILES,
        "read_errors": LEGACY_READ_ERRORS,
        "imported_files": 0,
        "imported_rows": 0,
        "included_files": 0,
        "published_files": 0,
        "published_rows": 0,
        "rejected_files": LEGACY_REJECTED_FILES,
    }


@dataclass(frozen=True, slots=True)
class FormalAuthorizationEnvelope:
    """The single-use formal execution identity for one S1-08R3 attempt."""

    schema_version: str
    release_manifest_sha256: str
    authorization_marker: str
    authorization_id: str
    attempt_id_format: str
    idempotency_key: str
    git_head: str
    code_tree_sha256: str
    dataset: str
    canonical_schema: str
    universe: UniverseDeclaration
    included_symbols: tuple[str, ...]
    included_symbols_sha256: str
    excluded_symbols: tuple[tuple[str, str], ...]
    excluded_symbols_sha256: str
    target_start: date
    target_end: date
    upstream_start: date
    lookback_days: int
    calendar_version_id: str
    calendar_sha256: str
    calendar_source: str
    base_coverage_binding: str
    a_baostock_endpoint_max: int
    b_tencent_endpoint_max: int
    c_sina_endpoint_max: int
    akshare_http_max: int
    baostock_protocol_max: int
    endpoint_total_max: int
    lineage_caps: dict[str, int] = field(hash=False)
    hard_timeouts: dict[str, float] = field(hash=False)
    allowed_write_paths: tuple[AllowedWritePath, ...]
    readonly_boundaries: tuple[str, ...]
    legacy_disposition: dict[str, object] = field(hash=False)

    def __post_init__(self) -> None:
        if self.schema_version != FORMAL_ENVELOPE_SCHEMA_VERSION:
            _fail("ENVELOPE_SCHEMA_MISMATCH", "unsupported envelope schema version")
        _require_sha(self.release_manifest_sha256, name="release_manifest_sha256")
        _require_pattern(self.authorization_marker, _SAFE_MARKER, name="authorization_marker")
        _require_pattern(self.authorization_id, _SAFE_AUTH_ID, name="authorization_id")
        _require_pattern(self.idempotency_key, _SAFE_IDEMPOTENCY, name="idempotency_key")
        _require_pattern(self.git_head, _SAFE_GIT_HEAD, name="git_head")
        _require_sha(self.code_tree_sha256, name="code_tree_sha256")
        if self.dataset != "daily_bar_unadjusted" or self.canonical_schema != (
            "daily_bar_unadjusted.v1"
        ):
            _fail("ENVELOPE_FIELD_INVALID", "envelope dataset binding is invalid")
        if not isinstance(self.attempt_id_format, str) or "{authorization_id}" not in (
            self.attempt_id_format
        ):
            _fail("ENVELOPE_FIELD_INVALID", "attempt id format must embed the authorization id")
        # Scope conservation: sorted unique symbols, sha binding, total balance.
        if tuple(sorted(set(self.included_symbols))) != self.included_symbols:
            _fail("ENVELOPE_SCOPE_INVALID", "included symbols must be sorted and unique")
        if symbols_sha256(self.included_symbols) != self.included_symbols_sha256:
            _fail("ENVELOPE_SCOPE_INVALID", "included symbols SHA does not match")
        normalized_exclusions: list[tuple[str, str]] = []
        for symbol, reason in self.excluded_symbols:
            if not isinstance(symbol, str) or re.fullmatch(r"\d{6}\.(?:SZ|SH|BJ)", symbol) is None:
                _fail("ENVELOPE_SCOPE_INVALID", "excluded symbol is not canonical")
            if reason not in _EXCLUSION_REASONS:
                _fail("ENVELOPE_SCOPE_INVALID", "exclusion reason is not a known stable code")
            normalized_exclusions.append((symbol, reason))
        ordered = tuple(sorted(normalized_exclusions))
        if ordered != self.excluded_symbols:
            _fail("ENVELOPE_SCOPE_INVALID", "excluded symbols must be sorted")
        if excluded_symbols_sha256(self.excluded_symbols) != self.excluded_symbols_sha256:
            _fail("ENVELOPE_SCOPE_INVALID", "excluded symbols SHA does not match")
        overlapping = set(self.included_symbols) & {symbol for symbol, _ in self.excluded_symbols}
        if overlapping:
            _fail("ENVELOPE_SCOPE_INVALID", "a symbol is both included and excluded")
        if len(self.included_symbols) + len(self.excluded_symbols) != self.universe.total:
            _fail("ENVELOPE_SCOPE_INVALID", "included + excluded must equal the universe total")
        # Window and calendar bindings.
        for field_name, field_value in (
            ("target_start", self.target_start),
            ("target_end", self.target_end),
            ("upstream_start", self.upstream_start),
        ):
            _require_date(field_value, name=field_name)
        if self.target_end < self.target_start:
            _fail("ENVELOPE_FIELD_INVALID", "target window is inverted")
        if self.upstream_start > self.target_start:
            _fail("ENVELOPE_FIELD_INVALID", "upstream window must precede the target start")
        if (self.target_start - self.upstream_start).days != self.lookback_days:
            _fail("ENVELOPE_FIELD_INVALID", "lookback must equal target minus upstream start")
        if self.lookback_days != RELEASE_SAFETY_CAPS["lookback_days"]:
            _fail("ENVELOPE_FIELD_INVALID", "lookback must stay at the frozen 15-day value")
        _require_sha(self.calendar_sha256, name="calendar_sha256")
        if not isinstance(self.calendar_version_id, str) or not self.calendar_version_id:
            _fail("ENVELOPE_FIELD_INVALID", "calendar version id must be non-empty")
        if not isinstance(self.calendar_source, str) or not self.calendar_source:
            _fail("ENVELOPE_FIELD_INVALID", "calendar source must be non-empty")
        if not isinstance(self.base_coverage_binding, str) or not self.base_coverage_binding:
            _fail("ENVELOPE_FIELD_INVALID", "base coverage binding must be non-empty")
        # Budget ceilings: every value must match the manifest caps and the
        # independent safety caps; the envelope can never widen them.
        budgets = {
            "a_baostock_endpoint_max": self.a_baostock_endpoint_max,
            "b_tencent_endpoint_max": self.b_tencent_endpoint_max,
            "c_sina_endpoint_max": self.c_sina_endpoint_max,
            "akshare_http_max": self.akshare_http_max,
            "baostock_protocol_max": self.baostock_protocol_max,
            "endpoint_total_max": self.endpoint_total_max,
        }
        for budget_name, budget_value in budgets.items():
            if (
                isinstance(budget_value, bool)
                or not isinstance(budget_value, int)
                or budget_value < 0
            ):
                _fail(
                    "ENVELOPE_FIELD_INVALID",
                    f"{budget_name} must be a non-negative integer",
                )
        if (
            self.a_baostock_endpoint_max > RELEASE_SAFETY_CAPS["endpoint_invocations_per_role"]
            or self.b_tencent_endpoint_max > RELEASE_SAFETY_CAPS["endpoint_invocations_per_role"]
            or self.c_sina_endpoint_max > RELEASE_SAFETY_CAPS["endpoint_invocations_per_role"]
        ):
            _fail("ENVELOPE_BUDGET_INVALID", "a per-role endpoint cap exceeds the safety cap")
        if self.endpoint_total_max > RELEASE_SAFETY_CAPS["endpoint_invocations_total"]:
            _fail("ENVELOPE_BUDGET_INVALID", "endpoint total exceeds the safety cap")
        if self.endpoint_total_max != (
            self.a_baostock_endpoint_max + self.b_tencent_endpoint_max + self.c_sina_endpoint_max
        ):
            _fail("ENVELOPE_BUDGET_INVALID", "endpoint total must equal the sum of roles")
        if self.akshare_http_max != (
            self.b_tencent_endpoint_max * 3 + self.c_sina_endpoint_max * 2
        ):
            _fail("ENVELOPE_BUDGET_INVALID", "AKShare HTTP cap must be derived from roles")
        if self.akshare_http_max > RELEASE_SAFETY_CAPS["akshare_http_requests"]:
            _fail("ENVELOPE_BUDGET_INVALID", "AKShare HTTP cap exceeds the safety cap")
        if self.baostock_protocol_max != self.a_baostock_endpoint_max * 3:
            _fail("ENVELOPE_BUDGET_INVALID", "BaoStock protocol cap must be derived from A")
        if self.baostock_protocol_max > RELEASE_SAFETY_CAPS["baostock_protocol_messages"]:
            _fail("ENVELOPE_BUDGET_INVALID", "BaoStock protocol cap exceeds the safety cap")
        # Lineage caps must equal the frozen R2 caps exactly.
        if not isinstance(self.lineage_caps, dict) or not self.lineage_caps:
            _fail("ENVELOPE_FIELD_INVALID", "lineage caps must be a non-empty mapping")
        for cap_value in self.lineage_caps.values():
            if isinstance(cap_value, bool) or not isinstance(cap_value, int) or cap_value < 1:
                _fail("ENVELOPE_FIELD_INVALID", "lineage caps must be positive integers")
        # Hard timeouts are the frozen session timeouts.
        if dict(self.hard_timeouts) != LEGACY_HARD_TIMEOUTS:
            _fail("ENVELOPE_FIELD_INVALID", "hard timeouts must equal the frozen session values")
        if not self.allowed_write_paths:
            _fail("ENVELOPE_FIELD_INVALID", "allowed write paths must not be empty")
        for entry in self.allowed_write_paths:
            if not isinstance(entry, AllowedWritePath):
                _fail("ENVELOPE_FIELD_INVALID", "allowed write entry is invalid")
        if not self.readonly_boundaries:
            _fail("ENVELOPE_FIELD_INVALID", "readonly boundaries must not be empty")
        if dict(self.legacy_disposition) != _legacy_disposition_dict():
            _fail(
                "ENVELOPE_LEGACY_INVALID",
                "legacy disposition must equal the frozen ADR-027 facts",
            )

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def envelope_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def authorization_token(self) -> str:
        """ADR-028 content-bound token: marker, id and the envelope SHA.

        Any change to the envelope content changes the SHA and therefore
        invalidates every previously issued token with the same marker and
        authorization id.
        """

        return f"{self.authorization_marker}:{self.authorization_id}:{self.envelope_sha256()}"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "release_manifest_sha256": self.release_manifest_sha256,
            "authorization_marker": self.authorization_marker,
            "authorization_id": self.authorization_id,
            "attempt_id_format": self.attempt_id_format,
            "idempotency_key": self.idempotency_key,
            "git_head": self.git_head,
            "code_tree_sha256": self.code_tree_sha256,
            "dataset": self.dataset,
            "canonical_schema": self.canonical_schema,
            "universe": self.universe.to_dict(),
            "included_symbols": list(self.included_symbols),
            "included_symbols_sha256": self.included_symbols_sha256,
            "excluded_symbols": [
                {"symbol": symbol, "reason_code": reason}
                for symbol, reason in self.excluded_symbols
            ],
            "excluded_symbols_sha256": self.excluded_symbols_sha256,
            "target_start": self.target_start.isoformat(),
            "target_end": self.target_end.isoformat(),
            "upstream_start": self.upstream_start.isoformat(),
            "lookback_days": self.lookback_days,
            "calendar_version_id": self.calendar_version_id,
            "calendar_sha256": self.calendar_sha256,
            "calendar_source": self.calendar_source,
            "base_coverage_binding": self.base_coverage_binding,
            "a_baostock_endpoint_max": self.a_baostock_endpoint_max,
            "b_tencent_endpoint_max": self.b_tencent_endpoint_max,
            "c_sina_endpoint_max": self.c_sina_endpoint_max,
            "akshare_http_max": self.akshare_http_max,
            "baostock_protocol_max": self.baostock_protocol_max,
            "endpoint_total_max": self.endpoint_total_max,
            "lineage_caps": dict(sorted(self.lineage_caps.items())),
            "application_retries": 0,
            "fail_fast": True,
            "hard_timeouts": dict(sorted(self.hard_timeouts.items())),
            "publish_intent": "PUBLISH_AFTER_QUALITY",
            "allowed_write_paths": [entry.to_dict() for entry in self.allowed_write_paths],
            "readonly_boundaries": list(self.readonly_boundaries),
            "legacy_disposition": dict(sorted(self.legacy_disposition.items())),
            "requires_new_real_authorization": True,
        }

    @classmethod
    def from_dict(cls, value: object) -> FormalAuthorizationEnvelope:
        if not isinstance(value, dict):
            _fail("ENVELOPE_SCHEMA_MISMATCH", "envelope must be a JSON object")
        required = {
            "schema_version",
            "release_manifest_sha256",
            "authorization_marker",
            "authorization_id",
            "attempt_id_format",
            "idempotency_key",
            "git_head",
            "code_tree_sha256",
            "dataset",
            "canonical_schema",
            "universe",
            "included_symbols",
            "included_symbols_sha256",
            "excluded_symbols",
            "excluded_symbols_sha256",
            "target_start",
            "target_end",
            "upstream_start",
            "lookback_days",
            "calendar_version_id",
            "calendar_sha256",
            "calendar_source",
            "base_coverage_binding",
            "a_baostock_endpoint_max",
            "b_tencent_endpoint_max",
            "c_sina_endpoint_max",
            "akshare_http_max",
            "baostock_protocol_max",
            "endpoint_total_max",
            "lineage_caps",
            "application_retries",
            "fail_fast",
            "hard_timeouts",
            "publish_intent",
            "allowed_write_paths",
            "readonly_boundaries",
            "legacy_disposition",
            "requires_new_real_authorization",
        }
        if set(value) != required:
            missing = sorted(required - set(value))
            extra = sorted(set(value) - required)
            _fail(
                "ENVELOPE_SCHEMA_MISMATCH",
                f"envelope fields mismatch (missing={missing}, extra={extra})",
            )
        if value["application_retries"] != 0 or value["fail_fast"] is not True:
            _fail("ENVELOPE_FIELD_INVALID", "retries must be 0 and fail_fast true")
        if value["publish_intent"] != "PUBLISH_AFTER_QUALITY":
            _fail("ENVELOPE_FIELD_INVALID", "publish intent must be PUBLISH_AFTER_QUALITY")
        if value["requires_new_real_authorization"] is not True:
            _fail("ENVELOPE_FIELD_INVALID", "envelope must require new real authorization")
        universe_raw = value["universe"]
        if not isinstance(universe_raw, dict) or set(universe_raw) != {
            "source",
            "version",
            "sha256",
            "total",
        }:
            _fail("ENVELOPE_FIELD_INVALID", "universe declaration fields are invalid")
        universe = UniverseDeclaration(
            source=universe_raw["source"],
            version=universe_raw["version"],
            sha256=universe_raw["sha256"],
            total=universe_raw["total"],
        )
        included_raw = value["included_symbols"]
        if not isinstance(included_raw, list) or any(
            not isinstance(item, str) for item in included_raw
        ):
            _fail("ENVELOPE_SCOPE_INVALID", "included symbols must be a list of strings")
        excluded_raw = value["excluded_symbols"]
        if not isinstance(excluded_raw, list):
            _fail("ENVELOPE_SCOPE_INVALID", "excluded symbols must be a list")
        excluded: list[tuple[str, str]] = []
        for item in excluded_raw:
            if not isinstance(item, dict) or set(item) != {"symbol", "reason_code"}:
                _fail("ENVELOPE_SCOPE_INVALID", "excluded entry fields are invalid")
            excluded.append((item["symbol"], item["reason_code"]))
        paths_raw = value["allowed_write_paths"]
        if not isinstance(paths_raw, list):
            _fail("ENVELOPE_FIELD_INVALID", "allowed write paths must be a list")
        paths = tuple(
            AllowedWritePath(
                relative_path=item["relative_path"],
                maximum_entries=item["maximum_entries"],
            )
            for item in paths_raw
            if isinstance(item, dict)
        )
        if len(paths) != len(paths_raw):
            _fail("ENVELOPE_FIELD_INVALID", "allowed write entries are invalid")
        lineage_raw = value["lineage_caps"]
        if not isinstance(lineage_raw, dict) or not all(
            isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
            for k, v in lineage_raw.items()
        ):
            _fail("ENVELOPE_FIELD_INVALID", "lineage caps must map strings to integers")
        timeouts_raw = value["hard_timeouts"]
        if not isinstance(timeouts_raw, dict):
            _fail("ENVELOPE_FIELD_INVALID", "hard timeouts must be a mapping")
        legacy_raw = value["legacy_disposition"]
        if not isinstance(legacy_raw, dict):
            _fail("ENVELOPE_LEGACY_INVALID", "legacy disposition must be a mapping")
        readonly_raw = value["readonly_boundaries"]
        if not isinstance(readonly_raw, list) or any(
            not isinstance(item, str) or not item for item in readonly_raw
        ):
            _fail("ENVELOPE_FIELD_INVALID", "readonly boundaries must be non-empty strings")
        return cls(
            schema_version=value["schema_version"],
            release_manifest_sha256=value["release_manifest_sha256"],
            authorization_marker=value["authorization_marker"],
            authorization_id=value["authorization_id"],
            attempt_id_format=value["attempt_id_format"],
            idempotency_key=value["idempotency_key"],
            git_head=value["git_head"],
            code_tree_sha256=value["code_tree_sha256"],
            dataset=value["dataset"],
            canonical_schema=value["canonical_schema"],
            universe=universe,
            included_symbols=tuple(included_raw),
            included_symbols_sha256=value["included_symbols_sha256"],
            excluded_symbols=tuple(excluded),
            excluded_symbols_sha256=value["excluded_symbols_sha256"],
            target_start=date.fromisoformat(value["target_start"]),
            target_end=date.fromisoformat(value["target_end"]),
            upstream_start=date.fromisoformat(value["upstream_start"]),
            lookback_days=value["lookback_days"],
            calendar_version_id=value["calendar_version_id"],
            calendar_sha256=value["calendar_sha256"],
            calendar_source=value["calendar_source"],
            base_coverage_binding=value["base_coverage_binding"],
            a_baostock_endpoint_max=value["a_baostock_endpoint_max"],
            b_tencent_endpoint_max=value["b_tencent_endpoint_max"],
            c_sina_endpoint_max=value["c_sina_endpoint_max"],
            akshare_http_max=value["akshare_http_max"],
            baostock_protocol_max=value["baostock_protocol_max"],
            endpoint_total_max=value["endpoint_total_max"],
            lineage_caps=dict(sorted(lineage_raw.items())),
            hard_timeouts=dict(sorted(timeouts_raw.items())),
            allowed_write_paths=paths,
            readonly_boundaries=tuple(readonly_raw),
            legacy_disposition=dict(sorted(legacy_raw.items())),
        )


def build_formal_authorization_envelope(
    *,
    manifest: ReleaseManifest,
    authorization_marker: str,
    authorization_id: str,
    idempotency_key: str,
    git_head: str,
    code_tree_sha256: str,
    universe_version: str,
    universe_sha256: str,
    universe_total: int,
    excluded_symbols: tuple[tuple[str, str], ...] = (),
    calendar_source: str,
    calendar_version_id: str | None = None,
    base_coverage_binding: str,
    allowed_write_paths: tuple[tuple[str, int], ...] = DEFAULT_ALLOWED_WRITE_PATHS,
    readonly_boundaries: tuple[str, ...] = DEFAULT_READONLY_BOUNDARIES,
) -> FormalAuthorizationEnvelope:
    """Derive the envelope from a verified R2 manifest plus R3 identity.

    All scope, budget and lineage values are taken from the manifest; the
    builder refuses to construct an envelope that disagrees with it.
    """
    if not isinstance(manifest, ReleaseManifest):
        raise TypeError("manifest must be a ReleaseManifest")
    budgets = manifest.budgets
    included = manifest.symbols
    if len(included) + len(excluded_symbols) != universe_total:
        _fail("ENVELOPE_SCOPE_INVALID", "manifest scope plus exclusions must equal the universe")
    return FormalAuthorizationEnvelope(
        schema_version=FORMAL_ENVELOPE_SCHEMA_VERSION,
        release_manifest_sha256=manifest.manifest_sha256(),
        authorization_marker=authorization_marker,
        authorization_id=authorization_id,
        attempt_id_format="S1-08R3-E1-{authorization_id}-attempt-1",
        idempotency_key=idempotency_key,
        git_head=git_head,
        code_tree_sha256=code_tree_sha256,
        dataset="daily_bar_unadjusted",
        canonical_schema="daily_bar_unadjusted.v1",
        universe=UniverseDeclaration(
            source="config-eod-symbols",
            version=universe_version,
            sha256=universe_sha256,
            total=universe_total,
        ),
        included_symbols=included,
        included_symbols_sha256=symbols_sha256(included),
        excluded_symbols=tuple(sorted(excluded_symbols)),
        excluded_symbols_sha256=excluded_symbols_sha256(tuple(sorted(excluded_symbols))),
        target_start=manifest.target_start,
        target_end=manifest.target_end,
        upstream_start=manifest.upstream_start,
        lookback_days=manifest.lookback_days,
        calendar_version_id=calendar_version_id or manifest.calendar_version_id,
        calendar_sha256=manifest.calendar_sha256,
        calendar_source=calendar_source,
        base_coverage_binding=base_coverage_binding,
        a_baostock_endpoint_max=budgets["a_baostock_endpoints"],
        b_tencent_endpoint_max=budgets["b_tencent_endpoints"],
        c_sina_endpoint_max=budgets["c_sina_max_endpoints"],
        akshare_http_max=budgets["akshare_http_max"],
        baostock_protocol_max=budgets["baostock_protocol_max"],
        endpoint_total_max=budgets["endpoint_total"],
        lineage_caps=dict(manifest.lineage_caps),
        hard_timeouts=dict(LEGACY_HARD_TIMEOUTS),
        allowed_write_paths=tuple(
            AllowedWritePath(relative_path=path, maximum_entries=maximum)
            for path, maximum in allowed_write_paths
        ),
        readonly_boundaries=readonly_boundaries,
        legacy_disposition=_legacy_disposition_dict(),
    )


def assert_envelope_matches_manifest(
    envelope: FormalAuthorizationEnvelope,
    manifest: ReleaseManifest,
) -> None:
    """Fail closed when an envelope and manifest describe different releases."""
    if not isinstance(envelope, FormalAuthorizationEnvelope):
        raise TypeError("envelope must be a FormalAuthorizationEnvelope")
    if not isinstance(manifest, ReleaseManifest):
        raise TypeError("manifest must be a ReleaseManifest")
    checks: tuple[tuple[str, object, object], ...] = (
        ("release_manifest_sha256", envelope.release_manifest_sha256, manifest.manifest_sha256()),
        ("included_symbols", envelope.included_symbols, manifest.symbols),
        ("target_start", envelope.target_start, manifest.target_start),
        ("target_end", envelope.target_end, manifest.target_end),
        ("lookback_days", envelope.lookback_days, manifest.lookback_days),
        # The calendar content SHA must agree (same frozen calendar); the
        # concrete published version id lives in the envelope only, because
        # the R2 manifest schema keeps a loose version label.
        ("calendar_sha256", envelope.calendar_sha256, manifest.calendar_sha256),
    )
    for name, left, right in checks:
        if left != right:
            _fail("ENVELOPE_MANIFEST_MISMATCH", f"{name} disagrees with the release manifest")
    budgets = manifest.budgets
    budget_checks = (
        (
            "a_baostock_endpoint_max",
            envelope.a_baostock_endpoint_max,
            budgets["a_baostock_endpoints"],
        ),
        (
            "b_tencent_endpoint_max",
            envelope.b_tencent_endpoint_max,
            budgets["b_tencent_endpoints"],
        ),
        (
            "c_sina_endpoint_max",
            envelope.c_sina_endpoint_max,
            budgets["c_sina_max_endpoints"],
        ),
        ("akshare_http_max", envelope.akshare_http_max, budgets["akshare_http_max"]),
        (
            "baostock_protocol_max",
            envelope.baostock_protocol_max,
            budgets["baostock_protocol_max"],
        ),
        ("endpoint_total_max", envelope.endpoint_total_max, budgets["endpoint_total"]),
    )
    for name, left, right in budget_checks:
        if left != right:
            _fail("ENVELOPE_MANIFEST_MISMATCH", f"{name} disagrees with the manifest budget")
    if dict(envelope.lineage_caps) != dict(manifest.lineage_caps):
        _fail("ENVELOPE_MANIFEST_MISMATCH", "lineage caps disagree with the manifest")


# -- zero-I/O formal inventory plan -----------------------------------------


def build_formal_inventory_plan(
    *,
    marker: str,
    authorization_id: str,
    allowed_root_label: str,
    sqlite_tables: tuple[str, ...],
    directory_listings: tuple[str, ...],
    file_sha_targets: tuple[str, ...],
    query_limit: int,
    code_tree_sha256: str,
    git_head: str,
) -> dict[str, object]:
    """Build the read-only formal-data-root inventory plan without any I/O.

    The plan is a pure declaration: no path is opened, listed or hashed here.
    It binds an exact marker/authorization pair, an explicit read whitelist,
    hard query bounds, and the before/after immutability proof that the later
    authorized execution must produce.
    """
    _require_pattern(marker, _SAFE_MARKER, name="marker")
    _require_pattern(authorization_id, _SAFE_AUTH_ID, name="authorization_id")
    if not isinstance(allowed_root_label, str) or not allowed_root_label:
        _fail("PLAN_FIELD_INVALID", "allowed root label must be non-empty")
    for group, values in (
        ("sqlite_tables", sqlite_tables),
        ("directory_listings", directory_listings),
        ("file_sha_targets", file_sha_targets),
    ):
        if not isinstance(values, tuple) or any(
            not isinstance(item, str)
            or not item
            or item.startswith(("/", "\\"))
            or ".." in item.split("/")
            for item in values
        ):
            _fail("PLAN_FIELD_INVALID", f"{group} must be safe relative targets")
        if len(set(values)) != len(values):
            _fail("PLAN_FIELD_INVALID", f"{group} must not contain duplicates")
    if isinstance(query_limit, bool) or not isinstance(query_limit, int) or query_limit < 1:
        _fail("PLAN_FIELD_INVALID", "query limit must be a positive integer")
    _require_sha(code_tree_sha256, name="code_tree_sha256")
    _require_pattern(git_head, _SAFE_GIT_HEAD, name="git_head")
    plan = {
        "schema_version": FORMAL_INVENTORY_PLAN_SCHEMA_VERSION,
        "authorization_marker": marker,
        "authorization_id": authorization_id,
        "allowed_root": allowed_root_label,
        "allowed_reads": {
            "sqlite_tables_readonly": [
                {
                    "table": table,
                    "mode": "ro&immutable=1",
                    "query_only": True,
                    "row_limit": query_limit,
                }
                for table in sqlite_tables
            ],
            "directory_listings": [
                {"relative_path": path, "depth": "single-level", "entries_limit": query_limit}
                for path in directory_listings
            ],
            "file_sha256": [{"relative_path": path} for path in file_sha_targets],
        },
        "network_calls": 0,
        "writes": 0,
        "sqlite_checkpoints": 0,
        "wal_shm_journal_creation": "forbidden",
        "mutations": "forbidden: no move, delete, recovery or repair",
        "provider_invocations": 0,
        "legacy_root_access": False,
        "backup_root_access": False,
        "output_schema": {
            "description": "sanitized inventory facts only",
            "contains": [
                "job/candidate closeout statuses",
                "published calendar/base version ids and SHAs when present",
                "stale .incomplete / lock / sidecar presence flags",
                "file-set inventory with size/mtime/sha256 snapshots",
            ],
            "excludes": ["absolute paths of other roots", "provider payloads", "secrets"],
        },
        "immutability_proof": (
            "file set, size, mtime and sha256 of the allowed root must be identical "
            "before and after the authorized inventory execution"
        ),
        "failure_policy": (
            "stop immediately on any blocker; recovery needs a separate authorization"
        ),
        "code_tree_sha256": code_tree_sha256,
        "git_head": git_head,
        "git_head_role": (
            "informational-baseline: the execution preflight binds"
            " code_tree_sha256 (exact repository content), not the moving HEAD"
        ),
        "requires_new_real_authorization": True,
    }
    return plan


def inventory_plan_sha256(plan: dict[str, object]) -> str:
    payload = json.dumps(
        plan,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# -- ADR-028 formal entry boundary checks -------------------------------------


def _boundary_path(
    value: Path,
    *,
    base: Path | None = None,
    code: str,
    label: str,
) -> Path:
    """Validate one absolute boundary path: no ``..``, no symlink/reparse escape.

    When ``base`` is given the fully resolved path must stay inside the fully
    resolved base. This is a mis-execution guard for the personal-local trust
    model (ADR-028); it is not a defence against a hostile local administrator.
    """

    if not isinstance(value, Path) or not value.is_absolute():
        _fail(code, f"{label} must be an absolute path")
    if any(part == ".." for part in value.parts):
        _fail(code, f"{label} must not contain '..' fragments")
    resolved = Path(os.path.realpath(value))
    if value.is_symlink():
        _fail(code, f"{label} must not be a symlink or reparse point")
    if base is None:
        return resolved
    if not isinstance(base, Path) or not base.is_absolute():
        _fail(code, f"{label} base must be an absolute path")
    base_resolved = Path(os.path.realpath(base))
    if resolved != base_resolved and base_resolved not in resolved.parents:
        _fail(code, f"{label} escapes its allowed base")
    return resolved


def assert_formal_entry_boundary(
    envelope: FormalAuthorizationEnvelope,
    *,
    settings_data_root: Path,
    declared_data_root: Path,
    attempts_root: Path,
    attempts_root_base: Path,
) -> None:
    """Fail closed before runtime construction on entry-boundary drift.

    Checks the frozen write-path template, the calendar source binding, the
    declared formal data root, and the attempt-ledger location. Every check
    precedes any provider call or business write (ADR-028).
    """

    if not isinstance(envelope, FormalAuthorizationEnvelope):
        raise TypeError("envelope must be a FormalAuthorizationEnvelope")
    expected_paths = {path: maximum for path, maximum in DEFAULT_ALLOWED_WRITE_PATHS}
    actual_paths = {
        entry.relative_path: entry.maximum_entries for entry in envelope.allowed_write_paths
    }
    if actual_paths != expected_paths:
        _fail(
            "ENVELOPE_WRITE_PATHS_INVALID",
            "allowed write paths must equal the frozen production template",
        )
    if envelope.calendar_source not in ALLOWED_CALENDAR_SOURCES:
        _fail("CALENDAR_SOURCE_INVALID", "calendar source is not an allowed formal source")
    if not envelope.calendar_version_id.startswith("data_version:"):
        _fail("CALENDAR_BINDING_INVALID", "calendar binding must reference a data_version id")
    settings_root = _boundary_path(
        settings_data_root,
        code="DATA_ROOT_INVALID",
        label="settings data root",
    )
    declared_root = _boundary_path(
        declared_data_root,
        code="DATA_ROOT_INVALID",
        label="declared data root",
    )
    if settings_root != declared_root:
        _fail(
            "DATA_ROOT_INVALID",
            "the configured data root is not the declared formal data root",
        )
    _boundary_path(
        attempts_root,
        base=attempts_root_base,
        code="ATTEMPTS_ROOT_INVALID",
        label="attempts root",
    )


# -- single-use attempt ledger ------------------------------------------------


class FormalAttemptLedger:
    """Exclusive one-attempt-per-authorization header directory guard.

    ``begin`` atomically creates ``<attempts_root>/<authorization_id>/`` with
    an ``attempt-header.json`` binding the envelope SHA, marker and
    idempotency key. A replayed authorization id fails before any provider
    call can start. Failure and completion facts are appended as separate
    immutable files; nothing is ever overwritten.
    """

    def __init__(self, attempts_root: Path) -> None:
        if not isinstance(attempts_root, Path):
            raise TypeError("attempts_root must be a Path")
        self._root = attempts_root

    def begin(
        self,
        *,
        envelope_sha256: str,
        marker: str,
        authorization_id: str,
        idempotency_key: str,
        now_iso: str,
    ) -> Path:
        _require_sha(envelope_sha256, name="envelope_sha256")
        _require_pattern(marker, _SAFE_MARKER, name="marker")
        _require_pattern(authorization_id, _SAFE_AUTH_ID, name="authorization_id")
        _require_pattern(idempotency_key, _SAFE_IDEMPOTENCY, name="idempotency_key")
        attempt_root = self._root / authorization_id
        try:
            attempt_root.mkdir(parents=True, exist_ok=False)
        except FileExistsError as error:
            raise FormalAuthorizationError(
                "AUTHORIZATION_ALREADY_CONSUMED",
                "this authorization id already has an attempt directory",
            ) from error
        header = {
            "schema_version": FORMAL_ATTEMPT_HEADER_SCHEMA,
            "authorization_id": authorization_id,
            "authorization_marker": marker,
            "envelope_sha256": envelope_sha256,
            "idempotency_key": idempotency_key,
            "started_at": now_iso,
            "status": "STARTED",
        }
        (attempt_root / "attempt-header.json").write_text(
            json.dumps(
                header,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        return attempt_root

    def record(
        self,
        authorization_id: str,
        *,
        status: str,
        now_iso: str,
        detail: dict[str, object] | None = None,
    ) -> Path:
        if status not in {"FAILED", "COMPLETED"}:
            _fail("ATTEMPT_STATUS_INVALID", "attempt status must be FAILED or COMPLETED")
        attempt_root = self._root / authorization_id
        if not attempt_root.is_dir():
            _fail("ATTEMPT_MISSING", "attempt directory does not exist")
        record = {
            "schema_version": FORMAL_ATTEMPT_HEADER_SCHEMA,
            "authorization_id": authorization_id,
            "status": status,
            "recorded_at": now_iso,
            "detail": dict(detail or {}),
        }
        target = attempt_root / f"{status.lower()}.json"
        if target.exists():
            _fail("ATTEMPT_RECORD_IMMUTABLE", "attempt records are append-only")
        target.write_text(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        return target


__all__ = [
    "ALLOWED_CALENDAR_SOURCES",
    "DECLARED_FORMAL_DATA_ROOT_LABEL",
    "DEFAULT_ALLOWED_WRITE_PATHS",
    "DEFAULT_READONLY_BOUNDARIES",
    "FORMAL_ATTEMPT_HEADER_SCHEMA",
    "FORMAL_ENVELOPE_SCHEMA_VERSION",
    "FORMAL_INVENTORY_PLAN_SCHEMA_VERSION",
    "LEGACY_HARD_TIMEOUTS",
    "AllowedWritePath",
    "FormalAttemptLedger",
    "FormalAuthorizationEnvelope",
    "FormalAuthorizationError",
    "UniverseDeclaration",
    "assert_envelope_matches_manifest",
    "assert_formal_entry_boundary",
    "build_formal_authorization_envelope",
    "build_formal_inventory_plan",
    "code_tree_entries",
    "compute_code_tree_sha256",
    "excluded_symbols_sha256",
    "inventory_plan_sha256",
]
