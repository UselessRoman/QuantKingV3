"""S1-07K4C-R1 repaired evidence contracts: plan/observation split, one-shot
attempt ledger, and an offline verifier that recomputes every summary value.

The first independent K4C acceptance rejected the v1 probe evidence because:

* ``representative_evidence_sufficient`` was set as soon as 36 calls
  completed, without validating that the planned scenarios (normal,
  ex-rights, suspension, low-liquidity, volume/amount boundary,
  missing/empty upstream, SH/SZ code segments) were actually observed;
* the one-shot authorization was a single environment marker, one attempt
  produced no evidence file at all, and the extreme 50-share/50-yuan
  differences were not reproducible from the retained rows.

This module therefore separates *planned* objectives from *observed*
objectives, computes sufficiency only after cross-source measurement and
objective validation, and defines:

* a file-backed ``K4CAttemptLedger``: one attempt per authorization_id, an
  exclusive attempt header written before the first network call, and an
  append+flush invocation journal that survives process death;
* frozen, executable observation criteria for every required objective;
* ``K4CVerifier``: an offline verifier that recomputes held-row histograms,
  per-field exact/divergent counts, uncapped per-field extrema with their
  three-source rows, objective coverage, threshold coverage, contract facts,
  artifact SHAs and invocation counts from the comparison/raw artifacts — it
  never trusts the summary values embedded in the evidence JSON.

No function in this module performs any network access.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from quantking.data.adapters.sources.contracts import VolumeUnit
from quantking.data.adapters.sources.parameters import is_supported_a_share
from quantking.data.adapters.sources.units import resolve_returned_units
from quantking.data.comparison.thresholds import (
    canonical_decimal,
    decimal_difference,
    decimal_from_number,
    k4_evidence_thresholds,
)
from quantking.data.models import DataProvider, DatasetKind, DataUpstream
from quantking.history.stage1.k4c_probe import K4CProbeInvocation, fixed_k4c_probe_plan

# ---------------------------------------------------------------------------
# Frozen R2 contract constants (frozen 2026-08-16; execution not authorized)
# ---------------------------------------------------------------------------

K4C_R2_MARKER = "K4C-REPRESENTATIVE-R2-2026-08-16-v1"
K4C_R3_MARKER = "K4C-REPRESENTATIVE-R3-2026-08-16-v1"
K4C_R2_DRY_RUN_SCHEMA = "s1-07k4c-r2-probe-dry-run.v1"
K4C_R2_EVIDENCE_SCHEMA = "s1-07k4c-r2-representative-probe-evidence.v1"
K4C_R2_ATTEMPT_HEADER_SCHEMA = "s1-07k4c-r2-attempt-header.v1"
K4C_R2_INVOCATION_RECORD_SCHEMA = "s1-07k4c-r2-invocation-record.v1"
K4C_R2_COMPARISON_SCHEMA = "s1-07k4c-r2-comparison-artifact.v1"
K4C_R2_RAW_SCHEMA = "s1-07k4c-r2-raw-envelope.v1"
K4C_R2_ARTIFACT_MANIFEST_SCHEMA = "s1-07k4c-r2-artifact-manifest.v1"
K4C_R3_DRY_RUN_SCHEMA = "s1-07k4c-r3-probe-dry-run.v1"
K4C_R3_EVIDENCE_SCHEMA = "s1-07k4c-r3-representative-probe-evidence.v1"
K4C_COMPOSITE_EVIDENCE_SCHEMA = "s1-07k4c-composite-evidence.v1"
K4C_R4_COMPOSITE_EVIDENCE_SCHEMA = "s1-07k4c-composite-evidence.v2"
K4C_R4_VERIFICATION_CONTRACT_VERSION = "k4c-r4-evidence-verification.v1"

# Frozen R4 calendar facts (bound to the SHA-frozen SH 2026-07 calendar
# reference and the pre-listing legal-empty classification).
K4C_R3_CALENDAR_REFERENCE_SHA256 = (
    "244e62109be84c8dc0cb778c751c6aa9e2f98c4b50e4dd13073221e81432e10f"
)
K4C_R3_LEGAL_EMPTY_REASON = (
    "frozen calendar proves no expected trading session for the requested scope"
)

# Frozen R4 suspension provenance facts (official SSE announcement binding).
K4C_R4_SUSPENSION_SYMBOL = "688237.SH"
K4C_R4_SUSPENSION_DATE = "2026-07-16"
K4C_R4_SUSPENSION_ANNOUNCEMENT_NUMBER = "2026-031"

# R4 evidence-chain verification error codes.
EVIDENCE_SCHEMA_MISMATCH = "EVIDENCE_SCHEMA_MISMATCH"
EVIDENCE_MANIFEST_SHA_MISMATCH = "EVIDENCE_MANIFEST_SHA_MISMATCH"
EVIDENCE_ARTIFACT_ROOT_MISMATCH = "EVIDENCE_ARTIFACT_ROOT_MISMATCH"
EVIDENCE_PLANNED_OBJECTIVES_MISMATCH = "EVIDENCE_PLANNED_OBJECTIVES_MISMATCH"
EVIDENCE_CALENDAR_PROOF_MISMATCH = "EVIDENCE_CALENDAR_PROOF_MISMATCH"
COMPOSITE_SCHEMA_MISMATCH = "COMPOSITE_SCHEMA_MISMATCH"
COMPOSITE_CONTRACT_VERSION_MISMATCH = "COMPOSITE_CONTRACT_VERSION_MISMATCH"
SUSPENSION_REFERENCE_SHA_MISMATCH = "SUSPENSION_REFERENCE_SHA_MISMATCH"
SUSPENSION_REFERENCE_MISSING = "SUSPENSION_REFERENCE_MISSING"
SUSPENSION_RAW_FACT_MISSING = "SUSPENSION_RAW_FACT_MISSING"
SUSPENSION_RAW_STATUS_MISMATCH = "SUSPENSION_RAW_STATUS_MISMATCH"
SUSPENSION_REFERENCE_INVALID_TYPE = "SUSPENSION_REFERENCE_INVALID_TYPE"
SUSPENSION_REFERENCE_SYMBOL_MISMATCH = "SUSPENSION_REFERENCE_SYMBOL_MISMATCH"
SUSPENSION_REFERENCE_DATE_MISMATCH = "SUSPENSION_REFERENCE_DATE_MISMATCH"
SUSPENSION_REFERENCE_ANNOUNCEMENT_MISMATCH = "SUSPENSION_REFERENCE_ANNOUNCEMENT_MISMATCH"
SUSPENSION_REFERENCE_SOURCE_TYPE_INVALID = "SUSPENSION_REFERENCE_SOURCE_TYPE_INVALID"
SUSPENSION_REFERENCE_URL_INVALID = "SUSPENSION_REFERENCE_URL_INVALID"
SUSPENSION_REFERENCE_SHA_INVALID = "SUSPENSION_REFERENCE_SHA_INVALID"
SUSPENSION_REFERENCE_PROVENANCE_FLAG_INVALID = "SUSPENSION_REFERENCE_PROVENANCE_FLAG_INVALID"

# Exact top-level field sets an evidence payload may carry; anything else is a
# forged field and fails verification.
_R2_EVIDENCE_FIELDS = frozenset(
    {
        "application_retries",
        "artifact_manifest_sha256",
        "artifacts_root",
        "attempt_id",
        "authorization_id",
        "authorization_marker",
        "contract_checks",
        "cross_source",
        "fail_fast",
        "failure",
        "invocation_budget",
        "invocation_count",
        "not_called_invocation_count",
        "observed_objectives",
        "plan_sha256",
        "planned_objectives",
        "representative_evidence_sufficient",
        "schema_version",
        "sufficiency_reasons",
        "threshold_coverage",
        "verifier_reasons",
        "writes_formal_data_root",
    }
)
_R3_EVIDENCE_FIELDS = frozenset(_R2_EVIDENCE_FIELDS | {"calendar_proofs"})


def _normalized_artifact_root(value: object) -> str | None:
    if isinstance(value, Path):
        candidate = value
    elif isinstance(value, str) and value.strip():
        candidate = Path(value)
    else:
        return None
    return os.path.normcase(os.path.normpath(str(candidate.resolve())))


def _expected_r3_calendar_proofs(
    samples: Sequence[K4CR3ObjectiveSample],
) -> dict[str, dict[str, object]]:
    legal_empty = next(
        (sample for sample in samples if sample.objective == "missing_or_empty_upstream"),
        None,
    )
    if legal_empty is None:
        return {}
    return {
        legal_empty.symbol: {
            "no_expected_trading_sessions": True,
            "classification": "NO_TRADING_SESSION",
            "reason": K4C_R3_LEGAL_EMPTY_REASON,
            "calendar_reference_sha256": K4C_R3_CALENDAR_REFERENCE_SHA256,
            "listing_date": legal_empty.listing_date,
        }
    }


# Frozen R2 baseline facts; the composite evidence references these SHAs.
R2_EVIDENCE_SHA256 = "217390b53e03345ac84a97739a88cb28b05299f9d2d2bc9ec036d7af89381835"
R2_ARTIFACT_MANIFEST_SHA256 = "04401321cac84bce94c8e540ae5b6196bf8cc423f1c57b731233eb04b7321d64"
R2_AUTHORIZATION_ID = "K4C-R2-cab803399515"
R2_ATTEMPT_ID = "attempt-3135a90149554d29"

# raw -> comparison closure error codes
RAW_COMPARISON_KEY_MISMATCH = "RAW_COMPARISON_KEY_MISMATCH"
RAW_COMPARISON_VALUE_MISMATCH = "RAW_COMPARISON_VALUE_MISMATCH"
RAW_UNIT_RECONSTRUCTION_MISMATCH = "RAW_UNIT_RECONSTRUCTION_MISMATCH"
RAW_PRE_CLOSE_RECONSTRUCTION_MISMATCH = "RAW_PRE_CLOSE_RECONSTRUCTION_MISMATCH"
JOURNAL_RAW_SHA_MISMATCH = "JOURNAL_RAW_SHA_MISMATCH"
RAW_PLAN_BINDING_MISMATCH = "RAW_PLAN_BINDING_MISMATCH"
UNREFERENCED_RAW_ARTIFACT = "UNREFERENCED_RAW_ARTIFACT"

# R3 sample-selection blockers (stable codes)
BLOCKED_SAMPLE_SELECTION = "BLOCKED_SAMPLE_SELECTION"
BLOCKED_MISSING_EMPTY_SAMPLE = "BLOCKED_MISSING_EMPTY_SAMPLE"

K4C_R2_INVOCATION_BUDGET = 36
K4C_R2_HARD_TIMEOUTS = {
    "baostock_session_seconds": 20.0,
    "akshare_session_seconds": 60.0,
}
K4C_R2_APPLICATION_RETRIES = 0
K4C_R2_FAIL_FAST = True

# Frozen, executable objective criteria. Every bound below is part of the
# frozen R2 plan payload and therefore part of ``plan_sha256``.
EX_RIGHTS_SYMBOLS = ("000858.SZ", "600519.SH")
EX_RIGHTS_VENDOR_PRECLOSE_RELATIVE_BOUND = Decimal("0.005")
LOW_LIQUIDITY_AMOUNT_BOUND_YUAN = Decimal("250000000")
VOLUME_AMOUNT_BOUNDARY_AMOUNT_YUAN = Decimal("1000000000")

# Stable reason codes for objectives that are planned but not observed.
NORMAL_SESSION_NOT_OBSERVED = "NORMAL_SESSION_NOT_OBSERVED"
EX_RIGHTS_GAP_NOT_OBSERVED = "EX_RIGHTS_GAP_NOT_OBSERVED"
SUSPENSION_NOT_OBSERVED = "SUSPENSION_NOT_OBSERVED"
LOW_LIQUIDITY_NOT_OBSERVED = "LOW_LIQUIDITY_NOT_OBSERVED"
VOLUME_AMOUNT_BOUNDARY_NOT_OBSERVED = "VOLUME_AMOUNT_BOUNDARY_NOT_OBSERVED"
NO_MISSING_OR_EMPTY_OBSERVED = "NO_MISSING_OR_EMPTY_OBSERVED"
SHENZHEN_SEGMENT_NOT_OBSERVED = "SHENZHEN_SEGMENT_NOT_OBSERVED"
SHANGHAI_SEGMENT_NOT_OBSERVED = "SHANGHAI_SEGMENT_NOT_OBSERVED"

_COMPARISON_FILENAME = "artifacts/comparison-rows.json"
_ARTIFACT_MANIFEST_FILENAME = "artifacts/artifact-manifest.json"
_ATTEMPT_HEADER_FILENAME = "attempt-header.json"
_INVOCATION_JOURNAL_FILENAME = "invocations.jsonl"
_RAW_DIRECTORY = "artifacts/raw"

_FIELDS = ("open", "high", "low", "close", "pre_close", "volume", "amount")
_DIFFERENCE_FIELDS = ("volume", "amount")
_ROLES = ("A", "B", "C")

_ROLE_BY_UPSTREAM = {"BAOSTOCK": "A", "TENCENT": "B", "SINA": "C"}
_FROZEN_LIBRARY_VERSIONS = {"BAOSTOCK": "0.9.3", "TENCENT": "1.18.83", "SINA": "1.18.83"}
_FROZEN_CONTRACT_VERSIONS = {
    "BAOSTOCK": "baostock-daily-bar.v1",
    "TENCENT": "akshare-daily-bar.v2",
    "SINA": "akshare-daily-bar.v2",
}


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_number(value: object, *, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError(f"{field} must be numeric")
    return canonical_decimal(decimal_from_number(value, field=field))


# ---------------------------------------------------------------------------
# Objective definitions and the plan payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class K4CObjectiveDefinition:
    """One frozen, executable objective criterion."""

    name: str
    criterion: str
    symbols: tuple[str, ...] = ()
    bounds: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("objective name must be non-empty text")
        if not isinstance(self.criterion, str) or not self.criterion.strip():
            raise ValueError("criterion must be non-empty text")
        if not isinstance(self.symbols, tuple) or any(
            not isinstance(item, str) for item in self.symbols
        ):
            raise TypeError("symbols must be a tuple of strings")
        if self.bounds is not None and (
            not isinstance(self.bounds, Mapping)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in self.bounds.items()
            )
        ):
            raise TypeError("bounds must map strings to canonical decimal strings")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "criterion": self.criterion,
            "symbols": list(self.symbols),
            "bounds": None if self.bounds is None else dict(sorted(self.bounds.items())),
        }


K4C_R2_OBJECTIVES: tuple[K4CObjectiveDefinition, ...] = (
    K4CObjectiveDefinition(
        "normal_session",
        criterion="positive_volume_and_amount",
    ),
    K4CObjectiveDefinition(
        "ex_rights_session",
        criterion="vendor_preclose_gap",
        symbols=EX_RIGHTS_SYMBOLS,
        bounds={"relative": canonical_decimal(EX_RIGHTS_VENDOR_PRECLOSE_RELATIVE_BOUND)},
    ),
    K4CObjectiveDefinition(
        "suspension_session",
        criterion="cross_sectional_missing_day",
    ),
    K4CObjectiveDefinition(
        "low_liquidity_session",
        criterion="amount_below_bound",
        bounds={"amount_yuan": canonical_decimal(LOW_LIQUIDITY_AMOUNT_BOUND_YUAN)},
    ),
    K4CObjectiveDefinition(
        "volume_amount_boundary",
        criterion="amount_above_bound",
        bounds={"amount_yuan": canonical_decimal(VOLUME_AMOUNT_BOUNDARY_AMOUNT_YUAN)},
    ),
    K4CObjectiveDefinition(
        "missing_or_empty_upstream",
        criterion="partial_hold_or_empty_invocation",
    ),
    K4CObjectiveDefinition(
        "shenzhen_code_segments",
        criterion="held_segment",
        bounds={"suffix": ".SZ"},
    ),
    K4CObjectiveDefinition(
        "shanghai_code_segments",
        criterion="held_segment",
        bounds={"suffix": ".SH"},
    ),
)


def k4c_r2_objective_names() -> tuple[str, ...]:
    """Return the frozen required objective names in plan order."""
    return tuple(item.name for item in K4C_R2_OBJECTIVES)


def frozen_k4c_r2_plan_payload() -> dict[str, object]:
    """Return the canonical plan sub-object (excludes ``plan_sha256`` itself).

    The invocation geometry is identical to the retired v1 probe plan (same
    six securities, same 36 chunks), but the R2 marker, the executable
    objective criteria and the one-shot attempt rules make the R2 plan a new
    frozen contract.
    """
    invocations = fixed_k4c_probe_plan()
    return {
        "authorization_marker": K4C_R2_MARKER,
        "dataset": "daily_bar_unadjusted",
        "schema": "daily_bar_unadjusted.v1",
        "symbols": sorted({item.symbol for item in invocations}),
        "target_window": {
            "start": min(item.target_start for item in invocations).isoformat(),
            "end": max(item.target_end for item in invocations).isoformat(),
        },
        "lookback_days": 15,
        "chunk_policy": {"max_days_per_chunk": 31, "max_symbols_per_chunk": 1},
        "objectives": [item.to_dict() for item in K4C_R2_OBJECTIVES],
        "invocations": [item.to_dict() for item in invocations],
        "invocation_budget": K4C_R2_INVOCATION_BUDGET,
        "network_budget": {
            "max_endpoint_invocations": K4C_R2_INVOCATION_BUDGET,
            "a_baostock_endpoint_invocations": 12,
            "b_tencent_endpoint_invocations": 12,
            "max_c_sina_endpoint_invocations": 12,
            "estimated_max_baostock_protocol_messages": 36,
            "estimated_max_akshare_http_requests": 60,
            "application_retries": K4C_R2_APPLICATION_RETRIES,
            "fail_fast": K4C_R2_FAIL_FAST,
        },
        "hard_timeouts_seconds": K4C_R2_HARD_TIMEOUTS,
        "unit_policies": {
            "baostock": "baostock-0.9.3-returned-units.v1",
            "akshare": "akshare-1.18.83-returned-units.v1",
        },
    }


def k4c_r2_plan_sha256() -> str:
    """Return the frozen plan hash bound to the R2 marker."""
    return _sha256_text(_canonical_json(frozen_k4c_r2_plan_payload()))


def k4c_r2_dry_run_payload() -> dict[str, object]:
    """Return the zero-network R2 dry-run; no provider call is possible here."""
    return {
        "schema_version": K4C_R2_DRY_RUN_SCHEMA,
        "authorization_marker": K4C_R2_MARKER,
        "plan_sha256": k4c_r2_plan_sha256(),
        "plan": frozen_k4c_r2_plan_payload(),
        "authorization": {
            "authorization_id_format": "K4C-R2-<12-hex>",
            "one_attempt_per_authorization_id": True,
            "replay_rejection_before_first_call": True,
            "evidence_output_root": "docs/stage1/evidence/k4c-r2-attempts",
            "formal_data_root_writes": False,
        },
        "artifacts": {
            "attempt_header": _ATTEMPT_HEADER_FILENAME,
            "invocation_journal": _INVOCATION_JOURNAL_FILENAME,
            "comparison_rows": _COMPARISON_FILENAME,
            "raw_envelopes": f"{_RAW_DIRECTORY}/<sequence>.json",
            "artifact_manifest": _ARTIFACT_MANIFEST_FILENAME,
        },
        "application_retries": K4C_R2_APPLICATION_RETRIES,
        "fail_fast": K4C_R2_FAIL_FAST,
        "requires_real_network_authorization": True,
    }


# ---------------------------------------------------------------------------
# Objective observation (pure, offline, on measured rows + raw facts)
# ---------------------------------------------------------------------------


def _all_three_hold(
    rows_by_role: Mapping[str, Mapping[tuple[str, str], Mapping[str, object]]],
    key: tuple[str, str],
) -> bool:
    return all(key in rows_by_role.get(role, {}) for role in _ROLES)


def _row_fact(
    rows_by_role: Mapping[str, Mapping[tuple[str, str], Mapping[str, object]]],
    key: tuple[str, str],
    fields: tuple[str, ...],
) -> dict[str, dict[str, str]]:
    return {
        field: {
            role: _canonical_number(rows_by_role[role][key][field], field=field)
            for role in _ROLES
            if key in rows_by_role.get(role, {}) and field in rows_by_role[role][key]
        }
        for field in fields
    }


def observe_k4c_r2_objectives(
    rows_by_role: Mapping[str, Mapping[tuple[str, str], Mapping[str, object]]],
    *,
    vendor_preclose: Mapping[str, Mapping[tuple[str, str], str]] | None = None,
    empty_invocations: Sequence[Mapping[str, object]] = (),
) -> tuple[dict[str, object], ...]:
    """Evaluate every frozen objective against measured rows and raw facts.

    A label such as ``corporate_action_history`` never counts as an
    observation: each objective has an executable criterion, and an unmet
    criterion yields ``observed=False`` plus a stable reason code.
    """
    if not isinstance(rows_by_role, Mapping):
        raise TypeError("rows_by_role must be a mapping")
    vendor = {} if vendor_preclose is None else dict(vendor_preclose)
    return tuple(
        _observe_one(definition, rows_by_role, vendor, tuple(empty_invocations))
        for definition in K4C_R2_OBJECTIVES
    )


def _observed_record(
    definition: K4CObjectiveDefinition,
    *,
    symbol: str | None,
    trade_date: str | None,
    roles: tuple[str, ...],
    evidence_type: str,
    fact: Mapping[str, object] | None,
) -> dict[str, object]:
    return {
        "objective": definition.name,
        "observed": True,
        "symbol": symbol,
        "trade_date": trade_date,
        "roles": list(roles),
        "evidence_type": evidence_type,
        "fact": {} if fact is None else dict(fact),
        "reason_code": None,
    }


def _unobserved_record(definition: K4CObjectiveDefinition, reason: str) -> dict[str, object]:
    return {
        "objective": definition.name,
        "observed": False,
        "symbol": None,
        "trade_date": None,
        "roles": [],
        "evidence_type": None,
        "fact": {},
        "reason_code": reason,
    }


def _sorted_keys(
    rows_by_role: Mapping[str, Mapping[tuple[str, str], Mapping[str, object]]],
) -> list[tuple[str, str]]:
    return sorted(
        {key for rows in rows_by_role.values() for key in rows},
        key=lambda key: (key[0], key[1]),
    )


def _observe_one(
    definition: K4CObjectiveDefinition,
    rows_by_role: Mapping[str, Mapping[tuple[str, str], Mapping[str, object]]],
    vendor_preclose: Mapping[str, Mapping[tuple[str, str], str]],
    empty_invocations: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    criterion = definition.criterion
    keys = _sorted_keys(rows_by_role)
    if criterion == "positive_volume_and_amount":
        for key in keys:
            if not _all_three_hold(rows_by_role, key):
                continue
            fact = _row_fact(rows_by_role, key, ("volume", "amount"))
            if all(
                Decimal(fact[field][role]) > 0 for field in ("volume", "amount") for role in _ROLES
            ):
                return _observed_record(
                    definition,
                    symbol=key[0],
                    trade_date=key[1],
                    roles=_ROLES,
                    evidence_type="held_row_fact",
                    fact=fact,
                )
        return _unobserved_record(definition, NORMAL_SESSION_NOT_OBSERVED)
    if criterion == "vendor_preclose_gap":
        bounds = cast(Mapping[str, str], definition.bounds)
        bound = Decimal(str(bounds["relative"]))
        for symbol in definition.symbols or ():
            for key in (item for item in keys if item[0] == symbol):
                row = rows_by_role.get("A", {}).get(key)
                reference = vendor_preclose.get("A", {}).get(key)
                if row is None or reference is None:
                    continue
                derived = _canonical_number(row["pre_close"], field="pre_close")
                derived_decimal = Decimal(derived)
                reference_decimal = Decimal(str(reference))
                if derived_decimal <= 0 or reference_decimal <= 0:
                    continue
                gap = abs(reference_decimal - derived_decimal) / derived_decimal
                if gap > bound:
                    return _observed_record(
                        definition,
                        symbol=key[0],
                        trade_date=key[1],
                        roles=("A",),
                        evidence_type="vendor_preclose_gap",
                        fact={
                            "derived_pre_close": derived,
                            "vendor_preclose": str(reference),
                            "relative_gap": canonical_decimal(gap),
                            "bound": canonical_decimal(bound),
                        },
                    )
        return _unobserved_record(definition, EX_RIGHTS_GAP_NOT_OBSERVED)
    if criterion == "cross_sectional_missing_day":
        dates_by_symbol: dict[str, set[str]] = {}
        for symbol, trade_date in keys:
            dates_by_symbol.setdefault(symbol, set()).add(trade_date)
        for symbol in sorted(dates_by_symbol):
            dates = dates_by_symbol[symbol]
            other_dates = {
                trade_date
                for other, values in dates_by_symbol.items()
                if other != symbol
                for trade_date in values
            }
            for trade_date in sorted(other_dates - dates):
                counter = next(
                    (
                        other
                        for other in sorted(dates_by_symbol)
                        if other != symbol and trade_date in dates_by_symbol[other]
                    ),
                    None,
                )
                return _observed_record(
                    definition,
                    symbol=symbol,
                    trade_date=trade_date,
                    roles=(),
                    evidence_type="cross_sectional_missing_day",
                    fact={
                        "missing_symbol": symbol,
                        "missing_roles": list(_ROLES),
                        "counter_example_symbol": counter,
                    },
                )
        return _unobserved_record(definition, SUSPENSION_NOT_OBSERVED)
    if criterion == "amount_below_bound":
        bounds = cast(Mapping[str, str], definition.bounds)
        bound = Decimal(str(bounds["amount_yuan"]))
        for key in keys:
            if not _all_three_hold(rows_by_role, key):
                continue
            fact = _row_fact(rows_by_role, key, ("amount",))
            if all(Decimal(fact["amount"][role]) <= bound for role in _ROLES):
                return _observed_record(
                    definition,
                    symbol=key[0],
                    trade_date=key[1],
                    roles=_ROLES,
                    evidence_type="held_row_fact",
                    fact={**fact, "bound_yuan": canonical_decimal(bound)},
                )
        return _unobserved_record(definition, LOW_LIQUIDITY_NOT_OBSERVED)
    if criterion == "amount_above_bound":
        bounds = cast(Mapping[str, str], definition.bounds)
        bound = Decimal(str(bounds["amount_yuan"]))
        for key in keys:
            if not _all_three_hold(rows_by_role, key):
                continue
            fact = _row_fact(rows_by_role, key, ("amount",))
            if all(Decimal(fact["amount"][role]) >= bound for role in _ROLES):
                return _observed_record(
                    definition,
                    symbol=key[0],
                    trade_date=key[1],
                    roles=_ROLES,
                    evidence_type="held_row_fact",
                    fact={**fact, "bound_yuan": canonical_decimal(bound)},
                )
        return _unobserved_record(definition, VOLUME_AMOUNT_BOUNDARY_NOT_OBSERVED)
    if criterion == "partial_hold_or_empty_invocation":
        for key in keys:
            held = tuple(role for role in _ROLES if key in rows_by_role.get(role, {}))
            if held and set(held) != set(_ROLES):
                return _observed_record(
                    definition,
                    symbol=key[0],
                    trade_date=key[1],
                    roles=held,
                    evidence_type="partial_hold",
                    fact={
                        "held_roles": list(held),
                        "missing_roles": sorted(set(_ROLES) - set(held)),
                    },
                )
        for invocation in empty_invocations:
            if cast(int, invocation["upstream_row_count"]) == 0:
                return _observed_record(
                    definition,
                    symbol=str(invocation.get("symbol")),
                    trade_date=str(invocation.get("target_start")),
                    roles=(str(invocation.get("role")),),
                    evidence_type="empty_upstream_invocation",
                    fact={
                        "sequence": cast(int, invocation["sequence"]),
                        "role": str(invocation["role"]),
                        "symbol": str(invocation["symbol"]),
                        "upstream_row_count": 0,
                    },
                )
        return _unobserved_record(definition, NO_MISSING_OR_EMPTY_OBSERVED)
    if criterion == "held_segment":
        suffix = str(cast(Mapping[str, str], definition.bounds)["suffix"])
        for key in keys:
            if not key[0].endswith(suffix) or not _all_three_hold(rows_by_role, key):
                continue
            return _observed_record(
                definition,
                symbol=key[0],
                trade_date=key[1],
                roles=_ROLES,
                evidence_type="held_row_fact",
                fact=_row_fact(rows_by_role, key, ("close", "volume")),
            )
        reason = SHENZHEN_SEGMENT_NOT_OBSERVED if suffix == ".SZ" else SHANGHAI_SEGMENT_NOT_OBSERVED
        return _unobserved_record(definition, reason)
    raise ValueError("criterion is unsupported")


# ---------------------------------------------------------------------------
# Representative sufficiency (computed after measurement and observation)
# ---------------------------------------------------------------------------


def validate_k4c_r2_objective_records(
    objectives: Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    """Reject objective records that reference symbols/dates outside the plan.

    An observed objective must point at a symbol from the frozen sample and a
    trade date inside the frozen target window; anything else is rejected.
    """
    plan = frozen_k4c_r2_plan_payload()
    symbols = frozenset(cast(list[str], plan["symbols"]))
    window = cast(dict[str, str], plan["target_window"])
    start = window["start"]
    end = window["end"]
    reasons: list[str] = []
    for record in objectives:
        if record.get("observed") is not True:
            continue
        objective = str(record.get("objective"))
        symbol = record.get("symbol")
        trade_date = record.get("trade_date")
        if not isinstance(symbol, str) or symbol not in symbols:
            reasons.append(f"OBJECTIVE_OUT_OF_PLAN:{objective}")
        if isinstance(trade_date, str) and not (start <= trade_date <= end):
            reasons.append(f"OBJECTIVE_OUT_OF_PLAN:{objective}")
    return tuple(sorted(set(reasons)))


def compute_k4c_r2_sufficiency(
    *,
    invocation_count: int,
    budget: int,
    failure: object,
    objectives: Sequence[Mapping[str, object]],
    contract_checks: Mapping[str, bool],
    comparison_verified: bool,
    unrecorded_attempts: int,
    unauthorized_calls: int = 0,
) -> tuple[bool, tuple[str, ...]]:
    """Return ``(sufficient, reasons)``; never call this before measurement.

    ``sufficient`` is True only when the whole fixed plan executed with no
    failure or not-called invocations, every required objective was actually
    observed, every contract check passed, the comparison evidence passed the
    offline verifier, no unrecorded attempt exists and no call outside the
    frozen plan was made.
    """
    reasons: list[str] = []
    if failure is not None:
        reasons.append("FAILURE_PRESENT")
    if invocation_count != budget:
        reasons.append("INVOCATIONS_INCOMPLETE")
    if unauthorized_calls:
        reasons.append("UNAUTHORIZED_CALL")
    for objective in objectives:
        name = str(objective["objective"])
        definition = next((item for item in K4C_R2_OBJECTIVES if item.name == name), None)
        if definition is not None and objective.get("observed") is not True:
            reasons.append(f"OBJECTIVE_UNOBSERVED:{name}")
    for name, ok in sorted(contract_checks.items()):
        if not ok:
            reasons.append(f"CONTRACT_CHECK_FAILED:{name}")
    if not comparison_verified:
        reasons.append("COMPARISON_NOT_VERIFIED")
    if unrecorded_attempts:
        reasons.append("UNRECORDED_ATTEMPT")
    return (not reasons, tuple(reasons))


# ---------------------------------------------------------------------------
# One-shot attempt ledger
# ---------------------------------------------------------------------------

AUTHORIZATION_ALREADY_CONSUMED = "AUTHORIZATION_ALREADY_CONSUMED"
ATTEMPT_NOT_OPEN = "ATTEMPT_NOT_OPEN"
MARKER_NOT_FROZEN = "MARKER_NOT_FROZEN"
INVALID_ATTEMPT_INPUT = "INVALID_ATTEMPT_INPUT"
INVALID_ATTEMPT_HEADER = "INVALID_ATTEMPT_HEADER"

_ACTIVE_PROBE_MARKERS = frozenset({K4C_R3_MARKER})


def active_probe_markers() -> frozenset[str]:
    """Return the markers that may open a NEW attempt.

    The R2 marker was consumed by the real 2026-08-16 run and is retired:
    existing R2 attempts remain verifiable, but no new attempt may reuse it.
    """
    return frozenset(_ACTIVE_PROBE_MARKERS)


class K4CLedgerError(Exception):
    """Stable ledger failure; never carries third-party text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class K4CAttemptHeader:
    """Immutable header created exclusively before the first network call."""

    authorization_id: str
    attempt_id: str
    marker: str
    plan_sha256: str
    invocation_budget: int
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": K4C_R2_ATTEMPT_HEADER_SCHEMA,
            "authorization_id": self.authorization_id,
            "attempt_id": self.attempt_id,
            "authorization_marker": self.marker,
            "plan_sha256": self.plan_sha256,
            "invocation_budget": self.invocation_budget,
            "created_at": self.created_at,
        }


def _validate_authorization_id(authorization_id: str) -> None:
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
    if (
        not isinstance(authorization_id, str)
        or len(authorization_id) < 8
        or len(authorization_id) > 64
        or any(character not in allowed for character in authorization_id)
    ):
        raise K4CLedgerError(INVALID_ATTEMPT_INPUT)


def _write_json(path: Path, payload: object) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_canonical_json(payload) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class K4CAttemptLedger:
    """One-shot, file-backed attempt ledger for the real K4C-R2 probe.

    * ``begin`` creates the exclusive attempt directory (``mkdir`` fails when
      the authorization_id was already consumed) and writes the attempt header
      before any network call may happen;
    * ``record`` appends one canonical JSON line per invocation and flushes it
      immediately, so an interrupted process still leaves an exact count of
      invocations actually made;
    * ``counts`` derives invoked/not-called counts from the journal without
      trusting any in-memory state.
    """

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise TypeError("root must be a Path")
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def attempt_directory(self, authorization_id: str) -> Path:
        _validate_authorization_id(authorization_id)
        return self._root / authorization_id

    def begin(
        self,
        *,
        authorization_id: str,
        marker: str,
        plan_sha256: str,
        invocation_budget: int,
        created_at: str,
    ) -> K4CAttemptHeader:
        """Open the single attempt for ``authorization_id`` (exclusive).

        Only active probe markers may open new attempts; the R2 marker was
        consumed and is retired. The plan hash binding is enforced by the
        verifier against the frozen plan; ``begin`` validates the identifier
        shape and rejects the all-zero sentinel.
        """
        _validate_authorization_id(authorization_id)
        if marker not in active_probe_markers():
            raise K4CLedgerError(MARKER_NOT_FROZEN)
        if (
            not isinstance(plan_sha256, str)
            or len(plan_sha256) != 64
            or any(character not in "0123456789abcdef" for character in plan_sha256)
            or plan_sha256 == "0" * 64
        ):
            raise K4CLedgerError(INVALID_ATTEMPT_INPUT)
        expected_budget = (
            K4C_R3_INVOCATION_BUDGET if marker == K4C_R3_MARKER else K4C_R2_INVOCATION_BUDGET
        )
        if isinstance(invocation_budget, bool) or not isinstance(invocation_budget, int):
            raise K4CLedgerError(INVALID_ATTEMPT_INPUT)
        if invocation_budget != expected_budget:
            raise K4CLedgerError(INVALID_ATTEMPT_INPUT)
        if not isinstance(created_at, str) or not created_at:
            raise K4CLedgerError(INVALID_ATTEMPT_INPUT)
        directory = self.attempt_directory(authorization_id)
        try:
            directory.mkdir(parents=True)
        except FileExistsError as error:
            raise K4CLedgerError(AUTHORIZATION_ALREADY_CONSUMED) from error
        except OSError as error:
            raise K4CLedgerError(INVALID_ATTEMPT_INPUT) from error
        header = K4CAttemptHeader(
            authorization_id=authorization_id,
            attempt_id=f"attempt-{uuid.uuid4().hex[:16]}",
            marker=marker,
            plan_sha256=plan_sha256,
            invocation_budget=invocation_budget,
            created_at=created_at,
        )
        _write_json(directory / _ATTEMPT_HEADER_FILENAME, header.to_dict())
        (directory / "artifacts" / "raw").mkdir(parents=True)
        return header

    def read_header(self, authorization_id: str) -> K4CAttemptHeader | None:
        _validate_authorization_id(authorization_id)
        path = self.attempt_directory(authorization_id) / _ATTEMPT_HEADER_FILENAME
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            raise K4CLedgerError(INVALID_ATTEMPT_HEADER) from error
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != K4C_R2_ATTEMPT_HEADER_SCHEMA
        ):
            raise K4CLedgerError(INVALID_ATTEMPT_HEADER)
        try:
            return K4CAttemptHeader(
                authorization_id=str(payload["authorization_id"]),
                attempt_id=str(payload["attempt_id"]),
                marker=str(payload["authorization_marker"]),
                plan_sha256=str(payload["plan_sha256"]),
                invocation_budget=int(payload["invocation_budget"]),
                created_at=str(payload["created_at"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise K4CLedgerError(INVALID_ATTEMPT_HEADER) from error

    def record(
        self,
        authorization_id: str,
        *,
        sequence: int,
        outcome: Mapping[str, object],
    ) -> None:
        """Append one invocation result and flush it before returning."""
        header = self.read_header(authorization_id)
        if header is None:
            raise K4CLedgerError(ATTEMPT_NOT_OPEN)
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or not (1 <= sequence <= header.invocation_budget)
        ):
            raise K4CLedgerError(INVALID_ATTEMPT_INPUT)
        payload = {
            "schema_version": K4C_R2_INVOCATION_RECORD_SCHEMA,
            "authorization_id": authorization_id,
            "sequence": sequence,
            "outcome": dict(outcome),
        }
        path = self.attempt_directory(authorization_id) / _INVOCATION_JOURNAL_FILENAME
        with open(path, "ab") as handle:
            handle.write(_canonical_json(payload).encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())

    def counts(self, authorization_id: str) -> tuple[int, int]:
        """Return ``(invoked, not_called)`` from the journal on disk."""
        header = self.read_header(authorization_id)
        if header is None:
            raise K4CLedgerError(ATTEMPT_NOT_OPEN)
        path = self.attempt_directory(authorization_id) / _INVOCATION_JOURNAL_FILENAME
        invoked = 0
        if path.is_file():
            with open(path, "rb") as handle:
                for line in handle:
                    try:
                        payload = json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue  # truncated final line after a crash
                    if (
                        isinstance(payload, dict)
                        and payload.get("schema_version") == K4C_R2_INVOCATION_RECORD_SCHEMA
                    ):
                        invoked += 1
        return invoked, header.invocation_budget - invoked


# ---------------------------------------------------------------------------
# Comparison artifacts and uncapped statistics
# ---------------------------------------------------------------------------


def comparison_rows_payload(
    rows_by_role: Mapping[str, Mapping[tuple[str, str], Mapping[str, object]]],
) -> dict[str, object]:
    """Serialize every compared row; all statistics become recomputable."""
    serialized: dict[str, list[list[str]]] = {}
    for role in _ROLES:
        rows = rows_by_role.get(role, {})
        serialized[role] = [
            [
                symbol,
                trade_date,
                *[_canonical_number(row[field], field=field) for field in _FIELDS],
            ]
            for (symbol, trade_date), row in sorted(
                rows.items(), key=lambda item: (item[0][0], item[0][1])
            )
        ]
    return {
        "schema_version": K4C_R2_COMPARISON_SCHEMA,
        "fields": list(_FIELDS),
        "rows_by_role": serialized,
    }


def rows_from_comparison_payload(
    payload: Mapping[str, object],
) -> dict[str, dict[tuple[str, str], Mapping[str, object]]]:
    """Parse the comparison artifact back into per-role held rows."""
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != K4C_R2_COMPARISON_SCHEMA
    ):
        raise ValueError("comparison artifact schema is invalid")
    fields = tuple(str(item) for item in cast(list[str], payload["fields"]))
    if fields != _FIELDS:
        raise ValueError("comparison artifact fields are invalid")
    raw = payload["rows_by_role"]
    if not isinstance(raw, Mapping):
        raise ValueError("comparison artifact rows are invalid")
    result: dict[str, dict[tuple[str, str], Mapping[str, object]]] = {}
    for role in _ROLES:
        rows: dict[tuple[str, str], Mapping[str, object]] = {}
        for entry in cast(list[object], raw.get(role, [])):
            if not isinstance(entry, list) or len(entry) != 2 + len(_FIELDS):
                raise ValueError("comparison artifact row is invalid")
            symbol = str(entry[0])
            trade_date = str(entry[1])
            rows[(symbol, trade_date)] = {
                field: Decimal(str(entry[2 + index])) for index, field in enumerate(_FIELDS)
            }
        result[role] = rows
    return result


def recompute_comparison_statistics(
    rows_by_role: Mapping[str, Mapping[tuple[str, str], Mapping[str, object]]],
) -> dict[str, object]:
    """Recompute held histogram and uncapped per-field statistics.

    The per-field maximum absolute/relative difference always keeps its full
    ``(symbol, trade_date)`` row with the original normalized values from all
    roles that hold the row — no top-N divergence cap may discard an extreme.
    """
    keys = _sorted_keys(rows_by_role)
    held_histogram: dict[str, int] = {}
    for key in keys:
        count = sum(1 for role in _ROLES if key in rows_by_role.get(role, {}))
        held_histogram[str(count)] = held_histogram.get(str(count), 0) + 1
    field_evidence: dict[str, dict[str, object]] = {}
    for field in _FIELDS:
        compared = 0
        exact = 0
        max_absolute: dict[str, object] | None = None
        max_relative: dict[str, object] | None = None
        for symbol, trade_date in keys:
            held: dict[str, str] = {}
            for role in _ROLES:
                row = rows_by_role.get(role, {}).get((symbol, trade_date))
                if row is not None and field in row:
                    held[role] = _canonical_number(row[field], field=field)
            if len(held) < 2:
                continue
            compared += 1
            values = tuple(held.values())
            if len(set(values)) == 1:
                exact += 1
                continue
            row_absolute: dict[str, object] | None = None
            row_relative: dict[str, object] | None = None
            for first in range(len(values)):
                for second in range(first + 1, len(values)):
                    absolute, relative = decimal_difference(
                        Decimal(values[first]), Decimal(values[second])
                    )
                    candidate: dict[str, object] = {
                        "symbol": symbol,
                        "trade_date": trade_date,
                        "values": held,
                        "absolute_difference": canonical_decimal(absolute),
                    }
                    if row_absolute is None or Decimal(
                        cast(str, candidate["absolute_difference"])
                    ) > Decimal(cast(str, row_absolute["absolute_difference"])):
                        row_absolute = candidate
                    if row_relative is None or Decimal(relative) > Decimal(
                        cast(str, row_relative["relative_difference"])
                    ):
                        row_relative = {
                            **candidate,
                            "relative_difference": canonical_decimal(relative),
                        }
            assert row_absolute is not None and row_relative is not None
            if max_absolute is None or Decimal(
                cast(str, row_absolute["absolute_difference"])
            ) > Decimal(cast(str, max_absolute["absolute_difference"])):
                max_absolute = row_absolute
            if max_relative is None or Decimal(
                cast(str, row_relative["relative_difference"])
            ) > Decimal(cast(str, max_relative["relative_difference"])):
                max_relative = row_relative
        field_evidence[field] = {
            "compared_rows": compared,
            "exact_rows": exact,
            "divergent_rows": compared - exact,
        }
        if field in _DIFFERENCE_FIELDS:
            field_evidence[field]["max_absolute"] = max_absolute
            field_evidence[field]["max_relative"] = max_relative
    return {
        "schema_version": K4C_R2_COMPARISON_SCHEMA,
        "held_rows_total": len(keys),
        "rows_held_by_role_count": held_histogram,
        "fields": field_evidence,
    }


def threshold_coverage(
    rows_by_role: Mapping[str, Mapping[tuple[str, str], Mapping[str, object]]],
) -> dict[str, object]:
    """Report whether the K4 thresholds cover every observed pair.

    The rule is ACCEPTED (re-frozen 2026-08-16, see
    ``thresholds.k4_threshold_acceptance()``); the coverage payload field
    ``thresholds_pending_reacceptance`` is pinned to the byte-frozen
    evidence payloads that were generated while the rule was pending, so
    frozen evidence keeps verifying byte-identically.
    """
    thresholds = k4_evidence_thresholds()
    specs = thresholds.for_dataset(DatasetKind.DAILY_BAR_UNADJUSTED)
    covered = True
    uncovered: list[dict[str, object]] = []
    for field in _FIELDS:
        spec = specs[field]
        for symbol, trade_date in _sorted_keys(rows_by_role):
            held = [
                cast(int | float | Decimal, rows_by_role[role][(symbol, trade_date)][field])
                for role in _ROLES
                if (symbol, trade_date) in rows_by_role.get(role, {})
                and field in rows_by_role[role][(symbol, trade_date)]
            ]
            for first in range(len(held)):
                for second in range(first + 1, len(held)):
                    if spec.exceeds(held[first], held[second]):
                        covered = False
                        if len(uncovered) < 20:
                            uncovered.append(
                                {
                                    "symbol": symbol,
                                    "trade_date": trade_date,
                                    "field": field,
                                    "values": {
                                        "first": _canonical_number(held[first], field=field),
                                        "second": _canonical_number(held[second], field=field),
                                    },
                                }
                            )
    return {
        "threshold_rule_version": thresholds.rule_version,
        # Byte-frozen evidence payloads (R2/R3/composite, generated while the
        # rule was pending) carry ``true`` here; the authoritative accepted
        # status is thresholds.k4_threshold_acceptance().
        "thresholds_pending_reacceptance": True,
        "covered": covered,
        "uncovered_rows": uncovered,
    }


def artifact_manifest_payload(
    *,
    comparison_rows_sha256: str,
    raw_shas: Mapping[int, str],
    rows_by_role_count: Mapping[str, int],
) -> dict[str, object]:
    """Build the artifact manifest that binds every artifact file SHA."""
    entries: dict[str, str] = {
        _COMPARISON_FILENAME: comparison_rows_sha256,
    }
    for sequence, sha in sorted(raw_shas.items()):
        entries[f"{_RAW_DIRECTORY}/{sequence}.json"] = sha
    return {
        "schema_version": K4C_R2_ARTIFACT_MANIFEST_SCHEMA,
        "files": entries,
        "rows_by_role_count": {role: rows_by_role_count.get(role, 0) for role in _ROLES},
    }


# ---------------------------------------------------------------------------
# Offline verifier: recompute everything, never trust embedded summaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class K4CVerificationOutcome:
    ok: bool
    reasons: tuple[str, ...]
    recomputed: dict[str, object]


def _raw_facts_from_envelope(
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Extract vendor preclose, empty facts and contract facts from one raw envelope."""
    binding = payload.get("binding")
    contract = payload.get("contract")
    response = payload.get("response")
    provider_payload = payload.get("provider_payload")
    binding_map = binding if isinstance(binding, Mapping) else {}
    contract_map = contract if isinstance(contract, Mapping) else {}
    lineage_value = contract_map.get("lineage")
    lineage_map = lineage_value if isinstance(lineage_value, Mapping) else {}
    upstream = str(lineage_map.get("upstream", ""))
    library_version = str(lineage_map.get("library_version", ""))
    contract_version = str(contract_map.get("contract_version", ""))
    volume_unit = str(contract_map.get("volume_unit", ""))
    amount_unit = str(contract_map.get("amount_unit", ""))
    unadjusted = (
        contract_map.get("unadjusted") if isinstance(contract_map.get("unadjusted"), bool) else None
    )
    complete: bool | None = None
    fields_ok = True
    dates_ok = True
    if isinstance(response, Mapping):
        complete = response.get("complete") if isinstance(response.get("complete"), bool) else None
        records = response.get("records")
        start = str(binding_map.get("start", ""))
        end = str(binding_map.get("end", ""))
        if isinstance(records, list):
            for record in records:
                if not isinstance(record, dict):
                    fields_ok = False
                    continue
                if any(
                    field not in record
                    for field in (
                        "stock_code",
                        "date",
                        "open",
                        "high",
                        "low",
                        "close",
                        "pre_close",
                        "volume",
                        "amount",
                    )
                ):
                    fields_ok = False
                day = str(record.get("date", ""))[:10]
                if start and end and not (start <= day <= end):
                    dates_ok = False
    vendor_preclose: dict[tuple[str, str], str] = {}
    if isinstance(provider_payload, Mapping):
        fields = provider_payload.get("fields")
        rows = provider_payload.get("rows")
        if (
            isinstance(fields, list)
            and "preclose" in fields
            and "date" in fields
            and isinstance(rows, list)
        ):
            date_index = fields.index("date")
            preclose_index = fields.index("preclose")
            scope = binding_map.get("scope")
            symbol = str(scope[0]) if isinstance(scope, list) and scope else ""
            for row in rows:
                if not isinstance(row, list) or len(row) <= max(date_index, preclose_index):
                    continue
                vendor_preclose[(symbol, str(row[date_index])[:10])] = str(row[preclose_index])
    scope = binding_map.get("scope")
    scope_symbol = str(scope[0]) if isinstance(scope, list) and scope else ""
    upstream_row_count = (
        response.get("upstream_row_count")
        if isinstance(response, Mapping) and isinstance(response.get("upstream_row_count"), int)
        else None
    )
    return {
        "upstream": upstream,
        "role": _ROLE_BY_UPSTREAM.get(upstream, ""),
        "library_version": library_version,
        "contract_version": contract_version,
        "volume_unit": volume_unit,
        "amount_unit": amount_unit,
        "unadjusted": unadjusted,
        "complete": complete,
        "fields_complete": fields_ok,
        "dates_in_window": dates_ok,
        "vendor_preclose": vendor_preclose,
        "symbol": scope_symbol,
        "upstream_row_count": upstream_row_count,
    }


_BAOSTOCK_VENDOR_FIELDS = ("date", "code", "open", "high", "low", "close", "volume", "amount")


def reconstruct_canonical_rows_from_raw(
    payload: Mapping[str, object],
    invocation: K4CProbeInvocation,
) -> tuple[list[dict[str, object]], list[str]]:
    """Rebuild canonical rows from the provider payload alone.

    The reconstruction never reads ``response.records`` (the adapter's own
    canonicalization) and never trusts the comparison artifact: vendor rows
    are parsed, the frozen unit policy is applied (Tencent ``sz000`` lots are
    multiplied by 100, amounts stay CNY yuan), rows are sorted by date, and
    canonical ``pre_close`` is derived from the previous row ``close`` over
    the complete upstream series before cutting back to the target window.
    """
    reasons: list[str] = []
    binding_map: Mapping[Any, Any] = {}
    if isinstance(payload.get("binding"), Mapping):
        binding_map = cast(Mapping[Any, Any], payload["binding"])
    contract_map: Mapping[Any, Any] = {}
    if isinstance(payload.get("contract"), Mapping):
        contract_map = cast(Mapping[Any, Any], payload["contract"])
    lineage_value = contract_map.get("lineage")
    lineage_map = lineage_value if isinstance(lineage_value, Mapping) else {}
    upstream = str(lineage_map.get("upstream", ""))
    provider = str(lineage_map.get("provider", ""))
    endpoint = str(lineage_map.get("endpoint", ""))
    declared_volume_unit = str(contract_map.get("volume_unit", ""))
    declared_amount_unit = str(contract_map.get("amount_unit", ""))
    scope = binding_map.get("scope")
    symbol = str(scope[0]) if isinstance(scope, list) and scope else ""
    start = str(binding_map.get("start", ""))
    end = str(binding_map.get("end", ""))
    if (
        symbol != invocation.symbol
        or start != invocation.target_start.isoformat()
        or end != invocation.target_end.isoformat()
        or provider != invocation.provider.value
        or upstream != invocation.upstream.value
        or endpoint != invocation.endpoint
    ):
        reasons.append(RAW_PLAN_BINDING_MISMATCH)
    if upstream == "BAOSTOCK":
        frozen_volume_unit = VolumeUnit.SHARE
    else:
        try:
            frozen_volume_unit = resolve_returned_units(
                DataUpstream(upstream),
                symbol,
                _FROZEN_LIBRARY_VERSIONS.get(upstream, ""),
            ).volume_unit
        except (TypeError, ValueError):
            reasons.append(RAW_UNIT_RECONSTRUCTION_MISMATCH)
            return [], reasons
    if declared_volume_unit != frozen_volume_unit.value or declared_amount_unit != "cny_yuan":
        reasons.append(RAW_UNIT_RECONSTRUCTION_MISMATCH)
    provider_payload: Mapping[Any, Any] = {}
    if isinstance(payload.get("provider_payload"), Mapping):
        provider_payload = cast(Mapping[Any, Any], payload["provider_payload"])
    parsed: list[dict[str, object]] = []
    if upstream == "BAOSTOCK":
        fields = provider_payload.get("fields")
        rows = provider_payload.get("rows")
        if (
            not isinstance(fields, list)
            or not isinstance(rows, list)
            or any(name not in fields for name in _BAOSTOCK_VENDOR_FIELDS)
        ):
            reasons.append("ARTIFACT_INVALID:raw-provider-payload")
            return [], reasons
        index = {name: fields.index(name) for name in _BAOSTOCK_VENDOR_FIELDS}
        tradestatus_index = fields.index("tradestatus") if "tradestatus" in fields else None
        for row in rows:
            if not isinstance(row, list) or len(row) <= max(index.values()):
                reasons.append("ARTIFACT_INVALID:raw-provider-payload")
                return [], reasons
            if (
                tradestatus_index is not None
                and len(row) > tradestatus_index
                and str(row[tradestatus_index]) == "0"
                and str(row[index["volume"]]) == ""
                and str(row[index["amount"]]) == ""
            ):
                # Suspended-day placeholder (K4C-R3-R1 diagnostic,
                # 2026-08-16): BaoStock fills OHLC with the last close and
                # leaves volume/amount empty; it carries no bar.
                continue
            try:
                parsed.append(
                    {
                        "date": str(row[index["date"]])[:10],
                        "open": float(row[index["open"]]),
                        "high": float(row[index["high"]]),
                        "low": float(row[index["low"]]),
                        "close": float(row[index["close"]]),
                        "volume": int(float(row[index["volume"]])),
                        "amount": float(row[index["amount"]]),
                    }
                )
            except (TypeError, ValueError):
                reasons.append("ARTIFACT_INVALID:raw-provider-payload")
                return [], reasons
    else:
        records = provider_payload.get("records")
        if not isinstance(records, list):
            reasons.append("ARTIFACT_INVALID:raw-provider-payload")
            return [], reasons
        for record in records:
            if not isinstance(record, Mapping):
                reasons.append("ARTIFACT_INVALID:raw-provider-payload")
                return [], reasons
            try:
                volume = int(float(record["volume"]))
                if frozen_volume_unit is VolumeUnit.LOT_100_SHARES:
                    volume *= 100
                parsed.append(
                    {
                        "date": str(record["date"])[:10],
                        "open": float(record["open"]),
                        "high": float(record["high"]),
                        "low": float(record["low"]),
                        "close": float(record["close"]),
                        "volume": volume,
                        "amount": float(record["amount"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                reasons.append("ARTIFACT_INVALID:raw-provider-payload")
                return [], reasons
    parsed.sort(key=lambda row: str(row["date"]))
    canonical_rows: list[dict[str, object]] = []
    previous_close: float | None = None
    for row in parsed:
        in_target = start <= str(row["date"]) <= end
        if in_target and previous_close is not None:
            canonical_rows.append(
                {
                    "symbol": symbol,
                    "trade_date": str(row["date"]),
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "pre_close": previous_close,
                    "volume": row["volume"],
                    "amount": row["amount"],
                }
            )
        close_value = row["close"]
        previous_close = float(cast(float, close_value))
    return canonical_rows, reasons


def closure_reasons(
    *,
    reconstructed: Mapping[str, Mapping[tuple[str, str], Mapping[str, object]]],
    comparison_rows: Mapping[str, Mapping[tuple[str, str], Mapping[str, object]]] | None,
) -> list[str]:
    """Compare raw-reconstructed rows against the comparison artifact.

    Key sets must match exactly and every field of every common row must be
    identical; ``pre_close`` mismatches get their own stable code.
    """
    reasons: list[str] = []
    comparison = {role: {} for role in _ROLES} if comparison_rows is None else comparison_rows
    for role in _ROLES:
        recon = reconstructed.get(role, {})
        comp = comparison.get(role, {})
        recon_keys = set(recon)
        comp_keys = set(comp)
        if recon_keys != comp_keys:
            reasons.append(RAW_COMPARISON_KEY_MISMATCH)
        for key in sorted(recon_keys & comp_keys):
            for field in _FIELDS:
                left = _canonical_number(recon[key][field], field=field)
                right = _canonical_number(comp[key][field], field=field)
                if left != right:
                    reasons.append(
                        RAW_PRE_CLOSE_RECONSTRUCTION_MISMATCH
                        if field == "pre_close"
                        else RAW_COMPARISON_VALUE_MISMATCH
                    )
    return reasons


def load_k4c_r2_artifacts(
    attempt_directory: Path,
    *,
    plan: Sequence[K4CProbeInvocation] | None = None,
    marker: str | None = None,
    plan_sha256: str | None = None,
    invocation_budget: int | None = None,
) -> tuple[list[str], dict[str, object]]:
    """Parse the attempt directory into recomputed facts.

    Returns ``(reasons, facts)`` where ``facts`` contains
    ``header/journal/manifest/comparison_rows/vendor_preclose/
    empty_invocations/contract_facts/statistics/observed_objectives/
    threshold_coverage/contract_checks/invocation_count/
    not_called_invocation_count``. Empty ``reasons`` means the artifacts are
    structurally valid; the caller still must compute sufficiency.

    When ``plan``/``marker``/``plan_sha256``/``invocation_budget`` are omitted
    the frozen R2 contract applies; R3 attempts pass their own frozen values.
    """
    selected_plan = tuple(plan) if plan is not None else fixed_k4c_probe_plan()
    selected_marker = marker if marker is not None else K4C_R2_MARKER
    selected_plan_sha = plan_sha256 if plan_sha256 is not None else k4c_r2_plan_sha256()
    selected_budget = (
        invocation_budget if invocation_budget is not None else K4C_R2_INVOCATION_BUDGET
    )
    reasons: list[str] = []
    facts: dict[str, object] = {}
    header_path = attempt_directory / _ATTEMPT_HEADER_FILENAME
    header = None
    if not header_path.is_file():
        reasons.append("ARTIFACT_MISSING:attempt-header.json")
    else:
        try:
            payload = json.loads(header_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            reasons.append("ARTIFACT_INVALID:attempt-header.json")
            payload = None
        if (
            isinstance(payload, dict)
            and payload.get("schema_version") == K4C_R2_ATTEMPT_HEADER_SCHEMA
        ):
            try:
                header = K4CAttemptHeader(
                    authorization_id=str(payload["authorization_id"]),
                    attempt_id=str(payload["attempt_id"]),
                    marker=str(payload["authorization_marker"]),
                    plan_sha256=str(payload["plan_sha256"]),
                    invocation_budget=int(payload["invocation_budget"]),
                    created_at=str(payload["created_at"]),
                )
            except (KeyError, TypeError, ValueError):
                reasons.append("ARTIFACT_INVALID:attempt-header.json")
        else:
            reasons.append("ARTIFACT_INVALID:attempt-header.json")
    if header is not None:
        if header.marker != selected_marker:
            reasons.append("MARKER_NOT_FROZEN")
        if header.plan_sha256 != selected_plan_sha:
            reasons.append("PLAN_SHA256_MISMATCH")
        if header.invocation_budget != selected_budget:
            reasons.append("BUDGET_MISMATCH")
    facts["header"] = None if header is None else header.to_dict()

    journal: list[dict[str, object]] = []
    sequences: set[int] = set()
    journal_path = attempt_directory / _INVOCATION_JOURNAL_FILENAME
    if header is not None:
        if not journal_path.is_file():
            reasons.append("ARTIFACT_MISSING:invocations.jsonl")
        else:
            with open(journal_path, "rb") as handle:
                for line in handle:
                    try:
                        record = json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if (
                        isinstance(record, dict)
                        and record.get("schema_version") == K4C_R2_INVOCATION_RECORD_SCHEMA
                    ):
                        journal.append(record)
                        sequence = record.get("sequence")
                        if isinstance(sequence, int):
                            if sequence in sequences:
                                reasons.append("DUPLICATE_INVOCATION_RECORD")
                            sequences.add(sequence)
    facts["journal"] = journal
    facts["invocation_count"] = len(journal)
    facts["not_called_invocation_count"] = (
        0 if header is None else header.invocation_budget - len(journal)
    )

    for record in journal:
        sequence = record.get("sequence")
        if not isinstance(sequence, int) or not 1 <= sequence <= len(selected_plan):
            reasons.append("UNAUTHORIZED_CALL")
            continue
        invocation = selected_plan[sequence - 1]
        outcome = record.get("outcome")
        if isinstance(outcome, Mapping) and (
            outcome.get("symbol") != invocation.symbol
            or outcome.get("role") != invocation.role
            or outcome.get("upstream") != invocation.upstream.value
        ):
            reasons.append("UNAUTHORIZED_CALL")

    manifest: dict[str, object] | None = None
    manifest_path = attempt_directory / _ARTIFACT_MANIFEST_FILENAME
    if not manifest_path.is_file():
        reasons.append("ARTIFACT_MISSING:artifact-manifest.json")
    else:
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload = None
        if (
            isinstance(payload, dict)
            and payload.get("schema_version") == K4C_R2_ARTIFACT_MANIFEST_SCHEMA
            and isinstance(payload.get("files"), dict)
        ):
            manifest = payload
            for relative, declared in payload["files"].items():
                if not isinstance(relative, str) or not isinstance(declared, str):
                    reasons.append("ARTIFACT_INVALID:artifact-manifest.json")
                    continue
                target = attempt_directory / relative
                if not target.is_file():
                    reasons.append(f"ARTIFACT_MISSING:{relative}")
                    continue
                actual = hashlib.sha256(target.read_bytes()).hexdigest()
                if actual != declared:
                    reasons.append(f"ARTIFACT_SHA_MISMATCH:{relative}")
        else:
            reasons.append("ARTIFACT_INVALID:artifact-manifest.json")
    facts["manifest"] = manifest

    comparison_path = attempt_directory / _COMPARISON_FILENAME
    rows_by_role: dict[str, dict[tuple[str, str], Mapping[str, object]]] | None = None
    if manifest is None:
        reasons.append("ARTIFACT_MISSING:comparison-rows.json")
    else:
        try:
            rows_by_role = rows_from_comparison_payload(
                json.loads(comparison_path.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, OSError, ValueError):
            reasons.append("ARTIFACT_INVALID:comparison-rows.json")
    facts["comparison_rows"] = rows_by_role

    vendor_preclose: dict[str, dict[tuple[str, str], str]] = {}
    empty_invocations: list[dict[str, object]] = []
    contract_facts: list[dict[str, object]] = []
    reconstructed: dict[str, dict[tuple[str, str], Mapping[str, object]]] = {}
    if manifest is not None:
        manifest_files_map = cast(dict[str, str], manifest["files"])
        raw_sequences = sorted(
            int(relative.rsplit("/", 1)[-1].rsplit(".", 1)[0])
            for relative in manifest_files_map
            if relative.startswith(_RAW_DIRECTORY + "/") and relative.endswith(".json")
        )
        for sequence in raw_sequences:
            path = attempt_directory / f"{_RAW_DIRECTORY}/{sequence}.json"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                reasons.append(f"ARTIFACT_INVALID:raw/{sequence}.json")
                continue
            if not isinstance(payload, dict):
                reasons.append(f"ARTIFACT_INVALID:raw/{sequence}.json")
                continue
            envelope_facts = _raw_facts_from_envelope(payload)
            envelope_facts["sequence"] = sequence
            contract_facts.append(envelope_facts)
            for key, value in cast(
                dict[tuple[str, str], str], envelope_facts["vendor_preclose"]
            ).items():
                vendor_preclose.setdefault("A", {})[key] = value
            if 1 <= sequence <= len(selected_plan):
                invocation = selected_plan[sequence - 1]
                reconstructed_rows, reconstruction_reasons = reconstruct_canonical_rows_from_raw(
                    payload, invocation
                )
                for reason in reconstruction_reasons:
                    reasons.append(reason)
                for row in reconstructed_rows:
                    role = cast(str, envelope_facts["role"])
                    reconstructed.setdefault(role, {})[
                        (str(row["symbol"]), str(row["trade_date"]))
                    ] = {field: row[field] for field in _FIELDS}
                journal_record = next(
                    (record for record in journal if record.get("sequence") == sequence),
                    None,
                )
                outcome = (
                    journal_record.get("outcome") if isinstance(journal_record, Mapping) else None
                )
                declared_sha = (
                    outcome.get("content_sha256") if isinstance(outcome, Mapping) else None
                )
                actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
                if isinstance(declared_sha, str) and declared_sha != actual_sha:
                    reasons.append(JOURNAL_RAW_SHA_MISMATCH)
            response = payload.get("response")
            binding = payload.get("binding")
            if (
                isinstance(response, dict)
                and isinstance(response.get("upstream_row_count"), int)
                and response["upstream_row_count"] == 0
            ):
                empty_invocations.append(
                    {
                        "sequence": sequence,
                        "role": envelope_facts["role"],
                        "symbol": (
                            str(binding["scope"][0])
                            if isinstance(binding, dict)
                            and isinstance(binding.get("scope"), list)
                            and binding["scope"]
                            else ""
                        ),
                        "target_start": (
                            str(binding["start"]) if isinstance(binding, dict) else ""
                        ),
                        "upstream_row_count": 0,
                    }
                )
    facts["vendor_preclose"] = vendor_preclose
    facts["empty_invocations"] = empty_invocations
    facts["contract_facts"] = contract_facts
    facts["reconstructed_rows"] = reconstructed
    if manifest is not None:
        manifest_files = set(str(name) for name in cast(dict[str, str], manifest["files"]))
        for record in journal:
            sequence = record.get("sequence")
            if isinstance(sequence, int):
                raw_relative = f"{_RAW_DIRECTORY}/{sequence}.json"
                if raw_relative not in manifest_files:
                    reasons.append(f"ARTIFACT_MISSING:{raw_relative}")
        journal_sequences = {
            record.get("sequence") for record in journal if isinstance(record.get("sequence"), int)
        }
        for relative in manifest_files:
            if relative.startswith(_RAW_DIRECTORY + "/") and relative.endswith(".json"):
                sequence = int(relative.rsplit("/", 1)[-1].rsplit(".", 1)[0])
                if sequence not in journal_sequences:
                    reasons.append(UNREFERENCED_RAW_ARTIFACT)
    reasons.extend(closure_reasons(reconstructed=reconstructed, comparison_rows=rows_by_role))
    return reasons, facts


class K4CVerifier:
    """Recompute all evidence statistics from the attempt artifacts.

    ``recompute`` derives every fact from the artifacts; ``verify`` repeats
    the recomputation and cross-checks each recomputed value against the
    evidence payload, rejecting any mismatch. The evidence's own summary
    values are never trusted. The verifier defaults to the frozen R2
    contract; R3 attempts pass their own frozen plan/marker/hash/budget.
    """

    def __init__(
        self,
        *,
        plan: Sequence[K4CProbeInvocation] | None = None,
        marker: str | None = None,
        plan_sha256: str | None = None,
        invocation_budget: int | None = None,
        r3_samples: Sequence[K4CR3ObjectiveSample] | None = None,
    ) -> None:
        self._plan = tuple(plan) if plan is not None else fixed_k4c_probe_plan()
        self._marker = marker if marker is not None else K4C_R2_MARKER
        self._plan_sha256 = plan_sha256 if plan_sha256 is not None else k4c_r2_plan_sha256()
        self._invocation_budget = (
            invocation_budget if invocation_budget is not None else K4C_R2_INVOCATION_BUDGET
        )
        self._r3_samples = tuple(r3_samples) if r3_samples is not None else None

    def recompute(
        self,
        attempt_directory: Path,
        *,
        failure: object = None,
        calendar_proofs: Mapping[str, Mapping[str, object]] | None = None,
    ) -> K4CVerificationOutcome:
        if not isinstance(attempt_directory, Path):
            raise TypeError("attempt_directory must be a Path")
        reasons, facts = load_k4c_r2_artifacts(
            attempt_directory,
            plan=self._plan,
            marker=self._marker,
            plan_sha256=self._plan_sha256,
            invocation_budget=self._invocation_budget,
        )
        rows_by_role = cast(
            dict[str, dict[tuple[str, str], Mapping[str, object]]] | None,
            facts["comparison_rows"],
        )
        statistics: dict[str, object]
        objectives: list[dict[str, object]] = []
        coverage: dict[str, object] = {
            "threshold_rule_version": k4_evidence_thresholds().rule_version,
            # Pinned to the byte-frozen evidence payloads; the accepted status
            # lives in thresholds.k4_threshold_acceptance().
            "thresholds_pending_reacceptance": True,
            "covered": True,
            "uncovered_rows": [],
        }
        contract_checks: dict[str, bool] = {
            "units_verified": False,
            "unadjusted": False,
            "complete": False,
            "dates_in_target_window": False,
            "fields_complete": False,
            "library_versions_frozen": False,
        }
        comparison_verified = not any(
            reason.startswith(
                (
                    "ARTIFACT",
                    "RAW_",
                    "JOURNAL_RAW",
                    "UNREFERENCED_RAW",
                )
            )
            for reason in reasons
        )
        if rows_by_role is not None:
            statistics = recompute_comparison_statistics(rows_by_role)
            vendor_preclose = cast(
                dict[str, dict[tuple[str, str], str]],
                facts["vendor_preclose"],
            )
            empty_invocations = cast(list[dict[str, object]], facts["empty_invocations"])
            if self._r3_samples is not None:
                objectives = list(
                    observe_k4c_r3_objectives(
                        self._r3_samples,
                        rows_by_role=rows_by_role,
                        empty_invocations=empty_invocations,
                        invocation_statuses=_r3_invocation_statuses(facts),
                        calendar_proofs=calendar_proofs,
                    )
                )
            else:
                objectives = list(
                    observe_k4c_r2_objectives(
                        rows_by_role,
                        vendor_preclose=vendor_preclose,
                        empty_invocations=empty_invocations,
                    )
                )
            coverage = threshold_coverage(rows_by_role)
        else:
            statistics = {
                "schema_version": K4C_R2_COMPARISON_SCHEMA,
                "held_rows_total": 0,
                "rows_held_by_role_count": {},
                "fields": {
                    field: {
                        "compared_rows": 0,
                        "exact_rows": 0,
                        "divergent_rows": 0,
                    }
                    for field in _FIELDS
                },
            }
        contract_facts = cast(list[dict[str, object]], facts["contract_facts"])
        if contract_facts:
            contract_checks = {
                "units_verified": all(
                    item["volume_unit"] not in {"", "unverified"}
                    and item["amount_unit"] == "cny_yuan"
                    for item in contract_facts
                ),
                "unadjusted": all(item["unadjusted"] is True for item in contract_facts),
                "complete": all(item["complete"] is True for item in contract_facts),
                "dates_in_target_window": all(item["dates_in_window"] for item in contract_facts),
                "fields_complete": all(item["fields_complete"] for item in contract_facts),
                "library_versions_frozen": all(
                    _FROZEN_LIBRARY_VERSIONS.get(str(item["upstream"])) == item["library_version"]
                    and _FROZEN_CONTRACT_VERSIONS.get(str(item["upstream"]))
                    == item["contract_version"]
                    for item in contract_facts
                ),
            }
        header = facts["header"]
        invocation_count = cast(int, facts["invocation_count"])
        not_called_count = cast(int, facts["not_called_invocation_count"])
        if self._r3_samples is not None:
            objective_reasons = validate_k4c_r3_objective_records(objectives, self._r3_samples)
            sufficient, sufficiency_reasons = compute_k4c_r3_sufficiency(
                invocation_count=invocation_count,
                budget=(
                    self._invocation_budget
                    if not isinstance(header, dict)
                    else int(header["invocation_budget"])
                ),
                failure=failure,
                objectives=objectives,
                contract_checks=contract_checks,
                comparison_verified=comparison_verified,
                unrecorded_attempts=0 if isinstance(header, dict) else 1,
                unauthorized_calls=sum(1 for reason in reasons if reason == "UNAUTHORIZED_CALL"),
            )
        else:
            objective_reasons = (
                ()
                if self._marker != K4C_R2_MARKER
                else validate_k4c_r2_objective_records(objectives)
            )
            unauthorized_calls = sum(1 for reason in reasons if reason == "UNAUTHORIZED_CALL")
            sufficient, sufficiency_reasons = compute_k4c_r2_sufficiency(
                invocation_count=invocation_count,
                budget=(
                    self._invocation_budget
                    if not isinstance(header, dict)
                    else int(header["invocation_budget"])
                ),
                failure=failure,
                objectives=objectives,
                contract_checks=contract_checks,
                comparison_verified=comparison_verified,
                unrecorded_attempts=0 if isinstance(header, dict) else 1,
                unauthorized_calls=unauthorized_calls,
            )
        recomputed: dict[str, object] = {
            "invocation_count": invocation_count,
            "not_called_invocation_count": not_called_count,
            "statistics": statistics,
            "observed_objectives": objectives,
            "threshold_coverage": coverage,
            "contract_checks": contract_checks,
            "reconstructed_rows": cast(dict[str, object], facts["reconstructed_rows"]),
            "representative_evidence_sufficient": sufficient,
            "sufficiency_reasons": list(sufficiency_reasons),
            "attempt_header": facts["header"],
            "journal": facts["journal"],
        }
        all_reasons = reasons + list(objective_reasons)
        return K4CVerificationOutcome(
            ok=not all_reasons,
            reasons=tuple(sorted(set(all_reasons))),
            recomputed=recomputed,
        )

    def verify(
        self,
        *,
        attempt_directory: Path,
        evidence: Mapping[str, object],
    ) -> K4CVerificationOutcome:
        """Recompute and cross-check every evidence field against artifacts.

        Identity, marker, plan and budget must be recomputed from the attempt
        header and the frozen verifier contract; the manifest SHA and the
        artifact root are recomputed from the attempt directory itself;
        ``sufficiency_reasons``, ``planned_objectives`` (R2 objectives or R3
        frozen samples) and R3 ``calendar_proofs`` must equal the recomputed
        values exactly. Self-reported evidence values are never trusted.
        """
        reasons: list[str] = []
        allowed_fields = (
            _R3_EVIDENCE_FIELDS if self._r3_samples is not None else _R2_EVIDENCE_FIELDS
        )
        for field in evidence:
            if field not in allowed_fields:
                reasons.append(f"EVIDENCE_UNKNOWN_FIELD:{field}")
        expected_schema = (
            K4C_R3_EVIDENCE_SCHEMA if self._r3_samples is not None else K4C_R2_EVIDENCE_SCHEMA
        )
        if evidence.get("schema_version") != expected_schema:
            reasons.append(EVIDENCE_SCHEMA_MISMATCH)
        calendar_proofs: Mapping[str, Mapping[str, object]] | None = None
        if self._r3_samples is not None:
            declared = evidence.get("calendar_proofs")
            if isinstance(declared, Mapping):
                calendar_proofs = cast(Mapping[str, Mapping[str, object]], declared)
        outcome = self.recompute(
            attempt_directory,
            failure=evidence.get("failure"),
            calendar_proofs=calendar_proofs,
        )
        reasons.extend(outcome.reasons)
        recomputed = dict(outcome.recomputed)
        header = recomputed.get("attempt_header")
        header_map = cast(Mapping[str, object], header) if isinstance(header, Mapping) else None
        if header_map is not None:
            for field in (
                "authorization_id",
                "attempt_id",
                "authorization_marker",
                "plan_sha256",
                "invocation_budget",
            ):
                if evidence.get(field) != header_map.get(field):
                    reasons.append(f"EVIDENCE_HEADER_BINDING_MISMATCH:{field}")
        for field, expected in (
            ("application_retries", K4C_R2_APPLICATION_RETRIES),
            ("fail_fast", K4C_R2_FAIL_FAST),
            ("writes_formal_data_root", False),
        ):
            if evidence.get(field) != expected:
                reasons.append(f"EVIDENCE_CONTRACT_BINDING_MISMATCH:{field}")
        journal = recomputed.get("journal")
        journal_list = cast(list[dict[str, object]], journal) if isinstance(journal, list) else []
        failed_records = [
            record
            for record in journal_list
            if isinstance(record.get("outcome"), Mapping)
            and cast(Mapping[str, object], record["outcome"]).get("status") == "FAILED"
        ]
        if failed_records:
            first = cast(Mapping[str, object], failed_records[0]["outcome"])
            expected_failure: object = {
                "stable_code": first.get("stable_code"),
                "error_type": first.get("error_type"),
            }
        else:
            expected_failure = None
        if evidence.get("failure") != expected_failure:
            reasons.append("EVIDENCE_CONTRACT_BINDING_MISMATCH:failure")
        manifest_path = attempt_directory / _ARTIFACT_MANIFEST_FILENAME
        if not manifest_path.is_file():
            reasons.append(EVIDENCE_MANIFEST_SHA_MISMATCH)
        else:
            actual_manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            if evidence.get("artifact_manifest_sha256") != actual_manifest_sha:
                reasons.append(EVIDENCE_MANIFEST_SHA_MISMATCH)
        declared_root = evidence.get("artifacts_root")
        normalized_declared = _normalized_artifact_root(declared_root)
        normalized_attempt = _normalized_artifact_root(attempt_directory)
        if normalized_declared != normalized_attempt:
            reasons.append(EVIDENCE_ARTIFACT_ROOT_MISMATCH)
        if evidence.get("sufficiency_reasons") != recomputed["sufficiency_reasons"]:
            reasons.append("EVIDENCE_SUMMARY_MISMATCH:sufficiency_reasons")
        if self._r3_samples is not None:
            expected_planned: object = [sample.to_dict() for sample in self._r3_samples]
            if evidence.get("planned_objectives") != expected_planned:
                reasons.append(EVIDENCE_PLANNED_OBJECTIVES_MISMATCH)
            if evidence.get("calendar_proofs") != _expected_r3_calendar_proofs(self._r3_samples):
                reasons.append(EVIDENCE_CALENDAR_PROOF_MISMATCH)
        else:
            expected_planned = [item.to_dict() for item in K4C_R2_OBJECTIVES]
            if evidence.get("planned_objectives") != expected_planned:
                reasons.append(EVIDENCE_PLANNED_OBJECTIVES_MISMATCH)
        declared_count = evidence.get("invocation_count")
        if declared_count != recomputed["invocation_count"]:
            reasons.append("EVIDENCE_SUMMARY_MISMATCH:invocation_count")
        declared_not_called = evidence.get("not_called_invocation_count")
        if declared_not_called != recomputed["not_called_invocation_count"]:
            reasons.append("EVIDENCE_SUMMARY_MISMATCH:not_called_invocation_count")
        declared_sufficient = evidence.get("representative_evidence_sufficient")
        if declared_sufficient != recomputed["representative_evidence_sufficient"]:
            reasons.append("EVIDENCE_SUMMARY_MISMATCH:representative_evidence_sufficient")
        if evidence.get("cross_source") != recomputed["statistics"]:
            reasons.append("EVIDENCE_SUMMARY_MISMATCH:cross_source")
        if evidence.get("observed_objectives") != recomputed["observed_objectives"]:
            reasons.append("EVIDENCE_SUMMARY_MISMATCH:observed_objectives")
        if evidence.get("contract_checks") != recomputed["contract_checks"]:
            reasons.append("EVIDENCE_SUMMARY_MISMATCH:contract_checks")
        if evidence.get("threshold_coverage") != recomputed["threshold_coverage"]:
            reasons.append("EVIDENCE_SUMMARY_MISMATCH:threshold_coverage")
        unique = tuple(sorted(set(reasons)))
        return K4CVerificationOutcome(ok=not unique, reasons=unique, recomputed=recomputed)


# ---------------------------------------------------------------------------
# K4C composite evidence: R2 + R3 combined through SHA references only
# ---------------------------------------------------------------------------

CONFLICTING_OBJECTIVE_FACTS = "CONFLICTING_OBJECTIVE_FACTS"
COMPONENT_EVIDENCE_SHA_MISMATCH = "COMPONENT_EVIDENCE_SHA_MISMATCH"
COMPONENT_MANIFEST_SHA_MISMATCH = "COMPONENT_MANIFEST_SHA_MISMATCH"
COMPONENT_HEADER_MISMATCH = "COMPONENT_HEADER_MISMATCH"
COMPONENT_VERIFICATION_FAILED = "COMPONENT_VERIFICATION_FAILED"
AUTHORIZATION_ID_REUSED = "AUTHORIZATION_ID_REUSED"
COMPONENT_ROW_CONFLICT = "COMPONENT_ROW_CONFLICT"


@dataclass(frozen=True, slots=True)
class K4CComponentReference:
    """SHA-bound reference to one probe component; never copies its data."""

    component: str
    plan_sha256: str
    authorization_id: str
    attempt_id: str
    evidence_sha256: str
    artifact_manifest_sha256: str
    attempt_directory: str
    evidence_file: str

    def __post_init__(self) -> None:
        if self.component not in {"R2", "R3"}:
            raise ValueError("component must be R2 or R3")
        for field in ("plan_sha256", "evidence_sha256", "artifact_manifest_sha256"):
            value = getattr(self, field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{field} must be a lowercase SHA-256")
        if (
            not isinstance(self.authorization_id, str)
            or len(self.authorization_id) < 8
            or any(
                character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
                for character in self.authorization_id
            )
        ):
            raise ValueError("authorization_id must be a safe identifier")
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise ValueError("attempt_id must be non-empty text")
        if not isinstance(self.attempt_directory, str) or not self.attempt_directory:
            raise ValueError("attempt_directory must be non-empty text")
        if not isinstance(self.evidence_file, str) or not self.evidence_file:
            raise ValueError("evidence_file must be non-empty text")

    def to_dict(self) -> dict[str, object]:
        return {
            "component": self.component,
            "plan_sha256": self.plan_sha256,
            "authorization_id": self.authorization_id,
            "attempt_id": self.attempt_id,
            "evidence_sha256": self.evidence_sha256,
            "artifact_manifest_sha256": self.artifact_manifest_sha256,
            "attempt_directory": self.attempt_directory,
            "evidence_file": self.evidence_file,
        }


def r2_component_reference() -> K4CComponentReference:
    """Return the frozen R2 component reference (SHA-bound, read-only)."""
    return K4CComponentReference(
        component="R2",
        plan_sha256=k4c_r2_plan_sha256(),
        authorization_id=R2_AUTHORIZATION_ID,
        attempt_id=R2_ATTEMPT_ID,
        evidence_sha256=R2_EVIDENCE_SHA256,
        artifact_manifest_sha256=R2_ARTIFACT_MANIFEST_SHA256,
        attempt_directory="docs/stage1/evidence/k4c-r2-attempts/K4C-R2-cab803399515",
        evidence_file="docs/stage1/evidence/k4c_r2_representative_probe_2026-08-16.json",
    )


R3_AUTHORIZATION_ID = "K4C-R3-2b8e5f14a6c9"
R3_ATTEMPT_ID = "attempt-9ced23d1844541fd"
R3_EVIDENCE_SHA256 = "1632ee8b2a210bcbe22742386072d9c16a10c789cc4bba9bd8dcf5f4ed0836ea"
R3_ARTIFACT_MANIFEST_SHA256 = "2b2f919ba40ef36835a8c3879b0a8a92c13ff689c66bf95a91ab0111aeb7c17c"


def r3_component_reference() -> K4CComponentReference:
    """Return the frozen R3 component reference (SHA-bound, read-only).

    Bound to the real Phase B attempt 3 (2026-08-16, 9/9 invocations): the
    first two one-shot authorization ids were consumed by honest fail-fast
    stops whose journals are preserved; this reference only ever points at
    the completed attempt.
    """
    return K4CComponentReference(
        component="R3",
        plan_sha256=k4c_r3_plan_sha256(k4c_r3_frozen_samples()),
        authorization_id=R3_AUTHORIZATION_ID,
        attempt_id=R3_ATTEMPT_ID,
        evidence_sha256=R3_EVIDENCE_SHA256,
        artifact_manifest_sha256=R3_ARTIFACT_MANIFEST_SHA256,
        attempt_directory="docs/stage1/evidence/k4c-r3-attempts/K4C-R3-2b8e5f14a6c9",
        evidence_file="docs/stage1/evidence/k4c_r3_representative_probe_2026-08-16.json",
    )


def merge_objective_records(
    r2: Sequence[Mapping[str, object]],
    r3: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], list[str]]:
    """Merge R2/R3 objective records; a planned objective never counts as observed.

    Two observed records for the same objective at the same (symbol,
    trade_date) with different facts are a conflict and fail closed.
    """
    reasons: list[str] = []
    merged: list[dict[str, object]] = []
    for name in k4c_r2_objective_names():
        records = [record for record in r2 if record.get("objective") == name] + [
            record for record in r3 if record.get("objective") == name
        ]
        observed = [record for record in records if record.get("observed") is True]
        for first in range(len(observed)):
            for second in range(first + 1, len(observed)):
                left, right = observed[first], observed[second]
                if (
                    left.get("symbol") == right.get("symbol")
                    and left.get("trade_date") == right.get("trade_date")
                    and left.get("fact") != right.get("fact")
                ):
                    reasons.append(CONFLICTING_OBJECTIVE_FACTS)
        merged.append(
            {
                "objective": name,
                "observed": bool(observed),
                "observed_records": observed,
                "unobserved_reason_codes": sorted(
                    {str(record["reason_code"]) for record in records if record.get("reason_code")}
                ),
            }
        )
    return merged, reasons


def merge_objective_records_with_r3_reobservation(
    r2: Sequence[Mapping[str, object]],
    r3: Sequence[Mapping[str, object]],
    samples: Sequence[K4CR3ObjectiveSample],
    merged_rows: Mapping[str, Mapping[tuple[str, str], Mapping[str, object]]],
    *,
    require_suspension_provenance: bool = False,
    suspension_reference: Mapping[str, object] | None = None,
    suspension_raw_fact: Mapping[str, object] | None = None,
) -> tuple[list[dict[str, object]], list[str]]:
    """Merge R2/R3 records and re-observe still-unobserved R3 samples.

    A sample may be unobserved inside its own attempt because the required
    counter-example lives in the other component (the suspension sample's
    cross-sectional counter-example can only come from immutable R2 rows).
    Re-observation runs the same frozen sample contract over the merged rows
    only; the legal-empty sample needs its calendar proof and therefore is
    never re-observed here. The suspension counter-example must be held by
    all three roles; when ``require_suspension_provenance`` is set (R4/v2),
    the official exchange-announcement reference and the BaoStock raw
    ``tradestatus=0`` fact are additional mandatory conditions.
    """
    merged, reasons = merge_objective_records(r2, r3)
    reobserved: list[dict[str, object]] = []
    for sample in samples:
        if any(
            record.get("objective") == sample.objective and record.get("observed") is True
            for record in merged
        ):
            continue
        record = _observe_r3_sample(
            sample,
            merged_rows,
            (),
            {},
            require_suspension_provenance=require_suspension_provenance,
            suspension_reference=suspension_reference,
            suspension_raw_fact=suspension_raw_fact,
        )
        # Unobserved re-observed records join the merge too: their specific
        # source reason codes must surface in the merged summary instead of
        # being silently replaced by the generic not-observed code.
        reobserved.append(record)
    if reobserved:
        reasons.extend(validate_k4c_r3_objective_records(reobserved, samples))
        merged, more_reasons = merge_objective_records(r2, [*r3, *reobserved])
        reasons.extend(more_reasons)
    return merged, reasons


def compute_k4c_composite_verdict(
    *,
    merged_objectives: Sequence[Mapping[str, object]],
    verification_clean: bool,
    conflict_free: bool,
    coverage_covered: bool,
) -> tuple[bool, bool, tuple[str, ...]]:
    """Return ``(sufficient, threshold_reacceptance_eligible, reasons)``.

    Sufficiency requires all eight objectives to carry at least one observed
    record and both components to pass verification; eligibility additionally
    requires the candidate 0/50 thresholds to cover every observed pair.
    """
    reasons: list[str] = []
    for record in merged_objectives:
        if record.get("observed") is not True:
            reasons.append(f"OBJECTIVE_UNOBSERVED:{record['objective']}")
    if not verification_clean:
        reasons.append(COMPONENT_VERIFICATION_FAILED)
    if not conflict_free:
        reasons.append(CONFLICTING_OBJECTIVE_FACTS)
    sufficient = not reasons
    eligible = sufficient and coverage_covered
    return sufficient, eligible, tuple(reasons)


@dataclass(frozen=True, slots=True)
class K4CCompositeVerificationOutcome:
    ok: bool
    reasons: tuple[str, ...]
    recomputed: dict[str, object]


class K4CCompositeVerifier:
    """Verify composite evidence by re-reading the referenced components.

    R2 and R3 are never copied: the verifier re-reads the referenced evidence
    files and attempt directories, recomputes their SHAs and headers, re-runs
    the raw-comparison closure verifier on each attempt directory, merges
    objective facts (conflicts fail closed), recomputes statistics over all
    comparable held rows and derives sufficiency/eligibility from those facts
    only.
    """

    def __init__(
        self,
        *,
        root: Path,
        r3_plan: Sequence[K4CProbeInvocation],
        r3_marker: str,
        r3_plan_sha256: str,
        r3_invocation_budget: int,
        r3_samples: Sequence[K4CR3ObjectiveSample] | None = None,
        schema_version: str = K4C_COMPOSITE_EVIDENCE_SCHEMA,
    ) -> None:
        if not isinstance(root, Path):
            raise TypeError("root must be a Path")
        if schema_version not in {K4C_COMPOSITE_EVIDENCE_SCHEMA, K4C_R4_COMPOSITE_EVIDENCE_SCHEMA}:
            raise ValueError("schema_version must be a frozen composite evidence schema")
        self._root = root
        self._schema_version = schema_version
        self._r3_samples = tuple(r3_samples) if r3_samples is not None else None
        self._r3_verifier = K4CVerifier(
            plan=r3_plan,
            marker=r3_marker,
            plan_sha256=r3_plan_sha256,
            invocation_budget=r3_invocation_budget,
            r3_samples=self._r3_samples,
        )

    def _resolve(self, path_text: str) -> Path:
        candidate = Path(path_text)
        return candidate if candidate.is_absolute() else self._root / candidate

    def verify(
        self,
        *,
        r2: K4CComponentReference,
        r3: K4CComponentReference,
        composite: Mapping[str, object],
    ) -> K4CCompositeVerificationOutcome:
        reasons: list[str] = []
        v2 = self._schema_version == K4C_R4_COMPOSITE_EVIDENCE_SCHEMA
        if composite.get("schema_version") != self._schema_version:
            reasons.append(COMPOSITE_SCHEMA_MISMATCH)
        frozen_r2 = r2_component_reference().to_dict()
        frozen_r3 = r3_component_reference().to_dict()
        declared_r2 = composite.get("r2_component")
        declared_r3 = composite.get("r3_component")
        if v2:
            if declared_r2 != frozen_r2 or r2.to_dict() != frozen_r2:
                reasons.append("COMPOSITE_COMPONENT_REFERENCE_MISMATCH:R2")
            if declared_r3 != frozen_r3 or r3.to_dict() != frozen_r3:
                reasons.append("COMPOSITE_COMPONENT_REFERENCE_MISMATCH:R3")
            if (
                composite.get("verification_contract_version")
                != K4C_R4_VERIFICATION_CONTRACT_VERSION
            ):
                reasons.append(COMPOSITE_CONTRACT_VERSION_MISMATCH)
        else:
            # Historical v1 composites may omit component bindings (legacy);
            # when present they must still equal the frozen references.
            for name, declared, frozen in (
                ("R2", declared_r2, frozen_r2),
                ("R3", declared_r3, frozen_r3),
            ):
                if declared is not None and declared != frozen:
                    reasons.append(f"COMPOSITE_COMPONENT_REFERENCE_MISMATCH:{name}")
        component_outcomes: dict[str, K4CVerificationOutcome] = {}
        for reference, verifier, label in (
            (r2, K4CVerifier(), "R2"),
            (r3, self._r3_verifier, "R3"),
        ):
            evidence_path = self._resolve(reference.evidence_file)
            if not evidence_path.is_file() or (
                hashlib.sha256(evidence_path.read_bytes()).hexdigest() != reference.evidence_sha256
            ):
                reasons.append(f"{COMPONENT_EVIDENCE_SHA_MISMATCH}:{label}")
            attempt_directory = self._resolve(reference.attempt_directory)
            manifest_path = attempt_directory / _ARTIFACT_MANIFEST_FILENAME
            if not manifest_path.is_file() or (
                hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                != reference.artifact_manifest_sha256
            ):
                reasons.append(f"{COMPONENT_MANIFEST_SHA_MISMATCH}:{label}")
            header_path = attempt_directory / _ATTEMPT_HEADER_FILENAME
            try:
                header = json.loads(header_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                header = None
            if (
                not isinstance(header, dict)
                or header.get("plan_sha256") != reference.plan_sha256
                or header.get("authorization_id") != reference.authorization_id
                or header.get("attempt_id") != reference.attempt_id
            ):
                reasons.append(f"{COMPONENT_HEADER_MISMATCH}:{label}")
            calendar_proofs: Mapping[str, Mapping[str, object]] | None = None
            if label == "R3":
                try:
                    r3_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    r3_evidence = None
                if isinstance(r3_evidence, Mapping):
                    declared = r3_evidence.get("calendar_proofs")
                    if isinstance(declared, Mapping):
                        calendar_proofs = cast(Mapping[str, Mapping[str, object]], declared)
            outcome = verifier.recompute(attempt_directory, calendar_proofs=calendar_proofs)
            component_outcomes[label] = outcome
            if outcome.reasons:
                reasons.append(f"{COMPONENT_VERIFICATION_FAILED}:{label}")
        if r2.authorization_id == r3.authorization_id:
            reasons.append(AUTHORIZATION_ID_REUSED)

        suspension_reference: Mapping[str, object] | None = None
        suspension_raw_fact: Mapping[str, object] | None = None
        if v2:
            declared_reference = composite.get("suspension_reference")
            if not isinstance(declared_reference, Mapping):
                reasons.append(SUSPENSION_REFERENCE_MISSING)
            else:
                file_text = declared_reference.get("file")
                declared_sha = declared_reference.get("sha256")
                if not isinstance(file_text, str) or not isinstance(declared_sha, str):
                    reasons.append(SUSPENSION_REFERENCE_MISSING)
                else:
                    reference_path = self._resolve(file_text)
                    if not reference_path.is_file():
                        reasons.append(SUSPENSION_REFERENCE_MISSING)
                    else:
                        actual_sha = hashlib.sha256(reference_path.read_bytes()).hexdigest()
                        if declared_sha != actual_sha:
                            reasons.append(SUSPENSION_REFERENCE_SHA_MISMATCH)
                        try:
                            parsed = json.loads(reference_path.read_text(encoding="utf-8"))
                        except (json.JSONDecodeError, OSError):
                            parsed = None
                        if not isinstance(parsed, dict):
                            reasons.append(SUSPENSION_REFERENCE_MISSING)
                        else:
                            declared_content = {
                                key: value
                                for key, value in declared_reference.items()
                                if key not in {"file", "sha256"}
                            }
                            if declared_content != parsed:
                                reasons.append("EVIDENCE_SUMMARY_MISMATCH:suspension_reference")
                            parsed_with_sha: dict[str, object] = {
                                **parsed,
                                "sha256": actual_sha,
                            }
                            reference_reasons = validate_k4c_r4_suspension_reference(
                                parsed_with_sha
                            )
                            if reference_reasons:
                                reasons.append(reference_reasons[0])
                            else:
                                suspension_reference = parsed_with_sha
            declared_raw = composite.get("suspension_raw_fact")
            recomputed_raw = k4c_r4_suspension_raw_fact(self._resolve(r3.attempt_directory))
            if recomputed_raw is None:
                reasons.append(SUSPENSION_RAW_FACT_MISSING)
            elif declared_raw != recomputed_raw:
                reasons.append("EVIDENCE_SUMMARY_MISMATCH:suspension_raw_fact")
            else:
                suspension_raw_fact = recomputed_raw

        r2_objectives = cast(
            list[dict[str, object]],
            component_outcomes["R2"].recomputed["observed_objectives"],
        )
        r3_objectives = cast(
            list[dict[str, object]],
            component_outcomes["R3"].recomputed["observed_objectives"],
        )
        rows: dict[str, dict[tuple[str, str], Mapping[str, object]]] = {role: {} for role in _ROLES}
        for outcome in component_outcomes.values():
            component_rows = cast(
                dict[str, dict[tuple[str, str], Mapping[str, object]]],
                outcome.recomputed["reconstructed_rows"],
            )
            for role in _ROLES:
                for key, row in component_rows.get(role, {}).items():
                    existing = rows[role].get(key)
                    if existing is None:
                        rows[role][key] = row
                        continue
                    for field in _FIELDS:
                        if canonical_decimal(
                            decimal_from_number(
                                cast(int | float | Decimal, existing[field]),
                                field=field,
                            )
                        ) != canonical_decimal(
                            decimal_from_number(
                                cast(int | float | Decimal, row[field]),
                                field=field,
                            )
                        ):
                            reasons.append(COMPONENT_ROW_CONFLICT)
                            break
        if self._r3_samples is not None:
            merged, merge_reasons = merge_objective_records_with_r3_reobservation(
                r2_objectives,
                r3_objectives,
                self._r3_samples,
                rows,
                require_suspension_provenance=v2,
                suspension_reference=suspension_reference,
                suspension_raw_fact=suspension_raw_fact,
            )
        else:
            merged, merge_reasons = merge_objective_records(r2_objectives, r3_objectives)
        reasons.extend(merge_reasons)
        statistics = recompute_comparison_statistics(rows)
        coverage = threshold_coverage(rows)
        verification_clean = not any(reason.startswith("COMPONENT") for reason in reasons)
        conflict_free = CONFLICTING_OBJECTIVE_FACTS not in reasons
        sufficient, eligible, verdict_reasons = compute_k4c_composite_verdict(
            merged_objectives=merged,
            verification_clean=verification_clean,
            conflict_free=conflict_free,
            coverage_covered=bool(coverage["covered"]),
        )
        reasons.extend(verdict_reasons)
        recomputed: dict[str, object] = {
            "merged_objectives": merged,
            "statistics": statistics,
            "threshold_coverage": coverage,
            "representative_evidence_sufficient": sufficient,
            "threshold_reacceptance_eligible": eligible,
            "sufficiency_reasons": list(verdict_reasons),
        }
        for field_name in (
            "merged_objectives",
            "statistics",
            "threshold_coverage",
            "representative_evidence_sufficient",
            "threshold_reacceptance_eligible",
        ):
            if composite.get(field_name) != recomputed[field_name]:
                reasons.append(f"EVIDENCE_SUMMARY_MISMATCH:{field_name}")
        if composite.get("sufficiency_reasons") != recomputed["sufficiency_reasons"]:
            reasons.append("EVIDENCE_SUMMARY_MISMATCH:sufficiency_reasons")
        unique = tuple(sorted(set(reasons)))
        structural = tuple(
            reason
            for reason in unique
            if not reason.startswith("OBJECTIVE_UNOBSERVED")
            and reason != COMPONENT_VERIFICATION_FAILED
        )
        return K4CCompositeVerificationOutcome(
            ok=not structural,
            reasons=unique,
            recomputed=recomputed,
        )


# ---------------------------------------------------------------------------
# K4C-R3: supplemental samples for the three unobserved objectives
# ---------------------------------------------------------------------------

K4C_R3_INVOCATION_BUDGET = 9
K4C_R3_OBJECTIVE_NAMES = (
    "suspension_session",
    "low_liquidity_session",
    "missing_or_empty_upstream",
)

_SUSPENSION_EVIDENCE_KINDS = frozenset({"exchange_announcement", "vendor_trading_status"})
_FORBIDDEN_MISSING_EXPLANATIONS = frozenset(
    {
        "invalid_symbol",
        "unsupported_market",
        "future_date",
        "start_after_end",
        "broken_parameters",
        "fault_injection",
        "network_blocked",
    }
)

# User-accepted contract revision (2026-08-16): the transient partial-missing
# sample was replaced by the legal full-range pre-listing empty contract.
LEGAL_EMPTY_PRE_LISTING_OBSERVED = "LEGAL_EMPTY_PRE_LISTING_OBSERVED"
LEGAL_EMPTY_PRE_LISTING_NOT_OBSERVED = "LEGAL_EMPTY_PRE_LISTING_NOT_OBSERVED"
LEGAL_EMPTY_SOURCE_FAILURE = "LEGAL_EMPTY_SOURCE_FAILURE"
LEGAL_EMPTY_ROW_PRESENT = "LEGAL_EMPTY_ROW_PRESENT"
LEGAL_EMPTY_INVOCATION_INCOMPLETE = "LEGAL_EMPTY_INVOCATION_INCOMPLETE"
LEGAL_EMPTY_CALENDAR_PROOF_MISSING = "LEGAL_EMPTY_CALENDAR_PROOF_MISSING"
ZERO_ROW_LEGAL_EMPTY_CONSENSUS = "ZERO_ROW_LEGAL_EMPTY_CONSENSUS"


@dataclass(frozen=True, slots=True)
class K4CR3SampleReference:
    """Frozen independent reference proving one sample fact."""

    source: str
    version_or_file: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be non-empty text")
        if not isinstance(self.version_or_file, str) or not self.version_or_file.strip():
            raise ValueError("version_or_file must be non-empty text")
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise ValueError("sha256 must be a lowercase SHA-256")

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "version_or_file": self.version_or_file,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class K4CR3ObjectiveSample:
    """One pre-fixed supplemental sample; opportunistic coverage is rejected."""

    objective: str
    symbol: str
    trade_date: str
    window_days: int
    exchange: str
    expected_roles: Mapping[str, str]
    reference: K4CR3SampleReference
    success_criterion: str
    failure_reason_code: str
    missing_explanation: str | None = None
    suspension_evidence_kind: str | None = None
    listing_date: str | None = None
    listing_reference: K4CR3SampleReference | None = None
    calendar_reference: K4CR3SampleReference | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "objective": self.objective,
            "symbol": self.symbol,
            "trade_date": self.trade_date,
            "window_days": self.window_days,
            "exchange": self.exchange,
            "expected_roles": dict(sorted(self.expected_roles.items())),
            "reference": self.reference.to_dict(),
            "success_criterion": self.success_criterion,
            "failure_reason_code": self.failure_reason_code,
            "missing_explanation": self.missing_explanation,
            "suspension_evidence_kind": self.suspension_evidence_kind,
            "listing_date": self.listing_date,
            "listing_reference": (
                None if self.listing_reference is None else self.listing_reference.to_dict()
            ),
            "calendar_reference": (
                None if self.calendar_reference is None else self.calendar_reference.to_dict()
            ),
        }


def _sample_window(sample: K4CR3ObjectiveSample) -> tuple[str, str]:
    trade_date = date.fromisoformat(sample.trade_date)
    start = trade_date - timedelta(days=sample.window_days - 1)
    return start.isoformat(), trade_date.isoformat()


_SUSPENSION_REFERENCE_SHA256 = "73e9cbecb769aadcad7f9d2a89276e35951514ee7c4b1c92b7f3a9b1785cb407"
_LOW_LIQUIDITY_REFERENCE_SHA256 = "060aac79c732aa749168c5b6883d7116cb8096dc4ba3dbadcad869e47f112720"
_LEGAL_EMPTY_REFERENCE_SHA256 = "4ebd0410c642bf2d5e813b1d2d86e1e718e603c3cc0157509fb863a74a14b064"
_R3_REFERENCE_DIRECTORY = "docs/stage1/evidence/k4c_r3_references"


def k4c_r3_frozen_samples() -> tuple[K4CR3ObjectiveSample, ...]:
    """Return the three user-accepted frozen samples (contract revision
    2026-08-16).

    Suspension: 688237.SH suspended from 2026-07-16 (exchange announcement,
    2026-07-15). Low liquidity: 688237.SH 2026-07-15 (published week
    statistics). Legal empty: 688825.SH listed 2026-07-27, so the whole
    upstream range for the 2026-07-24 request sits before listing and every
    source must return zero rows (``legal_full_range_empty_pre_listing``).
    """
    legal_empty_reference = K4CR3SampleReference(
        source="sse_listing_announcement_and_2026_holiday_calendar",
        version_or_file=(
            f"{_R3_REFERENCE_DIRECTORY}/legal_empty_688825.SH_listing_2026-07-27.json"
        ),
        sha256=_LEGAL_EMPTY_REFERENCE_SHA256,
    )
    return (
        K4CR3ObjectiveSample(
            objective="suspension_session",
            symbol="688237.SH",
            trade_date="2026-07-16",
            window_days=3,
            exchange="SH",
            expected_roles={role: "MISSING" for role in _ROLES},
            reference=K4CR3SampleReference(
                source="exchange_announcement",
                version_or_file=(f"{_R3_REFERENCE_DIRECTORY}/suspension_688237.SH_2026-07-16.json"),
                sha256=_SUSPENSION_REFERENCE_SHA256,
            ),
            success_criterion="cross_sectional_missing_day",
            failure_reason_code=SUSPENSION_NOT_OBSERVED,
            suspension_evidence_kind="exchange_announcement",
        ),
        K4CR3ObjectiveSample(
            objective="low_liquidity_session",
            symbol="688237.SH",
            trade_date="2026-07-15",
            window_days=1,
            exchange="SH",
            expected_roles={role: "HOLD" for role in _ROLES},
            reference=K4CR3SampleReference(
                source="public_market_statistics",
                version_or_file=(
                    f"{_R3_REFERENCE_DIRECTORY}/low_liquidity_688237.SH_2026-07-15.json"
                ),
                sha256=_LOW_LIQUIDITY_REFERENCE_SHA256,
            ),
            success_criterion="three_source_amount_below_bound",
            failure_reason_code=LOW_LIQUIDITY_NOT_OBSERVED,
        ),
        K4CR3ObjectiveSample(
            objective="missing_or_empty_upstream",
            symbol="688825.SH",
            trade_date="2026-07-24",
            window_days=1,
            exchange="SH",
            expected_roles={role: "EMPTY" for role in _ROLES},
            reference=legal_empty_reference,
            success_criterion="legal_full_range_empty_pre_listing",
            failure_reason_code=LEGAL_EMPTY_PRE_LISTING_NOT_OBSERVED,
            missing_explanation="legal_pre_listing_empty",
            listing_date="2026-07-27",
            listing_reference=legal_empty_reference,
            calendar_reference=legal_empty_reference,
        ),
    )


def validate_k4c_r3_sample(sample: K4CR3ObjectiveSample) -> tuple[str, ...]:
    """Return stable reasons when a sample fails its frozen contract."""
    reasons: list[str] = []
    if sample.objective not in K4C_R3_OBJECTIVE_NAMES:
        reasons.append("R3_SAMPLE_OBJECTIVE_UNKNOWN")
        return tuple(reasons)
    if not isinstance(sample.symbol, str) or not is_supported_a_share(sample.symbol):
        reasons.append("R3_SAMPLE_SYMBOL_INVALID")
    try:
        trade_date = date.fromisoformat(sample.trade_date)
    except ValueError:
        reasons.append("R3_SAMPLE_DATE_INVALID")
        return tuple(reasons)
    if trade_date.isoformat() != sample.trade_date:
        reasons.append("R3_SAMPLE_DATE_INVALID")
    if trade_date > date(2026, 8, 16):
        reasons.append("R3_SAMPLE_DATE_IN_FUTURE")
    if (
        isinstance(sample.window_days, bool)
        or not isinstance(sample.window_days, int)
        or not (1 <= sample.window_days <= 31)
    ):
        reasons.append("R3_SAMPLE_WINDOW_INVALID")
    if sample.exchange not in {"SH", "SZ"}:
        reasons.append("R3_SAMPLE_EXCHANGE_INVALID")
    if isinstance(sample.symbol, str) and not sample.symbol.endswith(f".{sample.exchange}"):
        reasons.append("R3_SAMPLE_EXCHANGE_INVALID")
    if (
        not isinstance(sample.expected_roles, Mapping)
        or set(sample.expected_roles) != set(_ROLES)
        or any(
            role not in _ROLES or state not in {"HOLD", "MISSING", "EMPTY"}
            for role, state in sample.expected_roles.items()
        )
    ):
        reasons.append("R3_SAMPLE_EXPECTED_ROLES_INVALID")
    if sample.objective == "suspension_session":
        if trade_date.weekday() >= 5:
            reasons.append("R3_SAMPLE_WEEKEND_NOT_SUSPENSION")
        if sample.suspension_evidence_kind not in _SUSPENSION_EVIDENCE_KINDS:
            reasons.append("R3_SAMPLE_SUSPENSION_REFERENCE_INVALID")
    if sample.objective == "low_liquidity_session" and any(
        state != "HOLD" for state in sample.expected_roles.values()
    ):
        reasons.append("R3_SAMPLE_LOW_LIQUIDITY_REQUIRES_THREE_HOLDS")
    if sample.objective == "missing_or_empty_upstream":
        if sample.missing_explanation != "legal_pre_listing_empty":
            reasons.append("R3_SAMPLE_MISSING_EXPLANATION_INVALID")
        if sample.missing_explanation in _FORBIDDEN_MISSING_EXPLANATIONS:
            reasons.append("R3_SAMPLE_MANUFACTURED_EMPTY")
        if sample.missing_explanation == "vendor_partial_missing":
            reasons.append("R3_SAMPLE_VENDOR_PARTIAL_MISSING_RETIRED")
        if any(state != "EMPTY" for state in sample.expected_roles.values()):
            reasons.append("R3_SAMPLE_LEGAL_EMPTY_REQUIRES_ALL_EMPTY")
        if sample.listing_reference is None or sample.calendar_reference is None:
            reasons.append("R3_SAMPLE_LISTING_REFERENCE_MISSING")
        if sample.listing_date is None:
            reasons.append("R3_SAMPLE_LISTING_REFERENCE_MISSING")
        else:
            try:
                listing_date = date.fromisoformat(sample.listing_date)
            except ValueError:
                listing_date = None
                reasons.append("R3_SAMPLE_LISTING_DATE_INVALID")
            if listing_date is not None:
                if listing_date.isoformat() != sample.listing_date:
                    reasons.append("R3_SAMPLE_LISTING_DATE_INVALID")
                if listing_date <= trade_date:
                    reasons.append("R3_SAMPLE_WINDOW_CROSSES_LISTING")
        if trade_date.weekday() >= 5:
            reasons.append("R3_SAMPLE_WEEKEND_NOT_PRE_LISTING_EMPTY")
    return tuple(sorted(set(reasons)))


def k4c_r3_plan_invocations(
    samples: Sequence[K4CR3ObjectiveSample],
) -> tuple[K4CProbeInvocation, ...]:
    """Freeze the nine R3 invocations from the samples (A/B/C per objective)."""
    invocations: list[K4CProbeInvocation] = []
    for sample in samples:
        start, end = _sample_window(sample)
        for upstream in (DataUpstream.BAOSTOCK, DataUpstream.TENCENT, DataUpstream.SINA):
            role = _ROLE_BY_UPSTREAM[upstream.value]
            provider = (
                DataProvider.BAOSTOCK if upstream is DataUpstream.BAOSTOCK else DataProvider.AKSHARE
            )
            target_start = date.fromisoformat(start)
            target_end = date.fromisoformat(end)
            invocations.append(
                K4CProbeInvocation(
                    role=role,
                    provider=provider,
                    upstream=upstream,
                    symbol=sample.symbol,
                    target_start=target_start,
                    target_end=target_end,
                    upstream_start=target_start - timedelta(days=15),
                    upstream_end=target_end,
                )
            )
    return tuple(invocations)


def k4c_r3_plan_payload(
    samples: Sequence[K4CR3ObjectiveSample],
) -> dict[str, object]:
    """Build the canonical R3 plan sub-object; rejects unfixed samples."""
    sample_tuple = tuple(samples)
    covered = {sample.objective for sample in sample_tuple}
    if set(K4C_R3_OBJECTIVE_NAMES) != covered:
        raise ValueError("R3 plan must cover exactly the three missing objectives")
    for sample in sample_tuple:
        reasons = validate_k4c_r3_sample(sample)
        if reasons:
            raise ValueError(f"R3 sample invalid for {sample.objective}: {reasons}")
        if not sample.symbol or not sample.trade_date:
            raise ValueError("R3 samples must fix an exact symbol and trade date")
    invocations = k4c_r3_plan_invocations(sample_tuple)
    a_count = sum(1 for item in invocations if item.upstream is DataUpstream.BAOSTOCK)
    b_count = sum(1 for item in invocations if item.upstream is DataUpstream.TENCENT)
    c_count = sum(1 for item in invocations if item.upstream is DataUpstream.SINA)
    return {
        "authorization_marker": K4C_R3_MARKER,
        "dataset": "daily_bar_unadjusted",
        "schema": "daily_bar_unadjusted.v1",
        "objective_samples": [sample.to_dict() for sample in sample_tuple],
        "symbols": sorted({sample.symbol for sample in sample_tuple}),
        "invocations": [item.to_dict() for item in invocations],
        "invocation_budget": K4C_R3_INVOCATION_BUDGET,
        "network_budget": {
            "max_endpoint_invocations": K4C_R3_INVOCATION_BUDGET,
            "a_baostock_endpoint_invocations": a_count,
            "b_tencent_endpoint_invocations": b_count,
            "max_c_sina_endpoint_invocations": c_count,
            "estimated_max_baostock_protocol_messages": a_count * 3,
            "estimated_max_akshare_http_requests": b_count * 3 + c_count * 2,
            "application_retries": K4C_R2_APPLICATION_RETRIES,
            "fail_fast": K4C_R2_FAIL_FAST,
        },
        "hard_timeouts_seconds": K4C_R2_HARD_TIMEOUTS,
        "unit_policies": {
            "baostock": "baostock-0.9.3-returned-units.v1",
            "akshare": "akshare-1.18.83-returned-units.v1",
        },
    }


def k4c_r3_plan_sha256(samples: Sequence[K4CR3ObjectiveSample]) -> str:
    """Return the plan hash bound to the R3 marker for the given samples."""
    return _sha256_text(_canonical_json(k4c_r3_plan_payload(samples)))


def k4c_r3_dry_run_payload(
    samples: Sequence[K4CR3ObjectiveSample],
) -> dict[str, object]:
    """Return the zero-network R3 dry-run referencing the frozen R2 component."""
    return {
        "schema_version": K4C_R3_DRY_RUN_SCHEMA,
        "authorization_marker": K4C_R3_MARKER,
        "plan_sha256": k4c_r3_plan_sha256(samples),
        "plan": k4c_r3_plan_payload(samples),
        "r2_component": r2_component_reference().to_dict(),
        "authorization": {
            "authorization_id_format": "K4C-R3-<12-hex>",
            "one_attempt_per_authorization_id": True,
            "replay_rejection_before_first_call": True,
            "evidence_output_root": "docs/stage1/evidence/k4c-r3-attempts",
            "formal_data_root_writes": False,
        },
        "application_retries": K4C_R2_APPLICATION_RETRIES,
        "fail_fast": K4C_R2_FAIL_FAST,
        "requires_real_network_authorization": True,
    }


def validate_k4c_r3_objective_records(
    objectives: Sequence[Mapping[str, object]],
    samples: Sequence[K4CR3ObjectiveSample],
) -> tuple[str, ...]:
    """Reject observed records that reference symbols/dates outside the samples."""
    reasons: list[str] = []
    by_objective: dict[str, list[K4CR3ObjectiveSample]] = {}
    for sample in samples:
        by_objective.setdefault(sample.objective, []).append(sample)
    for record in objectives:
        if record.get("observed") is not True:
            continue
        objective = str(record.get("objective"))
        symbol = record.get("symbol")
        trade_date = record.get("trade_date")
        candidates = by_objective.get(objective, [])
        if not any(
            symbol == sample.symbol
            and isinstance(trade_date, str)
            and _sample_window(sample)[0] <= trade_date <= _sample_window(sample)[1]
            for sample in candidates
        ):
            reasons.append(f"OBJECTIVE_OUT_OF_PLAN:{objective}")
    return tuple(sorted(set(reasons)))


def _r3_invocation_statuses(facts: Mapping[str, object]) -> list[dict[str, object]]:
    """Pair journal outcomes with envelope facts for R3 legal-empty checks."""
    journal = cast(list[dict[str, object]], facts.get("journal") or [])
    contract_facts = cast(list[dict[str, object]], facts.get("contract_facts") or [])
    statuses: list[dict[str, object]] = []
    for item in contract_facts:
        sequence = item.get("sequence")
        journal_record = next(
            (record for record in journal if record.get("sequence") == sequence), None
        )
        outcome = journal_record.get("outcome") if isinstance(journal_record, Mapping) else None
        statuses.append(
            {
                "symbol": item.get("symbol"),
                "role": item.get("role"),
                "status": outcome.get("status") if isinstance(outcome, Mapping) else None,
                "complete": item.get("complete"),
                "upstream_row_count": item.get("upstream_row_count"),
            }
        )
    return statuses


def validate_k4c_r4_suspension_reference(
    reference: Mapping[str, object],
) -> tuple[str, ...]:
    """Validate the R4 suspension provenance reference content (no file I/O).

    The reference must be the official SSE exchange announcement for
    688237.SH suspension from 2026-07-16 (announcement number 2026-031) and
    must be explicitly marked ``provenance_hardening_only``. Media URLs can
    never satisfy ``exchange_announcement``.
    """
    reasons: list[str] = []
    if not isinstance(reference, Mapping):
        return (SUSPENSION_REFERENCE_INVALID_TYPE,)
    if reference.get("symbol") != K4C_R4_SUSPENSION_SYMBOL:
        reasons.append(SUSPENSION_REFERENCE_SYMBOL_MISMATCH)
    if reference.get("suspension_start") != K4C_R4_SUSPENSION_DATE:
        reasons.append(SUSPENSION_REFERENCE_DATE_MISMATCH)
    if reference.get("announcement_number") != K4C_R4_SUSPENSION_ANNOUNCEMENT_NUMBER:
        reasons.append(SUSPENSION_REFERENCE_ANNOUNCEMENT_MISMATCH)
    if reference.get("source_type") != "exchange_announcement":
        reasons.append(SUSPENSION_REFERENCE_SOURCE_TYPE_INVALID)
    url = reference.get("announcement_url")
    if isinstance(url, str) and url.strip():
        host = urlparse(str(url)).netloc.lower()
        if host != "sse.com.cn" and not host.endswith(".sse.com.cn"):
            reasons.append(SUSPENSION_REFERENCE_URL_INVALID)
    else:
        reasons.append(SUSPENSION_REFERENCE_URL_INVALID)
    sha = reference.get("sha256")
    if (
        not isinstance(sha, str)
        or len(sha) != 64
        or any(character not in "0123456789abcdef" for character in sha)
    ):
        reasons.append(SUSPENSION_REFERENCE_SHA_INVALID)
    if reference.get("provenance_hardening_only") is not True:
        reasons.append(SUSPENSION_REFERENCE_PROVENANCE_FLAG_INVALID)
    return tuple(sorted(set(reasons)))


def k4c_r4_suspension_raw_fact(attempt_directory: Path) -> dict[str, object] | None:
    """Extract the BaoStock suspended-day row from the R3 raw sequence 1."""
    if not isinstance(attempt_directory, Path):
        raise TypeError("attempt_directory must be a Path")
    path = attempt_directory / f"{_RAW_DIRECTORY}/1.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    provider_payload = payload.get("provider_payload")
    if not isinstance(provider_payload, Mapping):
        return None
    fields = provider_payload.get("fields")
    rows = provider_payload.get("rows")
    if not isinstance(fields, list) or not isinstance(rows, list):
        return None
    if "tradestatus" not in fields or "date" not in fields:
        return None
    date_index = fields.index("date")
    tradestatus_index = fields.index("tradestatus")
    volume_index = fields.index("volume") if "volume" in fields else None
    amount_index = fields.index("amount") if "amount" in fields else None
    for row in rows:
        if not isinstance(row, list) or str(row[date_index])[:10] != K4C_R4_SUSPENSION_DATE:
            continue
        return {
            "date": str(row[date_index])[:10],
            "tradestatus": str(row[tradestatus_index]) if len(row) > tradestatus_index else "",
            "volume": (
                str(row[volume_index])
                if volume_index is not None and len(row) > volume_index
                else None
            ),
            "amount": (
                str(row[amount_index])
                if amount_index is not None and len(row) > amount_index
                else None
            ),
        }
    return None


def _r3_unobserved(sample: K4CR3ObjectiveSample, reason: str) -> dict[str, object]:
    return {
        "objective": sample.objective,
        "observed": False,
        "symbol": None,
        "trade_date": None,
        "roles": [],
        "evidence_type": None,
        "fact": {},
        "reason_code": reason,
    }


def _observe_r3_sample(
    sample: K4CR3ObjectiveSample,
    rows_by_role: Mapping[str, Mapping[tuple[str, str], Mapping[str, object]]],
    invocation_statuses: Sequence[Mapping[str, object]],
    calendar_proofs: Mapping[str, Mapping[str, object]],
    *,
    require_suspension_provenance: bool = False,
    suspension_reference: Mapping[str, object] | None = None,
    suspension_raw_fact: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if sample.objective == "suspension_session":
        dates_by_symbol: dict[str, set[str]] = {}
        for role_rows in rows_by_role.values():
            for symbol, trade_date in role_rows:
                dates_by_symbol.setdefault(symbol, set()).add(trade_date)
        if sample.trade_date not in dates_by_symbol.get(sample.symbol, set()):
            counter = next(
                (
                    other
                    for other in sorted(dates_by_symbol)
                    if other != sample.symbol
                    and sample.trade_date in dates_by_symbol[other]
                    and _all_three_hold(rows_by_role, (other, sample.trade_date))
                ),
                None,
            )
            if counter is not None:
                if require_suspension_provenance:
                    if suspension_reference is None:
                        return _r3_unobserved(sample, SUSPENSION_REFERENCE_MISSING)
                    reference_reasons = validate_k4c_r4_suspension_reference(suspension_reference)
                    if reference_reasons:
                        return _r3_unobserved(sample, reference_reasons[0])
                    if suspension_raw_fact is None:
                        return _r3_unobserved(sample, SUSPENSION_RAW_FACT_MISSING)
                    if (
                        suspension_raw_fact.get("tradestatus") != "0"
                        or suspension_raw_fact.get("volume") != ""
                        or suspension_raw_fact.get("amount") != ""
                    ):
                        return _r3_unobserved(sample, SUSPENSION_RAW_STATUS_MISMATCH)
                    return {
                        "objective": sample.objective,
                        "observed": True,
                        "symbol": sample.symbol,
                        "trade_date": sample.trade_date,
                        "roles": [],
                        "evidence_type": "cross_sectional_missing_day",
                        "fact": {
                            "missing_symbol": sample.symbol,
                            "missing_roles": list(_ROLES),
                            "counter_example_symbol": counter,
                            "suspension_evidence_kind": "exchange_announcement",
                            "suspension_announcement_number": suspension_reference.get(
                                "announcement_number"
                            ),
                            "suspension_reference_sha256": suspension_reference.get("sha256"),
                            "suspension_raw_fact": dict(suspension_raw_fact),
                        },
                        "reason_code": None,
                    }
                return {
                    "objective": sample.objective,
                    "observed": True,
                    "symbol": sample.symbol,
                    "trade_date": sample.trade_date,
                    "roles": [],
                    "evidence_type": "cross_sectional_missing_day",
                    "fact": {
                        "missing_symbol": sample.symbol,
                        "missing_roles": list(_ROLES),
                        "counter_example_symbol": counter,
                        "suspension_evidence_kind": sample.suspension_evidence_kind,
                    },
                    "reason_code": None,
                }
        return _r3_unobserved(sample, SUSPENSION_NOT_OBSERVED)
    if sample.objective == "low_liquidity_session":
        key = (sample.symbol, sample.trade_date)
        if _all_three_hold(rows_by_role, key):
            fact = _row_fact(rows_by_role, key, ("amount",))
            if all(
                Decimal(fact["amount"][role]) <= LOW_LIQUIDITY_AMOUNT_BOUND_YUAN for role in _ROLES
            ):
                return {
                    "objective": sample.objective,
                    "observed": True,
                    "symbol": sample.symbol,
                    "trade_date": sample.trade_date,
                    "roles": list(_ROLES),
                    "evidence_type": "held_row_fact",
                    "fact": {
                        **fact,
                        "bound_yuan": canonical_decimal(LOW_LIQUIDITY_AMOUNT_BOUND_YUAN),
                    },
                    "reason_code": None,
                }
        return _r3_unobserved(sample, LOW_LIQUIDITY_NOT_OBSERVED)
    if sample.objective == "missing_or_empty_upstream":
        statuses = [
            status for status in invocation_statuses if status.get("symbol") == sample.symbol
        ]
        by_role = {str(status.get("role")): status for status in statuses}
        if set(by_role) != set(_ROLES):
            return _r3_unobserved(sample, LEGAL_EMPTY_INVOCATION_INCOMPLETE)
        for role in _ROLES:
            status = by_role[role]
            if status.get("status") != "SUCCEEDED":
                return _r3_unobserved(sample, LEGAL_EMPTY_SOURCE_FAILURE)
            if status.get("complete") is not True:
                return _r3_unobserved(sample, LEGAL_EMPTY_INVOCATION_INCOMPLETE)
            if status.get("upstream_row_count") != 0:
                return _r3_unobserved(sample, LEGAL_EMPTY_ROW_PRESENT)
        for role_rows in rows_by_role.values():
            if any(symbol == sample.symbol for symbol, _ in role_rows):
                return _r3_unobserved(sample, LEGAL_EMPTY_ROW_PRESENT)
        proof = calendar_proofs.get(sample.symbol)
        if not isinstance(proof, Mapping) or proof.get("no_expected_trading_sessions") is not True:
            return _r3_unobserved(sample, LEGAL_EMPTY_CALENDAR_PROOF_MISSING)
        return {
            "objective": sample.objective,
            "observed": True,
            "symbol": sample.symbol,
            "trade_date": sample.trade_date,
            "roles": list(_ROLES),
            "evidence_type": "legal_full_range_empty_pre_listing",
            "fact": {
                "success_reason_code": LEGAL_EMPTY_PRE_LISTING_OBSERVED,
                "zero_row_reason_code": ZERO_ROW_LEGAL_EMPTY_CONSENSUS,
                "listing_date": sample.listing_date,
                "roles": list(_ROLES),
                "upstream_row_count": 0,
                "calendar_proof": dict(proof),
            },
            "reason_code": None,
        }
    raise ValueError("R3 objective is unsupported")


def observe_k4c_r3_objectives(
    samples: Sequence[K4CR3ObjectiveSample],
    *,
    rows_by_role: Mapping[str, Mapping[tuple[str, str], Mapping[str, object]]],
    empty_invocations: Sequence[Mapping[str, object]] = (),
    invocation_statuses: Sequence[Mapping[str, object]] = (),
    calendar_proofs: Mapping[str, Mapping[str, object]] | None = None,
) -> tuple[dict[str, object], ...]:
    """Evaluate the frozen R3 samples only; opportunistic coverage is rejected.

    ``empty_invocations`` is accepted for signature symmetry with the R2
    observer but is not used: the legal-empty contract requires per-invocation
    ``status``/``complete``/``upstream_row_count`` facts from the journal and
    raw envelopes, plus an independent calendar proof from the frozen
    reference. Suspension and pre-listing emptiness are distinct objectives
    with distinct samples and can never count for each other.
    """
    del empty_invocations
    if not isinstance(rows_by_role, Mapping):
        raise TypeError("rows_by_role must be a mapping")
    proofs = {} if calendar_proofs is None else dict(calendar_proofs)
    return tuple(
        _observe_r3_sample(sample, rows_by_role, tuple(invocation_statuses), proofs)
        for sample in samples
    )


def compute_k4c_r3_sufficiency(
    *,
    invocation_count: int,
    budget: int,
    failure: object,
    objectives: Sequence[Mapping[str, object]],
    contract_checks: Mapping[str, bool],
    comparison_verified: bool,
    unrecorded_attempts: int,
    unauthorized_calls: int = 0,
) -> tuple[bool, tuple[str, ...]]:
    """Return ``(sufficient, reasons)`` for the fixed R3 plan; never call
    before measurement."""
    reasons: list[str] = []
    if failure is not None:
        reasons.append("FAILURE_PRESENT")
    if invocation_count != budget:
        reasons.append("INVOCATIONS_INCOMPLETE")
    if unauthorized_calls:
        reasons.append("UNAUTHORIZED_CALL")
    for name in K4C_R3_OBJECTIVE_NAMES:
        if not any(
            record.get("objective") == name and record.get("observed") is True
            for record in objectives
        ):
            reasons.append(f"OBJECTIVE_UNOBSERVED:{name}")
    for name, ok in sorted(contract_checks.items()):
        if not ok:
            reasons.append(f"CONTRACT_CHECK_FAILED:{name}")
    if not comparison_verified:
        reasons.append("COMPARISON_NOT_VERIFIED")
    if unrecorded_attempts:
        reasons.append("UNRECORDED_ATTEMPT")
    return (not reasons, tuple(reasons))


__all__ = [
    "ATTEMPT_NOT_OPEN",
    "AUTHORIZATION_ALREADY_CONSUMED",
    "AUTHORIZATION_ID_REUSED",
    "BLOCKED_MISSING_EMPTY_SAMPLE",
    "BLOCKED_SAMPLE_SELECTION",
    "COMPONENT_EVIDENCE_SHA_MISMATCH",
    "COMPONENT_HEADER_MISMATCH",
    "COMPONENT_MANIFEST_SHA_MISMATCH",
    "COMPONENT_ROW_CONFLICT",
    "COMPONENT_VERIFICATION_FAILED",
    "COMPOSITE_CONTRACT_VERSION_MISMATCH",
    "COMPOSITE_SCHEMA_MISMATCH",
    "CONFLICTING_OBJECTIVE_FACTS",
    "EVIDENCE_ARTIFACT_ROOT_MISMATCH",
    "EVIDENCE_CALENDAR_PROOF_MISMATCH",
    "EVIDENCE_MANIFEST_SHA_MISMATCH",
    "EVIDENCE_PLANNED_OBJECTIVES_MISMATCH",
    "EVIDENCE_SCHEMA_MISMATCH",
    "EX_RIGHTS_GAP_NOT_OBSERVED",
    "EX_RIGHTS_SYMBOLS",
    "EX_RIGHTS_VENDOR_PRECLOSE_RELATIVE_BOUND",
    "INVALID_ATTEMPT_HEADER",
    "INVALID_ATTEMPT_INPUT",
    "JOURNAL_RAW_SHA_MISMATCH",
    "K4C_COMPOSITE_EVIDENCE_SCHEMA",
    "K4C_R2_APPLICATION_RETRIES",
    "K4C_R2_ARTIFACT_MANIFEST_SCHEMA",
    "K4C_R2_ATTEMPT_HEADER_SCHEMA",
    "K4C_R2_COMPARISON_SCHEMA",
    "K4C_R2_DRY_RUN_SCHEMA",
    "K4C_R2_EVIDENCE_SCHEMA",
    "K4C_R2_FAIL_FAST",
    "K4C_R2_HARD_TIMEOUTS",
    "K4C_R2_INVOCATION_BUDGET",
    "K4C_R2_INVOCATION_RECORD_SCHEMA",
    "K4C_R2_MARKER",
    "K4C_R2_OBJECTIVES",
    "K4C_R2_RAW_SCHEMA",
    "K4C_R3_CALENDAR_REFERENCE_SHA256",
    "K4C_R3_DRY_RUN_SCHEMA",
    "K4C_R3_EVIDENCE_SCHEMA",
    "K4C_R3_INVOCATION_BUDGET",
    "K4C_R3_LEGAL_EMPTY_REASON",
    "K4C_R3_MARKER",
    "K4C_R3_OBJECTIVE_NAMES",
    "K4C_R4_COMPOSITE_EVIDENCE_SCHEMA",
    "K4C_R4_SUSPENSION_ANNOUNCEMENT_NUMBER",
    "K4C_R4_SUSPENSION_DATE",
    "K4C_R4_SUSPENSION_SYMBOL",
    "K4C_R4_VERIFICATION_CONTRACT_VERSION",
    "LEGAL_EMPTY_CALENDAR_PROOF_MISSING",
    "LEGAL_EMPTY_INVOCATION_INCOMPLETE",
    "LEGAL_EMPTY_PRE_LISTING_NOT_OBSERVED",
    "LEGAL_EMPTY_PRE_LISTING_OBSERVED",
    "LEGAL_EMPTY_ROW_PRESENT",
    "LEGAL_EMPTY_SOURCE_FAILURE",
    "LOW_LIQUIDITY_AMOUNT_BOUND_YUAN",
    "LOW_LIQUIDITY_NOT_OBSERVED",
    "MARKER_NOT_FROZEN",
    "NORMAL_SESSION_NOT_OBSERVED",
    "NO_MISSING_OR_EMPTY_OBSERVED",
    "R2_ARTIFACT_MANIFEST_SHA256",
    "R2_ATTEMPT_ID",
    "R2_AUTHORIZATION_ID",
    "R2_EVIDENCE_SHA256",
    "R3_ARTIFACT_MANIFEST_SHA256",
    "R3_ATTEMPT_ID",
    "R3_AUTHORIZATION_ID",
    "R3_EVIDENCE_SHA256",
    "RAW_COMPARISON_KEY_MISMATCH",
    "RAW_COMPARISON_VALUE_MISMATCH",
    "RAW_PLAN_BINDING_MISMATCH",
    "RAW_PRE_CLOSE_RECONSTRUCTION_MISMATCH",
    "RAW_UNIT_RECONSTRUCTION_MISMATCH",
    "SHANGHAI_SEGMENT_NOT_OBSERVED",
    "SHENZHEN_SEGMENT_NOT_OBSERVED",
    "SUSPENSION_NOT_OBSERVED",
    "SUSPENSION_RAW_FACT_MISSING",
    "SUSPENSION_RAW_STATUS_MISMATCH",
    "SUSPENSION_REFERENCE_ANNOUNCEMENT_MISMATCH",
    "SUSPENSION_REFERENCE_DATE_MISMATCH",
    "SUSPENSION_REFERENCE_INVALID_TYPE",
    "SUSPENSION_REFERENCE_MISSING",
    "SUSPENSION_REFERENCE_PROVENANCE_FLAG_INVALID",
    "SUSPENSION_REFERENCE_SHA_INVALID",
    "SUSPENSION_REFERENCE_SHA_MISMATCH",
    "SUSPENSION_REFERENCE_SOURCE_TYPE_INVALID",
    "SUSPENSION_REFERENCE_SYMBOL_MISMATCH",
    "SUSPENSION_REFERENCE_URL_INVALID",
    "UNREFERENCED_RAW_ARTIFACT",
    "VOLUME_AMOUNT_BOUNDARY_AMOUNT_YUAN",
    "VOLUME_AMOUNT_BOUNDARY_NOT_OBSERVED",
    "ZERO_ROW_LEGAL_EMPTY_CONSENSUS",
    "K4CAttemptHeader",
    "K4CAttemptLedger",
    "K4CComponentReference",
    "K4CCompositeVerificationOutcome",
    "K4CCompositeVerifier",
    "K4CLedgerError",
    "K4CObjectiveDefinition",
    "K4CR3ObjectiveSample",
    "K4CR3SampleReference",
    "K4CVerificationOutcome",
    "K4CVerifier",
    "active_probe_markers",
    "artifact_manifest_payload",
    "closure_reasons",
    "comparison_rows_payload",
    "compute_k4c_composite_verdict",
    "compute_k4c_r2_sufficiency",
    "compute_k4c_r3_sufficiency",
    "frozen_k4c_r2_plan_payload",
    "k4c_r2_dry_run_payload",
    "k4c_r2_objective_names",
    "k4c_r2_plan_sha256",
    "k4c_r3_dry_run_payload",
    "k4c_r3_frozen_samples",
    "k4c_r3_plan_invocations",
    "k4c_r3_plan_payload",
    "k4c_r3_plan_sha256",
    "k4c_r4_suspension_raw_fact",
    "load_k4c_r2_artifacts",
    "merge_objective_records",
    "merge_objective_records_with_r3_reobservation",
    "observe_k4c_r2_objectives",
    "observe_k4c_r3_objectives",
    "r2_component_reference",
    "r3_component_reference",
    "recompute_comparison_statistics",
    "reconstruct_canonical_rows_from_raw",
    "rows_from_comparison_payload",
    "threshold_coverage",
    "validate_k4c_r2_objective_records",
    "validate_k4c_r3_objective_records",
    "validate_k4c_r3_sample",
    "validate_k4c_r4_suspension_reference",
]
