"""Immutable, strict EOD terminal evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from quantking.common.ids import parse_entity_id
from quantking.common.paths import is_reparse_point

_SUCCESS_FILES = frozenset({"request.json", "result.json", "manifest.json", "COMPLETE"})
_FAILURE_FILES = frozenset({"request.json", "failure.json"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class EodEvidenceConflictError(Exception):
    """Evidence is incomplete, tampered, unsafe, or conflicts with an immutable result."""


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_job_id(job_id: str) -> str:
    parsed = parse_entity_id(job_id)
    if parsed.namespace != "job":
        raise ValueError("job_id must use the job namespace")
    return str(parsed).replace(":", "_")


def _managed_root(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("artifacts_root must be an absolute path")
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve(strict=True)


def _read_json(path: Path) -> tuple[dict[str, object], bytes]:
    try:
        payload = path.read_bytes()
        raw = json.loads(payload)
    except (OSError, json.JSONDecodeError) as error:
        raise EodEvidenceConflictError("EOD evidence JSON is unreadable") from error
    if not isinstance(raw, dict) or payload != _json_bytes(raw):
        raise EodEvidenceConflictError("EOD evidence JSON is not canonical")
    return raw, payload


@dataclass(frozen=True, slots=True)
class VerifiedEodEvidence:
    request: Mapping[str, object]
    result: Mapping[str, object]
    manifest_sha256: str


def verify_eod_success_evidence(directory: Path, *, artifacts_root: Path) -> VerifiedEodEvidence:
    root = _managed_root(artifacts_root)
    if is_reparse_point(directory):
        raise EodEvidenceConflictError("EOD evidence directory cannot be a reparse point")
    resolved = directory.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_dir():
        raise EodEvidenceConflictError("EOD evidence directory is unmanaged")
    if {item.name for item in resolved.iterdir()} != _SUCCESS_FILES:
        raise EodEvidenceConflictError("EOD evidence file inventory is invalid")
    request, request_bytes = _read_json(resolved / "request.json")
    result, result_bytes = _read_json(resolved / "result.json")
    manifest, _manifest_bytes = _read_json(resolved / "manifest.json")
    expected_manifest_fields = {
        "schema_version",
        "job_id",
        "request_sha256",
        "result_sha256",
        "manifest_sha256",
    }
    try:
        expected_directory_name = _safe_job_id(str(manifest.get("job_id")))
    except (TypeError, ValueError) as error:
        raise EodEvidenceConflictError("EOD evidence job binding is invalid") from error
    if (
        set(manifest) != expected_manifest_fields
        or manifest.get("schema_version") != "eod-evidence-manifest.v1"
        or manifest.get("request_sha256") != _sha(request_bytes)
        or manifest.get("result_sha256") != _sha(result_bytes)
        or result.get("job_id") != manifest.get("job_id")
        or resolved.name != expected_directory_name
    ):
        raise EodEvidenceConflictError("EOD evidence manifest binding is invalid")
    declared_sha = manifest.get("manifest_sha256")
    if not isinstance(declared_sha, str) or _SHA256.fullmatch(declared_sha) is None:
        raise EodEvidenceConflictError("EOD evidence manifest SHA is invalid")
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256")
    if declared_sha != _sha(_json_bytes(unsigned)):
        raise EodEvidenceConflictError("EOD evidence manifest SHA does not match")
    complete, complete_bytes = _read_json(resolved / "COMPLETE")
    if complete_bytes != _json_bytes(complete) or complete != {
        "job_id": manifest["job_id"],
        "manifest_sha256": declared_sha,
        "schema_version": "eod-evidence-complete.v1",
    }:
        raise EodEvidenceConflictError("EOD COMPLETE marker is invalid")
    return VerifiedEodEvidence(request=request, result=result, manifest_sha256=declared_sha)


class EodEvidenceWriter:
    """Publish one immutable success directory or retain one immutable failure directory."""

    def __init__(self, *, artifacts_root: Path) -> None:
        self._root = _managed_root(artifacts_root)

    def write_success(
        self,
        *,
        job_id: str,
        request: Mapping[str, object],
        result: Mapping[str, object],
    ) -> Path:
        safe_id = _safe_job_id(job_id)
        final = self._root / safe_id
        if (self._root / f"{safe_id}.incomplete").exists():
            raise EodEvidenceConflictError("failure evidence already occupies this job_id")
        if final.exists():
            verified = verify_eod_success_evidence(final, artifacts_root=self._root)
            if dict(verified.request) == dict(request) and dict(verified.result) == dict(result):
                return final
            raise EodEvidenceConflictError("successful EOD evidence is immutable")
        incomplete = self._root / f"{safe_id}.writing"
        if incomplete.exists():
            raise EodEvidenceConflictError("conflicting EOD evidence write is in progress")
        incomplete.mkdir(parents=False, exist_ok=False)
        request_bytes = _json_bytes(request)
        result_bytes = _json_bytes(result)
        unsigned = {
            "schema_version": "eod-evidence-manifest.v1",
            "job_id": job_id,
            "request_sha256": _sha(request_bytes),
            "result_sha256": _sha(result_bytes),
        }
        manifest = {**unsigned, "manifest_sha256": _sha(_json_bytes(unsigned))}
        (incomplete / "request.json").write_bytes(request_bytes)
        (incomplete / "result.json").write_bytes(result_bytes)
        (incomplete / "manifest.json").write_bytes(_json_bytes(manifest))
        (incomplete / "COMPLETE").write_bytes(
            _json_bytes(
                {
                    "schema_version": "eod-evidence-complete.v1",
                    "job_id": job_id,
                    "manifest_sha256": manifest["manifest_sha256"],
                }
            )
        )
        os.replace(incomplete, final)
        verify_eod_success_evidence(final, artifacts_root=self._root)
        return final

    def write_failure(
        self,
        *,
        job_id: str,
        request: Mapping[str, object],
        code: str,
        message: str,
        completed_datasets: Sequence[Mapping[str, object]],
    ) -> Path:
        del message
        safe_id = _safe_job_id(job_id)
        directory = self._root / f"{safe_id}.incomplete"
        if (self._root / safe_id).exists():
            raise EodEvidenceConflictError("successful evidence already occupies this job_id")
        failure = {
            "schema_version": "eod-failure.v1",
            "job_id": job_id,
            "status": "FAILED",
            "code": code if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code) else "EOD_FAILED",
            "message": "EOD execution failed; inspect managed job events and JSONL logs.",
            "completed_datasets": [dict(item) for item in completed_datasets],
        }
        expected = {
            "request.json": _json_bytes(request),
            "failure.json": _json_bytes(failure),
        }
        if directory.exists():
            if {item.name for item in directory.iterdir()} != _FAILURE_FILES:
                raise EodEvidenceConflictError("failure evidence inventory is invalid")
            if any(
                (directory / name).read_bytes() != payload for name, payload in expected.items()
            ):
                raise EodEvidenceConflictError("failure EOD evidence is immutable")
            return directory
        directory.mkdir(parents=False, exist_ok=False)
        for name, payload in expected.items():
            (directory / name).write_bytes(payload)
        return directory


__all__ = [
    "EodEvidenceConflictError",
    "EodEvidenceWriter",
    "VerifiedEodEvidence",
    "verify_eod_success_evidence",
]
