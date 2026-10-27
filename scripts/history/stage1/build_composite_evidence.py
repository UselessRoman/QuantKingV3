"""Build the frozen Stage 1 composite evidence by SHA reference (offline).

This script never touches the network: it re-reads both immutable components
(R2 attempt directory and R3 attempt directory), re-runs the offline closure
verifiers on each, merges objective records (with the frozen-sample
re-observation over merged rows), recomputes statistics and threshold
coverage, and only writes the composite JSON after the composite verifier
cross-checks the payload.

``--schema v2`` (or ``--r4``) builds the R4 acceptance-repair composite:
``s1-07k4c-composite-evidence.v2`` with the official suspension provenance
binding and the BaoStock raw ``tradestatus=0`` fact. The default remains the
historical v1 schema. Without ``--output`` the payload is printed to stdout;
real placement into ``docs/stage1/evidence`` is a repository write and
requires the file-system authorization of the caller.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path

from quantking.history.stage1.k4c_evidence import (
    K4C_COMPOSITE_EVIDENCE_SCHEMA,
    K4C_R3_INVOCATION_BUDGET,
    K4C_R3_MARKER,
    K4C_R4_COMPOSITE_EVIDENCE_SCHEMA,
    K4C_R4_VERIFICATION_CONTRACT_VERSION,
    K4CComponentReference,
    K4CCompositeVerifier,
    K4CVerifier,
    compute_k4c_composite_verdict,
    k4c_r3_frozen_samples,
    k4c_r3_plan_invocations,
    k4c_r3_plan_sha256,
    k4c_r4_suspension_raw_fact,
    merge_objective_records_with_r3_reobservation,
    r2_component_reference,
    r3_component_reference,
    recompute_comparison_statistics,
    threshold_coverage,
    validate_k4c_r4_suspension_reference,
)

_ROLES = ("A", "B", "C")
_SUSPENSION_REFERENCE_FILE = (
    "docs/stage1/evidence/k4c_r4_references/suspension_688237.SH_2026-07-16_sse.json"
)


def _load_calendar_proofs(r3: K4CComponentReference, root: Path) -> dict[str, Mapping[str, object]]:
    evidence_path = root / r3.evidence_file
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    declared = payload.get("calendar_proofs")
    if not isinstance(declared, dict):
        raise ValueError("R3 evidence lacks calendar_proofs")
    return declared


def _load_suspension_reference(root: Path) -> dict[str, object]:
    path = root / _SUSPENSION_REFERENCE_FILE
    content = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(content, dict):
        raise ValueError("suspension reference must be a JSON object")
    reference = {**content, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    reasons = validate_k4c_r4_suspension_reference(reference)
    if reasons:
        raise ValueError(f"suspension reference invalid: {reasons}")
    return {
        "file": _SUSPENSION_REFERENCE_FILE,
        "sha256": reference["sha256"],
        **content,
    }


def build_payload(root: Path, *, schema_version: str) -> dict[str, object]:
    if schema_version not in {K4C_COMPOSITE_EVIDENCE_SCHEMA, K4C_R4_COMPOSITE_EVIDENCE_SCHEMA}:
        raise ValueError("schema_version must be a frozen composite evidence schema")
    v2 = schema_version == K4C_R4_COMPOSITE_EVIDENCE_SCHEMA
    samples = k4c_r3_frozen_samples()
    r2 = r2_component_reference()
    r3 = r3_component_reference()
    calendar_proofs = _load_calendar_proofs(r3, root)
    r3_verifier = K4CVerifier(
        plan=k4c_r3_plan_invocations(samples),
        marker=K4C_R3_MARKER,
        plan_sha256=k4c_r3_plan_sha256(samples),
        invocation_budget=K4C_R3_INVOCATION_BUDGET,
        r3_samples=samples,
    )
    r2_outcome = K4CVerifier().recompute(root / r2.attempt_directory)
    r3_outcome = r3_verifier.recompute(root / r3.attempt_directory, calendar_proofs=calendar_proofs)
    if r2_outcome.reasons:
        raise ValueError(f"R2 component verification failed: {r2_outcome.reasons}")
    if r3_outcome.reasons:
        raise ValueError(f"R3 component verification failed: {r3_outcome.reasons}")
    rows: dict[str, dict[tuple[str, str], Mapping[str, object]]] = {role: {} for role in _ROLES}
    for outcome in (r2_outcome, r3_outcome):
        component_rows = outcome.recomputed["reconstructed_rows"]
        for role in _ROLES:
            rows[role].update(component_rows[role])
    suspension_reference = _load_suspension_reference(root) if v2 else None
    suspension_raw_fact = k4c_r4_suspension_raw_fact(root / r3.attempt_directory) if v2 else None
    if v2 and suspension_raw_fact is None:
        raise ValueError("BaoStock suspended-day raw fact is missing from R3 raw sequence 1")
    merged, merge_reasons = merge_objective_records_with_r3_reobservation(
        r2_outcome.recomputed["observed_objectives"],
        r3_outcome.recomputed["observed_objectives"],
        samples,
        rows,
        require_suspension_provenance=v2,
        suspension_reference=suspension_reference,
        suspension_raw_fact=suspension_raw_fact,
    )
    if merge_reasons:
        raise ValueError(f"objective merge failed: {merge_reasons}")
    statistics = recompute_comparison_statistics(rows)
    coverage = threshold_coverage(rows)
    sufficient, eligible, reasons = compute_k4c_composite_verdict(
        merged_objectives=merged,
        verification_clean=True,
        conflict_free=True,
        coverage_covered=bool(coverage["covered"]),
    )
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "r2_component": r2.to_dict(),
        "r3_component": r3.to_dict(),
        "merged_objectives": merged,
        "statistics": statistics,
        "threshold_coverage": coverage,
        "representative_evidence_sufficient": sufficient,
        "threshold_reacceptance_eligible": eligible,
        "sufficiency_reasons": list(reasons),
    }
    if v2:
        payload["provenance_note"] = (
            "offline acceptance repair layer (K4C-R4); not a new real network attempt"
        )
        payload["verification_contract_version"] = K4C_R4_VERIFICATION_CONTRACT_VERSION
        payload["suspension_reference"] = suspension_reference
        payload["suspension_raw_fact"] = suspension_raw_fact
    verifier = K4CCompositeVerifier(
        root=root,
        r3_plan=k4c_r3_plan_invocations(samples),
        r3_marker=K4C_R3_MARKER,
        r3_plan_sha256=k4c_r3_plan_sha256(samples),
        r3_invocation_budget=K4C_R3_INVOCATION_BUDGET,
        r3_samples=samples,
        schema_version=schema_version,
    )
    outcome = verifier.verify(r2=r2, r3=r3, composite=payload)
    if not outcome.ok:
        raise ValueError(f"composite verification failed: {outcome.reasons}")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="K4C composite evidence builder (offline)")
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--output", help="write the composite JSON to this path")
    parser.add_argument(
        "--schema",
        choices=("v1", "v2"),
        default="v1",
        help="composite evidence schema (default v1, historical)",
    )
    arguments = parser.parse_args(argv)
    root = Path(arguments.root)
    schema_version = (
        K4C_R4_COMPOSITE_EVIDENCE_SCHEMA
        if arguments.schema == "v2"
        else K4C_COMPOSITE_EVIDENCE_SCHEMA
    )
    payload = build_payload(root, schema_version=schema_version)
    text = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    if arguments.output:
        Path(arguments.output).write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
