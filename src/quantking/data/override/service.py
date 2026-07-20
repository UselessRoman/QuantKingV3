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

from dataclasses import dataclass

from quantking.common.errors import OverrideNotApplicableError
from quantking.common.ids import EntityId, new_entity_id
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.persistence.sqlite import DataSqliteUnitOfWorkFactory
from quantking.data.models import (
    DataSource,
    SourceOverride,
    SourceOverrideStatus,
)
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

    def __init__(self, uow_factory: DataSqliteUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

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
    ) -> SourceOverride:
        """Apply an APPROVED override to a new direct-child DatasetVersion.

        The full workflow (create a new direct-child version, re-run the
        quality gate, atomic-publish, mark APPLIED in the same audited
        transaction) requires the CanonicalWarehouse, quality engine and
        publish coordinator and is deferred to a later stage. This method
        enforces the failure paths: non-existent, PENDING, REJECTED or
        already-APPLIED overrides must raise before any state change.
        """
        with self._uow_factory() as uow:
            current = uow.data.get_override(str(override_id))
            if current.status is not SourceOverrideStatus.APPROVED:
                raise OverrideNotApplicableError(
                    "only APPROVED overrides can be applied",
                    context={"current": current.status.value},
                )
            raise NotImplementedError(
                "apply_override workflow requires CanonicalWarehouse, "
                "QualityEngine and PublishCoordinator; deferred to a later stage"
            )


__all__ = [
    "CreateOverrideRequest",
    "SourceOverrideService",
]
