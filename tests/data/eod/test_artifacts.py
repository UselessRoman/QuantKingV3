from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantking.data.eod.artifacts import (
    EodEvidenceConflictError,
    EodEvidenceWriter,
    verify_eod_success_evidence,
)

JOB_ID = "job:11111111-1111-4111-8111-111111111111"


def _request() -> dict[str, object]:
    return {
        "schema_version": "eod-request.v1",
        "start_date": "2026-07-15",
        "end_date": "2026-07-15",
        "datasets": ["exchange_calendar"],
        "publish_intent": "PUBLISH_AFTER_QUALITY",
    }


def _result() -> dict[str, object]:
    return {
        "schema_version": "eod-result.v1",
        "job_id": JOB_ID,
        "status": "SUCCEEDED",
        "datasets": [
            {
                "dataset": "exchange_calendar",
                "status": "PUBLISHED",
                "batch_id": "data_batch:22222222-2222-4222-8222-222222222222",
                "version_id": "data_version:33333333-3333-4333-8333-333333333333",
                "quality_run_id": "data_quality:44444444-4444-4444-8444-444444444444",
                "content_sha256": "a" * 64,
            }
        ],
    }


def test_success_evidence_is_strict_sha_bound_and_immutable(tmp_path: Path) -> None:
    writer = EodEvidenceWriter(artifacts_root=tmp_path)
    directory = writer.write_success(job_id=JOB_ID, request=_request(), result=_result())

    verified = verify_eod_success_evidence(directory, artifacts_root=tmp_path)
    assert verified.result["job_id"] == JOB_ID
    assert {item.name for item in directory.iterdir()} == {
        "request.json",
        "result.json",
        "manifest.json",
        "COMPLETE",
    }
    before = {item.name: item.read_bytes() for item in directory.iterdir()}
    assert writer.write_success(job_id=JOB_ID, request=_request(), result=_result()) == directory
    assert {item.name: item.read_bytes() for item in directory.iterdir()} == before

    changed = _result()
    changed["status"] = "FAILED"
    with pytest.raises(EodEvidenceConflictError):
        writer.write_success(job_id=JOB_ID, request=_request(), result=changed)


@pytest.mark.parametrize("mutation", ["missing", "extra", "tamper", "complete"])
def test_verifier_rejects_missing_extra_tampered_or_wrong_complete(
    tmp_path: Path,
    mutation: str,
) -> None:
    directory = EodEvidenceWriter(artifacts_root=tmp_path).write_success(
        job_id=JOB_ID,
        request=_request(),
        result=_result(),
    )
    if mutation == "missing":
        (directory / "result.json").unlink()
    elif mutation == "extra":
        (directory / "extra.json").write_text("{}", encoding="utf-8")
    elif mutation == "tamper":
        (directory / "result.json").write_text(
            json.dumps({"secret_path": "E:\\private"}),
            encoding="utf-8",
        )
    else:
        (directory / "COMPLETE").write_text("bad", encoding="utf-8")

    with pytest.raises(EodEvidenceConflictError):
        verify_eod_success_evidence(directory, artifacts_root=tmp_path)


def test_failure_evidence_is_sanitized_and_immutable(tmp_path: Path) -> None:
    writer = EodEvidenceWriter(artifacts_root=tmp_path)
    directory = writer.write_failure(
        job_id=JOB_ID,
        request=_request(),
        code="CAPABILITY_UNAVAILABLE",
        message="password=hunter2 at E:\\private\\payload.json",
        completed_datasets=(),
    )
    payload = (directory / "failure.json").read_text(encoding="utf-8")
    assert "hunter2" not in payload
    assert "E:\\" not in payload
    assert "CAPABILITY_UNAVAILABLE" in payload
    with pytest.raises(EodEvidenceConflictError):
        writer.write_failure(
            job_id=JOB_ID,
            request=_request(),
            code="DIFFERENT",
            message="different",
            completed_datasets=(),
        )
