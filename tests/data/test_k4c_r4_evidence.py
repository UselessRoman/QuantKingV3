"""S1-07K4C-R4 reverse tests: evidence binding checks, composite v2 contract
and suspension provenance closure.

Everything here is offline: the real R2/R3 attempts and the frozen v1/v2
composite files are read-only inputs, tampering happens on in-memory copies,
and the suspension provenance machinery is exercised against the real
BaoStock raw rows and the official SSE reference binding.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from tests.data.test_k4c_r1_evidence import _ROLES, _row

from quantking.data.comparison.k4c_evidence import (
    COMPOSITE_CONTRACT_VERSION_MISMATCH,
    COMPOSITE_SCHEMA_MISMATCH,
    EVIDENCE_ARTIFACT_ROOT_MISMATCH,
    EVIDENCE_CALENDAR_PROOF_MISMATCH,
    EVIDENCE_MANIFEST_SHA_MISMATCH,
    EVIDENCE_PLANNED_OBJECTIVES_MISMATCH,
    EVIDENCE_SCHEMA_MISMATCH,
    K4C_R3_INVOCATION_BUDGET,
    K4C_R3_MARKER,
    K4C_R4_COMPOSITE_EVIDENCE_SCHEMA,
    SUSPENSION_NOT_OBSERVED,
    SUSPENSION_RAW_FACT_MISSING,
    SUSPENSION_RAW_STATUS_MISMATCH,
    SUSPENSION_REFERENCE_ANNOUNCEMENT_MISMATCH,
    SUSPENSION_REFERENCE_DATE_MISMATCH,
    SUSPENSION_REFERENCE_INVALID_TYPE,
    SUSPENSION_REFERENCE_MISSING,
    SUSPENSION_REFERENCE_PROVENANCE_FLAG_INVALID,
    SUSPENSION_REFERENCE_SHA_INVALID,
    SUSPENSION_REFERENCE_SHA_MISMATCH,
    SUSPENSION_REFERENCE_SOURCE_TYPE_INVALID,
    SUSPENSION_REFERENCE_SYMBOL_MISMATCH,
    SUSPENSION_REFERENCE_URL_INVALID,
    K4CCompositeVerifier,
    K4CVerifier,
    k4c_r3_frozen_samples,
    k4c_r3_plan_invocations,
    k4c_r3_plan_sha256,
    k4c_r4_suspension_raw_fact,
    merge_objective_records_with_r3_reobservation,
    r2_component_reference,
    r3_component_reference,
    rows_from_comparison_payload,
    validate_k4c_r4_suspension_reference,
)
from quantking.data.comparison.thresholds import (
    K4_THRESHOLD_RULE_VERSION,
    k4_evidence_thresholds,
    k4_threshold_acceptance,
)
from quantking.data.models import DatasetKind

_ROOT = Path(__file__).resolve().parents[2]
_R3_ATTEMPT = _ROOT / "docs/stage1/evidence/k4c-r3-attempts/K4C-R3-2b8e5f14a6c9"
_R3_EVIDENCE = _ROOT / "docs/stage1/evidence/k4c_r3_representative_probe_2026-08-16.json"
_R4_COMPOSITE = _ROOT / "docs/stage1/evidence/k4c_r4_composite_evidence_2026-08-16.json"
_SUSPENSION_REFERENCE = (
    _ROOT / "docs/stage1/evidence/k4c_r4_references/suspension_688237.SH_2026-07-16_sse.json"
)


def _r3_verifier() -> K4CVerifier:
    samples = k4c_r3_frozen_samples()
    return K4CVerifier(
        plan=k4c_r3_plan_invocations(samples),
        marker=K4C_R3_MARKER,
        plan_sha256=k4c_r3_plan_sha256(samples),
        invocation_budget=K4C_R3_INVOCATION_BUDGET,
        r3_samples=samples,
    )


def _load_r3_evidence() -> dict[str, object]:
    return json.loads(_R3_EVIDENCE.read_text(encoding="utf-8"))


def _r4_verifier() -> K4CCompositeVerifier:
    samples = k4c_r3_frozen_samples()
    return K4CCompositeVerifier(
        root=_ROOT,
        r3_plan=k4c_r3_plan_invocations(samples),
        r3_marker=K4C_R3_MARKER,
        r3_plan_sha256=k4c_r3_plan_sha256(samples),
        r3_invocation_budget=K4C_R3_INVOCATION_BUDGET,
        r3_samples=samples,
        schema_version=K4C_R4_COMPOSITE_EVIDENCE_SCHEMA,
    )


def _load_r4_composite() -> dict[str, object]:
    return json.loads(_R4_COMPOSITE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# R3 evidence binding checks (K4CVerifier.verify)
# ---------------------------------------------------------------------------


def test_real_r3_evidence_passes_the_full_binding_check() -> None:
    outcome = _r3_verifier().verify(attempt_directory=_R3_ATTEMPT, evidence=_load_r3_evidence())
    assert outcome.ok is True
    assert outcome.reasons == ()


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("schema_version", "forged-evidence.v9", EVIDENCE_SCHEMA_MISMATCH),
        (
            "authorization_id",
            "K4C-R3-forged000000",
            "EVIDENCE_HEADER_BINDING_MISMATCH:authorization_id",
        ),
        ("attempt_id", "attempt-forged00000001", "EVIDENCE_HEADER_BINDING_MISMATCH:attempt_id"),
        (
            "authorization_marker",
            "K4C-FORGED-MARKER",
            "EVIDENCE_HEADER_BINDING_MISMATCH:authorization_marker",
        ),
        ("plan_sha256", "f" * 64, "EVIDENCE_HEADER_BINDING_MISMATCH:plan_sha256"),
        ("invocation_budget", 99, "EVIDENCE_HEADER_BINDING_MISMATCH:invocation_budget"),
        ("artifact_manifest_sha256", "f" * 64, EVIDENCE_MANIFEST_SHA_MISMATCH),
        ("artifacts_root", "E:\\forged\\attempt", EVIDENCE_ARTIFACT_ROOT_MISMATCH),
        (
            "application_retries",
            1,
            "EVIDENCE_CONTRACT_BINDING_MISMATCH:application_retries",
        ),
        ("fail_fast", False, "EVIDENCE_CONTRACT_BINDING_MISMATCH:fail_fast"),
        (
            "writes_formal_data_root",
            True,
            "EVIDENCE_CONTRACT_BINDING_MISMATCH:writes_formal_data_root",
        ),
        (
            "failure",
            {"stable_code": "FORGED", "error_type": "Forged"},
            "EVIDENCE_CONTRACT_BINDING_MISMATCH:failure",
        ),
        ("sufficiency_reasons", [], "EVIDENCE_SUMMARY_MISMATCH:sufficiency_reasons"),
        ("planned_objectives", [], EVIDENCE_PLANNED_OBJECTIVES_MISMATCH),
        ("calendar_proofs", {}, EVIDENCE_CALENDAR_PROOF_MISMATCH),
    ],
)
def test_r3_evidence_field_tamper_is_rejected(field: str, value: object, expected: str) -> None:
    evidence = _load_r3_evidence()
    evidence[field] = value
    outcome = _r3_verifier().verify(attempt_directory=_R3_ATTEMPT, evidence=evidence)
    assert outcome.ok is False
    assert expected in outcome.reasons


def test_r3_evidence_unknown_field_is_rejected() -> None:
    evidence = _load_r3_evidence()
    evidence["forged_field"] = 42
    outcome = _r3_verifier().verify(attempt_directory=_R3_ATTEMPT, evidence=evidence)
    assert outcome.ok is False
    assert "EVIDENCE_UNKNOWN_FIELD:forged_field" in outcome.reasons


def test_r3_evidence_missing_field_is_rejected() -> None:
    evidence = _load_r3_evidence()
    del evidence["authorization_id"]
    outcome = _r3_verifier().verify(attempt_directory=_R3_ATTEMPT, evidence=evidence)
    assert outcome.ok is False
    assert "EVIDENCE_HEADER_BINDING_MISMATCH:authorization_id" in outcome.reasons


# ---------------------------------------------------------------------------
# Composite v2 contract (K4CCompositeVerifier)
# ---------------------------------------------------------------------------


def test_real_r4_composite_verifies_offline() -> None:
    outcome = _r4_verifier().verify(
        r2=r2_component_reference(),
        r3=r3_component_reference(),
        composite=_load_r4_composite(),
    )
    assert outcome.ok is True
    assert outcome.recomputed["representative_evidence_sufficient"] is True
    assert outcome.recomputed["threshold_reacceptance_eligible"] is True
    merged = outcome.recomputed["merged_objectives"]
    assert len(merged) == 8
    assert all(record["observed"] is True for record in merged)
    assert outcome.recomputed["statistics"]["held_rows_total"] == 248
    assert outcome.recomputed["threshold_coverage"]["covered"] is True


@pytest.mark.parametrize("component", ["r2", "r3"])
@pytest.mark.parametrize(
    "field",
    [
        "plan_sha256",
        "authorization_id",
        "attempt_id",
        "evidence_sha256",
        "artifact_manifest_sha256",
        "attempt_directory",
        "evidence_file",
        "component",
    ],
)
def test_composite_component_field_tamper_is_rejected(component: str, field: str) -> None:
    composite = _load_r4_composite()
    composite[f"{component}_component"][field] = "forged"
    outcome = _r4_verifier().verify(
        r2=r2_component_reference(),
        r3=r3_component_reference(),
        composite=composite,
    )
    assert outcome.ok is False
    assert f"COMPOSITE_COMPONENT_REFERENCE_MISMATCH:{component.upper()}" in outcome.reasons


def test_composite_schema_tamper_is_rejected() -> None:
    composite = _load_r4_composite()
    composite["schema_version"] = "s1-07k4c-composite-evidence.v1"
    outcome = _r4_verifier().verify(
        r2=r2_component_reference(),
        r3=r3_component_reference(),
        composite=composite,
    )
    assert outcome.ok is False
    assert COMPOSITE_SCHEMA_MISMATCH in outcome.reasons


def test_composite_sufficiency_reasons_tamper_is_rejected() -> None:
    composite = _load_r4_composite()
    composite["sufficiency_reasons"] = ["FORGED"]
    outcome = _r4_verifier().verify(
        r2=r2_component_reference(),
        r3=r3_component_reference(),
        composite=composite,
    )
    assert outcome.ok is False
    assert "EVIDENCE_SUMMARY_MISMATCH:sufficiency_reasons" in outcome.reasons


def test_composite_deleted_component_is_rejected() -> None:
    composite = _load_r4_composite()
    del composite["r2_component"]
    outcome = _r4_verifier().verify(
        r2=r2_component_reference(),
        r3=r3_component_reference(),
        composite=composite,
    )
    assert outcome.ok is False
    assert "COMPOSITE_COMPONENT_REFERENCE_MISMATCH:R2" in outcome.reasons


def test_composite_component_wrong_type_is_rejected() -> None:
    composite = _load_r4_composite()
    composite["r2_component"] = 42
    outcome = _r4_verifier().verify(
        r2=r2_component_reference(),
        r3=r3_component_reference(),
        composite=composite,
    )
    assert outcome.ok is False
    assert "COMPOSITE_COMPONENT_REFERENCE_MISMATCH:R2" in outcome.reasons


def test_composite_verification_contract_version_tamper_is_rejected() -> None:
    composite = _load_r4_composite()
    composite["verification_contract_version"] = "forged"
    outcome = _r4_verifier().verify(
        r2=r2_component_reference(),
        r3=r3_component_reference(),
        composite=composite,
    )
    assert outcome.ok is False
    assert COMPOSITE_CONTRACT_VERSION_MISMATCH in outcome.reasons


def test_composite_suspension_reference_sha_tamper_is_rejected() -> None:
    composite = _load_r4_composite()
    composite["suspension_reference"]["sha256"] = "f" * 64
    outcome = _r4_verifier().verify(
        r2=r2_component_reference(),
        r3=r3_component_reference(),
        composite=composite,
    )
    assert outcome.ok is False
    assert SUSPENSION_REFERENCE_SHA_MISMATCH in outcome.reasons


def test_composite_deleted_suspension_reference_is_rejected() -> None:
    composite = _load_r4_composite()
    del composite["suspension_reference"]
    outcome = _r4_verifier().verify(
        r2=r2_component_reference(),
        r3=r3_component_reference(),
        composite=composite,
    )
    assert outcome.ok is False
    assert SUSPENSION_REFERENCE_MISSING in outcome.reasons


def test_composite_suspension_raw_fact_tamper_is_rejected() -> None:
    composite = _load_r4_composite()
    composite["suspension_raw_fact"] = {
        "date": "2026-07-16",
        "tradestatus": "1",
        "volume": "5",
        "amount": "5",
    }
    outcome = _r4_verifier().verify(
        r2=r2_component_reference(),
        r3=r3_component_reference(),
        composite=composite,
    )
    assert outcome.ok is False
    assert "EVIDENCE_SUMMARY_MISMATCH:suspension_raw_fact" in outcome.reasons


# ---------------------------------------------------------------------------
# Suspension provenance closure
# ---------------------------------------------------------------------------


def _valid_reference() -> dict[str, object]:
    return {
        "symbol": "688237.SH",
        "suspension_start": "2026-07-16",
        "announcement_number": "2026-031",
        "source_type": "exchange_announcement",
        "announcement_url": (
            "https://static.sse.com.cn/disclosure/listedinfo/announcement/c/new/"
            "2026-07-16/688237_20260716_Y6W3.pdf"
        ),
        "sha256": "c" * 64,
        "provenance_hardening_only": True,
    }


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (None, ()),
        (
            lambda ref: ref.update({"symbol": "600000.SH"}),
            (SUSPENSION_REFERENCE_SYMBOL_MISMATCH,),
        ),
        (
            lambda ref: ref.update({"suspension_start": "2026-07-15"}),
            (SUSPENSION_REFERENCE_DATE_MISMATCH,),
        ),
        (
            lambda ref: ref.update({"announcement_number": "2026-999"}),
            (SUSPENSION_REFERENCE_ANNOUNCEMENT_MISMATCH,),
        ),
        (
            lambda ref: ref.update({"source_type": "media_report"}),
            (SUSPENSION_REFERENCE_SOURCE_TYPE_INVALID,),
        ),
        (
            lambda ref: ref.update({"announcement_url": "https://finance.sina.com.cn/a/1"}),
            (SUSPENSION_REFERENCE_URL_INVALID,),
        ),
        (lambda ref: ref.update({"sha256": "xyz"}), (SUSPENSION_REFERENCE_SHA_INVALID,)),
        (
            lambda ref: ref.update({"provenance_hardening_only": False}),
            (SUSPENSION_REFERENCE_PROVENANCE_FLAG_INVALID,),
        ),
    ],
)
def test_suspension_reference_validation(mutate: object, expected: tuple[str, ...]) -> None:
    reference = _valid_reference()
    if mutate is not None:
        mutate(reference)  # type: ignore[operator]
    assert validate_k4c_r4_suspension_reference(reference) == expected


def test_suspension_reference_non_mapping_is_invalid() -> None:
    assert validate_k4c_r4_suspension_reference("x") == (  # type: ignore[arg-type]
        SUSPENSION_REFERENCE_INVALID_TYPE,
    )


def test_suspension_reference_file_sha_is_frozen() -> None:
    assert hashlib.sha256(_SUSPENSION_REFERENCE.read_bytes()).hexdigest() == (
        "cca0e7d1052b6060cd1f87ad552e6356475b2aaf1568dfada968d3ff749ecf07"
    )
    payload = json.loads(_SUSPENSION_REFERENCE.read_text(encoding="utf-8"))
    assert payload["symbol"] == "688237.SH"
    assert payload["announcement_number"] == "2026-031"
    assert payload["suspension_start"] == "2026-07-16"
    assert payload["source_type"] == "exchange_announcement"
    assert payload["provenance_hardening_only"] is True
    assert "sse.com.cn" in payload["announcement_url"]


def _provenance_rows(
    *,
    hold_target_in: tuple[str, ...] = (),
    counter_roles: tuple[str, ...] = ("A", "B", "C"),
) -> dict[str, dict[tuple[str, str], Mapping[str, object]]]:
    rows: dict[str, dict[tuple[str, str], Mapping[str, object]]] = {role: {} for role in _ROLES}
    for role in _ROLES:
        for trade_date in ("2026-07-14", "2026-07-15"):
            rows[role][("688237.SH", trade_date)] = _row(symbol="688237.SH", trade_date=trade_date)
        if role in hold_target_in:
            rows[role][("688237.SH", "2026-07-16")] = _row(
                symbol="688237.SH", trade_date="2026-07-16"
            )
        if role in counter_roles:
            rows[role][("000001.SZ", "2026-07-16")] = _row(
                symbol="000001.SZ", trade_date="2026-07-16"
            )
    return rows


def _unobserved_suspension() -> dict[str, object]:
    return {
        "objective": "suspension_session",
        "observed": False,
        "symbol": None,
        "trade_date": None,
        "roles": [],
        "evidence_type": None,
        "fact": {},
        "reason_code": SUSPENSION_NOT_OBSERVED,
    }


def _reobserve(
    rows: Mapping[str, Mapping[tuple[str, str], Mapping[str, object]]],
    *,
    reference: Mapping[str, object] | None = None,
    raw_fact: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    merged, reasons = merge_objective_records_with_r3_reobservation(
        (),
        [_unobserved_suspension()],
        k4c_r3_frozen_samples()[:1],
        rows,
        require_suspension_provenance=True,
        suspension_reference=reference,
        suspension_raw_fact=raw_fact,
    )
    assert reasons == []
    return merged


def _suspension_record(
    merged: list[dict[str, object]],
) -> dict[str, object]:
    return next(record for record in merged if record["objective"] == "suspension_session")


def test_suspension_provenance_success_path() -> None:
    merged = _reobserve(
        _provenance_rows(),
        reference=_valid_reference(),
        raw_fact={"date": "2026-07-16", "tradestatus": "0", "volume": "", "amount": ""},
    )
    record = _suspension_record(merged)
    assert record["observed"] is True
    observed = record["observed_records"][0]
    assert observed["fact"]["counter_example_symbol"] == "000001.SZ"
    assert observed["fact"]["suspension_announcement_number"] == "2026-031"
    assert observed["fact"]["suspension_reference_sha256"] == "c" * 64
    assert observed["fact"]["suspension_raw_fact"]["tradestatus"] == "0"


def test_suspension_fails_when_a_role_still_holds_the_target_day() -> None:
    merged = _reobserve(
        _provenance_rows(hold_target_in=("A",)),
        reference=_valid_reference(),
        raw_fact={"date": "2026-07-16", "tradestatus": "0", "volume": "", "amount": ""},
    )
    record = _suspension_record(merged)
    assert record["observed"] is False
    assert record["unobserved_reason_codes"] == [SUSPENSION_NOT_OBSERVED]


def test_suspension_fails_when_counter_example_is_not_three_role_held() -> None:
    merged = _reobserve(
        _provenance_rows(counter_roles=("A", "B")),
        reference=_valid_reference(),
        raw_fact={"date": "2026-07-16", "tradestatus": "0", "volume": "", "amount": ""},
    )
    record = _suspension_record(merged)
    assert record["observed"] is False
    assert record["unobserved_reason_codes"] == [SUSPENSION_NOT_OBSERVED]


def test_suspension_fails_without_reference() -> None:
    merged = _reobserve(
        _provenance_rows(),
        reference=None,
        raw_fact={"date": "2026-07-16", "tradestatus": "0", "volume": "", "amount": ""},
    )
    record = _suspension_record(merged)
    assert record["observed"] is False
    assert SUSPENSION_REFERENCE_MISSING in record["unobserved_reason_codes"]


def test_suspension_fails_with_wrong_reference_date() -> None:
    reference = _valid_reference()
    reference["suspension_start"] = "2026-07-15"
    merged = _reobserve(
        _provenance_rows(),
        reference=reference,
        raw_fact={"date": "2026-07-16", "tradestatus": "0", "volume": "", "amount": ""},
    )
    record = _suspension_record(merged)
    assert record["observed"] is False
    assert SUSPENSION_REFERENCE_DATE_MISMATCH in record["unobserved_reason_codes"]


def test_suspension_fails_without_raw_fact() -> None:
    merged = _reobserve(_provenance_rows(), reference=_valid_reference(), raw_fact=None)
    record = _suspension_record(merged)
    assert record["observed"] is False
    assert SUSPENSION_RAW_FACT_MISSING in record["unobserved_reason_codes"]


def test_suspension_fails_when_raw_tradestatus_is_not_zero() -> None:
    merged = _reobserve(
        _provenance_rows(),
        reference=_valid_reference(),
        raw_fact={"date": "2026-07-16", "tradestatus": "1", "volume": "", "amount": ""},
    )
    record = _suspension_record(merged)
    assert record["observed"] is False
    assert SUSPENSION_RAW_STATUS_MISMATCH in record["unobserved_reason_codes"]


def test_suspension_fails_when_placeholder_carries_volume() -> None:
    merged = _reobserve(
        _provenance_rows(),
        reference=_valid_reference(),
        raw_fact={"date": "2026-07-16", "tradestatus": "0", "volume": "5", "amount": ""},
    )
    record = _suspension_record(merged)
    assert record["observed"] is False
    assert SUSPENSION_RAW_STATUS_MISMATCH in record["unobserved_reason_codes"]


def test_real_r3_raw_sequence_1_carries_the_suspended_placeholder() -> None:
    raw_fact = k4c_r4_suspension_raw_fact(_R3_ATTEMPT)
    assert raw_fact == {"date": "2026-07-16", "tradestatus": "0", "volume": "", "amount": ""}
    # The placeholder never became a canonical/comparison row in any role.
    comparison = rows_from_comparison_payload(
        json.loads((_R3_ATTEMPT / "artifacts/comparison-rows.json").read_text(encoding="utf-8"))
    )
    for role in _ROLES:
        assert ("688237.SH", "2026-07-16") not in comparison[role]


# ---------------------------------------------------------------------------
# Historical byte protection (R2/R3/old composite unchanged)
# ---------------------------------------------------------------------------


def test_historical_shas_remain_frozen() -> None:
    assert (
        hashlib.sha256(
            (
                _ROOT / "docs/stage1/evidence/k4c_r2_representative_probe_2026-08-16.json"
            ).read_bytes()
        ).hexdigest()
        == "217390b53e03345ac84a97739a88cb28b05299f9d2d2bc9ec036d7af89381835"
    )
    assert hashlib.sha256(_R3_EVIDENCE.read_bytes()).hexdigest() == (
        "1632ee8b2a210bcbe22742386072d9c16a10c789cc4bba9bd8dcf5f4ed0836ea"
    )
    assert (
        hashlib.sha256((_R3_ATTEMPT / "artifacts/artifact-manifest.json").read_bytes()).hexdigest()
        == "2b2f919ba40ef36835a8c3879b0a8a92c13ff689c66bf95a91ab0111aeb7c17c"
    )
    assert (
        hashlib.sha256(
            (_ROOT / "docs/stage1/evidence/k4c_composite_evidence_2026-08-16.json").read_bytes()
        ).hexdigest()
        == "910010da4c309cb93faacc2a7705b4a15cfc2279cfbe762112a86a4fa9b8f12d"
    )
    assert hashlib.sha256(_R4_COMPOSITE.read_bytes()).hexdigest() == (
        "87b04863756a6e92614f62fe26cfb7d8b1b591c101bcc28d902a7fc3c1bdd300"
    )


# ---------------------------------------------------------------------------
# Threshold re-freeze acceptance (K4C-R4 independent acceptance, 2026-08-16)
# ---------------------------------------------------------------------------


def test_k4_threshold_acceptance_record_is_frozen() -> None:
    acceptance = k4_threshold_acceptance()
    assert acceptance.rule_version == K4_THRESHOLD_RULE_VERSION
    assert K4_THRESHOLD_RULE_VERSION == "comparison-thresholds.daily-bar-unadjusted.k4.v1"
    assert acceptance.accepted_at == "2026-08-16"
    assert acceptance.accepted_by == "independent K4C-R4 acceptance (S1-07K4C-R4)"
    assert acceptance.bounds == {
        "open": "0/0",
        "high": "0/0",
        "low": "0/0",
        "close": "0/0",
        "pre_close": "0/0",
        "volume": "50/0",
        "amount": "50/0",
    }
    assert acceptance.evidence_references[0].endswith(
        "217390b53e03345ac84a97739a88cb28b05299f9d2d2bc9ec036d7af89381835"
    )
    assert acceptance.evidence_references[3].endswith(
        "87b04863756a6e92614f62fe26cfb7d8b1b591c101bcc28d902a7fc3c1bdd300"
    )
    record = acceptance.to_dict()
    assert record["rule_version"] == "comparison-thresholds.daily-bar-unadjusted.k4.v1"
    assert record["bounds"]["volume"] == "50/0"


def test_k4_threshold_values_match_the_accepted_bounds() -> None:
    specs = k4_evidence_thresholds().for_dataset(DatasetKind.DAILY_BAR_UNADJUSTED)
    for field in ("open", "high", "low", "close", "pre_close"):
        assert specs[field].absolute == 0
        assert specs[field].relative == 0
    for field in ("volume", "amount"):
        assert specs[field].absolute == 50
        assert specs[field].relative == 0


def test_frozen_evidence_coverage_payload_stays_byte_identical() -> None:
    # The accepted rule must not rewrite the historical payloads: frozen
    # evidence still recomputes byte-identically (pending-era flag included).
    payload = json.loads(_R3_EVIDENCE.read_text(encoding="utf-8"))
    assert payload["threshold_coverage"]["thresholds_pending_reacceptance"] is True
    assert payload["threshold_coverage"]["threshold_rule_version"] == (
        "comparison-thresholds.daily-bar-unadjusted.k4.v1"
    )
    outcome = _r3_verifier().verify(attempt_directory=_R3_ATTEMPT, evidence=payload)
    assert outcome.ok is True
    assert outcome.recomputed["threshold_coverage"] == payload["threshold_coverage"]
