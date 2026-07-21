"""Source-override application service (S1-04C).

The service orchestrates the explicit fallback-source override workflow on
top of the audited SQLite repository. It validates creation requests, binds
them to a verified :class:`DifferenceReport`, and transitions overrides
through PENDING → APPROVED/REJECTED → APPLIED with full audit trails.

The apply workflow (new direct-child DatasetVersion, quality gate re-run,
atomic publish, APPLIED marking) requires the CanonicalWarehouse, quality
engine and publish coordinator and is deferred to a later stage; the
failure paths (non-existent, PENDING, REJECTED or already-APPLIED
overrides) are enforced here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import cast

from quantking.common.errors import OverrideNotApplicableError
from quantking.common.ids import EntityId, new_entity_id
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.override_transform import apply_override_to_table
from quantking.data.adapters.persistence.sqlite import DataSqliteUnitOfWorkFactory
from quantking.data.adapters.warehouse.manifest import compute_sha256
from quantking.data.adapters.warehouse.publish_coordinator import (
    CanonicalPublishCoordinator,
    CanonicalPublishCoordinatorRequest,
)
from quantking.data.adapters.warehouse.query import CanonicalQueryAdapter
from quantking.data.comparison.verifier import (
    DifferenceReportVerifier,
    VerifiedDifferenceReport,
)
from quantking.data.models import (
    BatchFailureInfo,
    BatchFailureKind,
    DatasetVersion,
    DatasetVersionStatus,
    DataSource,
    QualityResult,
    QualityRun,
    QualityRunStatus,
    SourceOverride,
    SourceOverrideStatus,
)
from quantking.data.quality.artifacts import QualityArtifactWriter
from quantking.data.quality.engine import QualityEngine
from quantking.data.quality.models import QualityRequest, RuleOutcomeStatus
from quantking.data.quality.references import PublishedReferenceLoader
from quantking.data.quality.runner import QualityRunner
from quantking.data.schemas import DatasetSchemaDescriptor

# ---------------------------------------------------------------------------
# Canonical field registry (computed once at module load)
# ---------------------------------------------------------------------------

_ALL_FIELD_NAMES: frozenset[str] = frozenset().union(
    *(set(descriptor.field_names) for descriptor in DatasetSchemaDescriptor.all())
)
_ALL_PRIMARY_KEY_FIELDS: frozenset[str] = frozenset().union(
    *(set(descriptor.primary_key) for descriptor in DatasetSchemaDescriptor.all())
)


def _text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    return value


def _entity_id(value: EntityId, *, field_name: str, namespace: str) -> EntityId:
    if not isinstance(value, EntityId) or value.namespace != namespace:
        raise ValueError(f"{field_name} must use the {namespace} namespace")
    return value


def _tuple_text_unique(value: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} element must be non-empty text")
    if len(set(value)) != len(value):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(value)


def _tuple_trading_dates_unique(
    value: tuple[TradingDate, ...], *, field_name: str
) -> tuple[TradingDate, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    for item in value:
        if not isinstance(item, TradingDate):
            raise TypeError(f"{field_name} element must be a TradingDate")
    if len(set(value)) != len(value):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(sorted(value, key=lambda d: d.value))


def _validate_fields(value: tuple[str, ...]) -> tuple[str, ...]:
    normalized = _tuple_text_unique(value, field_name="fields")
    for field_name in normalized:
        if field_name not in _ALL_FIELD_NAMES:
            raise ValueError(f"override field {field_name!r} is not a known canonical field")
        if field_name in _ALL_PRIMARY_KEY_FIELDS:
            raise ValueError(
                f"override field {field_name!r} is a primary key and cannot be replaced"
            )
    return normalized


# ---------------------------------------------------------------------------
# Creation request
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreateOverrideRequest:
    """Validated request to create a PENDING :class:`SourceOverride`.

    The request validates structural invariants (non-empty reason, non-empty
    and duplicate-free scope, known non-primary-key fields, distinct
    sources) without touching the database. The service binds the request
    to a verified :class:`DifferenceReport` and persists it inside an
    audited transaction.
    """

    difference_report_id: EntityId
    primary_batch_id: EntityId
    fallback_batch_id: EntityId
    primary_source: str
    fallback_source: str
    scope_codes: tuple[str, ...]
    scope_dates: tuple[TradingDate, ...]
    fields: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        _entity_id(
            self.difference_report_id,
            field_name="difference_report_id",
            namespace="data_diff",
        )
        _entity_id(
            self.primary_batch_id,
            field_name="primary_batch_id",
            namespace="data_batch",
        )
        _entity_id(
            self.fallback_batch_id,
            field_name="fallback_batch_id",
            namespace="data_batch",
        )
        if self.primary_batch_id == self.fallback_batch_id:
            raise ValueError("primary_batch_id and fallback_batch_id must differ")
        primary = DataSource(self.primary_source)
        fallback = DataSource(self.fallback_source)
        if primary == fallback:
            raise ValueError("primary_source and fallback_source must differ")
        object.__setattr__(self, "primary_source", primary.value)
        object.__setattr__(self, "fallback_source", fallback.value)
        object.__setattr__(
            self, "scope_codes", _tuple_text_unique(self.scope_codes, field_name="scope_codes")
        )
        object.__setattr__(
            self,
            "scope_dates",
            _tuple_trading_dates_unique(self.scope_dates, field_name="scope_dates"),
        )
        object.__setattr__(self, "fields", _validate_fields(self.fields))
        _text(self.reason, field_name="reason")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SourceOverrideService:
    """Application service for the explicit source-override workflow.

    The service is the only entry point that creates, approves, rejects or
    applies a :class:`SourceOverride`. It enforces the difference-report
    binding, the audited transition table and the immutable-parent rule
    on top of the SQLite repository.
    """

    def __init__(
        self,
        uow_factory: DataSqliteUnitOfWorkFactory,
        *,
        query_adapter: CanonicalQueryAdapter,
        quality_engine: QualityEngine,
        reference_loader: PublishedReferenceLoader,
        quality_artifact_writer: QualityArtifactWriter,
        publish_coordinator: CanonicalPublishCoordinator,
        difference_report_verifier: DifferenceReportVerifier,
    ) -> None:
        if not isinstance(uow_factory, DataSqliteUnitOfWorkFactory):
            raise TypeError("uow_factory must be a DataSqliteUnitOfWorkFactory")
        if not isinstance(query_adapter, CanonicalQueryAdapter):
            raise TypeError("query_adapter must be a CanonicalQueryAdapter")
        if not isinstance(quality_engine, QualityEngine):
            raise TypeError("quality_engine must be a QualityEngine")
        if not isinstance(reference_loader, PublishedReferenceLoader):
            raise TypeError("reference_loader must be a PublishedReferenceLoader")
        if not isinstance(quality_artifact_writer, QualityArtifactWriter):
            raise TypeError("quality_artifact_writer must be a QualityArtifactWriter")
        if not isinstance(publish_coordinator, CanonicalPublishCoordinator):
            raise TypeError("publish_coordinator must be a CanonicalPublishCoordinator")
        if not isinstance(difference_report_verifier, DifferenceReportVerifier):
            raise TypeError("difference_report_verifier must be a DifferenceReportVerifier")
        self._uow_factory = uow_factory
        self._query_adapter = query_adapter
        self._quality_runner = QualityRunner(
            engine=quality_engine, reference_loader=reference_loader
        )
        self._quality_artifact_writer = quality_artifact_writer
        self._publish_coordinator = publish_coordinator
        self._difference_report_verifier = difference_report_verifier

    def create_override(
        self, request: CreateOverrideRequest, created_at: UtcTimestamp
    ) -> SourceOverride:
        """Create a PENDING override bound to a verified DifferenceReport."""
        verified = self._difference_report_verifier.verify(report_id=request.difference_report_id)
        override_id = new_entity_id("data_override")
        with self._uow_factory() as uow:
            report = uow.data.get_difference_report(str(request.difference_report_id))
            version = uow.data.get_version(str(report.version_id))
            self._require_verified_report_matches_database(
                verified=verified, report=report, dataset=version.dataset.value
            )
            if not verified.scope_matches(
                codes=frozenset(request.scope_codes),
                dates=frozenset(str(item) for item in request.scope_dates),
                fields=frozenset(request.fields),
            ):
                raise OverrideNotApplicableError(
                    "approved scope must be an exact subset of verified difference units"
                )
            override = SourceOverride.pending(
                override_id=override_id,
                dataset=version.dataset,
                difference_report_id=request.difference_report_id,
                primary_batch_id=request.primary_batch_id,
                fallback_batch_id=request.fallback_batch_id,
                primary_source=DataSource(request.primary_source),
                fallback_source=DataSource(request.fallback_source),
                scope_codes=request.scope_codes,
                scope_dates=request.scope_dates,
                fields=request.fields,
                reason=request.reason,
                created_at=created_at,
            )
            uow.data.save_override(override)
            uow.commit()
        return override

    def approve_override(
        self, override_id: EntityId, *, actor: str, at: UtcTimestamp
    ) -> SourceOverride:
        """Transition a PENDING override to APPROVED with an audit record."""
        with self._uow_factory() as uow:
            result = uow.data.approve_override(str(override_id), actor=actor, at=at)
            uow.commit()
        return result

    def reject_override(
        self, override_id: EntityId, *, actor: str, at: UtcTimestamp
    ) -> SourceOverride:
        """Transition a PENDING override to REJECTED with an audit record."""
        with self._uow_factory() as uow:
            result = uow.data.reject_override(str(override_id), actor=actor, at=at)
            uow.commit()
        return result

    def apply_override(
        self,
        override_id: EntityId,
        applied_at: UtcTimestamp,
        *,
        actor: str = "system",
        quality_request: QualityRequest,
    ) -> SourceOverride:
        """Apply one APPROVED override using its persisted recoverable child."""
        if not isinstance(quality_request, QualityRequest):
            raise TypeError("quality_request must be a QualityRequest")
        with self._uow_factory() as uow:
            current = uow.data.get_override(str(override_id))
            if current.status is SourceOverrideStatus.APPLIED:
                return current
            if current.status is not SourceOverrideStatus.APPROVED:
                raise OverrideNotApplicableError(
                    "only APPROVED overrides can be applied",
                    context={"current": current.status.value},
                )
            report = uow.data.get_difference_report(str(current.difference_report_id))
            parent = uow.data.get_version(str(report.primary_version_id))
            fallback = uow.data.get_version(str(report.fallback_version_id))
            if parent.status is not DatasetVersionStatus.PUBLISHED:
                raise OverrideNotApplicableError(
                    f"parent version {parent.version_id} is not PUBLISHED; "
                    f"current status: {parent.status.value}"
                )
            if fallback.status is not DatasetVersionStatus.PUBLISHED:
                raise OverrideNotApplicableError(
                    f"fallback version {fallback.version_id} is not PUBLISHED; "
                    f"current status: {fallback.status.value}"
                )
            if report.version_id != report.primary_version_id:
                raise OverrideNotApplicableError(
                    "difference report version_id must equal primary_version_id"
                )
            if parent.content_sha256 != report.primary_content_sha256:
                raise OverrideNotApplicableError(
                    f"parent version content SHA mismatch: "
                    f"version={parent.content_sha256}, report={report.primary_content_sha256}"
                )
            if fallback.content_sha256 != report.fallback_content_sha256:
                raise OverrideNotApplicableError(
                    f"fallback version content SHA mismatch: "
                    f"version={fallback.content_sha256}, report={report.fallback_content_sha256}"
                )

        verified_report = self._difference_report_verifier.verify(
            report_id=current.difference_report_id
        )
        self._require_verified_report_matches_database(
            verified=verified_report, report=report, dataset=current.dataset.value
        )
        if not verified_report.scope_matches(
            codes=frozenset(current.scope_codes),
            dates=frozenset(str(item) for item in current.scope_dates),
            fields=frozenset(current.fields),
        ):
            raise OverrideNotApplicableError(
                "persisted approval scope is not a verified difference-unit subset"
            )
        self._validate_quality_request(quality_request, child_template=parent)
        input_fingerprint = self._application_fingerprint(
            current=current, report=verified_report, quality_request=quality_request
        )
        proposed_child = DatasetVersion.building(
            version_id=new_entity_id("data_version"),
            dataset=current.dataset,
            schema_version=parent.schema_version,
            code_version=parent.code_version,
            rule_version=parent.rule_version,
            parent_version_id=parent.version_id,
            input_batch_ids=(current.primary_batch_id, current.fallback_batch_id),
            created_at=applied_at,
        )
        with self._uow_factory() as uow:
            attempt = uow.data.claim_override_application_child(
                override_id=str(override_id),
                child=proposed_child,
                input_fingerprint=input_fingerprint,
                actor=actor,
                at=applied_at,
            )
            uow.commit()
            child = uow.data.get_version(str(attempt.child_version_id))

        provenance = self._build_provenance(override=current, report=verified_report)
        if child.status is DatasetVersionStatus.PUBLISHED:
            return self._bind_and_apply_published_child(
                override=current,
                child=child,
                attempt_id=attempt.attempt_id,
                provenance=provenance,
                actor=actor,
                at=applied_at,
            )
        if child.version_id != proposed_child.version_id:
            raise OverrideNotApplicableError(
                "override application is already claimed by another worker"
            )
        if child.status is not DatasetVersionStatus.BUILDING:
            raise OverrideNotApplicableError(
                "active override application child is neither BUILDING nor PUBLISHED"
            )

        try:
            primary_table = self._query_adapter.query_by_id(
                version_id=str(report.primary_version_id),
                dataset=current.dataset.value,
                fields=None,
            )
            fallback_table = self._query_adapter.query_by_id(
                version_id=str(report.fallback_version_id),
                dataset=current.dataset.value,
                fields=None,
            )
            transformed = apply_override_to_table(
                dataset=current.dataset,
                primary_table=primary_table,
                fallback_table=fallback_table,
                scope_codes=frozenset(current.scope_codes),
                scope_dates=frozenset(item.value for item in current.scope_dates),
                fields=frozenset(current.fields),
                allow_fallback_only=False,
            )
            contributions = cast(list[dict[str, object]], provenance["fallback_contributions"])
            expected_changed = sum(len(cast(list[str], item["fields"])) for item in contributions)
            if transformed.changed_cells != expected_changed:
                raise OverrideNotApplicableError(
                    "actual transformed cells do not match verified fallback contributions"
                )
            request = replace(
                quality_request,
                version_id=child.version_id,
                input_batch_ids=child.input_batch_ids,
            )
            outcome = self._quality_runner.run(request=request, table=transformed.table)
        except Exception as error:
            self._fail_application(
                override_id=override_id,
                child=child,
                attempt_id=attempt.attempt_id,
                stage="transform_or_quality",
                error=error,
                at=applied_at,
                kind=BatchFailureKind.INTERNAL,
            )
            raise OverrideNotApplicableError(
                "override transformation or quality execution failed; child marked FAILED"
            ) from error

        results = tuple(
            QualityResult(
                rule_code=item.rule_code,
                passed=item.status is RuleOutcomeStatus.PASSED,
                affected_count=item.affected_count,
                summary=item.summary,
                report_relative_path=item.row_evidence_relative_path,
            )
            for item in outcome.results
            if item.status is not RuleOutcomeStatus.SKIPPED
        )
        artifact_directory = self._quality_artifact_writer.write(
            outcome=outcome, row_evidence_tables={}
        )
        run = QualityRun(
            run_id=outcome.run_id,
            version_id=child.version_id,
            status=outcome.status,
            rule_version=outcome.rule_version,
            schema_version=outcome.schema_version,
            content_sha256=outcome.content_sha256,
            started_at=outcome.started_at,
            ended_at=outcome.ended_at,
            results=results,
            summary_report_relative_path=artifact_directory.relative_to(
                self._quality_artifact_writer.artifacts_root
            ).as_posix(),
            failure=outcome.failure,
        )
        with self._uow_factory() as uow:
            uow.data.save_quality_run(run)
            uow.commit()

        if run.status is not QualityRunStatus.PASSED:
            self._fail_application(
                override_id=override_id,
                child=child,
                attempt_id=attempt.attempt_id,
                stage="quality",
                error=OverrideNotApplicableError(f"quality run {run.run_id} failed"),
                at=applied_at,
                kind=BatchFailureKind.QUALITY,
            )
            raise OverrideNotApplicableError(
                f"override candidate failed quality run {run.run_id}; child marked FAILED"
            )

        published = self._publish_coordinator.coordinate(
            CanonicalPublishCoordinatorRequest(
                version=child,
                tables=(transformed.table,),
                code_version=child.code_version,
                rule_version=child.rule_version,
                quality_run_id=run.run_id,
                source_override=provenance,
            )
        ).version
        return self._bind_and_apply_published_child(
            override=current,
            child=published,
            attempt_id=attempt.attempt_id,
            provenance=provenance,
            actor=actor,
            at=applied_at,
        )

    @staticmethod
    def _require_verified_report_matches_database(
        *, verified: VerifiedDifferenceReport, report: object, dataset: str
    ) -> None:
        fields = (
            "report_id",
            "primary_source",
            "fallback_source",
            "primary_batch_id",
            "fallback_batch_id",
            "primary_version_id",
            "fallback_version_id",
            "primary_content_sha256",
            "fallback_content_sha256",
            "primary_quality_run_id",
            "fallback_quality_run_id",
            "report_sha256",
        )
        mismatches = [
            name for name in fields if str(getattr(verified, name)) != str(getattr(report, name))
        ]
        if verified.dataset != dataset:
            mismatches.append("dataset")
        if mismatches:
            raise OverrideNotApplicableError(
                "verified artifact does not match persisted DifferenceReport",
                context={"mismatches": ",".join(mismatches)},
            )

    @staticmethod
    def _validate_quality_request(
        quality_request: QualityRequest, *, child_template: DatasetVersion
    ) -> None:
        if (
            quality_request.dataset is not child_template.dataset
            or quality_request.schema_version != child_template.schema_version
            or quality_request.code_version != child_template.code_version
            or quality_request.rule_version != child_template.rule_version
        ):
            raise OverrideNotApplicableError(
                "quality request does not match the child version contract"
            )

    @staticmethod
    def _application_fingerprint(
        *,
        current: SourceOverride,
        report: VerifiedDifferenceReport,
        quality_request: QualityRequest,
    ) -> str:
        quality = quality_request.to_deterministic_dict()
        quality["version_id"] = "DERIVED_CHILD"
        quality["content_sha256"] = "DERIVED_CONTENT"
        quality["input_batch_ids"] = [
            str(current.primary_batch_id),
            str(current.fallback_batch_id),
        ]
        payload = {
            "override_id": str(current.override_id),
            "report_sha256": report.report_sha256,
            "quality": quality,
        }
        return compute_sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
        )

    @staticmethod
    def _build_provenance(
        *, override: SourceOverride, report: VerifiedDifferenceReport
    ) -> dict[str, object]:
        selected: dict[tuple[tuple[str, str], ...], set[str]] = {}
        codes = set(override.scope_codes)
        dates = {str(item) for item in override.scope_dates}
        fields = set(override.fields)
        for unit in report.units:
            date_value = unit.primary_key.get(
                "trade_date", unit.primary_key.get("effective_from", "")
            )
            if (
                unit.classification == "BOTH"
                and unit.primary_key.get("code") in codes
                and date_value in dates
                and unit.field in fields
            ):
                key = tuple(sorted(unit.primary_key.items()))
                selected.setdefault(key, set()).add(unit.field)
        contributions = [
            {
                "primary_key": dict(key),
                "kind": "FIELD_OVERRIDE",
                "fields": sorted(unit_fields),
            }
            for key, unit_fields in sorted(selected.items())
        ]
        contribution_sha = compute_sha256(
            json.dumps(
                contributions, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        )
        return {
            "override_id": str(override.override_id),
            "difference_report_id": str(report.report_id),
            "report_sha256": report.report_sha256,
            "primary": {
                "source": report.primary_source,
                "batch_id": str(report.primary_batch_id),
                "version_id": str(report.primary_version_id),
                "content_sha256": report.primary_content_sha256,
            },
            "fallback": {
                "source": report.fallback_source,
                "batch_id": str(report.fallback_batch_id),
                "version_id": str(report.fallback_version_id),
                "content_sha256": report.fallback_content_sha256,
            },
            "fallback_contributions": contributions,
            "contribution_scope_sha256": contribution_sha,
        }

    def _bind_and_apply_published_child(
        self,
        *,
        override: SourceOverride,
        child: DatasetVersion,
        attempt_id: EntityId,
        provenance: dict[str, object],
        actor: str,
        at: UtcTimestamp,
    ) -> SourceOverride:
        verified = self._query_adapter.verify_manifest_by_id(
            version_id=str(child.version_id), dataset=child.dataset.value
        )
        manifest_provenance = verified.manifest.get("source_override")
        if manifest_provenance != provenance or not isinstance(manifest_provenance, dict):
            raise OverrideNotApplicableError(
                "published manifest provenance does not match the verified application"
            )
        report_sha = str(provenance["report_sha256"])
        contribution_sha = str(provenance["contribution_scope_sha256"])
        with self._uow_factory() as uow:
            binding = uow.data.get_override_application_binding(str(override.override_id))
            if binding.status == "CLAIMED":
                uow.data.bind_published_override_child(
                    override_id=str(override.override_id),
                    child_version_id=str(child.version_id),
                    report_sha256=report_sha,
                    contribution_scope_sha256=contribution_sha,
                    manifest_source_override=manifest_provenance,
                    at=at,
                )
                uow.commit()
            else:
                uow.rollback()
        with self._uow_factory() as uow:
            binding = uow.data.get_override_application_binding(str(override.override_id))
            if (
                binding.child_version_id != child.version_id
                or binding.status != "PUBLISHED"
                or binding.report_sha256 != report_sha
                or binding.contribution_scope_sha256 != contribution_sha
            ):
                raise OverrideNotApplicableError(
                    "application binding does not match the re-read published manifest"
                )
            result = uow.data.apply_override(
                str(override.override_id),
                actor=actor,
                at=at,
                applied_version_id=str(child.version_id),
            )
            uow.commit()
        return result

    def _fail_application(
        self,
        *,
        override_id: EntityId,
        child: DatasetVersion,
        attempt_id: EntityId,
        stage: str,
        error: Exception,
        at: UtcTimestamp,
        kind: BatchFailureKind,
    ) -> None:
        failed = replace(
            child,
            status=DatasetVersionStatus.FAILED,
            failure=BatchFailureInfo(
                error_type=kind,
                message=f"override {stage} failed: {error}",
                retryable=False,
            ),
        )
        with self._uow_factory() as uow:
            uow.data.save_version(failed)
            uow.data.record_override_application_failure(
                override_id=str(override_id),
                attempt_id=str(attempt_id),
                child_version_id=str(child.version_id),
                failure_stage=stage,
                message=str(error),
                at=at,
            )
            uow.commit()


__all__ = [
    "CreateOverrideRequest",
    "SourceOverrideService",
]
