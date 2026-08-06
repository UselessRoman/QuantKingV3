"""S1-07C0A versioned evidence-contract and downgrade attack regressions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

import pytest

from quantking.data.assessment.models import AssessmentStatus, EvidenceKind
from quantking.data.validation.artifacts import (
    ValidationEvidenceVerificationError,
    ValidationEvidenceVerifier,
)
from quantking.data.validation.artifacts_v3 import (
    ValidationEvidenceVerifierV3,
    ValidationEvidenceWriterV3,
)
from quantking.data.validation.contracts_v3 import (
    AttemptOutcomeV3,
    FakeV3RunnerTrustStore,
    canonical_json,
    sha256_json,
)
from quantking.data.validation.models import ValidationFailure
from quantking.data.validation.samples import load_packaged_fixed_sample
from quantking.data.validation.testing import build_deep_fake_run
from quantking.data.validation.testing_v3 import build_v3_context, build_v3_run


def _rewrite_outer(directory: Path) -> None:
    """Model an attacker who recomputes only ordinary manifest SHA values."""
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    entries = []
    for entry in manifest["files"]:
        relative = str(entry["relative_path"])
        payload = (directory / relative).read_bytes()
        entries.append(
            {
                "relative_path": relative,
                "row_count": entry["row_count"],
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        )
    manifest["files"] = entries
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json(unsigned)).hexdigest()
    manifest_path.write_bytes(canonical_json(manifest))
    (directory / "COMPLETE").write_bytes(
        canonical_json({"manifest_sha256": manifest["manifest_sha256"]})
    )


def _rewrite_request(directory: Path, mutate: object) -> None:
    request_path = directory / "request" / "manifest.json"
    request = json.loads(request_path.read_bytes())
    assert callable(mutate)
    mutate(request)
    unsigned = dict(request)
    unsigned.pop("request_sha256", None)
    request["request_sha256"] = sha256_json(unsigned)
    request_path.write_bytes(canonical_json(request))
    _rewrite_outer(directory)


def _rewrite_summary(summary: Path, mutate: object) -> None:
    manifest_path = summary / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    assert callable(mutate)
    mutate(manifest)
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json(unsigned)).hexdigest()
    manifest_path.write_bytes(canonical_json(manifest))
    (summary / "COMPLETE").write_bytes(
        canonical_json({"manifest_sha256": manifest["manifest_sha256"]})
    )


def test_v2_artifacts_keep_accepted_bytes_and_v3_is_explicitly_dispatched(tmp_path: Path) -> None:
    """C0A must not rewrite the S1-07A v2 trust anchor or its verifier path."""
    v2 = build_deep_fake_run(root=tmp_path, run_index=1)
    v2_release_bytes = (
        files("quantking.data.validation").joinpath("fixed_sample.release.v2.json").read_bytes()
    )
    assert hashlib.sha256(v2_release_bytes).hexdigest() == (
        "2b692c631018c1e600680af329d3936dd98267d9d82ffdbcbe854b3ca8574cb8"
    )
    v2_sample = load_packaged_fixed_sample()
    assert v2_sample.sample_spec_version == "s1-07.fixed-sample.v2"
    assert v2_sample.sample_spec_sha256 == (
        "03d3d6c1e898b0746b7c5ab3911917a600fa3975f802c1d414fd0b6196c80dfb"
    )
    assert json.loads((v2.directory / "manifest.json").read_bytes())["manifest_schema_version"] == (
        "source-validation-envelope.v2"
    )
    assert (
        v2.verifier.verify(directory=v2.directory).assessment.status
        is AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION
    )
    with pytest.raises(ValidationEvidenceVerificationError):
        ValidationEvidenceVerifierV3(
            validation_root=tmp_path,
            context=build_v3_context(),
            runner_attestation_verifier=FakeV3RunnerTrustStore(),
        ).verify_run(directory=v2.directory)

    v3 = build_v3_run(root=tmp_path, run_index=2)
    with pytest.raises(ValidationEvidenceVerificationError, match="v3 trust context"):
        ValidationEvidenceVerifier(validation_root=tmp_path).verify(directory=v3.directory)
    dispatched = ValidationEvidenceVerifier(
        validation_root=tmp_path,
        v3_trust_context=v3.context,
        v3_runner_attestation_verifier=FakeV3RunnerTrustStore(),
    ).verify(directory=v3.directory)
    assert dispatched.assessment.evidence_kind is EvidenceKind.SYNTHETIC


@pytest.mark.parametrize(
    "mutation",
    [
        lambda request: request.update(
            {
                "selected_case_ids": ["calendar_sh"],
                "selected_cases": [
                    case for case in request["selected_cases"] if case["case_id"] == "calendar_sh"
                ],
            }
        ),
        lambda request: request.update(
            {"selected_case_ids": ["calendar_bj", "calendar_bj", "calendar_sh"]}
        ),
        lambda request: request.update({"selected_case_ids": ["calendar_sh", "calendar_bj"]}),
        lambda request: request.update(
            {
                "selected_case_ids": ["calendar_bj", "calendar_sh", "calendar_sz"],
                "selected_cases": request["selected_cases"]
                + [
                    case.to_dict()
                    for case in build_v3_context().sample.cases
                    if case.case_id == "calendar_sz"
                ],
            }
        ),
    ],
    ids=("reduction", "duplicate", "reorder", "addition"),
)
def test_v3_rejects_case_set_reduction_addition_duplicate_and_reorder(
    tmp_path: Path, mutation: object
) -> None:
    built = build_v3_run(
        root=tmp_path,
        run_index=1,
        selected_case_ids=("calendar_bj", "calendar_sh"),
    )
    _rewrite_request(built.directory, mutation)
    with pytest.raises(ValidationEvidenceVerificationError):
        built.verifier.verify_run(directory=built.directory)


def test_v3_rejects_reference_bundle_and_source_profile_substitution(tmp_path: Path) -> None:
    bundle = build_v3_run(root=tmp_path, run_index=1)
    _rewrite_request(
        bundle.directory,
        lambda request: request["promotion_profile"].update(
            {
                "reference_bundle_id": "attacker-bundle.v1",
                "reference_bundle_manifest_sha256": "c" * 64,
            }
        ),
    )
    with pytest.raises(ValidationEvidenceVerificationError):
        bundle.verifier.verify_run(directory=bundle.directory)

    profile = build_v3_run(root=tmp_path, run_index=2)
    _rewrite_request(
        profile.directory,
        lambda request: request["source_dataset_contract"].update(
            {"contract_version": "attacker-contract.v9"}
        ),
    )
    with pytest.raises(ValidationEvidenceVerificationError):
        profile.verifier.verify_run(directory=profile.directory)


def test_v3_rejects_resigned_or_replaced_signer(tmp_path: Path) -> None:
    built = build_v3_run(root=tmp_path, run_index=1)
    path = built.directory / "runner" / "result-attestation.v2.json"
    payload = json.loads(path.read_bytes())
    payload["signer_key_id"] = "attacker-re-signer"
    payload["signature"] = "a" * 64
    path.write_bytes(canonical_json(payload))
    _rewrite_outer(built.directory)
    with pytest.raises(ValidationEvidenceVerificationError, match="untrusted"):
        built.verifier.verify_run(directory=built.directory)


@pytest.mark.parametrize(
    "category",
    ("CAPABILITY_UNSUPPORTED", "PERMISSION_DENIED", "RATE_LIMITED", "EXTERNAL_UNAVAILABLE"),
)
def test_v3_trusted_failures_are_blocked_and_have_no_success_credentials(
    tmp_path: Path, category: str
) -> None:
    built = build_v3_run(
        root=tmp_path,
        run_index=1,
        outcome=AttemptOutcomeV3.FAILED,
        failure=ValidationFailure(category, retryable=category != "CAPABILITY_UNSUPPORTED"),
    )
    verified = built.verifier.verify(directory=built.directory)
    assert verified.assessment.evidence_kind is EvidenceKind.SYNTHETIC
    assert verified.assessment.status is AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION
    assert verified.verified_runs[0].failure is not None
    assert not verified.verified_runs[0].gates.passed
    assert not any(
        path.parts[0] in {"raw-root", "canonical", "quality-root", "difference-root"}
        for path in built.directory.rglob("*")
        if path.is_file()
    )


def test_v3_failure_cannot_smuggle_success_files_even_if_outer_sha_is_recomputed(
    tmp_path: Path,
) -> None:
    built = build_v3_run(root=tmp_path, run_index=1, outcome=AttemptOutcomeV3.FAILED)
    smuggled = built.directory / "canonical" / "content.parquet"
    smuggled.parent.mkdir()
    smuggled.write_bytes(b"not-a-canonical-success-artifact")
    manifest = json.loads((built.directory / "manifest.json").read_bytes())
    manifest["files"].append(
        {
            "relative_path": "canonical/content.parquet",
            "row_count": 1,
            "sha256": hashlib.sha256(smuggled.read_bytes()).hexdigest(),
            "size": smuggled.stat().st_size,
        }
    )
    _rewrite_outer(built.directory)
    with pytest.raises(ValidationEvidenceVerificationError):
        built.verifier.verify_run(directory=built.directory)


def test_v3_writer_refuses_a_failed_attempt_with_success_credentials(tmp_path: Path) -> None:
    built = build_v3_run(root=tmp_path, run_index=1, outcome=AttemptOutcomeV3.FAILED)
    with pytest.raises(ValueError, match="failed v3 attempts"):
        ValidationEvidenceWriterV3(validation_root=tmp_path).write(
            envelope=built.envelope,
            files={
                "request/manifest.json": (b"{}", 0),
                "runner/result-attestation.v2.json": (b"{}", 0),
                "failure/attempt.json": (b"{}", 0),
                "canonical/content.parquet": (b"smuggled", 1),
            },
        )


def test_v3_assessment_rejects_forged_gates_and_recomputed_plain_sha(tmp_path: Path) -> None:
    first = build_v3_run(root=tmp_path, run_index=1)
    second = build_v3_run(root=tmp_path, run_index=2)
    writer = ValidationEvidenceWriterV3(validation_root=tmp_path)
    writer.finalize_assessment(assessment_directory=first.directory.parent, verifier=first.verifier)
    summary = first.directory.parent / "_assessment-v3"
    _rewrite_summary(
        summary,
        lambda manifest: manifest.update(
            {
                "gates": {
                    "semantic_contract_verified": True,
                    "schema_unit_verified": True,
                    "coverage_adequate": False,
                    "quality_passed": True,
                    "fixed_sample_differences_adequate": True,
                }
            }
        ),
    )
    with pytest.raises(ValidationEvidenceVerificationError, match="summary"):
        first.verifier.verify_assessment(assessment_directory=first.directory.parent)
    assert second.envelope.run_fingerprint == first.envelope.run_fingerprint


def test_single_case_attempt_never_promotes_a_source_dataset(tmp_path: Path) -> None:
    built = build_v3_run(root=tmp_path, run_index=1, selected_case_ids=("calendar_sh",))
    verified = built.verifier.verify(directory=built.directory)
    assert verified.assessment.status is AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION
    assert "selected case IDs do not satisfy the promotion profile" in str(
        verified.assessment.blocking_reason
    )


def test_v3_assessment_rejects_mixed_case_bundle_profile_and_fingerprint_contexts(
    tmp_path: Path,
) -> None:
    first = build_v3_run(root=tmp_path, run_index=1, selected_case_ids=("calendar_sh",))
    build_v3_run(root=tmp_path, run_index=2, selected_case_ids=("calendar_bj",))
    with pytest.raises(ValueError, match="mixed frozen evidence context"):
        ValidationEvidenceWriterV3(validation_root=tmp_path).finalize_assessment(
            assessment_directory=first.directory.parent,
            verifier=first.verifier,
        )

    other_context = replace(
        build_v3_context(),
        promotion_profile=replace(
            build_v3_context().promotion_profile,
            reference_bundle_manifest_sha256="d" * 64,
        ),
    )
    drifted = build_v3_run(root=tmp_path, run_index=3, context=other_context)
    with pytest.raises(ValidationEvidenceVerificationError):
        first.verifier.verify_run(directory=drifted.directory)
