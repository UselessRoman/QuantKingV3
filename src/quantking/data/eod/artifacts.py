"""Immutable, strict and repository-bound EOD terminal evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from quantking.common.ids import parse_entity_id
from quantking.common.paths import is_reparse_point
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.persistence.sqlite import DataSqliteUnitOfWorkFactory
from quantking.data.adapters.warehouse.manifest import path_safe_id
from quantking.data.adapters.warehouse.verifier import verify_canonical_directory
from quantking.data.eod.contracts import (
    EodDatasetResult,
    EodRunRequest,
    EodRunResult,
    canonical_json,
    request_sha256,
)
from quantking.data.models import DatasetVersionStatus, QualityRunStatus

_SUCCESS_FILES = frozenset({"request.json", "result.json", "manifest.json", "COMPLETE"})
_FAILURE_FILES = frozenset({"request.json", "failure.json"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}\Z")
_REQUEST_EVIDENCE_SCHEMA = "eod-request-evidence.v2"
_FAILURE_SCHEMA = "eod-failure.v2"
_CHECKPOINT_SCHEMA = "eod-dataset-checkpoint.v1"


class EodEvidenceConflictError(Exception):
    """Evidence is incomplete, tampered, unsafe, or conflicts with an immutable result."""


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return canonical_json(value).encode("utf-8")


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
    if root.exists() and is_reparse_point(root):
        raise EodEvidenceConflictError("artifacts_root cannot be a reparse point")
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve(strict=True)
    if is_reparse_point(resolved) or not resolved.is_dir():
        raise EodEvidenceConflictError("artifacts_root is unsafe")
    return resolved


def _require_plain_file(path: Path) -> None:
    if is_reparse_point(path) or not path.is_file():
        raise EodEvidenceConflictError("EOD evidence entries must be plain files")


def _read_json(path: Path) -> tuple[dict[str, object], bytes]:
    _require_plain_file(path)
    try:
        payload = path.read_bytes()
        raw = json.loads(payload)
    except (OSError, json.JSONDecodeError) as error:
        raise EodEvidenceConflictError("EOD evidence JSON is unreadable") from error
    if not isinstance(raw, dict) or payload != _json_bytes(raw):
        raise EodEvidenceConflictError("EOD evidence JSON is not canonical")
    return raw, payload


def _safe_directory(
    directory: Path,
    *,
    root: Path,
    expected_files: frozenset[str],
) -> Path:
    if is_reparse_point(directory):
        raise EodEvidenceConflictError("EOD evidence directory cannot be a reparse point")
    try:
        resolved = directory.resolve(strict=True)
    except OSError as error:
        raise EodEvidenceConflictError("EOD evidence directory is missing") from error
    if not resolved.is_relative_to(root) or not resolved.is_dir():
        raise EodEvidenceConflictError("EOD evidence directory is unmanaged")
    entries = tuple(resolved.iterdir())
    if {item.name for item in entries} != expected_files:
        raise EodEvidenceConflictError("EOD evidence file inventory is invalid")
    for item in entries:
        _require_plain_file(item)
    return resolved


def _request_from_evidence(value: Mapping[str, object]) -> EodRunRequest:
    expected = {
        "schema_version",
        "request",
        "request_sha256",
        "config",
        "config_sha256",
        "version_manifest",
    }
    if set(value) != expected or value.get("schema_version") != _REQUEST_EVIDENCE_SCHEMA:
        raise EodEvidenceConflictError("EOD request evidence fields are invalid")
    raw_request = value.get("request")
    raw_config = value.get("config")
    raw_versions = value.get("version_manifest")
    if not isinstance(raw_request, Mapping):
        raise EodEvidenceConflictError("EOD request evidence request is invalid")
    if not isinstance(raw_config, Mapping) or not isinstance(raw_versions, Mapping):
        raise EodEvidenceConflictError("EOD request evidence manifests are invalid")
    try:
        request = EodRunRequest.from_dict(raw_request)
    except (TypeError, ValueError) as error:
        raise EodEvidenceConflictError("EOD request evidence request is invalid") from error
    if value.get("request_sha256") != request_sha256(request):
        raise EodEvidenceConflictError("EOD request evidence request SHA is invalid")
    config_sha = value.get("config_sha256")
    if (
        not isinstance(config_sha, str)
        or _SHA256.fullmatch(config_sha) is None
        or config_sha != _sha(_json_bytes(raw_config))
    ):
        raise EodEvidenceConflictError("EOD request evidence config SHA is invalid")
    if set(raw_versions) != {
        "adapter_version",
        "code_version",
        "rule_version",
        "schema_versions_sha256",
    }:
        raise EodEvidenceConflictError("EOD version manifest fields are invalid")
    if any(not isinstance(item, str) or not item for item in raw_versions.values()):
        raise EodEvidenceConflictError("EOD version manifest values are invalid")
    if _SHA256.fullmatch(str(raw_versions["schema_versions_sha256"])) is None:
        raise EodEvidenceConflictError("EOD version manifest SHA is invalid")
    return request


@dataclass(frozen=True, slots=True)
class VerifiedEodEvidence:
    request: Mapping[str, object]
    result: Mapping[str, object]
    manifest_sha256: str
    parsed_request: EodRunRequest
    parsed_result: EodRunResult


def verify_eod_success_evidence(directory: Path, *, artifacts_root: Path) -> VerifiedEodEvidence:
    root = _managed_root(artifacts_root)
    resolved = _safe_directory(directory, root=root, expected_files=_SUCCESS_FILES)
    request, request_bytes = _read_json(resolved / "request.json")
    result, result_bytes = _read_json(resolved / "result.json")
    manifest, _manifest_bytes = _read_json(resolved / "manifest.json")
    parsed_request = _request_from_evidence(request)
    try:
        parsed_result = EodRunResult.from_dict(
            result,
            expected_datasets=parsed_request.datasets,
        )
    except (TypeError, ValueError) as error:
        raise EodEvidenceConflictError("EOD result semantics are invalid") from error
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
        or parsed_result.job_id != manifest.get("job_id")
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
    complete, _complete_bytes = _read_json(resolved / "COMPLETE")
    if complete != {
        "job_id": manifest["job_id"],
        "manifest_sha256": declared_sha,
        "schema_version": "eod-evidence-complete.v1",
    }:
        raise EodEvidenceConflictError("EOD COMPLETE marker is invalid")
    return VerifiedEodEvidence(
        request=request,
        result=result,
        manifest_sha256=declared_sha,
        parsed_request=parsed_request,
        parsed_result=parsed_result,
    )


def verify_eod_success_evidence_deep(
    directory: Path,
    *,
    artifacts_root: Path,
    data_root: Path,
    uow_factory: DataSqliteUnitOfWorkFactory,
) -> VerifiedEodEvidence:
    """Bind success evidence to PUBLISHED metadata, Warehouse and PASSED quality facts."""
    verified_evidence = verify_eod_success_evidence(
        directory,
        artifacts_root=artifacts_root,
    )
    for result in verified_evidence.parsed_result.datasets:
        with uow_factory() as uow:
            version = uow.data.get_version(result.version_id)
            quality = uow.data.get_quality_run(result.quality_run_id)
            uow.rollback()
        if (
            version.status is not DatasetVersionStatus.PUBLISHED
            or version.dataset.value != result.dataset
            or (str(version.parent_version_id) if version.parent_version_id is not None else None)
            != result.parent_version_id
            or version.content_sha256 != result.content_sha256
            or version.manifest_sha256 != result.manifest_sha256
            or str(version.quality_run_id) != result.quality_run_id
        ):
            raise EodEvidenceConflictError("EOD result version credentials do not match")
        if result.status == "PUBLISHED_NEW" and (
            result.batch_id is None
            or result.batch_id not in {str(item) for item in version.input_batch_ids}
        ):
            raise EodEvidenceConflictError("EOD result batch lineage does not match")
        if quality.status is not QualityRunStatus.PASSED or (
            str(quality.version_id) != result.version_id
            or quality.rule_version != version.rule_version
            or quality.schema_version != version.schema_version
            or quality.content_sha256 != result.content_sha256
        ):
            raise EodEvidenceConflictError("EOD result quality credentials do not match")
        warehouse_directory = (
            data_root / "warehouse" / result.dataset / path_safe_id(result.version_id)
        )
        verified_directory = verify_canonical_directory(
            warehouse_directory,
            data_root=data_root,
        )
        if (
            verified_directory.manifest_sha != result.manifest_sha256
            or verified_directory.manifest.get("content_sha256") != result.content_sha256
            or verified_directory.manifest.get("version_id") != result.version_id
        ):
            raise EodEvidenceConflictError("EOD Warehouse credentials do not match")
    return verified_evidence


@dataclass(frozen=True, slots=True)
class VerifiedEodFailureEvidence:
    request: Mapping[str, object]
    failure: Mapping[str, object]
    completed_datasets: tuple[EodDatasetResult, ...]


def verify_eod_failure_evidence(
    directory: Path,
    *,
    artifacts_root: Path,
) -> VerifiedEodFailureEvidence:
    root = _managed_root(artifacts_root)
    resolved = _safe_directory(directory, root=root, expected_files=_FAILURE_FILES)
    request, _request_bytes = _read_json(resolved / "request.json")
    failure, _failure_bytes = _read_json(resolved / "failure.json")
    expected = {
        "schema_version",
        "job_id",
        "terminal_status",
        "error_code",
        "message",
        "failed_at",
        "request_sha256",
        "completed_datasets",
    }
    if set(failure) != expected or failure.get("schema_version") != _FAILURE_SCHEMA:
        raise EodEvidenceConflictError("EOD failure fields are invalid")
    try:
        safe_id = _safe_job_id(str(failure["job_id"]))
        failed_at = UtcTimestamp.from_iso(str(failure["failed_at"]))
    except (TypeError, ValueError) as error:
        raise EodEvidenceConflictError("EOD failure identity or time is invalid") from error
    if failed_at.to_iso() != failure["failed_at"] or resolved.name != f"{safe_id}.incomplete":
        raise EodEvidenceConflictError("EOD failure directory binding is invalid")
    if failure["terminal_status"] not in {"FAILED", "CANCELLED", "INTERRUPTED"}:
        raise EodEvidenceConflictError("EOD failure terminal status is invalid")
    if (
        not isinstance(failure["error_code"], str)
        or _ERROR_CODE.fullmatch(failure["error_code"]) is None
        or not isinstance(failure["message"], str)
        or failure["message"]
        != "EOD execution did not succeed; inspect managed job events and JSONL logs."
    ):
        raise EodEvidenceConflictError("EOD failure error fields are invalid")
    raw_completed = failure["completed_datasets"]
    if not isinstance(raw_completed, list):
        raise EodEvidenceConflictError("EOD completed dataset results are invalid")
    try:
        completed = tuple(
            EodDatasetResult.from_dict(item)
            if isinstance(item, Mapping)
            else (_raise_completed_type())
            for item in raw_completed
        )
    except (TypeError, ValueError) as error:
        raise EodEvidenceConflictError("EOD completed dataset results are invalid") from error
    names = tuple(item.dataset for item in completed)
    if len(set(names)) != len(names):
        raise EodEvidenceConflictError("EOD completed dataset results contain duplicates")
    request_sha = failure["request_sha256"]
    if not isinstance(request_sha, str) or _SHA256.fullmatch(request_sha) is None:
        raise EodEvidenceConflictError("EOD failure request SHA is invalid")
    try:
        parsed = _request_from_evidence(request)
    except EodEvidenceConflictError:
        if request.get("schema_version") != "eod-invalid-request-evidence.v1":
            raise
    else:
        if request_sha != request_sha256(parsed):
            raise EodEvidenceConflictError("EOD failure request SHA does not match")
        order = {name: index for index, name in enumerate(parsed.datasets)}
        if (
            any(name not in order for name in names)
            or tuple(sorted(names, key=order.__getitem__)) != names
        ):
            raise EodEvidenceConflictError("EOD completed dataset order is invalid")
    return VerifiedEodFailureEvidence(
        request=request,
        failure=failure,
        completed_datasets=completed,
    )


def _raise_completed_type() -> EodDatasetResult:
    raise TypeError("completed dataset result must be an object")


class EodEvidenceWriter:
    """Publish immutable terminal evidence and per-dataset publish checkpoints."""

    def __init__(self, *, artifacts_root: Path) -> None:
        self._root = _managed_root(artifacts_root)

    def write_success(
        self,
        *,
        job_id: str,
        request: Mapping[str, object],
        result: Mapping[str, object],
    ) -> Path:
        parsed_request = _request_from_evidence(request)
        try:
            EodRunResult.from_dict(result, expected_datasets=parsed_request.datasets)
        except (TypeError, ValueError) as error:
            raise EodEvidenceConflictError("successful EOD result is invalid") from error
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
        if incomplete.exists() or is_reparse_point(incomplete):
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

    def write_checkpoint(
        self,
        *,
        job_id: str,
        request_sha256_value: str,
        config_sha256: str,
        result: EodDatasetResult,
    ) -> Path:
        if (
            _SHA256.fullmatch(request_sha256_value) is None
            or _SHA256.fullmatch(config_sha256) is None
        ):
            raise EodEvidenceConflictError("checkpoint command SHA is invalid")
        job_root = self._root / ".checkpoints" / _safe_job_id(job_id)
        if is_reparse_point(job_root):
            raise EodEvidenceConflictError("checkpoint directory cannot be a reparse point")
        job_root.mkdir(parents=True, exist_ok=True)
        path = job_root / f"{result.dataset}.json"
        payload = {
            "schema_version": _CHECKPOINT_SCHEMA,
            "job_id": job_id,
            "request_sha256": request_sha256_value,
            "config_sha256": config_sha256,
            "dataset_result": result.to_dict(),
        }
        expected = _json_bytes(payload)
        if path.exists():
            _require_plain_file(path)
            if path.read_bytes() != expected:
                raise EodEvidenceConflictError("published dataset checkpoint is immutable")
            return path
        if is_reparse_point(path):
            raise EodEvidenceConflictError("checkpoint cannot be a reparse point")
        temp = path.with_suffix(".writing")
        if temp.exists() or is_reparse_point(temp):
            raise EodEvidenceConflictError("checkpoint write conflicts")
        temp.write_bytes(expected)
        os.replace(temp, path)
        return path

    def load_checkpoints(
        self,
        *,
        job_id: str,
        request_sha256_value: str,
        config_sha256: str,
    ) -> tuple[EodDatasetResult, ...]:
        job_root = self._root / ".checkpoints" / _safe_job_id(job_id)
        if not job_root.exists():
            return ()
        if is_reparse_point(job_root) or not job_root.is_dir():
            raise EodEvidenceConflictError("checkpoint directory is unsafe")
        results: list[EodDatasetResult] = []
        for path in sorted(job_root.iterdir(), key=lambda item: item.name):
            raw, _payload = _read_json(path)
            if set(raw) != {
                "schema_version",
                "job_id",
                "request_sha256",
                "config_sha256",
                "dataset_result",
            } or (
                raw["schema_version"] != _CHECKPOINT_SCHEMA
                or raw["job_id"] != job_id
                or raw["request_sha256"] != request_sha256_value
                or raw["config_sha256"] != config_sha256
                or not isinstance(raw["dataset_result"], Mapping)
            ):
                raise EodEvidenceConflictError("checkpoint binding is invalid")
            result = EodDatasetResult.from_dict(raw["dataset_result"])
            if path.name != f"{result.dataset}.json":
                raise EodEvidenceConflictError("checkpoint filename is invalid")
            results.append(result)
        return tuple(results)

    def find_reusable_published(
        self,
        *,
        request_sha256_value: str,
        config_sha256: str,
        dataset: str,
    ) -> EodDatasetResult | None:
        """Find one prior failed job's checkpointed published result by frozen command."""
        matches: list[EodDatasetResult] = []
        for directory in sorted(self._root.iterdir(), key=lambda item: item.name):
            if not directory.name.endswith(".incomplete"):
                continue
            if is_reparse_point(directory):
                raise EodEvidenceConflictError("failure evidence directory is unsafe")
            verified = verify_eod_failure_evidence(
                directory,
                artifacts_root=self._root,
            )
            if (
                verified.failure.get("request_sha256") != request_sha256_value
                or verified.request.get("config_sha256") != config_sha256
            ):
                continue
            matches.extend(
                item
                for item in verified.completed_datasets
                if item.dataset == dataset and item.status == "PUBLISHED_NEW"
            )
        unique = {item.version_id: item for item in matches}
        if len(unique) > 1:
            raise EodEvidenceConflictError(
                "multiple reusable published results conflict for one frozen command"
            )
        return next(iter(unique.values()), None)

    def write_failure(
        self,
        *,
        job_id: str,
        request: Mapping[str, object],
        code: str,
        message: str,
        terminal_status: str,
        failed_at: UtcTimestamp,
        request_sha256_value: str,
        completed_datasets: Sequence[Mapping[str, object]],
        replace_uncommitted_success: bool = False,
    ) -> Path:
        del message
        safe_id = _safe_job_id(job_id)
        directory = self._root / f"{safe_id}.incomplete"
        success = self._root / safe_id
        if success.exists():
            if not replace_uncommitted_success:
                raise EodEvidenceConflictError("successful evidence already occupies this job_id")
            verify_eod_success_evidence(success, artifacts_root=self._root)
            shutil.rmtree(success)
        completed = tuple(EodDatasetResult.from_dict(item) for item in completed_datasets)
        failure = {
            "schema_version": _FAILURE_SCHEMA,
            "job_id": job_id,
            "terminal_status": terminal_status,
            "error_code": code if _ERROR_CODE.fullmatch(code) else "EOD_FAILED",
            "message": "EOD execution did not succeed; inspect managed job events and JSONL logs.",
            "failed_at": failed_at.to_iso(),
            "request_sha256": request_sha256_value,
            "completed_datasets": [item.to_dict() for item in completed],
        }
        expected = {
            "request.json": _json_bytes(request),
            "failure.json": _json_bytes(failure),
        }
        if directory.exists():
            _safe_directory(directory, root=self._root, expected_files=_FAILURE_FILES)
            if any(
                (directory / name).read_bytes() != payload for name, payload in expected.items()
            ):
                raise EodEvidenceConflictError("failure EOD evidence is immutable")
            return directory
        if is_reparse_point(directory):
            raise EodEvidenceConflictError("failure directory cannot be a reparse point")
        directory.mkdir(parents=False, exist_ok=False)
        for name, payload in expected.items():
            (directory / name).write_bytes(payload)
        verify_eod_failure_evidence(directory, artifacts_root=self._root)
        return directory


__all__ = [
    "EodEvidenceConflictError",
    "EodEvidenceWriter",
    "VerifiedEodEvidence",
    "VerifiedEodFailureEvidence",
    "verify_eod_failure_evidence",
    "verify_eod_success_evidence",
    "verify_eod_success_evidence_deep",
]
