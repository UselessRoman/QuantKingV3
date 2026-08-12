"""Deterministic immutable ResolutionRun artifact writer and verifier."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from quantking.common.ids import EntityId, parse_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.warehouse.manifest import path_safe_id
from quantking.data.comparison.thresholds import THRESHOLD_RULE_VERSION
from quantking.data.models import DatasetKind, DataUpstream
from quantking.data.resolution.models import (
    K2_RESOLUTION_RULE_VERSION,
    K3_RESOLUTION_RULE_VERSION,
    CRequestTrace,
    ResolutionDecision,
    ResolutionDefaultSummary,
    ResolutionReasonCode,
    ResolutionRequestTrace,
    ResolutionRun,
    ResolutionRunBlocker,
    ResolutionRunRecord,
    ResolutionSourceBinding,
    ResolutionSourceStatus,
)

_K2_SCHEMA_VERSION = "resolution-artifact.v1"
_K3_SCHEMA_VERSION = "resolution-artifact.v2"
_SUMMARY = "summary.json"
_EXCEPTIONS = "exceptions.jsonl"
_CONFLICTS = "conflicts.jsonl"
_MANIFEST = "manifest.json"
_COMPLETE = "COMPLETE"
_FILES = (_SUMMARY, _EXCEPTIONS, _CONFLICTS)
_MANIFEST_FIELDS = {
    "schema_version",
    "run_id",
    "dataset",
    "created_job_id",
    "created_at",
    "rule_version",
    "threshold_rule_version",
    "input_fingerprint",
    "candidate_version_id",
    "status",
    "total_decisions",
    "default_decision_count",
    "exception_count",
    "conflict_count",
    "blocker_count",
    "files",
    "manifest_sha256",
}


class ResolutionArtifactConflictError(Exception):
    """An immutable run directory already exists with different bytes."""


class ResolutionArtifactVerificationError(Exception):
    """A resolution artifact is structurally invalid or SHA-inconsistent."""


@dataclass(frozen=True, slots=True)
class VerifiedResolutionArtifact:
    run_id: EntityId
    relative_path: str
    manifest_sha256: str
    summary: dict[str, object]
    exceptions: tuple[ResolutionDecision, ...]
    conflicts: tuple[ResolutionDecision, ...]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _jsonl(decisions: tuple[ResolutionDecision, ...]) -> bytes:
    return b"".join(_canonical(item.to_dict()) + b"\n" for item in decisions)


def _managed_root(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("artifacts_root must be an absolute Path")
    resolved = root.resolve(strict=False)
    if root.exists() and resolved != root:
        raise ValueError("artifacts_root must not be a symlink or junction")
    return root


def _file_entry(payload: bytes, *, row_count: int) -> dict[str, object]:
    return {"sha256": _sha(payload), "size": len(payload), "row_count": row_count}


def _manifest_payload(run: ResolutionRun, files: dict[str, bytes]) -> dict[str, object]:
    return {
        "schema_version": _schema_for_rule(run.rule_version),
        "run_id": str(run.run_id),
        "dataset": run.dataset.value,
        "created_job_id": str(run.created_job_id),
        "created_at": run.created_at.to_iso(),
        "rule_version": run.rule_version,
        "threshold_rule_version": run.threshold_rule_version,
        "input_fingerprint": run.input_fingerprint,
        "candidate_version_id": (
            None if run.candidate_version_id is None else str(run.candidate_version_id)
        ),
        "status": run.status.value,
        "total_decisions": run.total_decisions,
        "default_decision_count": run.default_decision_count,
        "exception_count": len(run.exceptions),
        "conflict_count": len(run.conflicts),
        "blocker_count": len(run.run_blockers),
        "files": {
            name: _file_entry(
                files[name],
                row_count=(
                    len(run.exceptions)
                    if name == _EXCEPTIONS
                    else len(run.conflicts)
                    if name == _CONFLICTS
                    else 1
                ),
            )
            for name in _FILES
        },
    }


def _schema_for_rule(rule_version: str) -> str:
    if rule_version == K2_RESOLUTION_RULE_VERSION:
        return _K2_SCHEMA_VERSION
    if rule_version == K3_RESOLUTION_RULE_VERSION:
        return _K3_SCHEMA_VERSION
    raise ValueError("resolution rule version is unsupported")


def _signed_manifest(payload: dict[str, object]) -> tuple[bytes, str]:
    digest = _sha(_canonical(payload))
    return _canonical({**payload, "manifest_sha256": digest}), digest


class ResolutionArtifactWriter:
    def __init__(self, *, artifacts_root: Path) -> None:
        self._root = _managed_root(artifacts_root)

    def write(self, run: ResolutionRun) -> Path:
        if not isinstance(run, ResolutionRun):
            raise TypeError("run must be a ResolutionRun")
        directory = self._root / "data-resolution" / path_safe_id(str(run.run_id))
        summary = _canonical(run.summary_dict())
        exceptions = _jsonl(run.exceptions)
        conflicts = _jsonl(run.conflicts)
        files = {_SUMMARY: summary, _EXCEPTIONS: exceptions, _CONFLICTS: conflicts}
        manifest, manifest_sha = _signed_manifest(_manifest_payload(run, files))
        complete = _canonical({"manifest_sha256": manifest_sha})
        expected = {**files, _MANIFEST: manifest, _COMPLETE: complete}
        if directory.exists():
            actual = {
                item.name: item.read_bytes() for item in directory.iterdir() if item.is_file()
            }
            if actual != expected:
                raise ResolutionArtifactConflictError(
                    "existing resolution artifact differs from requested immutable run"
                )
            return directory
        incomplete = directory.with_name(f"{directory.name}.incomplete")
        incomplete.mkdir(parents=True, exist_ok=False)
        try:
            for name, payload in expected.items():
                (incomplete / name).write_bytes(payload)
            os.replace(incomplete, directory)
        except Exception:
            for child in incomplete.iterdir() if incomplete.exists() else ():
                child.unlink(missing_ok=True)
            incomplete.rmdir()
            raise
        return directory


class ResolutionArtifactVerifier:
    """Verify a persisted run record against its managed immutable artifact."""

    def __init__(self, *, artifacts_root: Path) -> None:
        self._root = _managed_root(artifacts_root)

    def verify(self, record: ResolutionRunRecord) -> VerifiedResolutionArtifact:
        if not isinstance(record, ResolutionRunRecord):
            raise TypeError("record must be a ResolutionRunRecord")
        try:
            verified = verify_resolution_artifact(
                self._root / record.artifact_relative_path,
                artifacts_root=self._root,
            )
        except (OSError, ValueError) as error:
            if isinstance(error, ResolutionArtifactVerificationError):
                raise
            raise ResolutionArtifactVerificationError(
                "resolution record artifact path is invalid"
            ) from error
        summary = verified.summary
        expected = {
            "run_id": str(record.run_id),
            "dataset": record.dataset.value,
            "created_job_id": str(record.created_job_id),
            "created_at": record.created_at.to_iso(),
            "rule_version": record.rule_version,
            "threshold_rule_version": record.threshold_rule_version,
            "input_fingerprint": record.input_fingerprint,
            "candidate_version_id": (
                None if record.candidate_version_id is None else str(record.candidate_version_id)
            ),
            "status": record.status.value,
            "total_decisions": record.total_decisions,
            "exception_count": record.exception_count,
            "conflict_count": record.conflict_count,
            "blocker_count": record.blocker_count,
        }
        default_decision = summary.get("default_decision")
        if (
            verified.run_id != record.run_id
            or verified.relative_path != record.artifact_relative_path
            or verified.manifest_sha256 != record.manifest_sha256
            or any(summary.get(field) != value for field, value in expected.items())
            or (default_decision is None and record.default_decision_count != 0)
            or (
                isinstance(default_decision, dict)
                and default_decision.get("decision_count") != record.default_decision_count
            )
        ):
            raise ResolutionArtifactVerificationError(
                "resolution record does not match its immutable artifact"
            )
        return verified


def _plain_json(path: Path) -> tuple[dict[str, object], bytes]:
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ResolutionArtifactVerificationError("resolution JSON is invalid") from error
    if not isinstance(value, dict) or _canonical(value) != payload:
        raise ResolutionArtifactVerificationError("resolution JSON is not canonical")
    return value, payload


def _jsonl_values(path: Path) -> tuple[list[dict[str, object]], bytes]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ResolutionArtifactVerificationError("resolution JSONL is unreadable") from error
    values: list[dict[str, object]] = []
    for line in payload.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            raise ResolutionArtifactVerificationError("resolution JSONL line is unterminated")
        try:
            value = json.loads(line[:-1].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ResolutionArtifactVerificationError("resolution JSONL is invalid") from error
        if not isinstance(value, dict) or _canonical(value) + b"\n" != line:
            raise ResolutionArtifactVerificationError("resolution JSONL is not canonical")
        values.append(value)
    return values, payload


def _source_binding(value: object) -> ResolutionSourceBinding:
    if not isinstance(value, dict) or set(value) != {
        "upstream",
        "status",
        "batch_id",
        "content_sha256",
    }:
        raise ResolutionArtifactVerificationError("source binding fields are invalid")
    batch = value["batch_id"]
    try:
        return ResolutionSourceBinding(
            upstream=DataUpstream(value["upstream"]),
            status=ResolutionSourceStatus(value["status"]),
            batch_id=None if batch is None else parse_entity_id(str(batch)),
            content_sha256=(
                None if value["content_sha256"] is None else str(value["content_sha256"])
            ),
        )
    except (TypeError, ValueError) as error:
        raise ResolutionArtifactVerificationError("source binding is invalid") from error


def _decision(
    value: dict[str, object],
    *,
    schema_version: str,
    manifest_rule_version: str,
) -> ResolutionDecision:
    common = {
        "symbol",
        "trade_date",
        "source_bindings",
        "compared_upstreams",
        "agreeing_upstreams",
        "selected_upstream",
        "selected_row_present",
        "reason_code",
        "rule_version",
        "threshold_rule_version",
        "difference_report_ids",
        "created_job_id",
        "created_at",
    }
    expected = common | (
        {"c_request"} if schema_version == _K2_SCHEMA_VERSION else {"request_traces"}
    )
    if set(value) != expected:
        raise ResolutionArtifactVerificationError("decision fields are invalid")
    bindings = value["source_bindings"]
    compared = value["compared_upstreams"]
    agreeing = value["agreeing_upstreams"]
    report_ids = value["difference_report_ids"]
    selected_row_present = value["selected_row_present"]
    if (
        not isinstance(bindings, list)
        or not isinstance(compared, list)
        or not isinstance(agreeing, list)
        or not isinstance(report_ids, list)
        or (selected_row_present is not None and not isinstance(selected_row_present, bool))
    ):
        raise ResolutionArtifactVerificationError("decision collections are invalid")
    c_request = value.get("c_request")
    trace_values = value.get("request_traces", [])
    legacy_trace: dict[str, object] | None = None
    if schema_version == _K2_SCHEMA_VERSION:
        if c_request is not None and (
            not isinstance(c_request, dict)
            or set(c_request) != {"symbol", "start_date", "end_date", "trigger_reason"}
        ):
            raise ResolutionArtifactVerificationError("C request fields are invalid")
        if isinstance(c_request, dict):
            legacy_trace = c_request
        request_traces: tuple[ResolutionRequestTrace, ...] = ()
    else:
        if not isinstance(trace_values, list):
            raise ResolutionArtifactVerificationError("request traces are invalid")
        parsed_traces: list[ResolutionRequestTrace] = []
        for trace in trace_values:
            if not isinstance(trace, dict) or set(trace) != {
                "upstream",
                "symbol",
                "start_date",
                "end_date",
                "trigger_reason",
            }:
                raise ResolutionArtifactVerificationError("request trace fields are invalid")
            try:
                parsed_traces.append(
                    ResolutionRequestTrace(
                        upstream=DataUpstream(str(trace["upstream"])),
                        symbol=str(trace["symbol"]),
                        start_date=date.fromisoformat(str(trace["start_date"])),
                        end_date=date.fromisoformat(str(trace["end_date"])),
                        trigger_reason=str(trace["trigger_reason"]),
                    )
                )
            except (TypeError, ValueError) as error:
                raise ResolutionArtifactVerificationError("request trace is invalid") from error
        request_traces = tuple(parsed_traces)
    try:
        result = ResolutionDecision(
            symbol=str(value["symbol"]),
            trade_date=date.fromisoformat(str(value["trade_date"])),
            source_bindings=tuple(_source_binding(item) for item in bindings),
            compared_upstreams=tuple(DataUpstream(item) for item in compared),
            agreeing_upstreams=tuple(DataUpstream(item) for item in agreeing),
            selected_upstream=(
                None
                if value["selected_upstream"] is None
                else DataUpstream(str(value["selected_upstream"]))
            ),
            selected_row_present=selected_row_present,
            reason_code=ResolutionReasonCode(str(value["reason_code"])),
            rule_version=str(value["rule_version"]),
            difference_report_ids=tuple(parse_entity_id(str(item)) for item in report_ids),
            created_job_id=parse_entity_id(str(value["created_job_id"])),
            created_at=UtcTimestamp.from_iso(str(value["created_at"])),
            c_request=(
                None
                if legacy_trace is None
                else CRequestTrace(
                    symbol=str(legacy_trace["symbol"]),
                    start_date=date.fromisoformat(str(legacy_trace["start_date"])),
                    end_date=date.fromisoformat(str(legacy_trace["end_date"])),
                    trigger_reason=str(legacy_trace["trigger_reason"]),
                )
            ),
            request_traces=request_traces,
        )
    except (TypeError, ValueError) as error:
        raise ResolutionArtifactVerificationError("decision is invalid") from error
    if value["threshold_rule_version"] != THRESHOLD_RULE_VERSION:
        raise ResolutionArtifactVerificationError("threshold rule version is invalid")
    if result.rule_version != manifest_rule_version:
        raise ResolutionArtifactVerificationError("decision rule does not match manifest")
    if result.to_dict() != value:
        raise ResolutionArtifactVerificationError("decision is not canonical")
    return result


def _default_summary(value: object) -> ResolutionDefaultSummary:
    if not isinstance(value, dict) or set(value) != {
        "source_bindings",
        "compared_upstreams",
        "agreeing_upstreams",
        "selected_upstream",
        "reason_code",
        "decision_count",
    }:
        raise ResolutionArtifactVerificationError("default decision fields are invalid")
    bindings = value["source_bindings"]
    compared = value["compared_upstreams"]
    agreeing = value["agreeing_upstreams"]
    if (
        not isinstance(bindings, list)
        or not isinstance(compared, list)
        or not isinstance(agreeing, list)
    ):
        raise ResolutionArtifactVerificationError("default decision collections are invalid")
    decision_count = value["decision_count"]
    if isinstance(decision_count, bool) or not isinstance(decision_count, int):
        raise ResolutionArtifactVerificationError("default decision count is invalid")
    try:
        result = ResolutionDefaultSummary(
            source_bindings=tuple(_source_binding(item) for item in bindings),
            compared_upstreams=tuple(DataUpstream(item) for item in compared),
            agreeing_upstreams=tuple(DataUpstream(item) for item in agreeing),
            selected_upstream=DataUpstream(value["selected_upstream"]),
            reason_code=ResolutionReasonCode(value["reason_code"]),
            decision_count=decision_count,
        )
    except (TypeError, ValueError) as error:
        raise ResolutionArtifactVerificationError("default decision is invalid") from error
    if result.to_dict() != value:
        raise ResolutionArtifactVerificationError("default decision is not canonical")
    return result


def _verify_summary(
    summary: dict[str, object],
    *,
    manifest: dict[str, object],
    exceptions: tuple[ResolutionDecision, ...],
    conflicts: tuple[ResolutionDecision, ...],
) -> None:
    expected = {
        "run_id",
        "dataset",
        "created_job_id",
        "created_at",
        "rule_version",
        "threshold_rule_version",
        "input_fingerprint",
        "candidate_version_id",
        "status",
        "input_bindings",
        "difference_report_ids",
        "default_decision",
        "run_blockers",
        "total_decisions",
        "exception_count",
        "conflict_count",
        "blocker_count",
    }
    if set(summary) != expected:
        raise ResolutionArtifactVerificationError("resolution summary fields are invalid")
    inputs = summary["input_bindings"]
    reports = summary["difference_report_ids"]
    blockers = summary["run_blockers"]
    if (
        not isinstance(inputs, list)
        or not isinstance(reports, list)
        or not isinstance(blockers, list)
    ):
        raise ResolutionArtifactVerificationError("resolution summary bindings are invalid")
    input_bindings = tuple(_source_binding(item) for item in inputs)
    report_ids = tuple(parse_entity_id(str(item)) for item in reports)
    default_value = summary["default_decision"]
    default = None if default_value is None else _default_summary(default_value)
    try:
        reconstructed = ResolutionRun(
            run_id=parse_entity_id(str(summary["run_id"])),
            dataset=DatasetKind(str(summary["dataset"])),
            created_job_id=parse_entity_id(str(summary["created_job_id"])),
            created_at=UtcTimestamp.from_iso(str(summary["created_at"])),
            rule_version=str(summary["rule_version"]),
            threshold_rule_version=str(summary["threshold_rule_version"]),
            input_fingerprint=str(summary["input_fingerprint"]),
            candidate_version_id=(
                None
                if summary["candidate_version_id"] is None
                else parse_entity_id(str(summary["candidate_version_id"]))
            ),
            input_bindings=input_bindings,
            difference_report_ids=report_ids,
            default_summary=default,
            run_blockers=tuple(ResolutionRunBlocker(str(item)) for item in blockers),
            exceptions=exceptions,
            conflicts=conflicts,
        )
    except (TypeError, ValueError) as error:
        raise ResolutionArtifactVerificationError("resolution summary is invalid") from error
    if reconstructed.summary_dict() != summary:
        raise ResolutionArtifactVerificationError("resolution summary is not canonical")
    if manifest.get("default_decision_count") != reconstructed.default_decision_count:
        raise ResolutionArtifactVerificationError("default decision count is not manifest-bound")
    if manifest.get("total_decisions") != reconstructed.total_decisions:
        raise ResolutionArtifactVerificationError("total decision count is invalid")


def verify_resolution_artifact(
    directory: Path,
    *,
    artifacts_root: Path,
) -> VerifiedResolutionArtifact:
    root = _managed_root(artifacts_root)
    resolved = directory.resolve(strict=True)
    try:
        relative = resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ResolutionArtifactVerificationError("resolution directory escapes root") from error
    if any(item.is_symlink() for item in resolved.iterdir()):
        raise ResolutionArtifactVerificationError("resolution artifact contains a link")
    actual_files = {item.name for item in resolved.iterdir() if item.is_file()}
    if actual_files != {*_FILES, _MANIFEST, _COMPLETE}:
        raise ResolutionArtifactVerificationError("resolution artifact file set is invalid")
    manifest, manifest_bytes = _plain_json(resolved / _MANIFEST)
    if set(manifest) != _MANIFEST_FIELDS:
        raise ResolutionArtifactVerificationError("resolution manifest fields are invalid")
    embedded = manifest.get("manifest_sha256")
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    if not isinstance(embedded, str) or _sha(_canonical(unsigned)) != embedded:
        raise ResolutionArtifactVerificationError("resolution manifest SHA is invalid")
    schema_version = manifest.get("schema_version")
    rule_version = manifest.get("rule_version")
    if not isinstance(schema_version, str) or not isinstance(rule_version, str):
        raise ResolutionArtifactVerificationError("resolution schema/rule is invalid")
    try:
        expected_schema = _schema_for_rule(rule_version)
    except ValueError as error:
        raise ResolutionArtifactVerificationError("resolution schema/rule is invalid") from error
    if schema_version != expected_schema:
        raise ResolutionArtifactVerificationError("resolution schema/rule is invalid")
    complete, _ = _plain_json(resolved / _COMPLETE)
    if complete != {"manifest_sha256": embedded}:
        raise ResolutionArtifactVerificationError("COMPLETE does not bind the manifest")
    summary, summary_bytes = _plain_json(resolved / _SUMMARY)
    exception_values, exception_bytes = _jsonl_values(resolved / _EXCEPTIONS)
    conflict_values, conflict_bytes = _jsonl_values(resolved / _CONFLICTS)
    payloads = {
        _SUMMARY: (summary_bytes, 1),
        _EXCEPTIONS: (exception_bytes, len(exception_values)),
        _CONFLICTS: (conflict_bytes, len(conflict_values)),
    }
    file_entries = manifest.get("files")
    if not isinstance(file_entries, dict) or set(file_entries) != set(_FILES):
        raise ResolutionArtifactVerificationError("resolution file manifest is invalid")
    for name, (payload, rows) in payloads.items():
        if file_entries[name] != {"sha256": _sha(payload), "size": len(payload), "row_count": rows}:
            raise ResolutionArtifactVerificationError("resolution file binding is invalid")
    exceptions = tuple(
        _decision(
            value,
            schema_version=schema_version,
            manifest_rule_version=rule_version,
        )
        for value in exception_values
    )
    conflicts = tuple(
        _decision(
            value,
            schema_version=schema_version,
            manifest_rule_version=rule_version,
        )
        for value in conflict_values
    )
    if any(item.conflict for item in exceptions) or any(not item.conflict for item in conflicts):
        raise ResolutionArtifactVerificationError("decision classification is invalid")
    if tuple(sorted(exceptions, key=lambda item: (item.symbol, item.trade_date))) != exceptions:
        raise ResolutionArtifactVerificationError("exceptions are not sorted")
    if tuple(sorted(conflicts, key=lambda item: (item.symbol, item.trade_date))) != conflicts:
        raise ResolutionArtifactVerificationError("conflicts are not sorted")
    run_id = parse_entity_id(str(manifest.get("run_id")))
    if run_id.namespace != "data_resolution" or summary.get("run_id") != str(run_id):
        raise ResolutionArtifactVerificationError("resolution run id is invalid")
    for field in (
        "dataset",
        "created_job_id",
        "created_at",
        "rule_version",
        "threshold_rule_version",
        "input_fingerprint",
        "candidate_version_id",
        "status",
        "total_decisions",
        "exception_count",
        "conflict_count",
        "blocker_count",
    ):
        if summary.get(field) != manifest.get(field):
            raise ResolutionArtifactVerificationError("summary does not bind the manifest")
    if manifest.get("exception_count") != len(exceptions) or manifest.get("conflict_count") != len(
        conflicts
    ):
        raise ResolutionArtifactVerificationError("decision counts are invalid")
    _verify_summary(
        summary,
        manifest=manifest,
        exceptions=exceptions,
        conflicts=conflicts,
    )
    if _canonical(manifest) != manifest_bytes:
        raise ResolutionArtifactVerificationError("manifest bytes changed during verification")
    return VerifiedResolutionArtifact(
        run_id=run_id,
        relative_path=relative.as_posix(),
        manifest_sha256=embedded,
        summary=summary,
        exceptions=exceptions,
        conflicts=conflicts,
    )


__all__ = [
    "ResolutionArtifactConflictError",
    "ResolutionArtifactVerificationError",
    "ResolutionArtifactVerifier",
    "ResolutionArtifactWriter",
    "VerifiedResolutionArtifact",
    "verify_resolution_artifact",
]
