"""Framework-free data ingestion, version, quality, and resolution domain objects.

These contracts are deliberately independent of SQLAlchemy, PyArrow, Pandas and
any vendor SDK. Persistent ORM rows live in ``data.adapters.persistence`` and are
mapped to and from these objects at the adapter boundary; web/API schemas and
vendor objects never become domain models.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType

from quantking.common.errors import (
    DatasetVersionImmutableError,
    InvalidDataTransitionError,
)
from quantking.common.ids import EntityId
from quantking.common.time import TradingDate, UtcTimestamp

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class DataProvider(StrEnum):
    """In-process market-data access library."""

    AKSHARE = "AKSHARE"


class DataUpstream(StrEnum):
    """Actual remote upstream reached through the provider library."""

    EASTMONEY = "EASTMONEY"
    TENCENT = "TENCENT"
    SINA = "SINA"


class BatchFailureKind(StrEnum):
    """Stable ingestion failure categories exposed across adapter boundaries."""

    NETWORK = "NETWORK_FAILURE"
    PERMISSION = "PERMISSION_FAILURE"
    PARSE = "PARSE_FAILURE"
    UNEXPECTED_EMPTY = "UNEXPECTED_EMPTY"
    QUALITY = "QUALITY_FAILURE"
    PARTIAL_SOURCE = "PARTIAL_SOURCE_FAILURE"
    INTERNAL = "INTERNAL_FAILURE"


class LegalEmptyClassification(StrEnum):
    """Explicit reasons for a successful zero-row supplier response."""

    NO_TRADING_SESSION = "NO_TRADING_SESSION"
    OUTSIDE_LISTING_RANGE = "OUTSIDE_LISTING_RANGE"
    EMPTY_REQUEST_UNIVERSE = "EMPTY_REQUEST_UNIVERSE"


class DatasetKind(StrEnum):
    """The six Stage 1 canonical datasets."""

    SECURITY_MASTER = "security_master"
    EXCHANGE_CALENDAR = "exchange_calendar"
    DAILY_BAR_UNADJUSTED = "daily_bar_unadjusted"
    ADJUSTMENT_FACTOR = "adjustment_factor"
    TRADING_STATUS = "trading_status"
    BENCHMARK_INDEX_DAILY = "benchmark_index_daily"


class IngestionBatchStatus(StrEnum):
    """Lifecycle of one supplier fetch batch; terminals are immutable."""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class DatasetVersionStatus(StrEnum):
    """Lifecycle of one canonical data version; PUBLISHED and FAILED are terminal."""

    BUILDING = "BUILDING"
    VALIDATED = "VALIDATED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class QualityRunStatus(StrEnum):
    """Lifecycle of one quality-rule run against a candidate version."""

    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


# ---------------------------------------------------------------------------
# Transition tables
# ---------------------------------------------------------------------------

_BATCH_TERMINAL = frozenset(
    {IngestionBatchStatus.SUCCEEDED, IngestionBatchStatus.PARTIAL, IngestionBatchStatus.FAILED}
)
BATCH_TRANSITIONS: Mapping[IngestionBatchStatus, frozenset[IngestionBatchStatus]] = (
    MappingProxyType(
        {
            IngestionBatchStatus.RUNNING: frozenset(
                {
                    IngestionBatchStatus.SUCCEEDED,
                    IngestionBatchStatus.PARTIAL,
                    IngestionBatchStatus.FAILED,
                }
            ),
            IngestionBatchStatus.SUCCEEDED: frozenset(),
            IngestionBatchStatus.PARTIAL: frozenset(),
            IngestionBatchStatus.FAILED: frozenset(),
        }
    )
)

_DATASET_VERSION_TERMINAL = frozenset({DatasetVersionStatus.PUBLISHED, DatasetVersionStatus.FAILED})
DATASET_VERSION_TRANSITIONS: Mapping[DatasetVersionStatus, frozenset[DatasetVersionStatus]] = (
    MappingProxyType(
        {
            DatasetVersionStatus.BUILDING: frozenset(
                {DatasetVersionStatus.VALIDATED, DatasetVersionStatus.FAILED}
            ),
            DatasetVersionStatus.VALIDATED: frozenset(
                {DatasetVersionStatus.PUBLISHED, DatasetVersionStatus.FAILED}
            ),
            DatasetVersionStatus.PUBLISHED: frozenset(),
            DatasetVersionStatus.FAILED: frozenset(),
        }
    )
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MANAGED_RELATIVE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*\Z")


def _text(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value


def _managed_relative(value: str, *, field: str) -> str:
    """Accept only a managed relative identifier, never an absolute path or escape."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or len(candidate.drive) != 0:
        raise ValueError(f"{field} must be a relative path, not absolute")
    if ".." in candidate.parts:
        raise ValueError(f"{field} must not contain parent traversal")
    if not _MANAGED_RELATIVE.fullmatch(value):
        raise ValueError(f"{field} must be a managed relative identifier")
    return value


def _sha256(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError(f"{field} must be a 64-character lowercase hex SHA-256")
    return value


def _sorted_manifest(value: Mapping[str, str], *, field: str) -> Mapping[str, str]:
    normalized: dict[str, str] = {}
    for key, item in value.items():
        normalized[_text(key, field=f"{field} key")] = _text(item, field=f"{field} value")
    return MappingProxyType(dict(sorted(normalized.items())))


def _canonical_json(value: Mapping[str, str]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _tuple_text(value: tuple[str, ...], *, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{field} must be a tuple")
    for item in value:
        _text(item, field=f"{field} element")
    return tuple(value)


def _tuple_trading_dates(value: tuple[TradingDate, ...], *, field: str) -> tuple[TradingDate, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{field} must be a tuple")
    for item in value:
        if not isinstance(item, TradingDate):
            raise TypeError(f"{field} element must be a TradingDate")
    return tuple(sorted(value, key=lambda d: d.value))


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatchFailureInfo:
    """Structured failure kept on a PARTIAL or FAILED batch."""

    error_type: BatchFailureKind
    message: str
    retryable: bool

    def __post_init__(self) -> None:
        if not isinstance(self.error_type, BatchFailureKind):
            raise TypeError("error_type must be a BatchFailureKind")
        _text(self.message, field="message")
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable must be a bool")


@dataclass(frozen=True, slots=True)
class EmptyResultInfo:
    """A successful zero-row result with a stable legal classification and reason."""

    classification: LegalEmptyClassification
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.classification, LegalEmptyClassification):
            raise TypeError("classification must be a LegalEmptyClassification")
        _text(self.reason, field="empty result reason")


@dataclass(frozen=True, slots=True)
class ManifestFileEntry:
    """One managed file in a version manifest; relative path only, no absolute paths."""

    relative_path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "relative_path", _managed_relative(self.relative_path, field="relative_path")
        )
        if not isinstance(self.size, int) or isinstance(self.size, bool) or self.size < 0:
            raise ValueError("size must be a non-negative integer")
        object.__setattr__(self, "sha256", _sha256(self.sha256, field="sha256"))


@dataclass(frozen=True, slots=True)
class IngestionBatch:
    """One supplier fetch batch, deliberately separate from any ORM row."""

    batch_id: EntityId
    provider: DataProvider
    upstream: DataUpstream
    endpoint: str
    library_version: str
    dataset: DatasetKind
    status: IngestionBatchStatus
    adapter_version: str
    request_params: Mapping[str, str]
    request_params_json: str
    started_at: UtcTimestamp
    ended_at: UtcTimestamp | None
    row_count: int | None
    security_count: int | None
    manifest_files: tuple[ManifestFileEntry, ...]
    failure: BatchFailureInfo | None
    job_run_id: str | None
    empty_result: EmptyResultInfo | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.batch_id, EntityId):
            raise TypeError("batch_id must be an EntityId")
        if self.batch_id.namespace != "data_batch":
            raise ValueError("batch_id must use the data_batch namespace")
        if not isinstance(self.provider, DataProvider):
            raise TypeError("provider must be a DataProvider")
        if not isinstance(self.upstream, DataUpstream):
            raise TypeError("upstream must be a DataUpstream")
        _text(self.endpoint, field="endpoint")
        _text(self.library_version, field="library_version")
        if not isinstance(self.dataset, DatasetKind):
            raise TypeError("dataset must be a DatasetKind")
        if not isinstance(self.status, IngestionBatchStatus):
            raise TypeError("status must be an IngestionBatchStatus")
        _text(self.adapter_version, field="adapter_version")
        object.__setattr__(
            self, "request_params", _sorted_manifest(self.request_params, field="request_params")
        )
        object.__setattr__(self, "request_params_json", _canonical_json(self.request_params))
        if not isinstance(self.started_at, UtcTimestamp):
            raise TypeError("started_at must be a UtcTimestamp")
        if self.ended_at is not None and not isinstance(self.ended_at, UtcTimestamp):
            raise TypeError("ended_at must be a UtcTimestamp or None")
        if self.ended_at is not None and self.ended_at.value < self.started_at.value:
            raise ValueError("ended_at must not precede started_at")
        for field in ("row_count", "security_count"):
            value = getattr(self, field)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{field} must be a non-negative integer or None")
        object.__setattr__(self, "manifest_files", tuple(self.manifest_files))
        if any(not isinstance(item, ManifestFileEntry) for item in self.manifest_files):
            raise TypeError("manifest_files must contain ManifestFileEntry values")
        if self.failure is not None and not isinstance(self.failure, BatchFailureInfo):
            raise TypeError("failure must be BatchFailureInfo or None")
        if self.empty_result is not None and not isinstance(self.empty_result, EmptyResultInfo):
            raise TypeError("empty_result must be EmptyResultInfo or None")
        if self.job_run_id is not None:
            _text(self.job_run_id, field="job_run_id")

        if self.status is IngestionBatchStatus.RUNNING:
            if (
                any(
                    value is not None
                    for value in (self.ended_at, self.row_count, self.security_count, self.failure)
                )
                or self.manifest_files
                or self.empty_result is not None
            ):
                raise ValueError("RUNNING batch cannot carry terminal outcome fields")
            return

        if self.ended_at is None:
            raise ValueError("terminal batch requires ended_at")
        if self.status in {IngestionBatchStatus.SUCCEEDED, IngestionBatchStatus.PARTIAL} and (
            self.row_count is None or self.security_count is None
        ):
            raise ValueError("SUCCEEDED/PARTIAL batch requires row_count and security_count")
        if self.status in {IngestionBatchStatus.FAILED, IngestionBatchStatus.PARTIAL}:
            if self.failure is None:
                raise ValueError("FAILED/PARTIAL batch requires structured failure")
            if self.empty_result is not None:
                raise ValueError("FAILED/PARTIAL batch cannot carry a legal empty result")
        elif self.failure is not None:
            raise ValueError("SUCCEEDED batch must not carry a failure")
        if self.status is IngestionBatchStatus.SUCCEEDED:
            if self.row_count == 0 and self.empty_result is None:
                raise ValueError("zero-row SUCCEEDED batch requires explicit legal empty result")
            if self.row_count != 0 and self.empty_result is not None:
                raise ValueError("non-empty SUCCEEDED batch cannot carry empty_result")

    @classmethod
    def running(
        cls,
        *,
        batch_id: EntityId,
        provider: DataProvider,
        upstream: DataUpstream,
        endpoint: str,
        library_version: str,
        dataset: DatasetKind,
        adapter_version: str,
        request_params: Mapping[str, str],
        started_at: UtcTimestamp,
        job_run_id: str | None = None,
    ) -> IngestionBatch:
        """Create a RUNNING batch without persistence side effects."""
        params = _sorted_manifest(request_params, field="request_params")
        return cls(
            batch_id=batch_id,
            provider=provider,
            upstream=upstream,
            endpoint=endpoint,
            library_version=library_version,
            dataset=dataset,
            status=IngestionBatchStatus.RUNNING,
            adapter_version=adapter_version,
            request_params=params,
            request_params_json=_canonical_json(params),
            started_at=started_at,
            ended_at=None,
            row_count=None,
            security_count=None,
            manifest_files=(),
            failure=None,
            job_run_id=job_run_id,
            empty_result=None,
        )


@dataclass(frozen=True, slots=True)
class DatasetVersion:
    """One canonical data version; PUBLISHED and FAILED are immutable."""

    version_id: EntityId
    dataset: DatasetKind
    status: DatasetVersionStatus
    schema_version: str
    code_version: str
    rule_version: str
    parent_version_id: EntityId | None
    input_batch_ids: tuple[EntityId, ...]
    created_at: UtcTimestamp
    validated_at: UtcTimestamp | None
    published_at: UtcTimestamp | None
    manifest_sha256: str | None
    security_count: int | None
    row_count: int | None
    coverage_start: TradingDate | None
    coverage_end: TradingDate | None
    coverage_summary: Mapping[str, str]
    failure: BatchFailureInfo | None
    quality_run_id: EntityId | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.version_id, EntityId):
            raise TypeError("version_id must be an EntityId")
        if self.version_id.namespace != "data_version":
            raise ValueError("version_id must use the data_version namespace")
        if not isinstance(self.dataset, DatasetKind):
            raise TypeError("dataset must be a DatasetKind")
        if not isinstance(self.status, DatasetVersionStatus):
            raise TypeError("status must be a DatasetVersionStatus")
        _text(self.schema_version, field="schema_version")
        _text(self.code_version, field="code_version")
        _text(self.rule_version, field="rule_version")
        if self.parent_version_id is not None:
            if not isinstance(self.parent_version_id, EntityId):
                raise TypeError("parent_version_id must be an EntityId or None")
            if self.parent_version_id.namespace != "data_version":
                raise ValueError("parent_version_id must use the data_version namespace")
            if self.parent_version_id == self.version_id:
                raise ValueError("parent_version_id cannot reference the version itself")
        object.__setattr__(self, "input_batch_ids", tuple(self.input_batch_ids))
        if len(set(self.input_batch_ids)) != len(self.input_batch_ids):
            raise ValueError("input_batch_ids must be unique")
        for batch_id in self.input_batch_ids:
            if not isinstance(batch_id, EntityId):
                raise TypeError("input_batch_ids must contain EntityId values")
            if batch_id.namespace != "data_batch":
                raise ValueError("input_batch_ids must use the data_batch namespace")
        if not isinstance(self.created_at, UtcTimestamp):
            raise TypeError("created_at must be a UtcTimestamp")
        for field in ("validated_at", "published_at"):
            value = getattr(self, field)
            if value is not None and not isinstance(value, UtcTimestamp):
                raise TypeError(f"{field} must be a UtcTimestamp or None")
        if self.manifest_sha256 is not None:
            object.__setattr__(
                self, "manifest_sha256", _sha256(self.manifest_sha256, field="manifest_sha256")
            )
        if self.quality_run_id is not None and (
            not isinstance(self.quality_run_id, EntityId)
            or self.quality_run_id.namespace != "data_quality"
        ):
            raise ValueError("quality_run_id must use the data_quality namespace")
        if self.content_sha256 is not None:
            object.__setattr__(
                self, "content_sha256", _sha256(self.content_sha256, field="content_sha256")
            )
        for field in ("security_count", "row_count"):
            value = getattr(self, field)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{field} must be a non-negative integer or None")
        if self.coverage_start is not None and not isinstance(self.coverage_start, TradingDate):
            raise TypeError("coverage_start must be a TradingDate or None")
        if self.coverage_end is not None and not isinstance(self.coverage_end, TradingDate):
            raise TypeError("coverage_end must be a TradingDate or None")
        if (self.coverage_start is None) != (self.coverage_end is None):
            raise ValueError("coverage_start and coverage_end must both be set or both be None")
        if (
            self.coverage_start is not None
            and self.coverage_end is not None
            and self.coverage_end.value < self.coverage_start.value
        ):
            raise ValueError("coverage_end must not precede coverage_start")
        object.__setattr__(
            self,
            "coverage_summary",
            _sorted_manifest(self.coverage_summary, field="coverage_summary"),
        )
        if self.failure is not None and not isinstance(self.failure, BatchFailureInfo):
            raise TypeError("failure must be BatchFailureInfo or None")

        if self.validated_at is not None and self.validated_at.value < self.created_at.value:
            raise ValueError("validated_at must not precede created_at")
        if self.published_at is not None and (
            self.validated_at is None or self.published_at.value < self.validated_at.value
        ):
            raise ValueError("published_at requires and must not precede validated_at")

        if self.status is DatasetVersionStatus.BUILDING:
            if (
                any(
                    value is not None
                    for value in (
                        self.validated_at,
                        self.published_at,
                        self.manifest_sha256,
                        self.security_count,
                        self.row_count,
                        self.coverage_start,
                        self.coverage_end,
                        self.failure,
                        self.quality_run_id,
                        self.content_sha256,
                    )
                )
                or self.coverage_summary
            ):
                raise ValueError("BUILDING version cannot carry validation or failure outcome")
        elif self.status in {DatasetVersionStatus.VALIDATED, DatasetVersionStatus.PUBLISHED}:
            if (
                self.validated_at is None
                or self.manifest_sha256 is None
                or self.security_count is None
                or self.row_count is None
                or not self.coverage_summary
            ):
                raise ValueError("VALIDATED/PUBLISHED version requires complete coverage metadata")
            if self.row_count > 0 and self.coverage_start is None:
                raise ValueError("non-empty VALIDATED/PUBLISHED version requires coverage dates")
            if self.failure is not None:
                raise ValueError("VALIDATED/PUBLISHED version cannot carry failure")
            if self.status is DatasetVersionStatus.VALIDATED and self.published_at is not None:
                raise ValueError("VALIDATED version cannot carry published_at")
            if self.status is DatasetVersionStatus.PUBLISHED and self.published_at is None:
                raise ValueError("PUBLISHED version requires published_at")
        elif self.status is DatasetVersionStatus.FAILED:
            if self.failure is None:
                raise ValueError("FAILED version requires structured failure")
            if self.published_at is not None:
                raise ValueError("FAILED version cannot carry published_at")

    @classmethod
    def building(
        cls,
        *,
        version_id: EntityId,
        dataset: DatasetKind,
        schema_version: str,
        code_version: str,
        parent_version_id: EntityId | None,
        input_batch_ids: tuple[EntityId, ...],
        created_at: UtcTimestamp,
        rule_version: str = "quality-rules.v1",
    ) -> DatasetVersion:
        """Create a BUILDING version without persistence side effects."""
        return cls(
            version_id=version_id,
            dataset=dataset,
            status=DatasetVersionStatus.BUILDING,
            schema_version=schema_version,
            code_version=code_version,
            rule_version=rule_version,
            parent_version_id=parent_version_id,
            input_batch_ids=input_batch_ids,
            created_at=created_at,
            validated_at=None,
            published_at=None,
            manifest_sha256=None,
            security_count=None,
            row_count=None,
            coverage_start=None,
            coverage_end=None,
            coverage_summary={},
            failure=None,
            quality_run_id=None,
            content_sha256=None,
        )


@dataclass(frozen=True, slots=True)
class QualityResult:
    """One quality-rule outcome; large row-level detail lives in a managed report file."""

    rule_code: str
    passed: bool
    affected_count: int
    summary: str
    report_relative_path: str | None

    def __post_init__(self) -> None:
        _text(self.rule_code, field="rule_code")
        if not isinstance(self.passed, bool):
            raise TypeError("passed must be a bool")
        if (
            not isinstance(self.affected_count, int)
            or isinstance(self.affected_count, bool)
            or self.affected_count < 0
        ):
            raise ValueError("affected_count must be a non-negative integer")
        _text(self.summary, field="summary")
        if self.report_relative_path is not None:
            object.__setattr__(
                self,
                "report_relative_path",
                _managed_relative(self.report_relative_path, field="report_relative_path"),
            )


@dataclass(frozen=True, slots=True)
class QualityRun:
    """One quality-rule run against a candidate dataset version."""

    run_id: EntityId
    version_id: EntityId
    status: QualityRunStatus
    rule_version: str
    schema_version: str
    content_sha256: str
    started_at: UtcTimestamp
    ended_at: UtcTimestamp | None
    results: tuple[QualityResult, ...]
    summary_report_relative_path: str | None
    failure: BatchFailureInfo | None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, EntityId) or self.run_id.namespace != "data_quality":
            raise ValueError("run_id must use the data_quality namespace")
        if not isinstance(self.version_id, EntityId) or self.version_id.namespace != "data_version":
            raise ValueError("version_id must use the data_version namespace")
        if not isinstance(self.status, QualityRunStatus):
            raise TypeError("status must be a QualityRunStatus")
        _text(self.rule_version, field="rule_version")
        _text(self.schema_version, field="schema_version")
        object.__setattr__(
            self, "content_sha256", _sha256(self.content_sha256, field="content_sha256")
        )
        if not isinstance(self.started_at, UtcTimestamp):
            raise TypeError("started_at must be a UtcTimestamp")
        if self.ended_at is not None and not isinstance(self.ended_at, UtcTimestamp):
            raise TypeError("ended_at must be a UtcTimestamp or None")
        if self.ended_at is not None and self.ended_at.value < self.started_at.value:
            raise ValueError("ended_at must not precede started_at")
        object.__setattr__(self, "results", tuple(self.results))
        if any(not isinstance(item, QualityResult) for item in self.results):
            raise TypeError("results must contain QualityResult values")
        if self.summary_report_relative_path is not None:
            object.__setattr__(
                self,
                "summary_report_relative_path",
                _managed_relative(
                    self.summary_report_relative_path, field="summary_report_relative_path"
                ),
            )
        if self.failure is not None and not isinstance(self.failure, BatchFailureInfo):
            raise TypeError("failure must be BatchFailureInfo or None")
        if self.status is QualityRunStatus.RUNNING:
            if self.ended_at is not None or self.results or self.failure is not None:
                raise ValueError("RUNNING quality run cannot carry terminal outcome")
        elif self.ended_at is None:
            raise ValueError("terminal quality run requires ended_at")
        elif self.status is QualityRunStatus.PASSED:
            if not self.results or any(not result.passed for result in self.results):
                raise ValueError("PASSED quality run requires non-empty passing results")
            if self.failure is not None:
                raise ValueError("PASSED quality run cannot carry failure")
        elif self.status is QualityRunStatus.FAILED:
            if self.failure is None and not any(not result.passed for result in self.results):
                raise ValueError("FAILED quality run requires failure or a failed result")
        elif self.status is QualityRunStatus.SKIPPED and (self.results or self.failure is not None):
            raise ValueError("SKIPPED quality run cannot carry results or failure")


@dataclass(frozen=True, slots=True)
class DifferenceReport:
    """Cross-source difference summary; row-level detail lives in a managed report file.

    The report binds to both-side batches, sources, datasets, version IDs,
    content SHAs, quality-run IDs, sample spec, threshold rule version and
    a deterministic row-level report SHA-256 so the SQLite repository can
    verify both-side lineage and quality evidence before persisting. The
    ``report_relative_path`` points at the managed, immutable artifact
    directory produced by the DifferenceReport writer.
    """

    report_id: EntityId
    version_id: EntityId
    primary_source: DataUpstream
    fallback_source: DataUpstream
    created_at: UtcTimestamp
    field_differences: Mapping[str, str]
    total_compared: int
    total_differences: int
    report_relative_path: str
    primary_batch_id: EntityId
    fallback_batch_id: EntityId
    primary_version_id: EntityId
    fallback_version_id: EntityId
    primary_content_sha256: str
    fallback_content_sha256: str
    primary_quality_run_id: EntityId
    fallback_quality_run_id: EntityId
    sample_spec_sha256: str
    threshold_rule_version: str
    report_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.report_id, EntityId) or self.report_id.namespace != "data_diff":
            raise ValueError("report_id must use the data_diff namespace")
        if not isinstance(self.version_id, EntityId) or self.version_id.namespace != "data_version":
            raise ValueError("version_id must use the data_version namespace")
        if not isinstance(self.primary_source, DataUpstream):
            raise TypeError("primary_source must be a DataUpstream")
        if not isinstance(self.fallback_source, DataUpstream):
            raise TypeError("fallback_source must be a DataUpstream")
        if self.primary_source == self.fallback_source:
            raise ValueError("primary and fallback sources must differ")
        for field_name, namespace in (
            ("primary_batch_id", "data_batch"),
            ("fallback_batch_id", "data_batch"),
            ("primary_version_id", "data_version"),
            ("fallback_version_id", "data_version"),
            ("primary_quality_run_id", "data_quality"),
            ("fallback_quality_run_id", "data_quality"),
        ):
            value = getattr(self, field_name)
            if not isinstance(value, EntityId) or value.namespace != namespace:
                raise ValueError(f"{field_name} must use the {namespace} namespace")
        if self.primary_batch_id == self.fallback_batch_id:
            raise ValueError("primary_batch_id and fallback_batch_id must differ")
        shared_candidate = self.primary_version_id == self.fallback_version_id
        if shared_candidate:
            if (
                self.version_id != self.primary_version_id
                or self.threshold_rule_version != "comparison-thresholds.daily-bar-unadjusted.k1.v1"
            ):
                raise ValueError(
                    "shared DifferenceReport versions require the K2 final candidate binding"
                )
        elif self.version_id != self.primary_version_id:
            raise ValueError("legacy DifferenceReport version_id must equal primary_version_id")
        if self.primary_quality_run_id == self.fallback_quality_run_id:
            raise ValueError("primary_quality_run_id and fallback_quality_run_id must differ")
        sha256_fields = (
            "primary_content_sha256",
            "fallback_content_sha256",
            "sample_spec_sha256",
            "report_sha256",
        )
        for field_name in sha256_fields:
            value = getattr(self, field_name)
            if not isinstance(value, str) or len(value) != 64:
                raise ValueError(f"{field_name} must be a 64-character hex string")
        if (
            not isinstance(self.threshold_rule_version, str)
            or not self.threshold_rule_version.strip()
        ):
            raise ValueError("threshold_rule_version must be non-empty text")
        object.__setattr__(
            self,
            "field_differences",
            _sorted_manifest(self.field_differences, field="field_differences"),
        )
        for field in ("total_compared", "total_differences"):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        object.__setattr__(
            self,
            "report_relative_path",
            _managed_relative(self.report_relative_path, field="report_relative_path"),
        )


# ---------------------------------------------------------------------------
# Transition functions
# ---------------------------------------------------------------------------


def transition_batch(
    batch: IngestionBatch,
    target: IngestionBatchStatus,
    *,
    at: UtcTimestamp,
    row_count: int | None = None,
    security_count: int | None = None,
    manifest_files: tuple[ManifestFileEntry, ...] = (),
    failure: BatchFailureInfo | None = None,
    empty_result: EmptyResultInfo | None = None,
) -> IngestionBatch:
    """Apply the authoritative ingestion-batch transition table."""
    if target not in BATCH_TRANSITIONS[batch.status]:
        raise InvalidDataTransitionError(
            "ingestion batch transition is not allowed",
            context={"current": batch.status.value, "target": target.value},
        )
    if target is IngestionBatchStatus.FAILED and failure is None:
        raise InvalidDataTransitionError("FAILED batch requires structured failure")
    if target is IngestionBatchStatus.PARTIAL and failure is None:
        raise InvalidDataTransitionError("PARTIAL batch requires structured failure")
    if target is IngestionBatchStatus.SUCCEEDED and failure is not None:
        raise InvalidDataTransitionError("SUCCEEDED batch must not carry a failure")
    if target is IngestionBatchStatus.SUCCEEDED and row_count == 0 and empty_result is None:
        raise InvalidDataTransitionError(
            "zero-row SUCCEEDED batch requires explicit legal empty result"
        )
    if target is not IngestionBatchStatus.SUCCEEDED and empty_result is not None:
        raise InvalidDataTransitionError("only SUCCEEDED batch may carry a legal empty result")
    return replace(
        batch,
        status=target,
        ended_at=at,
        row_count=row_count if target in _BATCH_TERMINAL else batch.row_count,
        security_count=security_count if target in _BATCH_TERMINAL else batch.security_count,
        manifest_files=manifest_files if target in _BATCH_TERMINAL else batch.manifest_files,
        failure=failure,
        empty_result=empty_result,
    )


def transition_dataset_version(
    version: DatasetVersion,
    target: DatasetVersionStatus,
    *,
    at: UtcTimestamp,
    manifest_sha256: str | None = None,
    security_count: int | None = None,
    row_count: int | None = None,
    coverage_start: TradingDate | None = None,
    coverage_end: TradingDate | None = None,
    coverage_summary: Mapping[str, str] | None = None,
    failure: BatchFailureInfo | None = None,
    quality_run_id: EntityId | None = None,
    content_sha256: str | None = None,
) -> DatasetVersion:
    """Apply the authoritative dataset-version transition table."""
    if version.status is DatasetVersionStatus.PUBLISHED:
        raise DatasetVersionImmutableError(
            "published dataset version is immutable",
            context={"current": version.status.value, "target": target.value},
        )
    if version.status is DatasetVersionStatus.FAILED:
        raise InvalidDataTransitionError(
            "failed dataset version cannot be transitioned; retry must create a new version",
            context={"current": version.status.value, "target": target.value},
        )
    if target not in DATASET_VERSION_TRANSITIONS[version.status]:
        raise InvalidDataTransitionError(
            "dataset version transition is not allowed",
            context={"current": version.status.value, "target": target.value},
        )
    if target is DatasetVersionStatus.FAILED and failure is None:
        raise InvalidDataTransitionError("FAILED version requires structured failure")
    if target is DatasetVersionStatus.VALIDATED and manifest_sha256 is None:
        raise InvalidDataTransitionError("VALIDATED requires a manifest SHA-256")
    if target is DatasetVersionStatus.VALIDATED and not coverage_summary:
        raise InvalidDataTransitionError("VALIDATED requires a coverage summary")
    if (
        target is DatasetVersionStatus.PUBLISHED
        and version.status is not DatasetVersionStatus.VALIDATED
    ):
        raise InvalidDataTransitionError("only VALIDATED versions can be PUBLISHED")
    return replace(
        version,
        status=target,
        validated_at=at if target is DatasetVersionStatus.VALIDATED else version.validated_at,
        published_at=at if target is DatasetVersionStatus.PUBLISHED else version.published_at,
        manifest_sha256=manifest_sha256
        if target is DatasetVersionStatus.VALIDATED
        else version.manifest_sha256,
        security_count=security_count
        if target is DatasetVersionStatus.VALIDATED
        else version.security_count,
        row_count=row_count if target is DatasetVersionStatus.VALIDATED else version.row_count,
        coverage_start=coverage_start
        if target is DatasetVersionStatus.VALIDATED
        else version.coverage_start,
        coverage_end=coverage_end
        if target is DatasetVersionStatus.VALIDATED
        else version.coverage_end,
        coverage_summary=coverage_summary
        if target is DatasetVersionStatus.VALIDATED and coverage_summary is not None
        else version.coverage_summary,
        failure=failure,
        quality_run_id=quality_run_id
        if target is DatasetVersionStatus.VALIDATED
        else version.quality_run_id,
        content_sha256=content_sha256
        if target is DatasetVersionStatus.VALIDATED
        else version.content_sha256,
    )
