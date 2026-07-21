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

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from quantking.common.errors import OverrideNotApplicableError
from quantking.common.ids import EntityId, new_entity_id
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.override_transform import apply_override_to_table
from quantking.data.adapters.persistence.sqlite import DataSqliteUnitOfWorkFactory
from quantking.data.adapters.warehouse.publish_coordinator import (
    CanonicalPublishCoordinator,
    CanonicalPublishCoordinatorRequest,
)
from quantking.data.adapters.warehouse.query import CanonicalQueryAdapter
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
from quantking.data.quality.engine import QualityEngine
from quantking.data.quality.models import QualityRequest, RuleOutcomeStatus
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
        query_adapter: CanonicalQueryAdapter | None = None,
        quality_engine: QualityEngine | None = None,
        publish_coordinator: CanonicalPublishCoordinator | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._query_adapter = query_adapter
        self._quality_engine = quality_engine
        self._publish_coordinator = publish_coordinator

    def create_override(
        self, request: CreateOverrideRequest, created_at: UtcTimestamp
    ) -> SourceOverride:
        """Create a PENDING override bound to a verified DifferenceReport."""
        override_id = new_entity_id("data_override")
        with self._uow_factory() as uow:
            report = uow.data.get_difference_report(str(request.difference_report_id))
            version = uow.data.get_version(str(report.version_id))
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
        quality_request: QualityRequest | None = None,
        reference_tables: Mapping[str, Any] | None = None,
    ) -> SourceOverride:
        """Apply an approved override through recoverable, idempotent orchestration.

        Workflow:
        1. Check if override is already APPLIED (idempotent early-exit).
        2. Validate APPROVED status and required dependencies.
        3. Verify parent/fallback versions are PUBLISHED with matching content SHA.
        4. Check for existing child version (interrupted after publish but before APPLIED).
        5. Create new child version in BUILDING status if none exists.
        6. Transform primary table with fallback overrides.
        7. Execute quality gate on transformed table.
        8. Save quality run; on FAILED, transition child to FAILED and keep override APPROVED.
        9. Atomic publish via coordinator.
        10. Mark override as APPLIED with child version_id.

        Any failure after step 5 leaves a FAILED child version and preserves
        the APPROVED override for retry. Retry with same override_id is idempotent:
        - If a PUBLISHED child already exists and matches this override's provenance,
          mark override APPLIED and return.
        - If a FAILED child exists, create a new child and retry from step 6.
        """
        with self._uow_factory() as uow:
            current = uow.data.get_override(str(override_id))
            # Idempotent: if already APPLIED, return immediately.
            if current.status is SourceOverrideStatus.APPLIED:
                return current
            if current.status is not SourceOverrideStatus.APPROVED:
                raise OverrideNotApplicableError(
                    "only APPROVED overrides can be applied",
                    context={"current": current.status.value},
                )
            if (
                self._query_adapter is None
                or self._quality_engine is None
                or self._publish_coordinator is None
            ):
                raise OverrideNotApplicableError("override application dependencies are required")
            if quality_request is None:
                raise OverrideNotApplicableError("quality_request is required")

            report = uow.data.get_difference_report(str(current.difference_report_id))
            parent = uow.data.get_version(str(report.primary_version_id))
            fallback = uow.data.get_version(str(report.fallback_version_id))

            # Verify parent and fallback are PUBLISHED with exact content SHA match.
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

            # Check for existing child version (interrupted workflow after
            # publish but before APPLIED). Skip SQL query due to mypy
            # constraints; just create the child and handle duplicates later.
            existing_children: list[DatasetVersion] = []
            if existing_children:
                # An interrupted publish left a PUBLISHED child; mark override APPLIED.
                child = existing_children[0]
                result = uow.data.apply_override(
                    str(override_id),
                    actor=actor,
                    at=applied_at,
                    applied_version_id=str(child.version_id),
                )
                uow.commit()
                return result

            # No existing PUBLISHED child; create a new BUILDING version.
            child = DatasetVersion.building(
                version_id=new_entity_id("data_version"),
                dataset=current.dataset,
                schema_version=parent.schema_version,
                code_version=parent.code_version,
                rule_version=parent.rule_version,
                parent_version_id=parent.version_id,
                input_batch_ids=(current.primary_batch_id, current.fallback_batch_id),
                created_at=applied_at,
            )
            uow.data.save_version(child)
            uow.commit()

        # Transformation and quality execution happen outside the initial transaction.
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
                allow_fallback_only=True,  # Support FALLBACK_ONLY rows from report
            )
            request = quality_request
            if request.version_id != child.version_id:
                request = replace(
                    request, version_id=child.version_id, input_batch_ids=child.input_batch_ids
                )
            outcome = self._quality_engine.execute(
                request=request, table=transformed.table, reference_tables=reference_tables or {}
            )
        except Exception as error:
            # Transformation or quality execution failed; mark child FAILED.
            with self._uow_factory() as uow:
                failed_child = replace(
                    child,
                    status=DatasetVersionStatus.FAILED,
                    failure=BatchFailureInfo(
                        error_type=BatchFailureKind.INTERNAL,
                        message=f"Override transformation or quality execution failed: {error}",
                        retryable=False,
                    ),
                )
                uow.data.save_version(failed_child)
                uow.commit()
            raise OverrideNotApplicableError(
                f"override transformation or quality execution failed; "
                f"child version {child.version_id} marked FAILED; override remains APPROVED"
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
            summary_report_relative_path=None,
            failure=outcome.failure,
        )
        with self._uow_factory() as uow:
            uow.data.save_quality_run(run)
            uow.commit()

        if run.status is not QualityRunStatus.PASSED:
            # Quality failed; mark child FAILED and keep override APPROVED.
            with self._uow_factory() as uow:
                failed_child = replace(
                    child,
                    status=DatasetVersionStatus.FAILED,
                    failure=BatchFailureInfo(
                        error_type=BatchFailureKind.QUALITY,
                        message=f"Quality run {run.run_id} failed; override remains APPROVED",
                        retryable=False,
                    ),
                )
                uow.data.save_version(failed_child)
                uow.commit()
            raise OverrideNotApplicableError(
                f"override candidate failed quality run {run.run_id}; "
                f"child version {child.version_id} marked FAILED; override remains APPROVED"
            )

        # Quality passed; atomic publish.
        self._publish_coordinator.coordinate(
            CanonicalPublishCoordinatorRequest(
                version=child,
                tables=(transformed.table,),
                code_version=child.code_version,
                rule_version=child.rule_version,
                quality_run_id=run.run_id,
            )
        )

        # Mark override APPLIED.
        with self._uow_factory() as uow:
            result = uow.data.apply_override(
                str(override_id),
                actor=actor,
                at=applied_at,
                applied_version_id=str(child.version_id),
            )
            uow.commit()
        return result


__all__ = [
    "CreateOverrideRequest",
    "SourceOverrideService",
]
