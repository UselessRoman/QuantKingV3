"""Framework-neutral read models and query service for the Stage 1 data center."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol, Self, cast

from quantking.common.errors import (
    DatasetVersionNotFoundError,
    InvalidIdError,
    InvalidTimeError,
    OverrideNotApplicableError,
)
from quantking.common.ids import EntityId, parse_entity_id
from quantking.common.time import Clock, TradingDate, UtcTimestamp, utc_now
from quantking.data.adapters.warehouse.errors import (
    IncompatibleInputError,
    ManifestIntegrityError,
    QueryContractError,
)
from quantking.data.assessment.matrix import default_source_matrix
from quantking.data.assessment.models import SourceCandidateAssessment
from quantking.data.models import (
    BatchFailureInfo,
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    DataSource,
    DifferenceReport,
    IngestionBatch,
    IngestionBatchStatus,
    QualityRun,
    QualityRunStatus,
    SourceOverride,
    SourceOverrideStatus,
)
from quantking.data.schemas import descriptor_for
from quantking.jobs.models import AuditRecord, FailureInfo, JobRun, JobStatus

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100
MAX_PREVIEW_LIMIT = 200


class InvalidDataCenterQueryError(ValueError):
    """Stable caller error for an invalid filter or opaque cursor."""


class DataCenterReadIntegrityError(InvalidDataCenterQueryError):
    """Stable conflict raised when persisted read-side lineage is inconsistent."""


@dataclass(frozen=True, slots=True)
class DataCenterPage:
    items: tuple[Mapping[str, object], ...]
    next_cursor: str | None


class DataRepositoryPort(Protocol):
    def get_batch(self, batch_id: str) -> IngestionBatch: ...
    def get_version(self, version_id: str) -> DatasetVersion: ...
    def get_quality_run(self, run_id: str) -> QualityRun: ...
    def get_difference_report(self, report_id: str) -> DifferenceReport: ...
    def get_override(self, override_id: str) -> SourceOverride: ...
    def find_latest_override_application_binding(
        self, override_id: str
    ) -> OverrideBinding | None: ...
    def find_active_override_application_binding(
        self, override_id: str
    ) -> OverrideBinding | None: ...
    def find_last_override_application_attempt(
        self, override_id: str
    ) -> OverrideAttempt | None: ...
    def list_override_audits(self, override_id: str, *, limit: int) -> list[AuditRecord]: ...
    def find_latest_source_candidate_assessment(
        self, *, source: str, dataset: DatasetKind
    ) -> SourceCandidateAssessment | None: ...
    def count_failed_entities(self) -> int: ...

    def list_batches_page(
        self,
        *,
        dataset: DatasetKind | None,
        source: DataSource | None,
        status: IngestionBatchStatus | None,
        started_from: str | None,
        started_to: str | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[IngestionBatch]: ...

    def list_versions_page(
        self,
        *,
        dataset: DatasetKind | None,
        status: DatasetVersionStatus | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[DatasetVersion]: ...

    def list_quality_runs_page(
        self,
        *,
        version_id: str | None,
        status: QualityRunStatus | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[QualityRun]: ...

    def list_difference_reports_page(
        self,
        *,
        version_id: str | None,
        primary_source: DataSource | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[DifferenceReport]: ...

    def list_overrides_page(
        self,
        *,
        dataset: DatasetKind | None,
        status: SourceOverrideStatus | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[SourceOverride]: ...


class DataUnitOfWork(Protocol):
    @property
    def data(self) -> DataRepositoryPort: ...

    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


class DataUnitOfWorkFactory(Protocol):
    def __call__(self) -> DataUnitOfWork: ...


class CanonicalPreviewPort(Protocol):
    def query_by_id(
        self,
        *,
        version_id: str,
        dataset: str,
        fields: Sequence[str] | None = None,
        codes: Sequence[str] | None = None,
        start_date: TradingDate | None = None,
        end_date: TradingDate | None = None,
        after_sort_key: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> CanonicalTable: ...

    def verify_manifest_by_id(self, *, version_id: str, dataset: str) -> VerifiedManifest: ...


class CanonicalTable(Protocol):
    def to_pylist(self) -> list[dict[str, object]]: ...


class VerifiedManifest(Protocol):
    manifest: Mapping[str, object]


class VerifiedDifference(Protocol):
    report_id: EntityId
    dataset: str
    primary_source: str
    fallback_source: str
    report_relative_path: str
    primary_version_id: EntityId
    fallback_version_id: EntityId
    primary_batch_id: EntityId
    fallback_batch_id: EntityId
    primary_content_sha256: str
    fallback_content_sha256: str
    primary_quality_run_id: EntityId
    fallback_quality_run_id: EntityId
    sample_spec_sha256: str
    threshold_rule_version: str
    report_sha256: str
    total_compared: int
    total_differences: int
    field_differences: Mapping[str, str]
    row_differences: list[dict[str, object]]
    created_at: UtcTimestamp


class OverrideBinding(Protocol):
    application_id: EntityId
    override_id: EntityId
    status: str
    child_version_id: EntityId
    input_fingerprint: str
    report_sha256: str | None
    contribution_scope_sha256: str | None
    claimed_by: str
    claimed_at: UtcTimestamp
    updated_at: UtcTimestamp
    failure_message: str | None


class OverrideAttempt(Protocol):
    attempt_id: EntityId
    override_id: EntityId
    attempt_no: int
    status: str
    child_version_id: EntityId
    input_fingerprint: str
    claimed_by: str
    started_at: UtcTimestamp
    ended_at: UtcTimestamp | None
    failure_message: str | None


class DifferenceVerifierPort(Protocol):
    def verify(self, *, report_id: EntityId) -> VerifiedDifference: ...


class JobListPort(Protocol):
    def get_job(self, job_id: str) -> JobRun: ...
    def list_jobs_page(
        self,
        *,
        job_type: str | None,
        status: JobStatus | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[JobRun]: ...
    def count_jobs(self, *, job_type: str | None, status: JobStatus | None) -> int: ...


_CURSOR_VERSION = 2
_CURSOR_SORT_VERSION = "created-desc-id-desc.v1"
_HEX64 = re.compile(r"^[0-9a-f]{64}\Z")


def _query_fingerprint(kind: str, filters: Mapping[str, object]) -> str:
    raw = json.dumps(
        {
            "resource": kind,
            "sort_version": _CURSOR_SORT_VERSION,
            "filters": dict(filters),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _cursor(
    kind: str,
    query_fingerprint: str,
    timestamp: str,
    stable_id: str,
    values: Sequence[str] = (),
    *,
    offset: int | None = None,
) -> str:
    raw = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "k": kind,
            "q": query_fingerprint,
            "t": timestamp,
            "i": stable_id,
            "s": list(values),
            "o": offset,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _parse_cursor(
    value: str | None,
    kind: str,
    *,
    query_fingerprint: str | None = None,
    stable_namespaces: frozenset[str] | None = None,
    sort_key_size: int | None = None,
    offset_required: bool = False,
) -> tuple[str, str, tuple[str, ...], int | None] | None:
    if value is None:
        return None
    if not value or len(value) > 1024:
        raise InvalidDataCenterQueryError("cursor is invalid")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != value:
            raise ValueError("cursor encoding is not canonical")
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidDataCenterQueryError("cursor is invalid") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"i", "k", "o", "q", "s", "t", "v"}
        or payload["v"] != _CURSOR_VERSION
        or payload["k"] != kind
        or not isinstance(payload["q"], str)
        or not _HEX64.fullmatch(payload["q"])
        or not isinstance(payload["t"], str)
        or not isinstance(payload["i"], str)
        or not payload["t"]
        or not payload["i"]
        or not isinstance(payload["s"], list)
        or any(not isinstance(item, str) or not item for item in payload["s"])
        or (
            payload["o"] is not None
            and (
                not isinstance(payload["o"], int)
                or isinstance(payload["o"], bool)
                or payload["o"] < 0
            )
        )
    ):
        raise InvalidDataCenterQueryError("cursor is invalid")
    try:
        if UtcTimestamp.from_iso(payload["t"]).to_iso() != payload["t"]:
            raise ValueError("timestamp is not canonical UTC")
    except (InvalidTimeError, ValueError) as error:
        raise InvalidDataCenterQueryError("cursor is invalid") from error
    if query_fingerprint is not None and payload["q"] != query_fingerprint:
        raise InvalidDataCenterQueryError("cursor does not match the current query")
    if stable_namespaces is not None:
        try:
            if parse_entity_id(payload["i"]).namespace not in stable_namespaces:
                raise ValueError("cursor id namespace is invalid")
        except (InvalidIdError, ValueError) as error:
            raise InvalidDataCenterQueryError("cursor is invalid") from error
    if sort_key_size is not None and len(payload["s"]) != sort_key_size:
        raise InvalidDataCenterQueryError("cursor sort key is invalid")
    if offset_required != (payload["o"] is not None):
        raise InvalidDataCenterQueryError("cursor offset is invalid")
    return payload["t"], payload["i"], tuple(payload["s"]), payload["o"]


def validate_cursor(value: str | None, kind: str) -> None:
    """Validate an opaque cursor at a transport boundary without returning internals."""
    _parse_cursor(value, kind)


def _failure(value: BatchFailureInfo | FailureInfo | None) -> Mapping[str, object] | None:
    if value is None:
        return None
    return {
        "code": str(getattr(value.error_type, "value", value.error_type)),
        "message": str(value.message),
        "retryable": bool(value.retryable),
    }


def _batch(item: IngestionBatch) -> Mapping[str, object]:
    return {
        "batch_id": str(item.batch_id),
        "source": item.source.value,
        "dataset": item.dataset.value,
        "status": item.status.value,
        "started_at": item.started_at.to_iso(),
        "ended_at": item.ended_at.to_iso() if item.ended_at else None,
        "row_count": item.row_count,
        "security_count": item.security_count,
        "job_id": item.job_run_id,
        "failure": _failure(item.failure),
    }


def _version(item: DatasetVersion) -> Mapping[str, object]:
    return {
        "version_id": str(item.version_id),
        "dataset": item.dataset.value,
        "status": item.status.value,
        "schema_version": item.schema_version,
        "code_version": item.code_version,
        "rule_version": item.rule_version,
        "parent_version_id": str(item.parent_version_id) if item.parent_version_id else None,
        "input_batch_ids": [str(value) for value in item.input_batch_ids],
        "created_at": item.created_at.to_iso(),
        "published_at": item.published_at.to_iso() if item.published_at else None,
        "row_count": item.row_count,
        "security_count": item.security_count,
        "coverage_start": str(item.coverage_start) if item.coverage_start else None,
        "coverage_end": str(item.coverage_end) if item.coverage_end else None,
        "coverage": dict(item.coverage_summary),
        "quality_run_id": str(item.quality_run_id) if item.quality_run_id else None,
        "manifest_sha256": item.manifest_sha256,
        "content_sha256": item.content_sha256,
        "validated_at": item.validated_at.to_iso() if item.validated_at else None,
        "failure": _failure(item.failure),
    }


def _quality(item: QualityRun, *, details: bool = False) -> Mapping[str, object]:
    result: dict[str, object] = {
        "run_id": str(item.run_id),
        "version_id": str(item.version_id),
        "status": item.status.value,
        "rule_version": item.rule_version,
        "schema_version": item.schema_version,
        "started_at": item.started_at.to_iso(),
        "ended_at": item.ended_at.to_iso() if item.ended_at else None,
        "failure": _failure(item.failure),
    }
    if details:
        result["results"] = [
            {
                "rule_code": value.rule_code,
                "status": "PASSED" if value.passed else "FAILED",
                "affected_count": value.affected_count,
                "summary": value.summary,
            }
            for value in item.results
        ]
    return result


def _report(item: DifferenceReport) -> Mapping[str, object]:
    return {
        "report_id": str(item.report_id),
        "version_id": str(item.version_id),
        "primary_source": item.primary_source.value,
        "fallback_source": item.fallback_source.value,
        "created_at": item.created_at.to_iso(),
        "field_differences": dict(item.field_differences),
        "total_compared": item.total_compared,
        "total_differences": item.total_differences,
        "primary_batch_id": str(item.primary_batch_id),
        "fallback_batch_id": str(item.fallback_batch_id),
        "primary_version_id": str(item.primary_version_id),
        "fallback_version_id": str(item.fallback_version_id),
        "primary_content_sha256": item.primary_content_sha256,
        "fallback_content_sha256": item.fallback_content_sha256,
        "primary_quality_run_id": str(item.primary_quality_run_id),
        "fallback_quality_run_id": str(item.fallback_quality_run_id),
        "sample_spec_sha256": item.sample_spec_sha256,
        "threshold_rule_version": item.threshold_rule_version,
        "report_sha256": item.report_sha256,
    }


def _override(item: SourceOverride) -> dict[str, object]:
    return {
        "override_id": str(item.override_id),
        "dataset": item.dataset.value,
        "status": item.status.value,
        "difference_report_id": str(item.difference_report_id),
        "primary_source": item.primary_source.value,
        "fallback_source": item.fallback_source.value,
        "primary_batch_id": str(item.primary_batch_id),
        "fallback_batch_id": str(item.fallback_batch_id),
        "field_replacements": [dict(value) for value in item.field_replacements],
        "fallback_only_keys": [dict(value) for value in item.fallback_only_keys],
        "exact_scope_sha256": item.exact_scope_sha256,
        "reason": item.reason,
        "created_at": item.created_at.to_iso(),
        "approved_at": item.approved_at.to_iso() if item.approved_at else None,
        "approved_by": item.approved_by,
        "applied_at": item.applied_at.to_iso() if item.applied_at else None,
        "applied_version_id": str(item.applied_version_id) if item.applied_version_id else None,
    }


def _job(item: JobRun) -> Mapping[str, object]:
    return {
        "job_id": item.job_id,
        "job_type": item.job_type,
        "status": item.status.value,
        "created_at": item.created_at.to_iso(),
        "started_at": item.started_at.to_iso() if item.started_at else None,
        "ended_at": item.ended_at.to_iso() if item.ended_at else None,
        "stage": item.stage,
        "progress": item.progress,
        "row_version": item.row_version,
        "failure": (
            {
                "code": item.failure.error_type,
                "message": item.failure.message,
                "retryable": item.failure.retryable,
            }
            if item.failure
            else None
        ),
    }


def _json_value(value: object) -> object:
    if isinstance(value, (date, datetime, Decimal)):
        return str(value)
    return value


def _canonical_timestamp_filter(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return UtcTimestamp.from_iso(value).to_iso()
    except InvalidTimeError as error:
        raise InvalidDataCenterQueryError("timestamp filter is invalid") from error


def _optional_entity_filter(value: str | None, namespace: str) -> str | None:
    if value is None:
        return None
    try:
        parsed = parse_entity_id(value)
    except InvalidIdError as error:
        raise InvalidDataCenterQueryError("entity filter is invalid") from error
    if parsed.namespace != namespace:
        raise InvalidDataCenterQueryError("entity filter is invalid")
    return str(parsed)


def _validate_preview_sort_values(dataset: str, values: Sequence[str]) -> None:
    from quantking.data.adapters.canonical_schema import sort_key

    keys = sort_key(dataset)
    if len(values) != len(keys):
        raise InvalidDataCenterQueryError("cursor sort key is invalid")
    for name, value in zip(keys, values, strict=True):
        if name.endswith("_date") or name in {"effective_from", "effective_to"}:
            try:
                if str(TradingDate.from_iso(value)) != value:
                    raise ValueError("non-canonical date")
            except (InvalidTimeError, ValueError) as error:
                raise InvalidDataCenterQueryError("cursor sort key is invalid") from error


def _override_provenance(override: SourceOverride, report: DifferenceReport) -> dict[str, object]:
    selected: dict[tuple[tuple[str, str], ...], set[str]] = {}
    for unit in override.field_replacements:
        key = tuple(sorted((name, value) for name, value in unit.items() if name != "field"))
        selected.setdefault(key, set()).add(unit["field"])
    contributions: list[dict[str, object]] = [
        {
            "primary_key": dict(key),
            "kind": "FIELD_REPLACEMENT",
            "fields": sorted(unit_fields),
        }
        for key, unit_fields in sorted(selected.items())
    ]
    descriptor = descriptor_for(override.dataset.value)
    inserted_fields = sorted(set(descriptor.field_names) - set(descriptor.primary_key))
    contributions.extend(
        {
            "primary_key": dict(sorted(unit.items())),
            "kind": "ROW_INSERT",
            "fields": inserted_fields,
        }
        for unit in override.fallback_only_keys
    )
    contributions.sort(
        key=lambda item: (
            tuple(sorted(cast(Mapping[str, str], item["primary_key"]).items())),
            str(item["kind"]),
        )
    )
    replacement_cells = 0
    row_insert_count = 0
    for item in contributions:
        if item["kind"] == "FIELD_REPLACEMENT":
            replacement_cells += len(cast(Sequence[str], item["fields"]))
        if item["kind"] == "ROW_INSERT":
            row_insert_count += 1
    summary = {
        "field_replacement_cells": str(replacement_cells),
        "row_insert_count": str(row_insert_count),
        "affected_primary_key_count": str(len(contributions)),
    }
    return {
        "override_id": str(override.override_id),
        "difference_report_id": str(report.report_id),
        "report_sha256": report.report_sha256,
        "primary": {
            "source": report.primary_source.value,
            "batch_id": str(report.primary_batch_id),
            "version_id": str(report.primary_version_id),
            "content_sha256": report.primary_content_sha256,
        },
        "fallback": {
            "source": report.fallback_source.value,
            "batch_id": str(report.fallback_batch_id),
            "version_id": str(report.fallback_version_id),
            "content_sha256": report.fallback_content_sha256,
        },
        "fallback_contributions": contributions,
        "contribution_summary": summary,
        "contribution_scope_sha256": override.contribution_scope_sha256,
    }


def _binding_response(binding: OverrideBinding) -> Mapping[str, object]:
    return {
        "application_id": str(binding.application_id),
        "override_id": str(binding.override_id),
        "status": binding.status,
        "child_version_id": str(binding.child_version_id),
        "input_fingerprint": binding.input_fingerprint,
        "report_sha256": binding.report_sha256,
        "contribution_scope_sha256": binding.contribution_scope_sha256,
        "claimed_by": binding.claimed_by,
        "claimed_at": binding.claimed_at.to_iso(),
        "updated_at": binding.updated_at.to_iso(),
        "failure_message": binding.failure_message,
    }


def _attempt_response(attempt: OverrideAttempt) -> Mapping[str, object]:
    return {
        "attempt_id": str(attempt.attempt_id),
        "override_id": str(attempt.override_id),
        "attempt_no": attempt.attempt_no,
        "status": attempt.status,
        "child_version_id": str(attempt.child_version_id),
        "input_fingerprint": attempt.input_fingerprint,
        "claimed_by": attempt.claimed_by,
        "started_at": attempt.started_at.to_iso(),
        "ended_at": attempt.ended_at.to_iso() if attempt.ended_at else None,
        "failure_message": attempt.failure_message,
    }


class DataCenterQueryService:
    """Compose metadata, verified artifacts and bounded Canonical queries."""

    def __init__(
        self,
        *,
        data_uow_factory: DataUnitOfWorkFactory,
        preview: CanonicalPreviewPort,
        difference_verifier: DifferenceVerifierPort,
        jobs: JobListPort,
        clock: Clock | None = None,
    ) -> None:
        self._uow_factory = data_uow_factory
        self._preview = preview
        self._difference_verifier = difference_verifier
        self._jobs = jobs
        self._clock = clock

    def sources(self) -> list[Mapping[str, object]]:
        rows: list[Mapping[str, object]] = []
        with self._uow_factory() as uow:
            for item in default_source_matrix().entries:
                try:
                    assessment = uow.data.find_latest_source_candidate_assessment(
                        source=item.source,
                        dataset=item.dataset,
                    )
                except (InvalidIdError, ValueError) as error:
                    raise DataCenterReadIntegrityError(
                        "source assessment evidence is invalid"
                    ) from error
                rows.append(
                    {
                        "source": item.source,
                        "dataset": item.dataset.value,
                        "role": item.role.value,
                        "status": item.status.value,
                        "matrix_status": item.status.value,
                        "evidence_kind": (
                            assessment.evidence_kind.value if assessment is not None else None
                        ),
                        "assessment_status": (
                            assessment.status.value if assessment is not None else None
                        ),
                        "stability_runs": (
                            assessment.stability_runs if assessment is not None else None
                        ),
                        "gates": (
                            {
                                "semantic_contract_verified": (
                                    assessment.semantic_contract_verified
                                ),
                                "schema_unit_verified": assessment.schema_unit_verified,
                                "coverage_adequate": assessment.coverage_adequate,
                                "quality_passed": assessment.quality_passed,
                                "fixed_sample_differences_adequate": (
                                    assessment.fixed_sample_differences_adequate
                                ),
                            }
                            if assessment is not None
                            else None
                        ),
                        "blocking_reason": (
                            assessment.blocking_reason
                            if assessment is not None
                            else item.blocking_reason
                        ),
                        "matrix_blocking_reason": item.blocking_reason,
                        "assessment_config_version": (
                            assessment.assessment_config_version if assessment is not None else None
                        ),
                        "sample_spec_version": (
                            assessment.sample_spec_version if assessment is not None else None
                        ),
                        "rule_version": (
                            assessment.rule_version if assessment is not None else None
                        ),
                        "assessment_id": (
                            str(assessment.assessment_id) if assessment is not None else None
                        ),
                        "auto_switch": item.auto_switch,
                    }
                )
        return rows

    def summary(self) -> Mapping[str, object]:
        datasets: list[Mapping[str, object]] = []
        with self._uow_factory() as uow:
            failure_count = uow.data.count_failed_entities()
            for dataset in DatasetKind:
                versions = uow.data.list_versions_page(
                    dataset=dataset, status=None, before=None, limit=1
                )
                published = uow.data.list_versions_page(
                    dataset=dataset,
                    status=DatasetVersionStatus.PUBLISHED,
                    before=None,
                    limit=1,
                )
                current = published[0] if published else (versions[0] if versions else None)
                datasets.append(
                    {
                        "dataset": dataset.value,
                        "version": _version(current) if current else None,
                    }
                )
        return {
            "datasets": datasets,
            "failures": failure_count
            + self._jobs.count_jobs(job_type="data.eod.v1", status=JobStatus.FAILED),
            "blocked_sources": sum(item["status"] != "PRODUCTION_READY" for item in self.sources()),
            "generated_at": utc_now(clock=self._clock).to_iso(),
        }

    @staticmethod
    def _page(
        kind: str,
        query_fingerprint: str,
        values: Sequence[Any],
        limit: int,
        mapper: Any,
        timestamp: Any,
        stable_id: Any,
    ) -> DataCenterPage:
        visible = values[:limit]
        next_cursor = None
        if len(values) > limit and visible:
            last = visible[-1]
            next_cursor = _cursor(
                kind,
                query_fingerprint,
                timestamp(last),
                stable_id(last),
            )
        return DataCenterPage(tuple(mapper(value) for value in visible), next_cursor)

    def list_batches(
        self,
        *,
        dataset: str | None = None,
        source: str | None = None,
        status: str | None = None,
        started_from: str | None = None,
        started_to: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> DataCenterPage:
        started_from = _canonical_timestamp_filter(started_from)
        started_to = _canonical_timestamp_filter(started_to)
        fingerprint = _query_fingerprint(
            "batches",
            {
                "dataset": dataset,
                "source": source,
                "status": status,
                "started_from": started_from,
                "started_to": started_to,
            },
        )
        parsed = _parse_cursor(
            cursor,
            "batches",
            query_fingerprint=fingerprint,
            stable_namespaces=frozenset({"data_batch"}),
        )
        with self._uow_factory() as uow:
            rows = uow.data.list_batches_page(
                dataset=DatasetKind(dataset) if dataset else None,
                source=DataSource(source) if source else None,
                status=IngestionBatchStatus(status) if status else None,
                started_from=started_from,
                started_to=started_to,
                before=(parsed[0], parsed[1]) if parsed else None,
                limit=limit + 1,
            )
        return self._page(
            "batches",
            fingerprint,
            rows,
            limit,
            _batch,
            lambda x: x.started_at.to_iso(),
            lambda x: str(x.batch_id),
        )

    def list_versions(
        self,
        *,
        dataset: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> DataCenterPage:
        fingerprint = _query_fingerprint(
            "versions",
            {"dataset": dataset, "status": status},
        )
        parsed = _parse_cursor(
            cursor,
            "versions",
            query_fingerprint=fingerprint,
            stable_namespaces=frozenset({"data_version"}),
        )
        with self._uow_factory() as uow:
            rows = uow.data.list_versions_page(
                dataset=DatasetKind(dataset) if dataset else None,
                status=DatasetVersionStatus(status) if status else None,
                before=(parsed[0], parsed[1]) if parsed else None,
                limit=limit + 1,
            )
        return self._page(
            "versions",
            fingerprint,
            rows,
            limit,
            _version,
            lambda x: x.created_at.to_iso(),
            lambda x: str(x.version_id),
        )

    def get_version(self, version_id: str) -> Mapping[str, object]:
        with self._uow_factory() as uow:
            return _version(uow.data.get_version(version_id))

    def preview(
        self,
        *,
        version_id: str,
        fields: Sequence[str] | None,
        codes: Sequence[str] | None,
        start_date: str | None,
        end_date: str | None,
        cursor: str | None,
        limit: int,
    ) -> DataCenterPage:
        if version_id.casefold() == "latest":
            raise QueryContractError("latest is forbidden")
        with self._uow_factory() as uow:
            version = uow.data.get_version(version_id)
        from quantking.data.adapters.canonical_schema import sort_key

        canonical_key = sort_key(version.dataset.value)
        requested_fields = tuple(fields) if fields is not None else None
        canonical_codes = tuple(sorted(set(codes))) if codes is not None else None
        fingerprint = _query_fingerprint(
            "preview",
            {
                "version_id": version_id,
                "dataset": version.dataset.value,
                "fields": list(requested_fields) if requested_fields is not None else None,
                "codes": list(canonical_codes) if canonical_codes is not None else None,
                "start_date": start_date,
                "end_date": end_date,
                "dataset_sort_key": list(canonical_key),
            },
        )
        parsed = _parse_cursor(
            cursor,
            f"preview:{version_id}",
            query_fingerprint=fingerprint,
            stable_namespaces=frozenset({"data_version"}),
            sort_key_size=len(canonical_key),
        )
        if parsed:
            _validate_preview_sort_values(version.dataset.value, parsed[2])
        query_fields = (
            tuple(dict.fromkeys((*requested_fields, *canonical_key)))
            if requested_fields is not None
            else None
        )
        table = self._preview.query_by_id(
            version_id=version_id,
            dataset=version.dataset.value,
            fields=query_fields,
            codes=canonical_codes,
            start_date=TradingDate.from_iso(start_date) if start_date else None,
            end_date=TradingDate.from_iso(end_date) if end_date else None,
            after_sort_key=parsed[2] if parsed else None,
            limit=limit + 1,
        )
        rows = list(table.to_pylist())
        visible = rows[:limit]
        next_cursor = None
        if len(rows) > limit and visible:
            last = visible[-1]
            values = tuple(str(last[name]) for name in canonical_key)
            next_cursor = _cursor(
                f"preview:{version_id}",
                fingerprint,
                version.created_at.to_iso(),
                version_id,
                values,
            )
        if requested_fields is not None:
            visible = [{name: row[name] for name in requested_fields} for row in visible]
        return DataCenterPage(
            tuple({name: _json_value(value) for name, value in row.items()} for row in visible),
            next_cursor,
        )

    def list_quality_runs(
        self,
        *,
        version_id: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> DataCenterPage:
        version_id = _optional_entity_filter(version_id, "data_version")
        fingerprint = _query_fingerprint(
            "quality",
            {"version_id": version_id, "status": status},
        )
        parsed = _parse_cursor(
            cursor,
            "quality",
            query_fingerprint=fingerprint,
            stable_namespaces=frozenset({"data_quality"}),
        )
        with self._uow_factory() as uow:
            rows = uow.data.list_quality_runs_page(
                version_id=version_id,
                status=QualityRunStatus(status) if status else None,
                before=(parsed[0], parsed[1]) if parsed else None,
                limit=limit + 1,
            )
        return self._page(
            "quality",
            fingerprint,
            rows,
            limit,
            _quality,
            lambda x: x.started_at.to_iso(),
            lambda x: str(x.run_id),
        )

    def get_quality_run(self, run_id: str) -> Mapping[str, object]:
        with self._uow_factory() as uow:
            return _quality(uow.data.get_quality_run(run_id), details=True)

    def list_difference_reports(
        self,
        *,
        version_id: str | None = None,
        primary_source: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> DataCenterPage:
        version_id = _optional_entity_filter(version_id, "data_version")
        fingerprint = _query_fingerprint(
            "differences",
            {"version_id": version_id, "primary_source": primary_source},
        )
        parsed = _parse_cursor(
            cursor,
            "differences",
            query_fingerprint=fingerprint,
            stable_namespaces=frozenset({"data_diff"}),
        )
        with self._uow_factory() as uow:
            rows = uow.data.list_difference_reports_page(
                version_id=version_id,
                primary_source=DataSource(primary_source) if primary_source else None,
                before=(parsed[0], parsed[1]) if parsed else None,
                limit=limit + 1,
            )
        return self._page(
            "differences",
            fingerprint,
            rows,
            limit,
            _report,
            lambda x: x.created_at.to_iso(),
            lambda x: str(x.report_id),
        )

    def get_difference_report(self, report_id: str) -> Mapping[str, object]:
        report, _verified = self._load_verified_difference(report_id)
        return _report(report)

    def _load_verified_difference(
        self, report_id: str
    ) -> tuple[DifferenceReport, VerifiedDifference]:
        """Load one report through the single strict SQLite/artifact read boundary."""
        parsed_report_id = parse_entity_id(report_id)
        verified = self._difference_verifier.verify(report_id=parsed_report_id)
        with self._uow_factory() as uow:
            report = uow.data.get_difference_report(report_id)
            artifact_credentials: tuple[tuple[str, object, object], ...] = (
                ("report_id", report.report_id, verified.report_id),
                ("version_id", report.version_id, verified.primary_version_id),
                ("primary_source", report.primary_source.value, verified.primary_source),
                ("fallback_source", report.fallback_source.value, verified.fallback_source),
                ("primary_batch_id", report.primary_batch_id, verified.primary_batch_id),
                ("fallback_batch_id", report.fallback_batch_id, verified.fallback_batch_id),
                ("primary_version_id", report.primary_version_id, verified.primary_version_id),
                ("fallback_version_id", report.fallback_version_id, verified.fallback_version_id),
                (
                    "primary_content_sha256",
                    report.primary_content_sha256,
                    verified.primary_content_sha256,
                ),
                (
                    "fallback_content_sha256",
                    report.fallback_content_sha256,
                    verified.fallback_content_sha256,
                ),
                (
                    "primary_quality_run_id",
                    report.primary_quality_run_id,
                    verified.primary_quality_run_id,
                ),
                (
                    "fallback_quality_run_id",
                    report.fallback_quality_run_id,
                    verified.fallback_quality_run_id,
                ),
                (
                    "sample_spec_sha256",
                    report.sample_spec_sha256,
                    verified.sample_spec_sha256,
                ),
                (
                    "threshold_rule_version",
                    report.threshold_rule_version,
                    verified.threshold_rule_version,
                ),
                ("report_sha256", report.report_sha256, verified.report_sha256),
                ("created_at", report.created_at.to_iso(), verified.created_at.to_iso()),
                ("total_compared", report.total_compared, verified.total_compared),
                ("total_differences", report.total_differences, verified.total_differences),
                (
                    "field_differences",
                    dict(report.field_differences),
                    dict(verified.field_differences),
                ),
                (
                    "report_relative_path",
                    report.report_relative_path,
                    verified.report_relative_path,
                ),
            )
            if any(left != right for _name, left, right in artifact_credentials):
                raise DataCenterReadIntegrityError(
                    "difference report artifact and metadata lineage do not match"
                )
            primary_version = uow.data.get_version(str(report.primary_version_id))
            fallback_version = uow.data.get_version(str(report.fallback_version_id))
            primary_quality = uow.data.get_quality_run(str(report.primary_quality_run_id))
            fallback_quality = uow.data.get_quality_run(str(report.fallback_quality_run_id))
            primary_batch = uow.data.get_batch(str(report.primary_batch_id))
            fallback_batch = uow.data.get_batch(str(report.fallback_batch_id))

        for side, version, quality, batch, expected_source, expected_batch, content_sha in (
            (
                "primary",
                primary_version,
                primary_quality,
                primary_batch,
                report.primary_source,
                report.primary_batch_id,
                report.primary_content_sha256,
            ),
            (
                "fallback",
                fallback_version,
                fallback_quality,
                fallback_batch,
                report.fallback_source,
                report.fallback_batch_id,
                report.fallback_content_sha256,
            ),
        ):
            valid = (
                version.status is DatasetVersionStatus.PUBLISHED
                and version.dataset.value == verified.dataset
                and version.content_sha256 == content_sha
                and version.quality_run_id == quality.run_id
                and quality.run_id
                == (
                    report.primary_quality_run_id
                    if side == "primary"
                    else report.fallback_quality_run_id
                )
                and quality.status is QualityRunStatus.PASSED
                and quality.version_id == version.version_id
                and quality.schema_version == version.schema_version
                and quality.rule_version == version.rule_version
                and quality.content_sha256 == version.content_sha256
                and batch.batch_id == expected_batch
                and batch.source is expected_source
                and batch.dataset is version.dataset
                and batch.status in {IngestionBatchStatus.SUCCEEDED, IngestionBatchStatus.PARTIAL}
                and expected_batch in version.input_batch_ids
            )
            if not valid:
                raise DataCenterReadIntegrityError(
                    f"difference report {side} version or quality lineage is invalid"
                )
            try:
                self._preview.verify_manifest_by_id(
                    version_id=str(version.version_id),
                    dataset=version.dataset.value,
                )
            except (ManifestIntegrityError, IncompatibleInputError, ValueError) as error:
                raise DataCenterReadIntegrityError(
                    f"difference report {side} published manifest is invalid"
                ) from error
        return report, verified

    def difference_rows(self, *, report_id: str, cursor: str | None, limit: int) -> DataCenterPage:
        fingerprint = _query_fingerprint("difference-rows", {"report_id": report_id})
        parsed = _parse_cursor(
            cursor,
            f"difference-rows:{report_id}",
            query_fingerprint=fingerprint,
            stable_namespaces=frozenset({"data_diff"}),
            offset_required=True,
        )
        _report_value, verified = self._load_verified_difference(report_id)
        rows = list(verified.row_differences)
        start = parsed[3] if parsed else 0
        assert start is not None
        visible = rows[start : start + limit]
        next_cursor = None
        if start + limit < len(rows):
            next_cursor = _cursor(
                f"difference-rows:{report_id}",
                fingerprint,
                verified.created_at.to_iso(),
                report_id,
                offset=start + limit,
            )
        return DataCenterPage(tuple(visible), next_cursor)

    def list_source_overrides(
        self,
        *,
        dataset: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> DataCenterPage:
        fingerprint = _query_fingerprint(
            "overrides",
            {"dataset": dataset, "status": status},
        )
        parsed = _parse_cursor(
            cursor,
            "overrides",
            query_fingerprint=fingerprint,
            stable_namespaces=frozenset({"data_override"}),
        )
        with self._uow_factory() as uow:
            rows = uow.data.list_overrides_page(
                dataset=DatasetKind(dataset) if dataset else None,
                status=SourceOverrideStatus(status) if status else None,
                before=(parsed[0], parsed[1]) if parsed else None,
                limit=limit + 1,
            )
        return self._page(
            "overrides",
            fingerprint,
            rows,
            limit,
            _override,
            lambda x: x.created_at.to_iso(),
            lambda x: str(x.override_id),
        )

    @staticmethod
    def _validate_override_state_graph(
        override: SourceOverride,
        latest_binding: OverrideBinding | None,
        active_binding: OverrideBinding | None,
        attempt: OverrideAttempt | None,
    ) -> None:
        if latest_binding is None:
            if active_binding is not None or attempt is not None:
                raise DataCenterReadIntegrityError(
                    "override application and attempt presence do not match"
                )
            if override.status is SourceOverrideStatus.APPLIED:
                raise DataCenterReadIntegrityError(
                    "APPLIED override application binding is missing"
                )
            return

        if attempt is None:
            raise DataCenterReadIntegrityError(
                "override application and attempt presence do not match"
            )
        if (
            latest_binding.override_id != override.override_id
            or attempt.override_id != override.override_id
            or latest_binding.child_version_id != attempt.child_version_id
            or latest_binding.input_fingerprint != attempt.input_fingerprint
            or latest_binding.status != attempt.status
        ):
            raise DataCenterReadIntegrityError(
                "override application and attempt lineage do not match"
            )

        allowed_statuses = {
            SourceOverrideStatus.PENDING: frozenset(),
            SourceOverrideStatus.REJECTED: frozenset(),
            SourceOverrideStatus.APPROVED: frozenset({"FAILED", "CLAIMED", "PUBLISHED"}),
            SourceOverrideStatus.APPLIED: frozenset({"APPLIED"}),
        }
        if latest_binding.status not in allowed_statuses[override.status]:
            raise DataCenterReadIntegrityError(
                f"{override.status.value} override application state is invalid"
            )

        requires_active = latest_binding.status in {"CLAIMED", "PUBLISHED", "APPLIED"}
        if requires_active != (active_binding is not None):
            raise DataCenterReadIntegrityError(
                "override active application does not match its state"
            )
        if active_binding is not None and (
            active_binding.application_id != latest_binding.application_id
            or active_binding.override_id != latest_binding.override_id
            or active_binding.child_version_id != latest_binding.child_version_id
            or active_binding.input_fingerprint != latest_binding.input_fingerprint
            or active_binding.status != latest_binding.status
            or active_binding.report_sha256 != latest_binding.report_sha256
            or active_binding.contribution_scope_sha256 != latest_binding.contribution_scope_sha256
            or active_binding.claimed_by != latest_binding.claimed_by
            or active_binding.claimed_at != latest_binding.claimed_at
            or active_binding.updated_at != latest_binding.updated_at
            or active_binding.failure_message != latest_binding.failure_message
        ):
            raise DataCenterReadIntegrityError(
                "override active application is not the latest binding"
            )
        if override.status is SourceOverrideStatus.APPLIED and (
            override.applied_version_id != latest_binding.child_version_id
            or attempt.child_version_id != latest_binding.child_version_id
        ):
            raise DataCenterReadIntegrityError("APPLIED override application binding is invalid")

    def get_source_override(self, override_id: str) -> Mapping[str, object]:
        try:
            with self._uow_factory() as uow:
                override = uow.data.get_override(override_id)
                latest_binding = uow.data.find_latest_override_application_binding(override_id)
                active_binding = uow.data.find_active_override_application_binding(override_id)
                attempt = uow.data.find_last_override_application_attempt(override_id)
                audits = uow.data.list_override_audits(override_id, limit=100)
            report, verified = self._load_verified_difference(str(override.difference_report_id))
            if (
                override.dataset.value != verified.dataset
                or override.primary_source is not report.primary_source
                or override.fallback_source is not report.fallback_source
                or override.primary_batch_id != report.primary_batch_id
                or override.fallback_batch_id != report.fallback_batch_id
            ):
                raise DataCenterReadIntegrityError(
                    "override does not match its verified difference report"
                )
            self._validate_override_state_graph(
                override,
                latest_binding,
                active_binding,
                attempt,
            )

            child_response: Mapping[str, object] | None = None
            if latest_binding is not None:
                with self._uow_factory() as uow:
                    child = uow.data.get_version(str(latest_binding.child_version_id))
                    child_quality = (
                        uow.data.get_quality_run(str(child.quality_run_id))
                        if child.quality_run_id is not None
                        else None
                    )
                if (
                    child.dataset is not override.dataset
                    or child.parent_version_id != report.primary_version_id
                    or child.input_batch_ids
                    != (override.primary_batch_id, override.fallback_batch_id)
                    or (
                        override.approved_at is not None
                        and child.created_at.value < override.approved_at.value
                    )
                ):
                    raise DataCenterReadIntegrityError(
                        "override child is not the required direct lineage child"
                    )
                if latest_binding.status == "FAILED":
                    if child.status is not DatasetVersionStatus.FAILED:
                        raise DataCenterReadIntegrityError(
                            "FAILED override attempt does not bind a FAILED child"
                        )
                elif latest_binding.status == "CLAIMED":
                    if child.status is not DatasetVersionStatus.BUILDING:
                        raise DataCenterReadIntegrityError(
                            "CLAIMED override application does not bind a BUILDING child"
                        )
                else:
                    if (
                        latest_binding.status not in {"PUBLISHED", "APPLIED"}
                        or child.status is not DatasetVersionStatus.PUBLISHED
                        or latest_binding.report_sha256 != report.report_sha256
                        or latest_binding.contribution_scope_sha256
                        != override.contribution_scope_sha256
                        or child_quality is None
                        or child_quality.status is not QualityRunStatus.PASSED
                        or child_quality.version_id != child.version_id
                        or child_quality.run_id != child.quality_run_id
                        or child_quality.schema_version != child.schema_version
                        or child_quality.rule_version != child.rule_version
                        or child_quality.content_sha256 != child.content_sha256
                    ):
                        raise DataCenterReadIntegrityError(
                            "published override child credentials are invalid"
                        )
                    manifest = self._preview.verify_manifest_by_id(
                        version_id=str(child.version_id),
                        dataset=child.dataset.value,
                    ).manifest
                    if manifest.get("source_override") != _override_provenance(override, report):
                        raise DataCenterReadIntegrityError(
                            "override child manifest provenance is invalid"
                        )
                child_response = _version(child)
        except (
            InvalidIdError,
            DatasetVersionNotFoundError,
            OverrideNotApplicableError,
            ManifestIntegrityError,
            IncompatibleInputError,
            ValueError,
        ) as error:
            if isinstance(error, DataCenterReadIntegrityError):
                raise
            raise DataCenterReadIntegrityError("override application lineage is invalid") from error

        result = _override(override)
        result["application"] = (
            _binding_response(active_binding) if active_binding is not None else None
        )
        result["last_application"] = (
            _binding_response(latest_binding) if latest_binding is not None else None
        )
        result["attempt"] = _attempt_response(attempt) if attempt is not None else None
        result["child"] = child_response
        result["audits"] = [
            {
                "audit_id": audit.audit_id,
                "actor": audit.actor,
                "action": audit.action,
                "occurred_at": audit.occurred_at.to_iso(),
                "details": dict(audit.details),
            }
            for audit in audits
        ]
        return result

    def list_eod_runs(
        self,
        *,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> DataCenterPage:
        fingerprint = _query_fingerprint("eod", {"status": status})
        parsed = _parse_cursor(
            cursor,
            "eod",
            query_fingerprint=fingerprint,
            stable_namespaces=frozenset({"job"}),
        )
        rows = self._jobs.list_jobs_page(
            job_type="data.eod.v1",
            status=JobStatus(status) if status else None,
            before=(parsed[0], parsed[1]) if parsed else None,
            limit=limit + 1,
        )
        return self._page(
            "eod",
            fingerprint,
            rows,
            limit,
            _job,
            lambda x: x.created_at.to_iso(),
            lambda x: x.job_id,
        )

    def _failures(
        self, *, limit: int, before: tuple[str, str] | None
    ) -> list[Mapping[str, object]]:
        with self._uow_factory() as uow:
            batches = uow.data.list_batches_page(
                dataset=None,
                source=None,
                status=IngestionBatchStatus.FAILED,
                started_from=None,
                started_to=None,
                before=before,
                limit=limit,
            )
            versions = uow.data.list_versions_page(
                dataset=None,
                status=DatasetVersionStatus.FAILED,
                before=before,
                limit=limit,
            )
            qualities = uow.data.list_quality_runs_page(
                version_id=None,
                status=QualityRunStatus.FAILED,
                before=before,
                limit=limit,
            )
        jobs = self._jobs.list_jobs_page(
            job_type="data.eod.v1",
            status=JobStatus.FAILED,
            before=before,
            limit=limit,
        )
        failures: list[Mapping[str, object]] = []
        for kind, entities in (
            ("BATCH", batches),
            ("VERSION", versions),
            ("QUALITY", qualities),
            ("EOD", jobs),
        ):
            for entity in entities:
                stable_id = str(
                    getattr(
                        entity,
                        {"BATCH": "batch_id", "VERSION": "version_id", "QUALITY": "run_id"}.get(
                            kind, "job_id"
                        ),
                    )
                )
                timestamp = (
                    entity.started_at
                    if isinstance(entity, (IngestionBatch, QualityRun))
                    else entity.created_at
                )
                failure = getattr(entity, "failure", None)
                failures.append(
                    {
                        "kind": kind,
                        "entity_id": stable_id,
                        "stage": getattr(entity, "stage", None) or kind.casefold(),
                        "occurred_at": timestamp.to_iso(),
                        "code": (
                            getattr(failure.error_type, "value", failure.error_type)
                            if failure
                            else "QUALITY_RULE_FAILED"
                        ),
                        "message": failure.message if failure else "质量规则未通过。",
                        "_sort_id": stable_id,
                    }
                )
        return sorted(
            failures,
            key=lambda value: (str(value["occurred_at"]), str(value["_sort_id"])),
            reverse=True,
        )[:limit]

    def list_failures(
        self, *, cursor: str | None = None, limit: int = DEFAULT_PAGE_LIMIT
    ) -> DataCenterPage:
        fingerprint = _query_fingerprint("failures", {})
        parsed = _parse_cursor(
            cursor,
            "failures",
            query_fingerprint=fingerprint,
            stable_namespaces=frozenset({"data_batch", "data_version", "data_quality", "job"}),
        )
        rows = self._failures(limit=limit + 1, before=(parsed[0], parsed[1]) if parsed else None)
        visible = rows[:limit]
        next_cursor = None
        if len(rows) > limit and visible:
            last = visible[-1]
            next_cursor = _cursor(
                "failures",
                fingerprint,
                str(last["occurred_at"]),
                str(last["_sort_id"]),
            )
        return DataCenterPage(
            tuple(
                {key: value for key, value in row.items() if key != "_sort_id"} for row in visible
            ),
            next_cursor,
        )


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "MAX_PREVIEW_LIMIT",
    "DataCenterPage",
    "DataCenterQueryService",
    "DataCenterReadIntegrityError",
    "InvalidDataCenterQueryError",
    "validate_cursor",
]
