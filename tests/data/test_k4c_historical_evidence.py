"""S1-07K4C historical evidence files are immutable byte fixtures.

The independent acceptance recorded the SHA-256 of every accepted historical
evidence file. These tests keep those bytes stable: nothing may rewrite the
v1 dry-run, the attempt-a evidence or the final 2026-08-15 evidence. Attempt
b has no evidence file at all and must never be fabricated retroactively.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from quantking.history.stage1.k4c_probe import fixed_k4c_probe_plan

_ROOT = Path(__file__).resolve().parents[2]

_HISTORICAL_EVIDENCE = {
    "docs/stage1/evidence/k4c_representative_probe_dry_run_2026-08-14.json": (
        "e5f180816e4c19a2425544a866a36f7336c9ca81a20624fbd24f2a584a368756"
    ),
    "docs/stage1/evidence/k4c_representative_probe_attempt_2026-08-15_a.json": (
        "ed26d6164479ffb0eb2a36414d89f3c207e04424a434ddaf14455e0180f10bfb"
    ),
    "docs/stage1/evidence/k4c_representative_probe_2026-08-15.json": (
        "eeabbb637112020045684fa306c5ad12e7f1edc1f74a8d042bc475c256e6ec17"
    ),
    "docs/stage1/evidence/k4c_r2_representative_probe_2026-08-16.json": (
        "217390b53e03345ac84a97739a88cb28b05299f9d2d2bc9ec036d7af89381835"
    ),
}


def test_historical_k4c_evidence_files_are_byte_stable() -> None:
    for relative, expected in _HISTORICAL_EVIDENCE.items():
        data = (_ROOT / relative).read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected


def test_attempt_b_has_no_evidence_file_and_is_not_fabricated() -> None:
    assert not (
        _ROOT / "docs/stage1/evidence/k4c_representative_probe_attempt_2026-08-15_b.json"
    ).exists()


def test_k4c_r2_attempt_artifacts_are_present_and_complete() -> None:
    attempt = _ROOT / "docs/stage1/evidence/k4c-r2-attempts/K4C-R2-cab803399515"
    assert (attempt / "attempt-header.json").is_file()
    assert (attempt / "artifacts/comparison-rows.json").is_file()
    assert (attempt / "artifacts/artifact-manifest.json").is_file()
    journal = (attempt / "invocations.jsonl").read_text(encoding="utf-8")
    assert journal.count("\n") == 36
    for sequence in range(1, 37):
        assert (attempt / "artifacts" / "raw" / f"{sequence}.json").is_file()


def test_k4c_r2_evidence_recomputes_offline() -> None:
    from quantking.history.stage1.k4c_evidence import K4CVerifier

    attempt = _ROOT / "docs/stage1/evidence/k4c-r2-attempts/K4C-R2-cab803399515"
    outcome = K4CVerifier().recompute(attempt)
    assert outcome.reasons == ()
    assert outcome.recomputed["invocation_count"] == 36
    assert outcome.recomputed["not_called_invocation_count"] == 0
    assert outcome.recomputed["representative_evidence_sufficient"] is False
    assert set(outcome.recomputed["sufficiency_reasons"]) == {
        "OBJECTIVE_UNOBSERVED:suspension_session",
        "OBJECTIVE_UNOBSERVED:low_liquidity_session",
        "OBJECTIVE_UNOBSERVED:missing_or_empty_upstream",
    }
    statistics = outcome.recomputed["statistics"]
    assert statistics["fields"]["volume"]["max_absolute"]["absolute_difference"] == "50"
    assert statistics["fields"]["amount"]["max_absolute"]["absolute_difference"] == "50"
    assert outcome.recomputed["threshold_coverage"]["covered"] is True


def test_retired_v1_plan_geometry_remains_frozen() -> None:
    plan = fixed_k4c_probe_plan()
    assert len(plan) == 36
    assert {item.role for item in plan} == {"A", "B", "C"}
    assert {item.symbol for item in plan} == {
        "000001.SZ",
        "600000.SH",
        "000858.SZ",
        "300750.SZ",
        "688981.SH",
        "600519.SH",
    }
    for invocation in plan:
        assert (invocation.target_start - invocation.upstream_start).days == 15
        assert invocation.target_start <= invocation.target_end
