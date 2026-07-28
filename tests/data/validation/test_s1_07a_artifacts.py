"""S1-07A immutable evidence writer and verifier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _synthetic_bundle(
    root: Path, *, run_text: str | None = None, run_index: int = 1
) -> tuple[object, object]:
    from quantking.data.validation.testing import build_synthetic_evidence_bundle

    return build_synthetic_evidence_bundle(root=root, run_text=run_text, run_index=run_index)


def test_fixture_can_only_produce_synthetic_evidence(tmp_path: Path) -> None:
    from quantking.data.assessment.models import EvidenceKind

    _written, verified = _synthetic_bundle(tmp_path)
    assert verified.assessment.evidence_kind is EvidenceKind.SYNTHETIC
    assert verified.assessment.status.value == "BLOCKED_EXTERNAL_VALIDATION"


def test_writer_is_idempotent_and_conflicts_on_reused_run_id(tmp_path: Path) -> None:
    from quantking.data.validation.artifacts import ValidationEvidenceConflictError

    written, verified = _synthetic_bundle(tmp_path, run_text="first")
    second_written, second_verified = _synthetic_bundle(tmp_path, run_text="first")
    assert written == second_written
    assert verified == second_verified
    with pytest.raises(ValidationEvidenceConflictError):
        _synthetic_bundle(tmp_path, run_text="different")


@pytest.mark.parametrize("target", ("manifest.json", "COMPLETE", "raw/payload.json"))
def test_verifier_rejects_manifest_complete_or_file_tampering(tmp_path: Path, target: str) -> None:
    from quantking.data.validation.artifacts import (
        ValidationEvidenceVerificationError,
        ValidationEvidenceVerifier,
    )

    directory, _verified = _synthetic_bundle(tmp_path)
    path = directory / target
    if target == "COMPLETE":
        path.write_text('{"manifest_sha256":"' + "0" * 64 + '"}', encoding="utf-8")
    elif target == "manifest.json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["source"] = "TAMPERED"
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValidationEvidenceVerificationError):
        ValidationEvidenceVerifier(validation_root=tmp_path).verify(directory=directory)


def test_verifier_rejects_unlisted_extra_file(tmp_path: Path) -> None:
    from quantking.data.validation.artifacts import (
        ValidationEvidenceVerificationError,
        ValidationEvidenceVerifier,
    )

    directory, _verified = _synthetic_bundle(tmp_path)
    (directory / "unexpected-provider-file.txt").write_text("sensitive-marker", encoding="utf-8")
    with pytest.raises(ValidationEvidenceVerificationError, match="file set"):
        ValidationEvidenceVerifier(validation_root=tmp_path).verify(directory=directory)


def test_finalized_assessment_counts_distinct_runs_but_synthetic_stays_blocked(
    tmp_path: Path,
) -> None:
    from quantking.data.validation.artifacts import ValidationEvidenceWriter

    directories = [
        _synthetic_bundle(tmp_path, run_text=f"run-{index}", run_index=index)[0]
        for index in (1, 2, 3)
    ]
    verified = ValidationEvidenceWriter(validation_root=tmp_path).finalize_assessment(
        assessment_directory=directories[0].parent
    )
    assert verified.assessment.stability_runs == 3
    assert verified.assessment.status.value == "BLOCKED_EXTERNAL_VALIDATION"
    assert len({run.run_id for run in verified.verified_runs}) == 3
