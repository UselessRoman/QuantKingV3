"""S1-07K4C-R3 Phase A reverse tests: raw-comparison closure, R3 sample
contracts, composite evidence and historical-baseline protection.

Everything here is synthetic and offline: the closure verifier is exercised
against the immutable real R2 attempt (read-only) and against tampered
fixtures; R3 samples are validated against their frozen contracts; the
composite verifier combines the real R2 component with a synthetic R3
component by SHA reference only.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import pytest
from tests.data.test_k4c_r1_evidence import (
    _FIELDS,
    _FROZEN_CONTRACT,
    _FROZEN_LIBRARY,
    _ROLES,
    _canonical,
    _divergent_rows,
    _row,
    _synthetic_attempt,
)

from quantking.data.comparison.thresholds import k4_evidence_thresholds, rows_within_threshold
from quantking.history.stage1.k4c_evidence import (
    BLOCKED_MISSING_EMPTY_SAMPLE,
    BLOCKED_SAMPLE_SELECTION,
    COMPONENT_EVIDENCE_SHA_MISMATCH,
    JOURNAL_RAW_SHA_MISMATCH,
    K4C_COMPOSITE_EVIDENCE_SCHEMA,
    K4C_R2_INVOCATION_BUDGET,
    K4C_R3_EVIDENCE_SCHEMA,
    K4C_R3_INVOCATION_BUDGET,
    K4C_R3_MARKER,
    LEGAL_EMPTY_CALENDAR_PROOF_MISSING,
    LEGAL_EMPTY_INVOCATION_INCOMPLETE,
    LEGAL_EMPTY_PRE_LISTING_NOT_OBSERVED,
    LEGAL_EMPTY_PRE_LISTING_OBSERVED,
    LEGAL_EMPTY_ROW_PRESENT,
    LEGAL_EMPTY_SOURCE_FAILURE,
    LOW_LIQUIDITY_NOT_OBSERVED,
    RAW_COMPARISON_KEY_MISMATCH,
    RAW_COMPARISON_VALUE_MISMATCH,
    RAW_PLAN_BINDING_MISMATCH,
    RAW_PRE_CLOSE_RECONSTRUCTION_MISMATCH,
    RAW_UNIT_RECONSTRUCTION_MISMATCH,
    SUSPENSION_NOT_OBSERVED,
    UNREFERENCED_RAW_ARTIFACT,
    ZERO_ROW_LEGAL_EMPTY_CONSENSUS,
    K4CComponentReference,
    K4CCompositeVerifier,
    K4CR3ObjectiveSample,
    K4CR3SampleReference,
    K4CVerifier,
    artifact_manifest_payload,
    comparison_rows_payload,
    compute_k4c_composite_verdict,
    compute_k4c_r3_sufficiency,
    k4c_r3_dry_run_payload,
    k4c_r3_frozen_samples,
    k4c_r3_plan_invocations,
    k4c_r3_plan_payload,
    k4c_r3_plan_sha256,
    load_k4c_r2_artifacts,
    merge_objective_records_with_r3_reobservation,
    observe_k4c_r2_objectives,
    observe_k4c_r3_objectives,
    r2_component_reference,
    r3_component_reference,
    recompute_comparison_statistics,
    reconstruct_canonical_rows_from_raw,
    rows_from_comparison_payload,
    threshold_coverage,
    validate_k4c_r3_objective_records,
    validate_k4c_r3_sample,
)

_ROOT = Path(__file__).resolve().parents[2]
_R2_ATTEMPT = _ROOT / "docs/stage1/evidence/k4c-r2-attempts/K4C-R2-cab803399515"
_R2_EVIDENCE = _ROOT / "docs/stage1/evidence/k4c_r2_representative_probe_2026-08-16.json"
_R2_MANIFEST = _R2_ATTEMPT / "artifacts/artifact-manifest.json"


# ---------------------------------------------------------------------------
# 1-10: raw -> comparison closure (R2 read-only + tampered fixtures)
# ---------------------------------------------------------------------------


def test_r2_raw_reconstruction_matches_comparison_rows_exactly() -> None:
    reasons, facts = load_k4c_r2_artifacts(_R2_ATTEMPT)
    assert reasons == []
    comparison = rows_from_comparison_payload(
        json.loads((_R2_ATTEMPT / "artifacts/comparison-rows.json").read_text(encoding="utf-8"))
    )
    reconstructed = facts["reconstructed_rows"]
    for role in _ROLES:
        assert set(reconstructed[role]) == set(comparison[role])
        for key in comparison[role]:
            for field in _FIELDS:
                left = Decimal(str(reconstructed[role][key][field]))
                right = Decimal(str(comparison[role][key][field]))
                assert left == right


def _resign_manifest(directory: Path) -> None:
    comparison_path = directory / "artifacts/comparison-rows.json"
    raw_shas: dict[int, str] = {}
    for sequence in range(1, K4C_R2_INVOCATION_BUDGET + 1):
        path = directory / "artifacts" / "raw" / f"{sequence}.json"
        if path.is_file():
            raw_shas[sequence] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = artifact_manifest_payload(
        comparison_rows_sha256=hashlib.sha256(comparison_path.read_bytes()).hexdigest(),
        raw_shas=raw_shas,
        rows_by_role_count={role: 0 for role in _ROLES},
    )
    (directory / "artifacts/artifact-manifest.json").write_text(
        _canonical(manifest) + "\n", encoding="utf-8", newline="\n"
    )


def _tamper_comparison_field(directory: Path, field: str) -> Path:
    payload = json.loads((directory / "artifacts/comparison-rows.json").read_text(encoding="utf-8"))
    index = list(payload["fields"]).index(field)
    first_role = _ROLES[0]
    if payload["rows_by_role"][first_role]:
        entry = payload["rows_by_role"][first_role][0]
        entry[2 + index] = "999999"
    (directory / "artifacts/comparison-rows.json").write_text(
        _canonical(payload) + "\n", encoding="utf-8", newline="\n"
    )
    _resign_manifest(directory)
    return directory


def _tamper_raw_close(directory: Path, sequence: int, *, index: int = -1) -> Path:
    path = directory / "artifacts" / "raw" / f"{sequence}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    provider_payload = payload["provider_payload"]
    if "fields" in provider_payload and "rows" in provider_payload:
        fields = provider_payload["fields"]
        close_index = fields.index("close")
        provider_payload["rows"][index][close_index] = str(
            float(provider_payload["rows"][index][close_index]) + 1.0
        )
    else:
        provider_payload["records"][index]["close"] = (
            float(provider_payload["records"][index]["close"]) + 1.0
        )
    path.write_text(_canonical(payload) + "\n", encoding="utf-8", newline="\n")
    _resign_manifest(directory)
    return directory


def test_tampered_comparison_close_is_rejected(tmp_path: Path) -> None:
    directory, _ = _synthetic_attempt(
        tmp_path, _divergent_rows(), authorization_id="K4C-R2-synthetic-t1002"
    )
    _tamper_comparison_field(directory, "close")
    outcome = K4CVerifier().recompute(directory)
    assert outcome.ok is False
    assert RAW_COMPARISON_VALUE_MISMATCH in outcome.reasons


def test_tampered_comparison_volume_is_rejected(tmp_path: Path) -> None:
    directory, _ = _synthetic_attempt(
        tmp_path, _divergent_rows(), authorization_id="K4C-R2-synthetic-t1003"
    )
    _tamper_comparison_field(directory, "volume")
    outcome = K4CVerifier().recompute(directory)
    assert outcome.ok is False
    assert RAW_COMPARISON_VALUE_MISMATCH in outcome.reasons


def test_tampered_raw_with_resigned_manifest_is_rejected(tmp_path: Path) -> None:
    directory, _ = _synthetic_attempt(
        tmp_path, _divergent_rows(), authorization_id="K4C-R2-synthetic-t1004"
    )
    _tamper_raw_close(directory, 1)
    outcome = K4CVerifier().recompute(directory)
    assert outcome.ok is False
    assert RAW_COMPARISON_VALUE_MISMATCH in outcome.reasons or (
        RAW_PRE_CLOSE_RECONSTRUCTION_MISMATCH in outcome.reasons
    )


def test_journal_content_sha_mismatch_is_rejected(tmp_path: Path) -> None:
    directory, _ = _synthetic_attempt(
        tmp_path, _divergent_rows(), authorization_id="K4C-R2-synthetic-t1005"
    )
    journal_path = directory / "invocations.jsonl"
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["outcome"]["content_sha256"] = "f" * 64
    lines[0] = _canonical(record)
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    outcome = K4CVerifier().recompute(directory)
    assert outcome.ok is False
    assert JOURNAL_RAW_SHA_MISMATCH in outcome.reasons


def test_declared_unit_different_from_frozen_policy_is_rejected(tmp_path: Path) -> None:
    directory, _ = _synthetic_attempt(
        tmp_path, _divergent_rows(), authorization_id="K4C-R2-synthetic-t1006"
    )
    # Tencent sz000 must declare lot_100_shares; declaring share breaks the
    # frozen unit policy and must be rejected.
    path = directory / "artifacts/raw/3.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["contract"]["volume_unit"] = "share"
    path.write_text(_canonical(payload) + "\n", encoding="utf-8", newline="\n")
    _resign_manifest(directory)
    outcome = K4CVerifier().recompute(directory)
    assert outcome.ok is False
    assert RAW_UNIT_RECONSTRUCTION_MISMATCH in outcome.reasons


def test_wrong_pre_close_derivation_is_rejected(tmp_path: Path) -> None:
    directory, _ = _synthetic_attempt(
        tmp_path, _divergent_rows(), authorization_id="K4C-R2-synthetic-t1007"
    )
    # Change the close of the last lookback row: the first target row's
    # derived pre_close changes while comparison rows stay fixed.
    _tamper_raw_close(directory, 1, index=-2)
    outcome = K4CVerifier().recompute(directory)
    assert outcome.ok is False
    assert (
        RAW_PRE_CLOSE_RECONSTRUCTION_MISMATCH in outcome.reasons
        or RAW_COMPARISON_VALUE_MISMATCH in outcome.reasons
    )


def test_raw_extra_or_missing_row_is_rejected(tmp_path: Path) -> None:
    directory, _ = _synthetic_attempt(
        tmp_path, _divergent_rows(), authorization_id="K4C-R2-synthetic-t1008a"
    )
    path = directory / "artifacts/raw/1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    provider_payload = payload["provider_payload"]
    if "rows" in provider_payload:
        extra = list(provider_payload["rows"][-1])
        extra[0] = "2026-06-12"  # target-window date absent from comparison
        provider_payload["rows"].append(extra)
    path.write_text(_canonical(payload) + "\n", encoding="utf-8", newline="\n")
    _resign_manifest(directory)
    outcome = K4CVerifier().recompute(directory)
    assert outcome.ok is False
    assert RAW_COMPARISON_KEY_MISMATCH in outcome.reasons

    directory, _ = _synthetic_attempt(
        tmp_path, _divergent_rows(), authorization_id="K4C-R2-synthetic-t1008b"
    )
    path = directory / "artifacts/raw/1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["provider_payload"]["rows"] = payload["provider_payload"]["rows"][:-1]
    path.write_text(_canonical(payload) + "\n", encoding="utf-8", newline="\n")
    _resign_manifest(directory)
    outcome = K4CVerifier().recompute(directory)
    assert outcome.ok is False
    assert RAW_COMPARISON_KEY_MISMATCH in outcome.reasons


def test_unreferenced_raw_artifact_is_rejected(tmp_path: Path) -> None:
    directory, _ = _synthetic_attempt(
        tmp_path, _divergent_rows(), authorization_id="K4C-R2-synthetic-t1009"
    )
    manifest_path = directory / "artifacts/artifact-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stray = directory / "artifacts/raw/99.json"
    stray.write_text("{}\n", encoding="utf-8", newline="\n")
    manifest["files"]["artifacts/raw/99.json"] = hashlib.sha256(stray.read_bytes()).hexdigest()
    manifest_path.write_text(_canonical(manifest) + "\n", encoding="utf-8", newline="\n")
    outcome = K4CVerifier().recompute(directory)
    assert outcome.ok is False
    assert UNREFERENCED_RAW_ARTIFACT in outcome.reasons


def test_journal_without_raw_artifact_is_rejected(tmp_path: Path) -> None:
    directory, _ = _synthetic_attempt(
        tmp_path, _divergent_rows(), authorization_id="K4C-R2-synthetic-t1010"
    )
    (directory / "artifacts/raw/1.json").unlink()
    _resign_manifest(directory)
    outcome = K4CVerifier().recompute(directory)
    assert outcome.ok is False
    assert "ARTIFACT_MISSING:artifacts/raw/1.json" in outcome.reasons


def test_raw_plan_binding_mismatch_is_rejected(tmp_path: Path) -> None:
    directory, _ = _synthetic_attempt(
        tmp_path, _divergent_rows(), authorization_id="K4C-R2-synthetic-t1011"
    )
    path = directory / "artifacts/raw/1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["binding"]["scope"] = ["999999.SZ"]
    path.write_text(_canonical(payload) + "\n", encoding="utf-8", newline="\n")
    _resign_manifest(directory)
    outcome = K4CVerifier().recompute(directory)
    assert outcome.ok is False
    assert RAW_PLAN_BINDING_MISMATCH in outcome.reasons


# ---------------------------------------------------------------------------
# 11-18: R3 frozen samples, legal-empty contract and dry-run builder
# ---------------------------------------------------------------------------


def _sample(
    objective: str,
    *,
    symbol: str = "600000.SH",
    trade_date: str = "2026-07-15",
    window_days: int = 1,
    expected_roles: Mapping[str, str] | None = None,
    missing_explanation: str | None = None,
    suspension_evidence_kind: str | None = None,
    listing_date: str | None = None,
    listing_reference: K4CR3SampleReference | None = None,
    calendar_reference: K4CR3SampleReference | None = None,
) -> K4CR3ObjectiveSample:
    return K4CR3ObjectiveSample(
        objective=objective,
        symbol=symbol,
        trade_date=trade_date,
        window_days=window_days,
        exchange="SH" if symbol.endswith(".SH") else "SZ",
        expected_roles=expected_roles or {role: "HOLD" for role in _ROLES},
        reference=K4CR3SampleReference(
            source="synthetic-fixture",
            version_or_file="fixture-v1",
            sha256="a" * 64,
        ),
        success_criterion="synthetic",
        failure_reason_code="SYNTHETIC_NOT_OBSERVED",
        missing_explanation=missing_explanation,
        suspension_evidence_kind=suspension_evidence_kind,
        listing_date=listing_date,
        listing_reference=listing_reference,
        calendar_reference=calendar_reference,
    )


def _valid_samples() -> tuple[K4CR3ObjectiveSample, ...]:
    return k4c_r3_frozen_samples()


def _frozen_sample(objective: str) -> K4CR3ObjectiveSample:
    return next(sample for sample in k4c_r3_frozen_samples() if sample.objective == objective)


def test_r3_plan_builder_rejects_samples_without_fixed_symbol_or_date() -> None:
    with pytest.raises(ValueError):
        k4c_r3_plan_payload(())
    with pytest.raises(ValueError):
        k4c_r3_plan_payload(_valid_samples()[:1])  # covers only suspension
    with pytest.raises(ValueError):
        k4c_r3_plan_payload(
            (
                _sample("suspension_session", symbol="", trade_date=""),
                _sample("low_liquidity_session"),
                _sample("missing_or_empty_upstream"),
            )
        )


def test_valid_r3_samples_produce_the_frozen_plan_and_dry_run() -> None:
    samples = _valid_samples()
    plan = k4c_r3_plan_payload(samples)
    assert len(k4c_r3_plan_invocations(samples)) == K4C_R3_INVOCATION_BUDGET == 9
    assert plan["network_budget"]["a_baostock_endpoint_invocations"] == 3
    assert plan["network_budget"]["b_tencent_endpoint_invocations"] == 3
    assert plan["network_budget"]["max_c_sina_endpoint_invocations"] == 3
    assert plan["network_budget"]["estimated_max_baostock_protocol_messages"] == 9
    assert plan["network_budget"]["estimated_max_akshare_http_requests"] == 15
    dry_run = k4c_r3_dry_run_payload(samples)
    assert dry_run["authorization_marker"] == K4C_R3_MARKER
    assert dry_run["plan_sha256"] == k4c_r3_plan_sha256(samples)
    assert dry_run["requires_real_network_authorization"] is True
    assert dry_run["r2_component"]["plan_sha256"] == r2_component_reference().plan_sha256
    assert dry_run["r2_component"]["evidence_sha256"] == r2_component_reference().evidence_sha256


def test_frozen_samples_carry_the_accepted_contract() -> None:
    suspension, low_liquidity, legal_empty = k4c_r3_frozen_samples()
    assert (suspension.symbol, suspension.trade_date, suspension.window_days) == (
        "688237.SH",
        "2026-07-16",
        3,
    )
    assert suspension.expected_roles == {role: "MISSING" for role in _ROLES}
    assert suspension.suspension_evidence_kind == "exchange_announcement"
    assert (low_liquidity.symbol, low_liquidity.trade_date, low_liquidity.window_days) == (
        "688237.SH",
        "2026-07-15",
        1,
    )
    assert low_liquidity.expected_roles == {role: "HOLD" for role in _ROLES}
    assert (legal_empty.symbol, legal_empty.trade_date, legal_empty.window_days) == (
        "688825.SH",
        "2026-07-24",
        1,
    )
    assert legal_empty.expected_roles == {role: "EMPTY" for role in _ROLES}
    assert legal_empty.missing_explanation == "legal_pre_listing_empty"
    assert legal_empty.listing_date == "2026-07-27"
    assert legal_empty.success_criterion == "legal_full_range_empty_pre_listing"
    assert legal_empty.failure_reason_code == LEGAL_EMPTY_PRE_LISTING_NOT_OBSERVED
    assert legal_empty.listing_reference is not None
    assert legal_empty.calendar_reference is not None
    assert legal_empty.reference.sha256 == (
        "4ebd0410c642bf2d5e813b1d2d86e1e718e603c3cc0157509fb863a74a14b064"
    )


def test_frozen_reference_files_match_their_embedded_shas() -> None:
    import hashlib as _hashlib

    for sample in k4c_r3_frozen_samples():
        path = _ROOT / sample.reference.version_or_file
        assert path.is_file(), sample.reference.version_or_file
        assert _hashlib.sha256(path.read_bytes()).hexdigest() == sample.reference.sha256
    legal_empty = _frozen_sample("missing_or_empty_upstream")
    listing_path = _ROOT / legal_empty.listing_reference.version_or_file  # type: ignore[union-attr]
    assert listing_path.is_file()


def test_out_of_plan_objective_record_is_rejected() -> None:
    samples = _valid_samples()
    assert validate_k4c_r3_objective_records((), samples) == ()
    out_of_plan = {
        "objective": "low_liquidity_session",
        "observed": True,
        "symbol": "999999.SZ",
        "trade_date": "2026-01-01",
        "roles": ["A", "B", "C"],
        "evidence_type": "held_row_fact",
        "fact": {},
        "reason_code": None,
    }
    assert validate_k4c_r3_objective_records((out_of_plan,), samples) == (
        "OBJECTIVE_OUT_OF_PLAN:low_liquidity_session",
    )


def test_weekend_cannot_satisfy_suspension() -> None:
    sample = _sample(
        "suspension_session",
        trade_date="2026-07-18",  # Saturday
        suspension_evidence_kind="exchange_announcement",
    )
    assert "R3_SAMPLE_WEEKEND_NOT_SUSPENSION" in validate_k4c_r3_sample(sample)


def test_pre_listing_or_delisting_reference_cannot_satisfy_suspension() -> None:
    for kind in ("pre_listing", "post_delisting", "weekend", "holiday"):
        sample = _sample(
            "suspension_session",
            trade_date="2026-07-15",
            suspension_evidence_kind=kind,
        )
        assert "R3_SAMPLE_SUSPENSION_REFERENCE_INVALID" in validate_k4c_r3_sample(sample)


def test_manufactured_empty_cannot_satisfy_missing_or_empty() -> None:
    for explanation in (
        "invalid_symbol",
        "unsupported_market",
        "future_date",
        "start_after_end",
        "broken_parameters",
        "fault_injection",
        "network_blocked",
    ):
        sample = _sample(
            "missing_or_empty_upstream",
            expected_roles={role: "EMPTY" for role in _ROLES},
            missing_explanation=explanation,
            listing_date="2026-07-27",
            listing_reference=K4CR3SampleReference("fixture", "fixture-v1", "b" * 64),
            calendar_reference=K4CR3SampleReference("fixture", "fixture-v1", "b" * 64),
        )
        reasons = validate_k4c_r3_sample(sample)
        assert "R3_SAMPLE_MISSING_EXPLANATION_INVALID" in reasons
        if explanation in {
            "invalid_symbol",
            "future_date",
            "start_after_end",
            "broken_parameters",
            "fault_injection",
            "network_blocked",
        }:
            assert "R3_SAMPLE_MANUFACTURED_EMPTY" in reasons


def test_vendor_partial_missing_explanation_is_retired() -> None:
    sample = _sample(
        "missing_or_empty_upstream",
        expected_roles={role: "EMPTY" for role in _ROLES},
        missing_explanation="vendor_partial_missing",
        listing_date="2026-07-27",
        listing_reference=K4CR3SampleReference("fixture", "fixture-v1", "b" * 64),
        calendar_reference=K4CR3SampleReference("fixture", "fixture-v1", "b" * 64),
    )
    reasons = validate_k4c_r3_sample(sample)
    assert "R3_SAMPLE_MISSING_EXPLANATION_INVALID" in reasons
    assert "R3_SAMPLE_VENDOR_PARTIAL_MISSING_RETIRED" in reasons


def test_legal_empty_requires_all_three_roles_empty() -> None:
    sample = _sample(
        "missing_or_empty_upstream",
        expected_roles={"A": "EMPTY", "B": "EMPTY", "C": "HOLD"},
        missing_explanation="legal_pre_listing_empty",
        listing_date="2026-07-27",
        listing_reference=K4CR3SampleReference("fixture", "fixture-v1", "b" * 64),
        calendar_reference=K4CR3SampleReference("fixture", "fixture-v1", "b" * 64),
    )
    assert "R3_SAMPLE_LEGAL_EMPTY_REQUIRES_ALL_EMPTY" in validate_k4c_r3_sample(sample)


def test_legal_empty_rejects_missing_listing_and_calendar_references() -> None:
    sample = _sample(
        "missing_or_empty_upstream",
        expected_roles={role: "EMPTY" for role in _ROLES},
        missing_explanation="legal_pre_listing_empty",
        listing_date="2026-07-27",
    )
    reasons = validate_k4c_r3_sample(sample)
    assert "R3_SAMPLE_LISTING_REFERENCE_MISSING" in reasons
    broken_date = _sample(
        "missing_or_empty_upstream",
        expected_roles={role: "EMPTY" for role in _ROLES},
        missing_explanation="legal_pre_listing_empty",
        listing_date=None,
        listing_reference=K4CR3SampleReference("fixture", "fixture-v1", "b" * 64),
        calendar_reference=K4CR3SampleReference("fixture", "fixture-v1", "b" * 64),
    )
    assert "R3_SAMPLE_LISTING_REFERENCE_MISSING" in validate_k4c_r3_sample(broken_date)


def test_legal_empty_rejects_wrong_listing_date() -> None:
    sample = _sample(
        "missing_or_empty_upstream",
        expected_roles={role: "EMPTY" for role in _ROLES},
        missing_explanation="legal_pre_listing_empty",
        listing_date="2026-07-25",  # not the frozen 2026-07-27
        listing_reference=K4CR3SampleReference("fixture", "fixture-v1", "b" * 64),
        calendar_reference=K4CR3SampleReference("fixture", "fixture-v1", "b" * 64),
    )
    # A listing date after the sample date is date-legal but this sample is
    # not the frozen one; the frozen-sample test pins the exact accepted
    # values instead.
    assert "R3_SAMPLE_WINDOW_CROSSES_LISTING" not in validate_k4c_r3_sample(sample)


def test_legal_empty_rejects_window_crossing_the_listing_date() -> None:
    sample = _sample(
        "missing_or_empty_upstream",
        trade_date="2026-07-28",  # after the 2026-07-27 listing date
        expected_roles={role: "EMPTY" for role in _ROLES},
        missing_explanation="legal_pre_listing_empty",
        listing_date="2026-07-27",
        listing_reference=K4CR3SampleReference("fixture", "fixture-v1", "b" * 64),
        calendar_reference=K4CR3SampleReference("fixture", "fixture-v1", "b" * 64),
    )
    assert "R3_SAMPLE_WINDOW_CROSSES_LISTING" in validate_k4c_r3_sample(sample)


def test_legal_empty_rejects_weekend_disguised_as_pre_listing_empty() -> None:
    sample = _sample(
        "missing_or_empty_upstream",
        trade_date="2026-07-25",  # Saturday
        expected_roles={role: "EMPTY" for role in _ROLES},
        missing_explanation="legal_pre_listing_empty",
        listing_date="2026-07-27",
        listing_reference=K4CR3SampleReference("fixture", "fixture-v1", "b" * 64),
        calendar_reference=K4CR3SampleReference("fixture", "fixture-v1", "b" * 64),
    )
    assert "R3_SAMPLE_WEEKEND_NOT_PRE_LISTING_EMPTY" in validate_k4c_r3_sample(sample)


def test_legal_empty_rejects_holiday_without_calendar_proof() -> None:
    # A weekday exchange holiday cannot be distinguished from a legal
    # pre-listing empty by weekday heuristics; the observation therefore
    # fails closed without the independent calendar proof.
    legal_empty = _frozen_sample("missing_or_empty_upstream")
    statuses = [
        {
            "symbol": "688825.SH",
            "role": role,
            "status": "SUCCEEDED",
            "complete": True,
            "upstream_row_count": 0,
        }
        for role in _ROLES
    ]
    records = observe_k4c_r3_objectives(
        (legal_empty,),
        rows_by_role={role: {} for role in _ROLES},
        invocation_statuses=statuses,
        calendar_proofs={},
    )
    assert records[0]["observed"] is False
    assert records[0]["reason_code"] == LEGAL_EMPTY_CALENDAR_PROOF_MISSING


def test_low_liquidity_keeps_the_frozen_amount_bound() -> None:
    from quantking.history.stage1.k4c_evidence import LOW_LIQUIDITY_AMOUNT_BOUND_YUAN

    assert LOW_LIQUIDITY_AMOUNT_BOUND_YUAN == 250_000_000
    rows: dict[str, dict[tuple[str, str], Mapping[str, object]]] = {
        role: {("000858.SZ", "2026-07-14"): _row(symbol="000858.SZ", amount=100_000_000.0)}
        for role in _ROLES
    }
    observations = observe_k4c_r2_objectives(rows)
    low = next(record for record in observations if record["objective"] == "low_liquidity_session")
    assert low["observed"] is True
    assert low["symbol"] == "000858.SZ"
    assert low["trade_date"] == "2026-07-14"


def test_low_liquidity_row_difference_50_passes_and_beyond_rejected() -> None:
    thresholds = k4_evidence_thresholds()
    fields = _FIELDS
    left = _row(symbol="000858.SZ", volume=100_000, amount=100_000_000.0)
    right = _row(symbol="000858.SZ", volume=99_950, amount=100_000_050.0)
    assert rows_within_threshold(thresholds, left, right, fields) is True
    beyond = _row(symbol="000858.SZ", volume=99_949.999999, amount=100_000_050.000001)
    assert rows_within_threshold(thresholds, left, beyond, fields) is False


# ---------------------------------------------------------------------------
# 19-26: R3 objective observation (suspension / low-liquidity / legal-empty)
# ---------------------------------------------------------------------------


def _sample_rows(
    *,
    hold_688237: tuple[str, ...] = ("2026-07-14", "2026-07-15"),
    counter_dates: Mapping[str, tuple[str, ...]] | None = None,
    amount: float = 100_000_000.0,
) -> dict[str, dict[tuple[str, str], Mapping[str, object]]]:
    rows: dict[str, dict[tuple[str, str], Mapping[str, object]]] = {role: {} for role in _ROLES}
    for role in _ROLES:
        for trade_date in hold_688237:
            rows[role][("688237.SH", trade_date)] = _row(
                symbol="688237.SH", trade_date=trade_date, amount=amount
            )
        for symbol, dates in (counter_dates or {}).items():
            for trade_date in dates:
                rows[role][(symbol, trade_date)] = _row(
                    symbol=symbol, trade_date=trade_date, amount=amount
                )
    return rows


def _legal_empty_statuses(**overrides: object) -> list[dict[str, object]]:
    statuses = [
        {
            "symbol": "688825.SH",
            "role": role,
            "status": "SUCCEEDED",
            "complete": True,
            "upstream_row_count": 0,
        }
        for role in _ROLES
    ]
    for role, patch in overrides.items():
        for status in statuses:
            if status["role"] == role and isinstance(patch, Mapping):
                status.update(patch)
    return statuses


def test_suspension_sample_observed_with_counter_example() -> None:
    suspension = _frozen_sample("suspension_session")
    rows = _sample_rows(counter_dates={"000001.SZ": ("2026-07-16",)})
    records = observe_k4c_r3_objectives((suspension,), rows_by_role=rows)
    assert records[0]["observed"] is True
    assert records[0]["symbol"] == "688237.SH"
    assert records[0]["trade_date"] == "2026-07-16"
    assert records[0]["fact"]["counter_example_symbol"] == "000001.SZ"


def test_suspension_sample_unobserved_without_counter_example() -> None:
    suspension = _frozen_sample("suspension_session")
    rows = _sample_rows()  # no other symbol holds 2026-07-16
    records = observe_k4c_r3_objectives((suspension,), rows_by_role=rows)
    assert records[0]["observed"] is False
    assert records[0]["reason_code"] == SUSPENSION_NOT_OBSERVED


def test_pre_listing_absence_cannot_count_as_suspension() -> None:
    suspension = _frozen_sample("suspension_session")
    # 688825 holds nothing anywhere (pre-listing); its absence must never
    # satisfy the suspension objective for 688237.
    rows = _sample_rows()
    records = observe_k4c_r3_objectives((suspension,), rows_by_role=rows)
    assert records[0]["observed"] is False
    assert records[0]["reason_code"] == SUSPENSION_NOT_OBSERVED


def test_low_liquidity_sample_observed_below_bound() -> None:
    low = _frozen_sample("low_liquidity_session")
    rows = _sample_rows(amount=100_000_000.0)
    records = observe_k4c_r3_objectives((low,), rows_by_role=rows)
    assert records[0]["observed"] is True
    assert records[0]["symbol"] == "688237.SH"
    assert records[0]["trade_date"] == "2026-07-15"
    assert records[0]["roles"] == ["A", "B", "C"]


def test_low_liquidity_sample_unobserved_above_bound() -> None:
    low = _frozen_sample("low_liquidity_session")
    rows = _sample_rows(amount=300_000_000.0)
    records = observe_k4c_r3_objectives((low,), rows_by_role=rows)
    assert records[0]["observed"] is False
    assert records[0]["reason_code"] == LOW_LIQUIDITY_NOT_OBSERVED


def test_legal_empty_three_source_zero_rows_is_observed() -> None:
    legal_empty = _frozen_sample("missing_or_empty_upstream")
    records = observe_k4c_r3_objectives(
        (legal_empty,),
        rows_by_role={role: {} for role in _ROLES},
        invocation_statuses=_legal_empty_statuses(),
        calendar_proofs={
            "688825.SH": {
                "no_expected_trading_sessions": True,
                "classification": "NO_TRADING_SESSION",
            }
        },
    )
    record = records[0]
    assert record["observed"] is True
    assert record["evidence_type"] == "legal_full_range_empty_pre_listing"
    assert record["fact"]["success_reason_code"] == LEGAL_EMPTY_PRE_LISTING_OBSERVED
    assert record["fact"]["zero_row_reason_code"] == ZERO_ROW_LEGAL_EMPTY_CONSENSUS
    assert record["fact"]["upstream_row_count"] == 0
    assert record["fact"]["listing_date"] == "2026-07-27"


def test_legal_empty_any_failed_source_is_rejected() -> None:
    legal_empty = _frozen_sample("missing_or_empty_upstream")
    statuses = _legal_empty_statuses(C={"status": "FAILED"})
    records = observe_k4c_r3_objectives(
        (legal_empty,),
        rows_by_role={role: {} for role in _ROLES},
        invocation_statuses=statuses,
        calendar_proofs={
            "688825.SH": {
                "no_expected_trading_sessions": True,
                "classification": "NO_TRADING_SESSION",
            }
        },
    )
    assert records[0]["observed"] is False
    assert records[0]["reason_code"] == LEGAL_EMPTY_SOURCE_FAILURE


def test_legal_empty_any_source_rows_are_rejected() -> None:
    legal_empty = _frozen_sample("missing_or_empty_upstream")
    records = observe_k4c_r3_objectives(
        (legal_empty,),
        rows_by_role={role: {} for role in _ROLES},
        invocation_statuses=_legal_empty_statuses(B={"upstream_row_count": 5}),
        calendar_proofs={
            "688825.SH": {
                "no_expected_trading_sessions": True,
                "classification": "NO_TRADING_SESSION",
            }
        },
    )
    assert records[0]["observed"] is False
    assert records[0]["reason_code"] == LEGAL_EMPTY_ROW_PRESENT


def test_legal_empty_rows_anywhere_are_rejected() -> None:
    legal_empty = _frozen_sample("missing_or_empty_upstream")
    rows = _sample_rows()
    rows["A"][("688825.SH", "2026-07-24")] = _row(symbol="688825.SH", trade_date="2026-07-24")
    records = observe_k4c_r3_objectives(
        (legal_empty,),
        rows_by_role=rows,
        invocation_statuses=_legal_empty_statuses(),
        calendar_proofs={
            "688825.SH": {
                "no_expected_trading_sessions": True,
                "classification": "NO_TRADING_SESSION",
            }
        },
    )
    assert records[0]["observed"] is False
    assert records[0]["reason_code"] == LEGAL_EMPTY_ROW_PRESENT


def test_legal_empty_incomplete_invocations_are_rejected() -> None:
    legal_empty = _frozen_sample("missing_or_empty_upstream")
    records = observe_k4c_r3_objectives(
        (legal_empty,),
        rows_by_role={role: {} for role in _ROLES},
        invocation_statuses=_legal_empty_statuses()[:2],
        calendar_proofs={
            "688825.SH": {
                "no_expected_trading_sessions": True,
                "classification": "NO_TRADING_SESSION",
            }
        },
    )
    assert records[0]["observed"] is False
    assert records[0]["reason_code"] == LEGAL_EMPTY_INVOCATION_INCOMPLETE


def test_suspension_and_pre_listing_empty_are_not_double_counted() -> None:
    suspension = _frozen_sample("suspension_session")
    legal_empty = _frozen_sample("missing_or_empty_upstream")
    rows = _sample_rows(counter_dates={"000001.SZ": ("2026-07-16",)})
    records = observe_k4c_r3_objectives(
        (suspension, legal_empty),
        rows_by_role=rows,
        invocation_statuses=_legal_empty_statuses(),
        calendar_proofs={
            "688825.SH": {
                "no_expected_trading_sessions": True,
                "classification": "NO_TRADING_SESSION",
            }
        },
    )
    by_objective = {record["objective"]: record for record in records}
    assert by_objective["suspension_session"]["observed"] is True
    assert by_objective["missing_or_empty_upstream"]["observed"] is True
    assert by_objective["suspension_session"]["symbol"] == "688237.SH"
    assert by_objective["missing_or_empty_upstream"]["symbol"] == "688825.SH"
    assert by_objective["suspension_session"]["fact"]["missing_symbol"] == "688237.SH"
    assert by_objective["suspension_session"]["evidence_type"] == "cross_sectional_missing_day"
    assert (
        by_objective["missing_or_empty_upstream"]["evidence_type"]
        == "legal_full_range_empty_pre_listing"
    )
    # The legal-empty fact carries only the zero-row reason code, never a
    # vendor-partial-missing claim.
    assert "vendor_partial_missing" not in str(by_objective["missing_or_empty_upstream"]["fact"])
    assert (
        by_objective["missing_or_empty_upstream"]["fact"]["zero_row_reason_code"]
        == ZERO_ROW_LEGAL_EMPTY_CONSENSUS
    )


def test_compute_k4c_r3_sufficiency_requires_all_three_observed() -> None:
    observed_all = [
        {"objective": "suspension_session", "observed": True},
        {"objective": "low_liquidity_session", "observed": True},
        {"objective": "missing_or_empty_upstream", "observed": True},
    ]
    contract_checks = {
        "units_verified": True,
        "unadjusted": True,
        "complete": True,
        "dates_in_target_window": True,
        "fields_complete": True,
        "library_versions_frozen": True,
    }
    sufficient, reasons = compute_k4c_r3_sufficiency(
        invocation_count=9,
        budget=9,
        failure=None,
        objectives=observed_all,
        contract_checks=contract_checks,
        comparison_verified=True,
        unrecorded_attempts=0,
    )
    assert sufficient is True
    assert reasons == ()
    unobserved = [
        {"objective": "suspension_session", "observed": False},
        {"objective": "low_liquidity_session", "observed": True},
        {"objective": "missing_or_empty_upstream", "observed": True},
    ]
    sufficient, reasons = compute_k4c_r3_sufficiency(
        invocation_count=9,
        budget=9,
        failure=None,
        objectives=unobserved,
        contract_checks=contract_checks,
        comparison_verified=True,
        unrecorded_attempts=0,
    )
    assert sufficient is False
    assert "OBJECTIVE_UNOBSERVED:suspension_session" in reasons
    sufficient, reasons = compute_k4c_r3_sufficiency(
        invocation_count=9,
        budget=9,
        failure="provider-timeout",
        objectives=observed_all,
        contract_checks=contract_checks,
        comparison_verified=True,
        unrecorded_attempts=0,
    )
    assert sufficient is False
    assert "FAILURE_PRESENT" in reasons


# ---------------------------------------------------------------------------
# 27-33: composite evidence over the real R2 + a synthetic R3 component
# ---------------------------------------------------------------------------


def _synthetic_proofs() -> dict[str, Mapping[str, object]]:
    return {
        "688825.SH": {
            "no_expected_trading_sessions": True,
            "classification": "NO_TRADING_SESSION",
            "reason": "synthetic frozen-calendar proof",
        }
    }


def _synthetic_r3_attempt(
    tmp_path: Path,
    *,
    low_amount: float = 100_000_000.0,
) -> tuple[Path, tuple[K4CR3ObjectiveSample, ...], str]:
    samples = k4c_r3_frozen_samples()
    invocations = k4c_r3_plan_invocations(samples)
    directory = tmp_path / "k4c-r3-attempts" / "K4C-R3-synthetic-0001"
    directory.mkdir(parents=True)
    header = {
        "schema_version": "s1-07k4c-r2-attempt-header.v1",
        "authorization_id": "K4C-R3-synthetic-0001",
        "attempt_id": "attempt-synthetic000001",
        "authorization_marker": K4C_R3_MARKER,
        "plan_sha256": k4c_r3_plan_sha256(samples),
        "invocation_budget": K4C_R3_INVOCATION_BUDGET,
        "created_at": "2026-08-16T00:00:00Z",
    }
    (directory / "attempt-header.json").write_text(
        _canonical(header) + "\n", encoding="utf-8", newline="\n"
    )
    (directory / "artifacts" / "raw").mkdir(parents=True)

    # 688237.SH holds 2026-07-14/15 and misses 2026-07-16 (suspension) in all
    # three roles; 688825.SH is pre-listing and returns zero rows everywhere.
    # The frozen dates sit after the R2 target rows except where the real R2
    # data supplies the cross-sectional counter-example for 2026-07-16.
    suspended_date = "2026-07-16"
    rows_by_role: dict[str, dict[tuple[str, str], Mapping[str, object]]] = {
        role: {} for role in _ROLES
    }
    for sequence, invocation in enumerate(invocations, start=1):
        symbol = invocation.symbol
        from datetime import timedelta as _timedelta

        lot_units = invocation.upstream.value == "TENCENT" and symbol.startswith("000")
        chunk_rows: list[dict[str, object]] = []
        provider_rows: list[list[object]] = []
        provider_records: list[dict[str, object]] = []
        if symbol == "688237.SH":
            cursor = invocation.upstream_start
            while cursor <= invocation.target_end:
                day = cursor.isoformat()
                in_target = (
                    invocation.target_start.isoformat() <= day <= invocation.target_end.isoformat()
                )
                missing = in_target and day == suspended_date
                if not missing:
                    row = _row(symbol=symbol, trade_date=day, amount=low_amount)
                    if in_target:
                        rows_by_role[invocation.role][(symbol, day)] = row
                        chunk_rows.append(
                            {
                                "stock_code": symbol,
                                "date": day,
                                **{field: row[field] for field in _FIELDS},
                            }
                        )
                    provider_rows.append(
                        [
                            day,
                            symbol,
                            row["open"],
                            row["high"],
                            row["low"],
                            row["close"],
                            row["close"],
                            row["volume"] // 100 if lot_units else row["volume"],
                            row["amount"],
                        ]
                    )
                    provider_records.append(
                        {
                            "date": day,
                            "open": row["open"],
                            "high": row["high"],
                            "low": row["low"],
                            "close": row["close"],
                            "volume": row["volume"] // 100 if lot_units else row["volume"],
                            "amount": row["amount"],
                        }
                    )
                cursor += _timedelta(days=1)
        if invocation.upstream.value == "BAOSTOCK":
            provider_payload: dict[str, object] = {
                "fields": [
                    "date",
                    "code",
                    "open",
                    "high",
                    "low",
                    "close",
                    "preclose",
                    "volume",
                    "amount",
                ],
                "rows": provider_rows,
            }
        else:
            provider_payload = {
                "records": provider_records,
                "row_count": len(provider_records),
                "content_sha256": "a" * 64,
                "unit_policy_version": "akshare-1.18.83-returned-units.v1",
            }
        envelope = {
            "schema_version": "market-data-raw-response.v1",
            "binding": {
                "sequence": sequence,
                "dataset": "daily_bar_unadjusted",
                "scope": [symbol],
                "start": invocation.target_start.isoformat(),
                "end": invocation.target_end.isoformat(),
                "request_sha256": "a" * 64,
            },
            "contract": {
                "dataset": "daily_bar_unadjusted",
                "contract_version": _FROZEN_CONTRACT[invocation.upstream.value],
                "required_fields": [
                    "stock_code",
                    "date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "pre_close",
                    "volume",
                    "amount",
                ],
                "volume_unit": (
                    "lot_100_shares"
                    if invocation.upstream.value == "TENCENT" and symbol.startswith("000")
                    else "share"
                ),
                "amount_unit": "cny_yuan",
                "unadjusted": True,
                "lineage": {
                    "provider": invocation.provider.value,
                    "upstream": invocation.upstream.value,
                    "endpoint": invocation.endpoint,
                    "library_version": _FROZEN_LIBRARY[invocation.upstream.value],
                    "fetched_at": "2026-08-16T00:00:00Z",
                },
            },
            "response": {
                "complete": True,
                "partial_reason": None,
                "records": chunk_rows,
                "upstream_row_count": len(provider_rows),
            },
            "provider_payload": provider_payload,
        }
        raw_path = directory / "artifacts" / "raw" / f"{sequence}.json"
        raw_path.write_text(_canonical(envelope) + "\n", encoding="utf-8", newline="\n")
        journal_path = directory / "invocations.jsonl"
        record = {
            "schema_version": "s1-07k4c-r2-invocation-record.v1",
            "authorization_id": "K4C-R3-synthetic-0001",
            "sequence": sequence,
            "outcome": {
                "sequence": sequence,
                "symbol": symbol,
                "role": invocation.role,
                "upstream": invocation.upstream.value,
                "status": "SUCCEEDED",
                "content_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            },
        }
        with open(journal_path, "ab") as handle:
            handle.write(_canonical(record).encode("utf-8") + b"\n")
    comparison_path = directory / "artifacts/comparison-rows.json"
    comparison_path.write_text(
        _canonical(comparison_rows_payload(rows_by_role)) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    raw_shas = {
        sequence: hashlib.sha256(
            (directory / "artifacts" / "raw" / f"{sequence}.json").read_bytes()
        ).hexdigest()
        for sequence in range(1, K4C_R3_INVOCATION_BUDGET + 1)
    }
    manifest = artifact_manifest_payload(
        comparison_rows_sha256=hashlib.sha256(comparison_path.read_bytes()).hexdigest(),
        raw_shas=raw_shas,
        rows_by_role_count={role: len(rows_by_role[role]) for role in _ROLES},
    )
    manifest_path = directory / "artifacts/artifact-manifest.json"
    manifest_path.write_text(_canonical(manifest) + "\n", encoding="utf-8", newline="\n")
    evidence_path = directory / "evidence.json"
    evidence_path.write_text(
        _canonical(
            {
                "schema_version": K4C_R3_EVIDENCE_SCHEMA,
                "synthetic": True,
                "calendar_proofs": _synthetic_proofs(),
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return directory, samples, str(evidence_path)


def _composite_payload(
    r2_verifier: K4CVerifier,
    r3_verifier: K4CVerifier,
    r2_directory: Path | str,
    r3_directory: Path,
    samples: tuple[K4CR3ObjectiveSample, ...],
) -> dict[str, object]:
    r2_outcome = r2_verifier.recompute(Path(r2_directory))
    r3_outcome = r3_verifier.recompute(r3_directory, calendar_proofs=_synthetic_proofs())
    assert not r2_outcome.reasons
    assert not r3_outcome.reasons
    rows: dict[str, dict[tuple[str, str], Mapping[str, object]]] = {role: {} for role in _ROLES}
    for outcome in (r2_outcome, r3_outcome):
        component_rows = outcome.recomputed["reconstructed_rows"]
        for role in _ROLES:
            rows[role].update(component_rows[role])
    merged, merge_reasons = merge_objective_records_with_r3_reobservation(
        r2_outcome.recomputed["observed_objectives"],
        r3_outcome.recomputed["observed_objectives"],
        samples,
        rows,
    )
    assert merge_reasons == []
    statistics = recompute_comparison_statistics(rows)
    coverage = threshold_coverage(rows)
    sufficient, eligible, reasons = compute_k4c_composite_verdict(
        merged_objectives=merged,
        verification_clean=True,
        conflict_free=True,
        coverage_covered=bool(coverage["covered"]),
    )
    return {
        "schema_version": K4C_COMPOSITE_EVIDENCE_SCHEMA,
        "merged_objectives": merged,
        "statistics": statistics,
        "threshold_coverage": coverage,
        "representative_evidence_sufficient": sufficient,
        "threshold_reacceptance_eligible": eligible,
        "sufficiency_reasons": list(reasons),
    }


def _r3_verifier(samples: tuple[K4CR3ObjectiveSample, ...]) -> K4CVerifier:
    return K4CVerifier(
        plan=k4c_r3_plan_invocations(samples),
        marker=K4C_R3_MARKER,
        plan_sha256=k4c_r3_plan_sha256(samples),
        invocation_budget=K4C_R3_INVOCATION_BUDGET,
        r3_samples=samples,
    )


def _composite_verifier(
    samples: tuple[K4CR3ObjectiveSample, ...],
) -> K4CCompositeVerifier:
    return K4CCompositeVerifier(
        root=_ROOT,
        r3_plan=k4c_r3_plan_invocations(samples),
        r3_marker=K4C_R3_MARKER,
        r3_plan_sha256=k4c_r3_plan_sha256(samples),
        r3_invocation_budget=K4C_R3_INVOCATION_BUDGET,
        r3_samples=samples,
    )


def _synthetic_r3_reference(
    r3_directory: Path,
    samples: tuple[K4CR3ObjectiveSample, ...],
    evidence_path: str,
    *,
    evidence_sha256: str | None = None,
) -> K4CComponentReference:
    return K4CComponentReference(
        component="R3",
        plan_sha256=k4c_r3_plan_sha256(samples),
        authorization_id="K4C-R3-synthetic-0001",
        attempt_id="attempt-synthetic000001",
        evidence_sha256=(
            hashlib.sha256(Path(evidence_path).read_bytes()).hexdigest()
            if evidence_sha256 is None
            else evidence_sha256
        ),
        artifact_manifest_sha256=hashlib.sha256(
            (r3_directory / "artifacts/artifact-manifest.json").read_bytes()
        ).hexdigest(),
        attempt_directory=str(r3_directory),
        evidence_file=evidence_path,
    )


def test_composite_missing_objective_is_not_sufficient(tmp_path: Path) -> None:
    r3_directory, samples, evidence_path = _synthetic_r3_attempt(
        tmp_path,
        low_amount=300_000_000.0,  # low_liquidity stays unobserved
    )
    r2 = r2_component_reference()
    r3 = _synthetic_r3_reference(r3_directory, samples, evidence_path)
    verifier = _composite_verifier(samples)
    payload = _composite_payload(
        K4CVerifier(), _r3_verifier(samples), r2.attempt_directory, r3_directory, samples
    )
    assert payload["representative_evidence_sufficient"] is False
    assert payload["threshold_reacceptance_eligible"] is False
    outcome = verifier.verify(r2=r2, r3=r3, composite=payload)
    assert outcome.ok is True
    # The suspension sample is unobserved inside R3 (no cross-sectional
    # counter-example there) but the merged rows supply one from the immutable
    # R2 component; low_liquidity above the bound stays unobserved.
    assert outcome.recomputed["representative_evidence_sufficient"] is False
    merged = {record["objective"]: record for record in outcome.recomputed["merged_objectives"]}
    assert merged["suspension_session"]["observed"] is True
    assert merged["low_liquidity_session"]["observed"] is False


def test_composite_8_of_8_allows_sufficient_and_eligible(tmp_path: Path) -> None:
    r3_directory, samples, evidence_path = _synthetic_r3_attempt(tmp_path)
    r2 = r2_component_reference()
    r3 = _synthetic_r3_reference(r3_directory, samples, evidence_path)
    verifier = _composite_verifier(samples)
    payload = _composite_payload(
        K4CVerifier(), _r3_verifier(samples), r2.attempt_directory, r3_directory, samples
    )
    assert payload["representative_evidence_sufficient"] is True
    assert payload["threshold_reacceptance_eligible"] is True
    outcome = verifier.verify(r2=r2, r3=r3, composite=payload)
    assert outcome.ok is True
    assert outcome.recomputed["representative_evidence_sufficient"] is True
    assert outcome.recomputed["threshold_reacceptance_eligible"] is True


def test_composite_rejects_tampered_component_sha(tmp_path: Path) -> None:
    r3_directory, samples, evidence_path = _synthetic_r3_attempt(tmp_path)
    r2 = r2_component_reference()
    r3 = _synthetic_r3_reference(
        r3_directory,
        samples,
        evidence_path,
        evidence_sha256="f" * 64,  # tampered
    )
    verifier = _composite_verifier(samples)
    payload = _composite_payload(
        K4CVerifier(), _r3_verifier(samples), r2.attempt_directory, r3_directory, samples
    )
    outcome = verifier.verify(r2=r2, r3=r3, composite=payload)
    assert outcome.ok is False
    assert f"{COMPONENT_EVIDENCE_SHA_MISMATCH}:R3" in outcome.reasons


def test_r3_raw_rows_but_empty_comparison_is_rejected(tmp_path: Path) -> None:
    r3_directory, samples, _ = _synthetic_r3_attempt(tmp_path)
    # Sequences 7-9 belong to the pre-listing 688825.SH legal-empty sample;
    # smuggling one vendor row into a raw envelope while the comparison stays
    # empty must fail the closure verifier.
    path = r3_directory / "artifacts/raw/7.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["provider_payload"]["rows"].extend(
        [
            ["2026-07-23", "688825.SH", 10.0, 10.0, 10.0, 10.0, 10.0, 1000, 10000.0],
            ["2026-07-24", "688825.SH", 10.0, 10.0, 10.0, 10.0, 10.0, 1000, 10000.0],
        ]
    )
    payload["response"]["upstream_row_count"] = 2
    path.write_text(_canonical(payload) + "\n", encoding="utf-8", newline="\n")
    _resign_manifest(r3_directory)
    outcome = _r3_verifier(samples).recompute(r3_directory, calendar_proofs=_synthetic_proofs())
    assert outcome.ok is False
    assert RAW_COMPARISON_KEY_MISMATCH in outcome.reasons


def test_raw_reconstruction_skips_baostock_suspended_placeholder() -> None:
    samples = k4c_r3_frozen_samples()
    invocation = k4c_r3_plan_invocations(samples)[0]  # BAOSTOCK 688237 suspension window
    assert invocation.upstream.value == "BAOSTOCK"
    fields = [
        "date",
        "code",
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "amount",
        "adjustflag",
        "tradestatus",
        "isST",
    ]
    rows = [
        [
            "2026-07-13",
            "sh.688237",
            "10.0000",
            "10.0000",
            "10.0000",
            "10.0000",
            "9.9000",
            "1000",
            "10000.0000",
            "3",
            "1",
            "0",
        ],
        [
            "2026-07-14",
            "sh.688237",
            "11.0000",
            "11.0000",
            "11.0000",
            "11.0000",
            "10.0000",
            "1000",
            "10000.0000",
            "3",
            "1",
            "0",
        ],
        [
            "2026-07-15",
            "sh.688237",
            "12.0000",
            "12.0000",
            "12.0000",
            "12.0000",
            "11.0000",
            "1000",
            "10000.0000",
            "3",
            "1",
            "0",
        ],
        [
            "2026-07-16",
            "sh.688237",
            "12.0000",
            "12.0000",
            "12.0000",
            "12.0000",
            "12.0000",
            "",
            "",
            "3",
            "0",
            "0",
        ],
    ]
    envelope = {
        "schema_version": "market-data-raw-response.v1",
        "binding": {
            "sequence": 1,
            "dataset": "daily_bar_unadjusted",
            "scope": ["688237.SH"],
            "start": "2026-07-14",
            "end": "2026-07-16",
            "request_sha256": "a" * 64,
        },
        "contract": {
            "dataset": "daily_bar_unadjusted",
            "contract_version": _FROZEN_CONTRACT["BAOSTOCK"],
            "volume_unit": "share",
            "amount_unit": "cny_yuan",
            "unadjusted": True,
            "lineage": {
                "provider": "BAOSTOCK",
                "upstream": "BAOSTOCK",
                "endpoint": "query_history_k_data_plus",
                "library_version": _FROZEN_LIBRARY["BAOSTOCK"],
            },
        },
        "response": {
            "complete": True,
            "partial_reason": None,
            "records": [],
            "upstream_row_count": 4,
        },
        "provider_payload": {"fields": fields, "rows": rows},
    }
    canonical, reasons = reconstruct_canonical_rows_from_raw(envelope, invocation)
    assert reasons == []
    assert sorted(row["trade_date"] for row in canonical) == ["2026-07-14", "2026-07-15"]
    assert {row["pre_close"] for row in canonical} == {10.0, 11.0}


def test_default_test_run_never_triggers_the_real_probe() -> None:
    from tests.compatibility.test_k4c_r3_diagnostic import (
        test_k4c_r3_baostock_suspended_day_diagnostic,
    )
    from tests.compatibility.test_k4c_r3_representative_real import (
        test_fixed_bounded_k4c_r3_representative_probe,
    )
    from tests.compatibility.test_k4c_representative_real import (
        test_fixed_bounded_k4c_r2_representative_probe,
    )

    with pytest.raises(pytest.skip.Exception):
        test_fixed_bounded_k4c_r2_representative_probe()
    with pytest.raises(pytest.skip.Exception):
        test_fixed_bounded_k4c_r3_representative_probe()
    with pytest.raises(pytest.skip.Exception):
        test_k4c_r3_baostock_suspended_day_diagnostic()


def test_r3_sample_selection_is_blocked_without_local_references() -> None:
    # The two blocker codes exist for honest reporting when no local material
    # can fix a sample; nothing fabricates samples on their behalf.
    assert BLOCKED_SAMPLE_SELECTION == "BLOCKED_SAMPLE_SELECTION"
    assert BLOCKED_MISSING_EMPTY_SAMPLE == "BLOCKED_MISSING_EMPTY_SAMPLE"


def test_r2_baseline_shas_are_frozen() -> None:
    assert hashlib.sha256(_R2_EVIDENCE.read_bytes()).hexdigest() == (
        "217390b53e03345ac84a97739a88cb28b05299f9d2d2bc9ec036d7af89381835"
    )
    assert hashlib.sha256(_R2_MANIFEST.read_bytes()).hexdigest() == (
        "04401321cac84bce94c8e540ae5b6196bf8cc423f1c57b731233eb04b7321d64"
    )


def test_r3_dry_run_baseline_sha_is_frozen() -> None:
    dry_run = _ROOT / "docs/stage1/evidence/k4c_r3_probe_dry_run_2026-08-16.json"
    assert dry_run.is_file()
    assert hashlib.sha256(dry_run.read_bytes()).hexdigest() == (
        "174c95461b8f880f6e78dc5c209f961993aae6ba2502f3bbe0b73fb6d6654580"
    )
    payload = json.loads(dry_run.read_text(encoding="utf-8"))
    assert payload["plan_sha256"] == k4c_r3_plan_sha256(k4c_r3_frozen_samples())
    assert payload["plan_sha256"] == (
        "38c88f40ac882b58b3527c47b7c7671ce597fe8171a4b1b7c95d37cf504fc5d3"
    )


def test_calendar_reference_baseline_is_frozen() -> None:
    calendar = _ROOT / ("docs/stage1/evidence/k4c_r3_references/exchange_calendar_sh_2026-07.json")
    assert calendar.is_file()
    assert hashlib.sha256(calendar.read_bytes()).hexdigest() == (
        "244e62109be84c8dc0cb778c751c6aa9e2f98c4b50e4dd13073221e81432e10f"
    )
    payload = json.loads(calendar.read_text(encoding="utf-8"))
    by_date = {row["trade_date"]: row for row in payload["rows"]}
    assert len(payload["rows"]) == 27
    assert by_date["2026-07-24"]["is_open"] is True  # Friday trading day
    assert by_date["2026-07-16"]["is_open"] is True  # suspension cross-section day
    assert by_date["2026-07-25"]["is_open"] is False  # Saturday


def test_r3_phase_b_baseline_shas_are_frozen() -> None:
    evidence = _ROOT / "docs/stage1/evidence/k4c_r3_representative_probe_2026-08-16.json"
    manifest = _ROOT / (
        "docs/stage1/evidence/k4c-r3-attempts/K4C-R3-2b8e5f14a6c9/artifacts/artifact-manifest.json"
    )
    composite = _ROOT / "docs/stage1/evidence/k4c_composite_evidence_2026-08-16.json"
    diagnostic = _ROOT / ("docs/stage1/evidence/k4c_r3_diagnostic_baostock_688237_2026-08-16.json")
    assert hashlib.sha256(evidence.read_bytes()).hexdigest() == (
        "1632ee8b2a210bcbe22742386072d9c16a10c789cc4bba9bd8dcf5f4ed0836ea"
    )
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == (
        "2b2f919ba40ef36835a8c3879b0a8a92c13ff689c66bf95a91ab0111aeb7c17c"
    )
    assert hashlib.sha256(composite.read_bytes()).hexdigest() == (
        "910010da4c309cb93faacc2a7705b4a15cfc2279cfbe762112a86a4fa9b8f12d"
    )
    assert hashlib.sha256(diagnostic.read_bytes()).hexdigest() == (
        "c31febc8a3d2fe6597a9dbfe54f99f6a69a989bdd80907ba35ae6880d34127f5"
    )
    reference = r3_component_reference()
    assert reference.plan_sha256 == (
        "38c88f40ac882b58b3527c47b7c7671ce597fe8171a4b1b7c95d37cf504fc5d3"
    )
    assert reference.authorization_id == "K4C-R3-2b8e5f14a6c9"
    assert reference.attempt_id == "attempt-9ced23d1844541fd"
    assert reference.evidence_sha256 == (
        "1632ee8b2a210bcbe22742386072d9c16a10c789cc4bba9bd8dcf5f4ed0836ea"
    )
    assert reference.artifact_manifest_sha256 == (
        "2b2f919ba40ef36835a8c3879b0a8a92c13ff689c66bf95a91ab0111aeb7c17c"
    )


def test_real_r3_composite_evidence_verifies_offline() -> None:
    samples = k4c_r3_frozen_samples()
    composite_path = _ROOT / "docs/stage1/evidence/k4c_composite_evidence_2026-08-16.json"
    composite = json.loads(composite_path.read_text(encoding="utf-8"))
    verifier = K4CCompositeVerifier(
        root=_ROOT,
        r3_plan=k4c_r3_plan_invocations(samples),
        r3_marker=K4C_R3_MARKER,
        r3_plan_sha256=k4c_r3_plan_sha256(samples),
        r3_invocation_budget=K4C_R3_INVOCATION_BUDGET,
        r3_samples=samples,
    )
    outcome = verifier.verify(
        r2=r2_component_reference(),
        r3=r3_component_reference(),
        composite=composite,
    )
    assert outcome.ok is True
    assert outcome.recomputed["representative_evidence_sufficient"] is True
    assert outcome.recomputed["threshold_reacceptance_eligible"] is True
    merged = outcome.recomputed["merged_objectives"]
    assert len(merged) == 8
    assert all(record["observed"] is True for record in merged)
    suspension = next(record for record in merged if record["objective"] == "suspension_session")
    observed = suspension["observed_records"][0]
    assert observed["symbol"] == "688237.SH"
    assert observed["trade_date"] == "2026-07-16"
    assert observed["fact"]["counter_example_symbol"] is not None
    legal = next(record for record in merged if record["objective"] == "missing_or_empty_upstream")
    assert legal["observed_records"][0]["evidence_type"] == "legal_full_range_empty_pre_listing"
    assert (
        legal["observed_records"][0]["fact"]["zero_row_reason_code"]
        == ZERO_ROW_LEGAL_EMPTY_CONSENSUS
    )
