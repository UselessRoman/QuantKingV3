"""Pure S1-07K4D pilot contracts: semantics, confirmation rules, and plan.

Everything in this module is framework-free and filesystem-free: building a
pilot plan reads only the frozen in-repo inventory payload, and the semantics
confirmation functions operate on plain row mappings. The real legacy root,
service network, and staging writes belong exclusively to the authorized
executor in ``quantking.data.adapters.legacy_pilot``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from quantking.data.comparison.thresholds import (
    K4_THRESHOLD_RULE_VERSION,
    ThresholdSet,
    rows_within_threshold,
)
from quantking.data.legacy_import.contracts import (
    K4D_PILOT_REAL_MARKER,
    LegacyInventoryEntry,
    canonical_json_bytes,
    inventory_from_payload,
    sha256_bytes,
)
from quantking.data.models import DataUpstream

K4D_SEMANTICS_SCHEMA_VERSION = "legacy-import-semantics.k4d.v1"
K4D_PILOT_PLAN_SCHEMA_VERSION = "legacy-pilot-dry-run.k4d.v1"
K4D_SEMANTICS_CONFIRMATION_SCHEMA_VERSION = "legacy-semantics-confirmation.k4d.v1"
# The six fixed pilot securities; selection is exact-match only.
K4D_PILOT_SYMBOLS: tuple[str, ...] = (
    "000001.SZ",
    "000858.SZ",
    "300750.SZ",
    "600000.SH",
    "688237.SH",
    "688981.SH",
)
K4D_RECHECK_WINDOW_START = "2025-05-01"
K4D_RECHECK_WINDOW_END = "2026-06-03"
K4D_ENDPOINT_CALL_BUDGET = 18
K4D_PER_SOURCE_CALL_BUDGET = 6
K4D_TENCENT_HTTP_UPPER_BOUND = 18
K4D_SINA_HTTP_UPPER_BOUND = 12
K4D_HTTP_TOTAL_UPPER_BOUND = 30
K4D_BAOSTOCK_PROTOCOL_UPPER_BOUND = 18
K4D_BAOSTOCK_SESSION_TIMEOUT_SECONDS = 20
K4D_AKSHARE_SESSION_TIMEOUT_SECONDS = 60
K4D_EXPECTED_FILE_COUNT = 6
K4D_EXPECTED_BYTE_COUNT = 878_893
K4D_EXPECTED_RAW_ROW_COUNT = 26_200
K4D_EXPECTED_FIRST_ROW_EXCLUSIONS = 6
K4D_EXPECTED_CANONICAL_ROW_COUNT = 26_194
K4D_UNIT_MIN_SAMPLE_ROWS = 20
K4D_UNADJUSTED_MIN_RELATIVE_DIFF = Decimal("0.005")
K4D_BAOSTOCK_UNIT_POLICY_VERSION = "baostock-0.9.3-returned-units.v1"
K4D_AKSHARE_UNIT_POLICY_VERSION = "akshare-1.18.83-returned-units.v1"
_VOLUME_FACTORS = (1, 100)
_AMOUNT_FACTORS = (1, 1_000)
_PRICE_FIELDS = ("open", "high", "low", "close")
Key = tuple[str, str]


class K4DSemanticsError(ValueError):
    """Stable-code semantics failure (never leaks paths or raw payloads)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class K4DSemanticsStatus(StrEnum):
    HYPOTHESIS = "HYPOTHESIS"
    CONFIRMED = "CONFIRMED"


def _numeric(value: object) -> float | None:
    """Narrow one row value to a plain finite-capable float, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def parse_k4d_date(value: object) -> date:
    """Parse one strict eight-digit ``yyyyMMdd`` source date; no locale."""
    if not isinstance(value, str) or len(value) != 8 or not value.isascii() or not value.isdigit():
        raise K4DSemanticsError("INVALID_DATE_FORMAT")
    try:
        return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    except ValueError as error:
        raise K4DSemanticsError("INVALID_DATE_FORMAT") from error


@dataclass(frozen=True, slots=True)
class LegacyK4DSemantics:
    """Explicit no-guess semantics for the real legacy schema (k4d.v1).

    The real inventory proved the source columns are ``code``/``date``
    (yyyyMMdd strings) plus double OHLC/volume/amount with no pre_close and
    no suspension field, so every derived meaning must be an explicit,
    versioned policy instead of the K3C assumption that both already exist.
    """

    status: K4DSemanticsStatus = K4DSemanticsStatus.HYPOTHESIS

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": K4D_SEMANTICS_SCHEMA_VERSION,
            "column_mapping": {
                "code": "code",
                "trade_date": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
                "amount": "amount",
            },
            "date_format": "yyyyMMdd",
            "pre_close_policy": "derive_from_previous_unadjusted_close",
            "first_row_policy": "retain_raw_exclude_from_canonical",
            "suspension_policy": "derive_from_price_equals_pre_close_and_zero_volume_amount",
            "missing_suspension_row_policy": "preserve_as_absent_do_not_synthesize",
            "null_policy": "reject_required_field_nulls",
            "timezone": "Asia/Shanghai",
            "unadjusted": True,
            "volume_unit": "share",
            "amount_unit": "cny_yuan",
            "status": self.status.value,
            "confirmation_conditions": [
                "unit_scale_volume_factor_1_unique_within_k4_thresholds",
                "unit_scale_amount_factor_1_unique_within_k4_thresholds",
                "volume_integral_finite_non_negative_after_scale",
                "amount_finite_non_negative_after_scale",
                "unadjusted_boundary_observed_vendor_pre_close_vs_derived",
                "x_close_agrees_with_unadjusted_service_within_k4",
            ],
        }


def k4d_hypothesis_payload() -> dict[str, object]:
    """The only semantics state a dry-run may ever freeze."""
    return LegacyK4DSemantics().to_payload()


def k4d_semantics_from_payload(value: object) -> LegacyK4DSemantics:
    if not isinstance(value, dict) or "status" not in value:
        raise K4DSemanticsError("SEMANTICS_PAYLOAD_INVALID")
    try:
        status = K4DSemanticsStatus(str(value["status"]))
    except ValueError as error:
        raise K4DSemanticsError("SEMANTICS_PAYLOAD_INVALID") from error
    if value != LegacyK4DSemantics(status).to_payload():
        raise K4DSemanticsError("SEMANTICS_PAYLOAD_INVALID")
    return LegacyK4DSemantics(status)


def classify_k4d_row(row: Mapping[str, object], derived_pre_close: float) -> str:
    """Classify one source row once its derived pre_close is known.

    Returns ``NORMAL``, ``SUSPENDED_PLACEHOLDER``, or ``ISOLATED:<code>``.
    Missing trading days are never synthesized; they simply stay absent.
    """
    values: dict[str, float] = {}
    for name in ("open", "high", "low", "close", "volume", "amount"):
        value = _numeric(row.get(name))
        if value is None or not math.isfinite(value):
            return f"ISOLATED:FIELD_ANOMALY:{name}"
        if value < 0:
            return f"ISOLATED:NEGATIVE_VALUE:{name}"
        values[name] = value
    volume = values["volume"]
    amount = values["amount"]
    if volume == 0 and amount == 0:
        if all(values[name] == derived_pre_close for name in _PRICE_FIELDS):
            return "SUSPENDED_PLACEHOLDER"
        return "ISOLATED:ZERO_VOLUME_PRICE_MOVEMENT"
    if volume == 0 or amount == 0:
        return "ISOLATED:PARTIAL_ZERO_VOLUME_AMOUNT"
    return "NORMAL"


def confirm_k4d_units(
    *,
    x_rows: Mapping[Key, Mapping[str, object]],
    service_rows: Mapping[DataUpstream, Mapping[Key, Mapping[str, object]]],
    thresholds: ThresholdSet,
    min_sample: int = K4D_UNIT_MIN_SAMPLE_ROWS,
) -> tuple[bool, tuple[str, ...]]:
    """Confirm volume/amount scale factor 1 is the unique K4-stable choice.

    Candidate scales are 1/100 for volume (share vs lot) and 1/1000 for
    amount (yuan vs thousand-yuan). Factor 1 must hold for every co-held row
    of every service source that holds rows, no other factor may also hold,
    and the co-held sample must be large enough to be meaningful.
    """
    if thresholds.rule_version != K4_THRESHOLD_RULE_VERSION:
        raise K4DSemanticsError("UNIT_CONFIRMATION_REQUIRES_K4_THRESHOLDS")
    reasons: list[str] = []
    co_held: set[Key] = set(x_rows)
    for rows in service_rows.values():
        co_held &= set(rows)
    if len(co_held) < min_sample:
        reasons.append(f"UNIT_SAMPLE_INSUFFICIENT:{len(co_held)}<{min_sample}")
    for field_name, factors in (("volume", _VOLUME_FACTORS), ("amount", _AMOUNT_FACTORS)):
        satisfying: set[int] = set(factors)
        compared_sources = 0
        for rows in service_rows.values():
            held_keys = [key for key in sorted(co_held) if key in rows]
            if not held_keys:
                continue
            compared_sources += 1
            for factor in factors:
                for key in held_keys:
                    raw_value = _numeric(x_rows[key][field_name])
                    if raw_value is None:
                        raise K4DSemanticsError(f"UNIT_FIELD_INVALID:{field_name}")
                    scaled = {**x_rows[key], field_name: raw_value * factor}
                    if not rows_within_threshold(thresholds, scaled, rows[key], (field_name,)):
                        satisfying.discard(factor)
                        break
        if compared_sources == 0:
            reasons.append(f"UNIT_NO_COMPARED_SOURCE:{field_name}")
        elif 1 not in satisfying:
            reasons.append(f"UNIT_SCALE_FACTOR_ONE_NOT_SATISFIED:{field_name}")
        elif len(satisfying) > 1:
            reasons.append(
                f"UNIT_SCALE_AMBIGUOUS:{field_name}:"
                + ",".join(str(factor) for factor in sorted(satisfying))
            )
    for row in x_rows.values():
        volume = _numeric(row.get("volume"))
        amount = _numeric(row.get("amount"))
        if volume is None or not math.isfinite(volume) or volume < 0:
            reasons.append("UNIT_VOLUME_INVALID")
            break
        if abs(volume - round(volume)) > 1e-6:
            reasons.append("UNIT_VOLUME_NOT_INTEGRAL")
            break
        if amount is None or not math.isfinite(amount) or amount < 0:
            reasons.append("UNIT_AMOUNT_INVALID")
            break
    ordered = tuple(dict.fromkeys(reasons))
    return not ordered, ordered


def prove_k4d_unadjusted(
    *,
    vendor_pre_close: Mapping[Key, float],
    service_canonical: Mapping[Key, Mapping[str, object]],
    x_rows: Mapping[Key, Mapping[str, object]],
    thresholds: ThresholdSet,
    min_relative_diff: Decimal = K4D_UNADJUSTED_MIN_RELATIVE_DIFF,
) -> tuple[bool, dict[str, object] | None, tuple[str, ...]]:
    """Prove the unadjusted reading via a vendor-vs-derived pre_close boundary.

    A boundary is a co-held row whose service vendor reference pre_close
    differs materially from the derived unadjusted pre_close while the legacy
    close still agrees with the unadjusted service close inside the K4 rules.
    Ordinary agreeing days never masquerade as such a boundary.
    """
    if thresholds.rule_version != K4_THRESHOLD_RULE_VERSION:
        raise K4DSemanticsError("UNADJUSTED_PROOF_REQUIRES_K4_THRESHOLDS")
    for key in sorted(vendor_pre_close):
        reference = vendor_pre_close[key]
        service_row = service_canonical.get(key)
        x_row = x_rows.get(key)
        if service_row is None or x_row is None:
            continue
        derived = service_row.get("pre_close")
        if not isinstance(derived, (int, float)) or isinstance(derived, bool):
            continue
        if float(derived) == 0:
            continue
        relative = abs(float(reference) - float(derived)) / abs(float(derived))
        if Decimal(str(relative)) < min_relative_diff:
            continue
        if not rows_within_threshold(thresholds, x_row, service_row, ("close",)):
            continue
        return (
            True,
            {
                "code": key[0],
                "trade_date": key[1],
                "vendor_reference_pre_close": float(reference),
                "derived_unadjusted_pre_close": float(derived),
                "relative_difference": str(Decimal(str(relative))),
            },
            (),
        )
    return False, None, ("SEMANTICS_UNADJUSTED_NOT_PROVEN",)


@dataclass(frozen=True, slots=True)
class K4DSemanticsConfirmation:
    """Outcome of the unit/unadjusted verification against A/B/C results."""

    status: K4DSemanticsStatus
    unit_confirmed: bool
    unit_reasons: tuple[str, ...]
    unadjusted_proven: bool
    unadjusted_boundary: Mapping[str, object] | None
    unadjusted_reasons: tuple[str, ...]
    x_row_count: int
    first_row_excluded_count: int
    isolated_row_count: int
    isolated_reasons: tuple[str, ...]
    suspended_placeholder_count: int

    def __post_init__(self) -> None:
        expected = (
            K4DSemanticsStatus.CONFIRMED
            if (self.unit_confirmed and self.unadjusted_proven)
            else K4DSemanticsStatus.HYPOTHESIS
        )
        if self.status is not expected:
            raise K4DSemanticsError("CONFIRMATION_STATUS_INCONSISTENT")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": K4D_SEMANTICS_CONFIRMATION_SCHEMA_VERSION,
            "status": self.status.value,
            "unit_confirmed": self.unit_confirmed,
            "unit_reasons": list(self.unit_reasons),
            "unadjusted_proven": self.unadjusted_proven,
            "unadjusted_boundary": (
                dict(self.unadjusted_boundary) if self.unadjusted_boundary is not None else None
            ),
            "unadjusted_reasons": list(self.unadjusted_reasons),
            "x_row_count": self.x_row_count,
            "first_row_excluded_count": self.first_row_excluded_count,
            "isolated_row_count": self.isolated_row_count,
            "isolated_reasons": list(self.isolated_reasons),
            "suspended_placeholder_count": self.suspended_placeholder_count,
        }


@dataclass(frozen=True, slots=True)
class K4DPilotPlan:
    """The frozen pilot plan payload plus its stable SHA."""

    payload: dict[str, object] = field(repr=False)

    @property
    def plan_sha256(self) -> str:
        return str(self.payload["plan_sha256"])


def select_k4d_pilot_entries(
    entries: tuple[LegacyInventoryEntry, ...],
    symbols: tuple[str, ...] = K4D_PILOT_SYMBOLS,
) -> tuple[LegacyInventoryEntry, ...]:
    """Exact-match the six fixed securities; fuzzy or partial hits fail."""
    selected: list[LegacyInventoryEntry] = []
    for symbol in symbols:
        matches = [
            entry for entry in entries if entry.code_min == symbol and entry.code_max == symbol
        ]
        if not matches:
            raise K4DSemanticsError(f"PILOT_SYMBOL_MISSING:{symbol}")
        if len(matches) > 1:
            raise K4DSemanticsError(f"PILOT_SYMBOL_AMBIGUOUS:{symbol}")
        selected.append(matches[0])
    return tuple(selected)


def build_k4d_pilot_plan(
    *,
    inventory_payload: Mapping[str, object],
    inventory_sha256: str,
    marker: str,
    inventory_relative_path: str,
    enforce_expected_totals: bool = False,
) -> K4DPilotPlan:
    """Build the zero-I/O pilot plan from the frozen inventory payload.

    Recomputes the six-file selection, freezes every file identity (path,
    SHA, size, mtime, rows, coverage), the A/B/C window and call budgets, the
    HYPOTHESIS semantics, and derives ``plan_sha256`` over the canonical
    unsigned payload. ``enforce_expected_totals`` is the real-inventory
    fail-closed check (6 files / 878,893 bytes / 26,200 rows); synthetic
    fixtures only need internal consistency.
    """
    if marker != K4D_PILOT_REAL_MARKER and not marker.endswith("SYNTHETIC-FIXTURE-V1"):
        raise K4DSemanticsError("PILOT_PLAN_MARKER_INVALID")
    inventory = inventory_from_payload(dict(inventory_payload))
    entries = select_k4d_pilot_entries(inventory.entries)
    file_count = len(entries)
    byte_count = sum(entry.size for entry in entries)
    raw_row_count = sum(entry.row_count or 0 for entry in entries)
    if file_count != K4D_EXPECTED_FILE_COUNT:
        raise K4DSemanticsError(f"PILOT_TOTALS_MISMATCH:files:{file_count}")
    if enforce_expected_totals:
        if byte_count != K4D_EXPECTED_BYTE_COUNT:
            raise K4DSemanticsError(f"PILOT_TOTALS_MISMATCH:bytes:{byte_count}")
        if raw_row_count != K4D_EXPECTED_RAW_ROW_COUNT:
            raise K4DSemanticsError(f"PILOT_TOTALS_MISMATCH:rows:{raw_row_count}")
    unsigned: dict[str, object] = {
        "schema_version": K4D_PILOT_PLAN_SCHEMA_VERSION,
        "mode": "PILOT",
        "provider": "XTQUANT_LEGACY",
        "upstream": "XTQUANT",
        "authorization_marker": marker,
        "inventory_file": inventory_relative_path,
        "inventory_sha256": inventory_sha256,
        "source_fingerprint": inventory.source_fingerprint,
        "pilot_symbols": list(K4D_PILOT_SYMBOLS),
        "pilot_files": [
            {
                "code": entry.code_min,
                "relative_path": entry.relative_path,
                "sha256": entry.sha256,
                "size": entry.size,
                "mtime_ns": entry.mtime_ns,
                "row_count": entry.row_count,
                "date_min": entry.date_min,
                "date_max": entry.date_max,
            }
            for entry in entries
        ],
        "totals": {
            "file_count": file_count,
            "byte_count": byte_count,
            "raw_row_count": raw_row_count,
        },
        "expected_first_row_exclusions": K4D_EXPECTED_FIRST_ROW_EXCLUSIONS,
        "expected_canonical_row_count": K4D_EXPECTED_CANONICAL_ROW_COUNT,
        "recheck_window": {
            "start": K4D_RECHECK_WINDOW_START,
            "end": K4D_RECHECK_WINDOW_END,
        },
        "service_invocation_budget": {
            "endpoint_calls": K4D_ENDPOINT_CALL_BUDGET,
            "per_source": {
                "BAOSTOCK": K4D_PER_SOURCE_CALL_BUDGET,
                "TENCENT": K4D_PER_SOURCE_CALL_BUDGET,
                "SINA": K4D_PER_SOURCE_CALL_BUDGET,
            },
            "tencent_http_upper_bound": K4D_TENCENT_HTTP_UPPER_BOUND,
            "sina_http_upper_bound": K4D_SINA_HTTP_UPPER_BOUND,
            "http_total_upper_bound": K4D_HTTP_TOTAL_UPPER_BOUND,
            "baostock_protocol_upper_bound": K4D_BAOSTOCK_PROTOCOL_UPPER_BOUND,
        },
        "timeouts": {
            "baostock_session_seconds": K4D_BAOSTOCK_SESSION_TIMEOUT_SECONDS,
            "akshare_session_seconds": K4D_AKSHARE_SESSION_TIMEOUT_SECONDS,
        },
        "application_retries": 0,
        "fail_fast": True,
        "semantics": k4d_hypothesis_payload(),
        "k4_threshold_rule_version": K4_THRESHOLD_RULE_VERSION,
        "unit_policy_versions": {
            "baostock": K4D_BAOSTOCK_UNIT_POLICY_VERSION,
            "akshare": K4D_AKSHARE_UNIT_POLICY_VERSION,
        },
        "execution_contract": {
            "writes_pilot_staging_only_after_semantics_confirmed": True,
            "publishes_warehouse": False,
        },
        "reads_legacy_root": False,
        "calls_service_network": False,
        "writes_staging": False,
        "accesses_backup_root": False,
        "publishes_warehouse": False,
        "requires_new_authorization_for_execution": True,
    }
    return K4DPilotPlan(
        payload={**unsigned, "plan_sha256": sha256_bytes(canonical_json_bytes(unsigned))}
    )


__all__ = [
    "K4D_AKSHARE_SESSION_TIMEOUT_SECONDS",
    "K4D_AKSHARE_UNIT_POLICY_VERSION",
    "K4D_BAOSTOCK_PROTOCOL_UPPER_BOUND",
    "K4D_BAOSTOCK_SESSION_TIMEOUT_SECONDS",
    "K4D_BAOSTOCK_UNIT_POLICY_VERSION",
    "K4D_ENDPOINT_CALL_BUDGET",
    "K4D_EXPECTED_BYTE_COUNT",
    "K4D_EXPECTED_CANONICAL_ROW_COUNT",
    "K4D_EXPECTED_FILE_COUNT",
    "K4D_EXPECTED_FIRST_ROW_EXCLUSIONS",
    "K4D_EXPECTED_RAW_ROW_COUNT",
    "K4D_HTTP_TOTAL_UPPER_BOUND",
    "K4D_PER_SOURCE_CALL_BUDGET",
    "K4D_PILOT_PLAN_SCHEMA_VERSION",
    "K4D_PILOT_SYMBOLS",
    "K4D_RECHECK_WINDOW_END",
    "K4D_RECHECK_WINDOW_START",
    "K4D_SEMANTICS_CONFIRMATION_SCHEMA_VERSION",
    "K4D_SEMANTICS_SCHEMA_VERSION",
    "K4D_SINA_HTTP_UPPER_BOUND",
    "K4D_TENCENT_HTTP_UPPER_BOUND",
    "K4D_UNIT_MIN_SAMPLE_ROWS",
    "K4DPilotPlan",
    "K4DSemanticsConfirmation",
    "K4DSemanticsError",
    "K4DSemanticsStatus",
    "LegacyK4DSemantics",
    "build_k4d_pilot_plan",
    "classify_k4d_row",
    "confirm_k4d_units",
    "k4d_hypothesis_payload",
    "k4d_semantics_from_payload",
    "parse_k4d_date",
    "prove_k4d_unadjusted",
    "select_k4d_pilot_entries",
]
