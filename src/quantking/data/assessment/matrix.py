"""Default source matrix for fallback-source candidate assessment (S1-04B).

The matrix declares the production role and current assessment status of
every known data source. It is deliberately conservative: the three
fallback sources (BaoStock, AKShare, Tushare) remain
``BLOCKED_EXTERNAL_VALIDATION`` until a real external validation run is
authorized and executed. No entry may declare ``auto_switch=True``;
fallback selection must always be an explicit :class:`SourceOverride`.
"""

from __future__ import annotations

from dataclasses import dataclass

from quantking.data.assessment.models import (
    _BLOCKED_STATUSES,
    AssessmentStatus,
    SourceRole,
    _text,
)


@dataclass(frozen=True, slots=True)
class SourceMatrixEntry:
    """One row in the source matrix.

    ``auto_switch`` is always ``False``: the production system never
    silently switches to a fallback source. Selection of a fallback source
    requires an explicit, approved :class:`SourceOverride` bound to a
    :class:`DifferenceReport`.
    """

    source: str
    role: SourceRole
    status: AssessmentStatus
    blocking_reason: str | None
    auto_switch: bool

    def __post_init__(self) -> None:
        _text(self.source, field_name="source")
        if not isinstance(self.role, SourceRole):
            raise TypeError("role must be a SourceRole")
        if not isinstance(self.status, AssessmentStatus):
            raise TypeError("status must be an AssessmentStatus")
        if self.blocking_reason is not None:
            _text(self.blocking_reason, field_name="blocking_reason")
        if not isinstance(self.auto_switch, bool):
            raise TypeError("auto_switch must be a bool")
        if self.auto_switch:
            raise ValueError("auto_switch is forbidden; fallback selection must be explicit")
        if self.status in _BLOCKED_STATUSES and not self.blocking_reason:
            raise ValueError(f"{self.status.value} status requires a blocking reason")
        if self.status is AssessmentStatus.PRODUCTION_READY and self.blocking_reason is not None:
            raise ValueError("PRODUCTION_READY status cannot carry a blocking reason")


@dataclass(frozen=True, slots=True)
class SourceMatrix:
    """Immutable collection of :class:`SourceMatrixEntry` rows.

    Entries must be unique by ``source``. The matrix is the authoritative
    declaration of which sources may participate in production data flows
    and in what role.
    """

    entries: tuple[SourceMatrixEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        if not self.entries:
            raise ValueError("source matrix must contain at least one entry")
        for entry in self.entries:
            if not isinstance(entry, SourceMatrixEntry):
                raise TypeError("entries must contain SourceMatrixEntry values")
        sources = [entry.source for entry in self.entries]
        if len(set(sources)) != len(sources):
            raise ValueError("source matrix entries must be unique by source")


def default_source_matrix() -> SourceMatrix:
    """Return the canonical default source matrix.

    XTQuant is the designated ``PRIMARY`` production source. The three
    fallback sources (BaoStock, AKShare, Tushare) remain
    ``BLOCKED_EXTERNAL_VALIDATION`` until a real external validation run
    is authorized and executed; none of them declare ``auto_switch``.
    """
    return SourceMatrix(
        entries=(
            SourceMatrixEntry(
                source="XTQUANT",
                role=SourceRole.PRIMARY,
                status=AssessmentStatus.PRODUCTION_READY,
                blocking_reason=None,
                auto_switch=False,
            ),
            SourceMatrixEntry(
                source="BAOSTOCK",
                role=SourceRole.FALLBACK,
                status=AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION,
                blocking_reason=(
                    "BaoStock requires authorized external validation before production use"
                ),
                auto_switch=False,
            ),
            SourceMatrixEntry(
                source="AKSHARE",
                role=SourceRole.FALLBACK,
                status=AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION,
                blocking_reason=(
                    "AKShare requires authorized external validation before production use"
                ),
                auto_switch=False,
            ),
            SourceMatrixEntry(
                source="TUSHARE",
                role=SourceRole.FALLBACK,
                status=AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION,
                blocking_reason=(
                    "Tushare requires authorized external validation before production use"
                ),
                auto_switch=False,
            ),
        )
    )


__all__ = [
    "SourceMatrix",
    "SourceMatrixEntry",
    "default_source_matrix",
]
