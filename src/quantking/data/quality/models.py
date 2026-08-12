"""Framework-free quality request, rule, and outcome contracts (S1-04A).

These dataclasses are deliberately independent of SQLAlchemy, DuckDB,
PyArrow, vendor SDKs and FastAPI. They are the only contracts the
quality rule engine and artifact writer consume. The PyArrow table that
the executor validates is passed in at call time and never stored on
these objects.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType

from quantking.common.ids import EntityId
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.models import (
    BatchFailureInfo,
    DatasetKind,
    EmptyResultInfo,
    QualityRunStatus,
)

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MANAGED_RELATIVE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./=-]*\Z")
S108_DAILY_RULE_VERSION = "quality-rules.daily-bar-first-release.v1"


def _sha256(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError(f"{field_name} must be a 64-character lowercase hex SHA-256")
    return value


def _managed_relative(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or len(candidate.drive) != 0:
        raise ValueError(f"{field_name} must be a relative path, not absolute")
    if ".." in candidate.parts:
        raise ValueError(f"{field_name} must not contain parent traversal")
    if not _MANAGED_RELATIVE.fullmatch(value):
        raise ValueError(f"{field_name} must be a managed relative identifier")
    return value


def _tuple_text(value: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} element must be non-empty text")
    return tuple(value)


def _tuple_entity_ids(
    value: tuple[EntityId, ...], *, field_name: str, namespace: str
) -> tuple[EntityId, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    for item in value:
        if not isinstance(item, EntityId) or item.namespace != namespace:
            raise ValueError(f"{field_name} must contain {namespace} EntityId values")
    if len(set(value)) != len(value):
        raise ValueError(f"{field_name} must be unique")
    return value


def _tuple_trading_dates(
    value: tuple[TradingDate, ...], *, field_name: str
) -> tuple[TradingDate, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    for item in value:
        if not isinstance(item, TradingDate):
            raise TypeError(f"{field_name} element must be a TradingDate")
    return tuple(sorted(value, key=lambda d: d.value))


# ---------------------------------------------------------------------------
# Reference data binding
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReferenceDataBinding:
    """Binding to one PUBLISHED reference dataset version used by a rule.

    The binding carries an explicit ``version_id`` and ``content_sha256`` so
    the quality rule engine can refuse to run when the reference data is
    missing, stale or tampered. Rules must never silently fall back to
    "latest" or "today".
    """

    dataset: DatasetKind
    version_id: EntityId
    content_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.dataset, DatasetKind):
            raise TypeError("dataset must be a DatasetKind")
        if not isinstance(self.version_id, EntityId) or self.version_id.namespace != "data_version":
            raise ValueError("version_id must use the data_version namespace")
        object.__setattr__(
            self, "content_sha256", _sha256(self.content_sha256, field_name="content_sha256")
        )


# ---------------------------------------------------------------------------
# Quality request
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QualityRequest:
    """A quality check request bound to one candidate version's content SHA.

    The request never accepts implicit "today", "latest" or default-version
    lookups. Every credential required to reproduce the rule outcome must
    be supplied explicitly: ``version_id``, ``rule_version``,
    ``schema_version``, ``code_version``, ``content_sha256``,
    ``input_batch_ids``, expected securities/exchanges, the inclusive
    coverage window, the required reference data with explicit
    version/content fingerprints, and an optional
    :class:`EmptyResultInfo` for legal zero-row candidates.
    """

    dataset: DatasetKind
    version_id: EntityId
    rule_version: str
    schema_version: str
    code_version: str
    content_sha256: str
    input_batch_ids: tuple[EntityId, ...]
    expected_securities: tuple[str, ...]
    expected_exchanges: tuple[str, ...]
    coverage_start: TradingDate
    coverage_end: TradingDate
    reference_data: Mapping[str, ReferenceDataBinding]
    empty_result: EmptyResultInfo | None

    def __post_init__(self) -> None:
        if not isinstance(self.dataset, DatasetKind):
            raise TypeError("dataset must be a DatasetKind")
        if not isinstance(self.version_id, EntityId) or self.version_id.namespace != "data_version":
            raise ValueError("version_id must use the data_version namespace")
        if not isinstance(self.rule_version, str) or not self.rule_version.strip():
            raise ValueError("rule_version must be non-empty text")
        if not isinstance(self.schema_version, str) or not self.schema_version.strip():
            raise ValueError("schema_version must be non-empty text")
        if not isinstance(self.code_version, str) or not self.code_version.strip():
            raise ValueError("code_version must be non-empty text")
        object.__setattr__(
            self, "content_sha256", _sha256(self.content_sha256, field_name="content_sha256")
        )
        object.__setattr__(
            self,
            "input_batch_ids",
            _tuple_entity_ids(
                self.input_batch_ids, field_name="input_batch_ids", namespace="data_batch"
            ),
        )
        object.__setattr__(
            self,
            "expected_securities",
            _tuple_text(self.expected_securities, field_name="expected_securities"),
        )
        object.__setattr__(
            self,
            "expected_exchanges",
            _tuple_text(self.expected_exchanges, field_name="expected_exchanges"),
        )
        if not isinstance(self.coverage_start, TradingDate):
            raise TypeError("coverage_start must be a TradingDate")
        if not isinstance(self.coverage_end, TradingDate):
            raise TypeError("coverage_end must be a TradingDate")
        if self.coverage_end.value < self.coverage_start.value:
            raise ValueError("coverage_end must not precede coverage_start")
        if not isinstance(self.reference_data, Mapping):
            raise TypeError("reference_data must be a mapping")
        normalized: dict[str, ReferenceDataBinding] = {}
        for key, binding in self.reference_data.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("reference_data key must be non-empty text")
            if not isinstance(binding, ReferenceDataBinding):
                raise TypeError("reference_data value must be a ReferenceDataBinding")
            normalized[key] = binding
        sorted_reference_data = MappingProxyType(dict(sorted(normalized.items())))
        object.__setattr__(self, "reference_data", sorted_reference_data)
        if self.empty_result is not None and not isinstance(self.empty_result, EmptyResultInfo):
            raise TypeError("empty_result must be an EmptyResultInfo or None")
        # Dataset-specific required reference data.
        required = _required_reference_data(self.dataset, self.rule_version)
        missing = required - set(self.reference_data)
        if missing:
            raise ValueError(
                f"dataset {self.dataset.value} requires reference_data keys: {sorted(missing)}"
            )

    def to_deterministic_dict(self) -> dict[str, object]:
        """Render a stable, JSON-serializable view used for run_id derivation."""
        return {
            "dataset": self.dataset.value,
            "version_id": str(self.version_id),
            "rule_version": self.rule_version,
            "schema_version": self.schema_version,
            "code_version": self.code_version,
            "content_sha256": self.content_sha256,
            "input_batch_ids": [str(item) for item in self.input_batch_ids],
            "expected_securities": list(self.expected_securities),
            "expected_exchanges": list(self.expected_exchanges),
            "coverage_start": str(self.coverage_start),
            "coverage_end": str(self.coverage_end),
            "reference_data": {
                key: {
                    "dataset": binding.dataset.value,
                    "version_id": str(binding.version_id),
                    "content_sha256": binding.content_sha256,
                }
                for key, binding in self.reference_data.items()
            },
            "empty_result": (
                None
                if self.empty_result is None
                else {
                    "classification": self.empty_result.classification.value,
                    "reason": self.empty_result.reason,
                }
            ),
        }


def _required_reference_data(dataset: DatasetKind, rule_version: str) -> frozenset[str]:
    """Return the reference data keys required for the dataset's blocking rules."""
    if rule_version == S108_DAILY_RULE_VERSION:
        if dataset is not DatasetKind.DAILY_BAR_UNADJUSTED:
            raise ValueError("S1-08 daily rule version supports daily_bar_unadjusted only")
        return frozenset()
    if dataset is DatasetKind.DAILY_BAR_UNADJUSTED:
        return frozenset({"exchange_calendar"})
    if dataset is DatasetKind.ADJUSTMENT_FACTOR:
        return frozenset({"exchange_calendar"})
    if dataset is DatasetKind.TRADING_STATUS:
        return frozenset({"exchange_calendar"})
    if dataset is DatasetKind.BENCHMARK_INDEX_DAILY:
        return frozenset({"exchange_calendar"})
    return frozenset()


# ---------------------------------------------------------------------------
# Rule outcomes
# ---------------------------------------------------------------------------


class RuleOutcomeStatus(StrEnum):
    """Status of one rule execution. Aligned with QualityRunStatus."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class QualityOutcomeResult:
    """One rule outcome produced by the engine.

    The ``row_evidence_relative_path`` is a managed relative identifier of
    the per-rule row-level evidence file written by the artifact writer.
    ``None`` is allowed when the rule produced no row-level evidence.
    """

    rule_code: str
    status: RuleOutcomeStatus
    affected_count: int
    summary: str
    row_evidence_relative_path: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.rule_code, str) or not self.rule_code.strip():
            raise ValueError("rule_code must be non-empty text")
        if not isinstance(self.status, RuleOutcomeStatus):
            raise TypeError("status must be a RuleOutcomeStatus")
        if (
            not isinstance(self.affected_count, int)
            or isinstance(self.affected_count, bool)
            or self.affected_count < 0
        ):
            raise ValueError("affected_count must be a non-negative integer")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("summary must be non-empty text")
        if self.row_evidence_relative_path is not None:
            object.__setattr__(
                self,
                "row_evidence_relative_path",
                _managed_relative(
                    self.row_evidence_relative_path, field_name="row_evidence_relative_path"
                ),
            )


@dataclass(frozen=True, slots=True)
class QualityRunArtifact:
    """The full outcome of one quality rule run.

    A run is ``PASSED`` only when every applicable blocking rule produced
    a ``PASSED`` :class:`QualityOutcomeResult`. The ``content_sha256``
    binds the run to the exact candidate content the engine validated.
    """

    run_id: EntityId
    version_id: EntityId
    rule_version: str
    schema_version: str
    content_sha256: str
    started_at: UtcTimestamp
    ended_at: UtcTimestamp
    status: QualityRunStatus
    results: tuple[QualityOutcomeResult, ...]
    failure: BatchFailureInfo | None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, EntityId) or self.run_id.namespace != "data_quality":
            raise ValueError("run_id must use the data_quality namespace")
        if not isinstance(self.version_id, EntityId) or self.version_id.namespace != "data_version":
            raise ValueError("version_id must use the data_version namespace")
        if not isinstance(self.rule_version, str) or not self.rule_version.strip():
            raise ValueError("rule_version must be non-empty text")
        if not isinstance(self.schema_version, str) or not self.schema_version.strip():
            raise ValueError("schema_version must be non-empty text")
        object.__setattr__(
            self, "content_sha256", _sha256(self.content_sha256, field_name="content_sha256")
        )
        if not isinstance(self.started_at, UtcTimestamp):
            raise TypeError("started_at must be a UtcTimestamp")
        if not isinstance(self.ended_at, UtcTimestamp):
            raise TypeError("ended_at must be a UtcTimestamp")
        if self.ended_at.value < self.started_at.value:
            raise ValueError("ended_at must not precede started_at")
        if not isinstance(self.status, QualityRunStatus):
            raise TypeError("status must be a QualityRunStatus")
        object.__setattr__(self, "results", tuple(self.results))
        if any(not isinstance(item, QualityOutcomeResult) for item in self.results):
            raise TypeError("results must contain QualityOutcomeResult values")
        if self.failure is not None and not isinstance(self.failure, BatchFailureInfo):
            raise TypeError("failure must be BatchFailureInfo or None")
        if self.status is QualityRunStatus.PASSED:
            if not self.results or any(
                result.status is not RuleOutcomeStatus.PASSED for result in self.results
            ):
                raise ValueError("PASSED artifact requires every result to be PASSED")
            if self.failure is not None:
                raise ValueError("PASSED artifact cannot carry failure")
        elif self.status is QualityRunStatus.FAILED:
            has_failed_result = any(
                result.status is RuleOutcomeStatus.FAILED for result in self.results
            )
            if self.failure is None and not has_failed_result:
                raise ValueError("FAILED artifact requires a failure or a failed result")
        elif self.status is QualityRunStatus.RUNNING:
            raise ValueError("artifact status cannot be RUNNING")
        # SKIPPED is allowed with results for traceability.


__all__: tuple[str, ...] = (
    "S108_DAILY_RULE_VERSION",
    "QualityOutcomeResult",
    "QualityRequest",
    "QualityRunArtifact",
    "ReferenceDataBinding",
    "RuleOutcomeStatus",
)
