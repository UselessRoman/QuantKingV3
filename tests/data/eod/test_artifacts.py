from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from quantking.common.time import UtcTimestamp
from quantking.data.eod.artifacts import (
    EodEvidenceConflictError,
    EodEvidenceWriter,
    verify_eod_failure_evidence,
    verify_eod_success_evidence,
)

JOB_ID = "job:11111111-1111-4111-8111-111111111111"


def _request() -> dict[str, object]:
    request = {
        "schema_version": "eod-request.v1",
        "start_date": "2026-07-15",
        "end_date": "2026-07-15",
        "datasets": ["daily_bar_unadjusted"],
        "base_versions": {},
        "publish_intent": "PUBLISH_AFTER_QUALITY",
        "source": "AKSHARE",
    }
    schema_versions = {"daily_bar_unadjusted": "daily_bar_unadjusted.v1"}
    config = {
        "schema_version": "eod-execution-config.v1",
        "code_version": "test-code",
        "adapter_version": "test-adapter",
        "rule_version": "test-rules",
        "lookback_days": 0,
        "max_days_per_chunk": 31,
        "max_items_per_chunk": 500,
        "datasets": {
            "daily_bar_unadjusted": {
                "scope": ["000001.SZ"],
                "known_coverage": [],
                "base_version": None,
                "references": {},
            }
        },
        "schema_versions": schema_versions,
    }
    return {
        "schema_version": "eod-request-evidence.v2",
        "request": request,
        "request_sha256": hashlib.sha256(_canonical_bytes(request)).hexdigest(),
        "config": config,
        "config_sha256": hashlib.sha256(_canonical_bytes(config)).hexdigest(),
        "version_manifest": {
            "adapter_version": "test-adapter",
            "code_version": "test-code",
            "rule_version": "test-rules",
            "schema_versions_sha256": hashlib.sha256(_canonical_bytes(schema_versions)).hexdigest(),
        },
    }


def _result() -> dict[str, object]:
    return {
        "schema_version": "eod-result.v1",
        "job_id": JOB_ID,
        "status": "SUCCEEDED",
        "datasets": [
            {
                "dataset": "daily_bar_unadjusted",
                "status": "PUBLISHED_NEW",
                "batch_id": "data_batch:22222222-2222-4222-8222-222222222222",
                "version_id": "data_version:33333333-3333-4333-8333-333333333333",
                "quality_run_id": "data_quality:44444444-4444-4444-8444-444444444444",
                "content_sha256": "a" * 64,
                "manifest_sha256": "c" * 64,
                "parent_version_id": None,
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
        terminal_status="FAILED",
        failed_at=UtcTimestamp.from_iso("2026-07-15T10:00:00.000000Z"),
        request_sha256_value=_request()["request_sha256"],  # type: ignore[arg-type]
        completed_datasets=(),
    )
    payload = (directory / "failure.json").read_text(encoding="utf-8")
    assert "hunter2" not in payload
    assert "E:\\" not in payload
    assert "CAPABILITY_UNAVAILABLE" in payload
    verified = verify_eod_failure_evidence(directory, artifacts_root=tmp_path)
    assert verified.failure["terminal_status"] == "FAILED"
    with pytest.raises(EodEvidenceConflictError):
        writer.write_failure(
            job_id=JOB_ID,
            request=_request(),
            code="DIFFERENT",
            message="different",
            terminal_status="FAILED",
            failed_at=UtcTimestamp.from_iso("2026-07-15T10:00:00.000000Z"),
            request_sha256_value=_request()["request_sha256"],  # type: ignore[arg-type]
            completed_datasets=(),
        )


def _canonical_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def test_resigned_failed_result_is_not_valid_success_evidence(tmp_path: Path) -> None:
    directory = EodEvidenceWriter(artifacts_root=tmp_path).write_success(
        job_id=JOB_ID,
        request=_request(),
        result=_result(),
    )
    result = _result()
    result["status"] = "FAILED"
    result_bytes = _canonical_bytes(result)
    (directory / "result.json").write_bytes(result_bytes)
    request_bytes = (directory / "request.json").read_bytes()
    unsigned = {
        "schema_version": "eod-evidence-manifest.v1",
        "job_id": JOB_ID,
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "authority_state": "PROVISIONAL_UNTIL_JOB_SUCCEEDED",
    }
    manifest_sha = hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()
    (directory / "manifest.json").write_bytes(
        _canonical_bytes({**unsigned, "manifest_sha256": manifest_sha})
    )
    (directory / "COMPLETE").write_bytes(
        _canonical_bytes(
            {
                "schema_version": "eod-evidence-complete.v1",
                "job_id": JOB_ID,
                "manifest_sha256": manifest_sha,
            }
        )
    )

    with pytest.raises(EodEvidenceConflictError):
        verify_eod_success_evidence(directory, artifacts_root=tmp_path)


@pytest.mark.parametrize("target_name", ["directory", "result.json"])
def test_success_verifier_rejects_reparse_directory_or_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
) -> None:
    directory = EodEvidenceWriter(artifacts_root=tmp_path).write_success(
        job_id=JOB_ID,
        request=_request(),
        result=_result(),
    )
    target = directory if target_name == "directory" else directory / target_name

    def fake_reparse(path: Path) -> bool:
        return path == target

    monkeypatch.setattr("quantking.data.eod.artifacts.is_reparse_point", fake_reparse)

    with pytest.raises(EodEvidenceConflictError, match=r"reparse|plain"):
        verify_eod_success_evidence(directory, artifacts_root=tmp_path)


def test_failure_verifier_rejects_resigned_invalid_terminal_status(tmp_path: Path) -> None:
    writer = EodEvidenceWriter(artifacts_root=tmp_path)
    directory = writer.write_failure(
        job_id=JOB_ID,
        request=_request(),
        code="CANCELLED",
        message="cancel",
        terminal_status="CANCELLED",
        failed_at=UtcTimestamp.from_iso("2026-07-15T10:00:00.000000Z"),
        request_sha256_value=_request()["request_sha256"],  # type: ignore[arg-type]
        completed_datasets=(),
    )
    payload = json.loads((directory / "failure.json").read_bytes())
    payload["terminal_status"] = "SUCCEEDED"
    (directory / "failure.json").write_bytes(_canonical_bytes(payload))

    with pytest.raises(EodEvidenceConflictError, match="terminal"):
        verify_eod_failure_evidence(directory, artifacts_root=tmp_path)
