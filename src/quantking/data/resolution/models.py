"""Framework-free immutable contracts for K2 source resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import PurePosixPath

from quantking.common.ids import EntityId
from quantking.common.time import UtcTimestamp
from quantking.data.comparison.thresholds import THRESHOLD_RULE_VERSION
from quantking.data.models import DatasetKind, DataUpstream

RESOLUTION_RULE_VERSION = "source-resolution.daily-bar-unadjusted.k2.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_ORDER = (DataUpstream.EASTMONEY, DataUpstream.TENCENT, DataUpstream.SINA)


class ResolutionSourceStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    CALL_FAILED = "CALL_FAILED"
    EMPTY = "EMPTY"
    QUALITY_FAILED = "QUALITY_FAILED"
    MISSING_ROW = "MISSING_ROW"
    NOT_CALLED = "NOT_CALLED"


class ResolutionReasonCode(StrEnum):
    AB_AGREE_DEFAULT = "AB_AGREE_DEFAULT"
    ABC_AGREE_A_SELECTED = "ABC_AGREE_A_SELECTED"
    AC_AGREE_A_SELECTED = "AC_AGREE_A_SELECTED"
    BC_AGREE_B_SELECTED = "BC_AGREE_B_SELECTED"
    AB_AGREE_A_SELECTED = "AB_AGREE_A_SELECTED"
    NO_TWO_SOURCE_CONSENSUS = "NO_TWO_SOURCE_CONSENSUS"


class ResolutionRunStatus(StrEnum):
    RESOLVED = "RESOLVED"
    EMPTY_CONSENSUS = "EMPTY_CONSENSUS"
    BLOCKED = "BLOCKED"


class ResolutionRunBlocker(StrEnum):
    ALL_SOURCES_UNAVAILABLE = "ALL_SOURCES_UNAVAILABLE"
    INSUFFICIENT_EMPTY_CONSENSUS = "INSUFFICIENT_EMPTY_CONSENSUS"


def _upstreams(value: tuple[DataUpstream, ...], *, field: str) -> tuple[DataUpstream, ...]:
    if not isinstance(value, tuple) or any(not isinstance(item, DataUpstream) for item in value):
        raise TypeError(f"{field} must be a tuple of DataUpstream")
    ordered = tuple(item for item in _SOURCE_ORDER if item in value)
    if value != ordered or len(set(value)) != len(value):
        raise ValueError(f"{field} must use stable source order without duplicates")
    return value


@dataclass(frozen=True, slots=True)
class ResolutionSourceBinding:
    """One row-level upstream state with credentials only when they exist."""

    upstream: DataUpstream
    status: ResolutionSourceStatus
    batch_id: EntityId | None
    content_sha256: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.upstream, DataUpstream):
            raise TypeError("upstream must be a DataUpstream")
        if not isinstance(self.status, ResolutionSourceStatus):
            raise TypeError("status must be a ResolutionSourceStatus")
        if self.batch_id is not None and (
            not isinstance(self.batch_id, EntityId) or self.batch_id.namespace != "data_batch"
        ):
            raise ValueError("batch_id must use data_batch namespace")
        if self.content_sha256 is not None and _SHA256.fullmatch(self.content_sha256) is None:
            raise ValueError("content_sha256 must be lowercase SHA-256")
        if self.status is ResolutionSourceStatus.NOT_CALLED and (
            self.batch_id is not None or self.content_sha256 is not None
        ):
            raise ValueError("NOT_CALLED must not fabricate batch or content SHA")
        if self.status in {
            ResolutionSourceStatus.AVAILABLE,
            ResolutionSourceStatus.EMPTY,
            ResolutionSourceStatus.QUALITY_FAILED,
            ResolutionSourceStatus.MISSING_ROW,
        } and (self.batch_id is None or self.content_sha256 is None):
            raise ValueError("landed source states require batch and content SHA")
        if self.status is ResolutionSourceStatus.CALL_FAILED and self.content_sha256 is not None:
            raise ValueError("CALL_FAILED must not fabricate content SHA")

    def to_dict(self) -> dict[str, object]:
        return {
            "upstream": self.upstream.value,
            "status": self.status.value,
            "batch_id": None if self.batch_id is None else str(self.batch_id),
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class CRequestTrace:
    """The exact affected symbol/date span passed to the C endpoint once."""

    symbol: str
    start_date: date
    end_date: date
    trigger_reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("symbol must be non-empty text")
        if not isinstance(self.start_date, date) or not isinstance(self.end_date, date):
            raise TypeError("C request dates must be date values")
        if self.start_date > self.end_date:
            raise ValueError("C request start must not follow end")
        if not isinstance(self.trigger_reason, str) or not self.trigger_reason:
            raise ValueError("trigger_reason must be non-empty text")

    def to_dict(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "trigger_reason": self.trigger_reason,
        }


@dataclass(frozen=True, slots=True)
class ResolutionDefaultSummary:
    source_bindings: tuple[ResolutionSourceBinding, ...]
    compared_upstreams: tuple[DataUpstream, ...]
    agreeing_upstreams: tuple[DataUpstream, ...]
    selected_upstream: DataUpstream
    reason_code: ResolutionReasonCode
    decision_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.source_bindings, tuple) or any(
            not isinstance(item, ResolutionSourceBinding) for item in self.source_bindings
        ):
            raise TypeError("source_bindings must contain ResolutionSourceBinding values")
        if tuple(item.upstream for item in self.source_bindings) != _SOURCE_ORDER:
            raise ValueError("default source_bindings must contain A/B/C in stable order")
        statuses = tuple(item.status for item in self.source_bindings)
        if statuses[2] is not ResolutionSourceStatus.NOT_CALLED:
            raise ValueError("default summary must keep C NOT_CALLED")
        _upstreams(self.compared_upstreams, field="compared_upstreams")
        _upstreams(self.agreeing_upstreams, field="agreeing_upstreams")
        if (
            self.compared_upstreams
            != (
                DataUpstream.EASTMONEY,
                DataUpstream.TENCENT,
            )
            or self.agreeing_upstreams != self.compared_upstreams
        ):
            raise ValueError("default summary must represent A/B agreement")
        if self.selected_upstream is not DataUpstream.EASTMONEY:
            raise ValueError("default summary must select A")
        if self.reason_code is not ResolutionReasonCode.AB_AGREE_DEFAULT:
            raise ValueError("default summary reason is invalid")
        if isinstance(self.decision_count, bool) or self.decision_count <= 0:
            raise ValueError("decision_count must be positive")
        if statuses[:2] != (
            ResolutionSourceStatus.AVAILABLE,
            ResolutionSourceStatus.AVAILABLE,
        ):
            raise ValueError("materialized default decisions require available A/B credentials")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_bindings": [item.to_dict() for item in self.source_bindings],
            "compared_upstreams": [item.value for item in self.compared_upstreams],
            "agreeing_upstreams": [item.value for item in self.agreeing_upstreams],
            "selected_upstream": self.selected_upstream.value,
            "reason_code": self.reason_code.value,
            "decision_count": self.decision_count,
        }


@dataclass(frozen=True, slots=True)
class ResolutionDecision:
    """One exceptional or conflicting symbol/date decision."""

    symbol: str
    trade_date: date
    source_bindings: tuple[ResolutionSourceBinding, ...]
    compared_upstreams: tuple[DataUpstream, ...]
    agreeing_upstreams: tuple[DataUpstream, ...]
    selected_upstream: DataUpstream | None
    selected_row_present: bool | None
    reason_code: ResolutionReasonCode
    rule_version: str
    difference_report_ids: tuple[EntityId, ...]
    created_job_id: EntityId
    created_at: UtcTimestamp
    c_request: CRequestTrace | None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("symbol must be non-empty text")
        if not isinstance(self.trade_date, date):
            raise TypeError("trade_date must be a date")
        if not isinstance(self.source_bindings, tuple) or any(
            not isinstance(item, ResolutionSourceBinding) for item in self.source_bindings
        ):
            raise TypeError("source_bindings must contain ResolutionSourceBinding values")
        expected = tuple(item.upstream for item in self.source_bindings)
        if expected != _SOURCE_ORDER:
            raise ValueError("source_bindings must contain A/B/C in stable order")
        _upstreams(self.compared_upstreams, field="compared_upstreams")
        _upstreams(self.agreeing_upstreams, field="agreeing_upstreams")
        comparable = tuple(
            item.upstream
            for item in self.source_bindings
            if item.status
            in {
                ResolutionSourceStatus.AVAILABLE,
                ResolutionSourceStatus.MISSING_ROW,
                ResolutionSourceStatus.EMPTY,
            }
        )
        if self.compared_upstreams != comparable:
            raise ValueError("compared_upstreams must exactly match comparable source states")
        if any(item not in self.compared_upstreams for item in self.agreeing_upstreams):
            raise ValueError("agreeing_upstreams must be a subset of compared_upstreams")
        if self.selected_upstream is None:
            if (
                self.agreeing_upstreams
                or self.selected_row_present is not None
                or self.reason_code is not ResolutionReasonCode.NO_TWO_SOURCE_CONSENSUS
            ):
                raise ValueError("conflict decisions must have no agreeing/selected source")
        elif (
            self.selected_upstream not in self.agreeing_upstreams
            or len(self.agreeing_upstreams) < 2
            or not isinstance(self.selected_row_present, bool)
            or self.reason_code is ResolutionReasonCode.NO_TWO_SOURCE_CONSENSUS
        ):
            raise ValueError("selected source requires two-source agreement")
        if self.selected_upstream is not None:
            selected_status = next(
                item.status
                for item in self.source_bindings
                if item.upstream is self.selected_upstream
            )
            expected_presence = selected_status is ResolutionSourceStatus.AVAILABLE
            if self.selected_row_present is not expected_presence:
                raise ValueError("selected_row_present does not match selected source state")
        expected_resolution = {
            ResolutionReasonCode.ABC_AGREE_A_SELECTED: (
                (
                    DataUpstream.EASTMONEY,
                    DataUpstream.TENCENT,
                    DataUpstream.SINA,
                ),
                DataUpstream.EASTMONEY,
            ),
            ResolutionReasonCode.AC_AGREE_A_SELECTED: (
                (DataUpstream.EASTMONEY, DataUpstream.SINA),
                DataUpstream.EASTMONEY,
            ),
            ResolutionReasonCode.BC_AGREE_B_SELECTED: (
                (DataUpstream.TENCENT, DataUpstream.SINA),
                DataUpstream.TENCENT,
            ),
            ResolutionReasonCode.AB_AGREE_A_SELECTED: (
                (DataUpstream.EASTMONEY, DataUpstream.TENCENT),
                DataUpstream.EASTMONEY,
            ),
        }
        if self.reason_code in expected_resolution:
            expected_agreeing, expected_selected = expected_resolution[self.reason_code]
            if (
                self.agreeing_upstreams != expected_agreeing
                or self.selected_upstream is not expected_selected
            ):
                raise ValueError("reason_code does not match agreeing/selected sources")
        elif self.reason_code is ResolutionReasonCode.AB_AGREE_DEFAULT:
            raise ValueError("row exceptions cannot use the default-summary reason")
        if self.rule_version != RESOLUTION_RULE_VERSION:
            raise ValueError("rule_version is unsupported")
        if any(
            not isinstance(item, EntityId) or item.namespace != "data_diff"
            for item in self.difference_report_ids
        ):
            raise ValueError("difference_report_ids must use data_diff namespace")
        if tuple(sorted(self.difference_report_ids, key=str)) != self.difference_report_ids:
            raise ValueError("difference_report_ids must be sorted")
        if len(self.compared_upstreams) >= 2 and not self.difference_report_ids:
            raise ValueError("multi-source decisions require DifferenceReport bindings")
        if not isinstance(self.created_job_id, EntityId) or self.created_job_id.namespace != "job":
            raise ValueError("created_job_id must use job namespace")
        if not isinstance(self.created_at, UtcTimestamp):
            raise TypeError("created_at must be UtcTimestamp")
        c_binding = self.source_bindings[2]
        if c_binding.status is ResolutionSourceStatus.NOT_CALLED:
            if self.c_request is not None:
                raise ValueError("NOT_CALLED C cannot carry a request trace")
        elif self.c_request is None:
            raise ValueError("called C must carry its exact request trace")
        elif (
            self.c_request.symbol != self.symbol
            or not self.c_request.start_date <= self.trade_date <= self.c_request.end_date
        ):
            raise ValueError("C request trace must cover the decision symbol/date")

    @property
    def conflict(self) -> bool:
        return self.selected_upstream is None

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "trade_date": self.trade_date.isoformat(),
            "source_bindings": [item.to_dict() for item in self.source_bindings],
            "compared_upstreams": [item.value for item in self.compared_upstreams],
            "agreeing_upstreams": [item.value for item in self.agreeing_upstreams],
            "selected_upstream": (
                None if self.selected_upstream is None else self.selected_upstream.value
            ),
            "selected_row_present": self.selected_row_present,
            "reason_code": self.reason_code.value,
            "rule_version": self.rule_version,
            "threshold_rule_version": THRESHOLD_RULE_VERSION,
            "difference_report_ids": [str(item) for item in self.difference_report_ids],
            "created_job_id": str(self.created_job_id),
            "created_at": self.created_at.to_iso(),
            "c_request": None if self.c_request is None else self.c_request.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ResolutionRun:
    run_id: EntityId
    dataset: DatasetKind
    created_job_id: EntityId
    created_at: UtcTimestamp
    rule_version: str
    threshold_rule_version: str
    input_fingerprint: str
    candidate_version_id: EntityId | None
    input_bindings: tuple[ResolutionSourceBinding, ...]
    difference_report_ids: tuple[EntityId, ...]
    default_summary: ResolutionDefaultSummary | None
    run_blockers: tuple[ResolutionRunBlocker, ...]
    exceptions: tuple[ResolutionDecision, ...]
    conflicts: tuple[ResolutionDecision, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, EntityId) or self.run_id.namespace != "data_resolution":
            raise ValueError("run_id must use data_resolution namespace")
        if self.dataset is not DatasetKind.DAILY_BAR_UNADJUSTED:
            raise ValueError("resolution supports daily_bar_unadjusted only")
        if not isinstance(self.created_job_id, EntityId) or self.created_job_id.namespace != "job":
            raise ValueError("created_job_id must use job namespace")
        if not isinstance(self.created_at, UtcTimestamp):
            raise TypeError("created_at must be UtcTimestamp")
        if self.rule_version != RESOLUTION_RULE_VERSION:
            raise ValueError("rule_version is unsupported")
        if self.threshold_rule_version != THRESHOLD_RULE_VERSION:
            raise ValueError("threshold_rule_version is unsupported")
        if _SHA256.fullmatch(self.input_fingerprint) is None:
            raise ValueError("input_fingerprint must be lowercase SHA-256")
        if self.candidate_version_id is not None and (
            not isinstance(self.candidate_version_id, EntityId)
            or self.candidate_version_id.namespace != "data_version"
        ):
            raise ValueError("candidate_version_id must use data_version namespace")
        if not isinstance(self.input_bindings, tuple) or any(
            not isinstance(item, ResolutionSourceBinding) for item in self.input_bindings
        ):
            raise TypeError("input_bindings must contain ResolutionSourceBinding values")
        source_rank = {source: index for index, source in enumerate(_SOURCE_ORDER)}
        binding_order = tuple(
            sorted(
                self.input_bindings,
                key=lambda item: (
                    source_rank[item.upstream],
                    "" if item.batch_id is None else str(item.batch_id),
                    item.status.value,
                    item.content_sha256 or "",
                ),
            )
        )
        if self.input_bindings != binding_order or len(
            {json_key(item) for item in self.input_bindings}
        ) != len(self.input_bindings):
            raise ValueError("input_bindings must be sorted and unique")
        c_bindings = tuple(
            item for item in self.input_bindings if item.upstream is DataUpstream.SINA
        )
        if (
            any(item.status is ResolutionSourceStatus.NOT_CALLED for item in c_bindings)
            and len(c_bindings) != 1
        ):
            raise ValueError("C NOT_CALLED cannot coexist with called C bindings")
        if not isinstance(self.difference_report_ids, tuple) or any(
            not isinstance(item, EntityId) or item.namespace != "data_diff"
            for item in self.difference_report_ids
        ):
            raise ValueError("difference_report_ids must use data_diff namespace")
        if tuple(sorted(self.difference_report_ids, key=str)) != self.difference_report_ids or len(
            set(self.difference_report_ids)
        ) != len(self.difference_report_ids):
            raise ValueError("difference_report_ids must be sorted and unique")
        if self.default_summary is not None:
            if not isinstance(self.default_summary, ResolutionDefaultSummary):
                raise TypeError("default_summary must be ResolutionDefaultSummary or None")
            for default_binding in self.default_summary.source_bindings[:2]:
                if default_binding not in self.input_bindings:
                    raise ValueError(
                        "default A/B credentials must be present in run input bindings"
                    )
        if not isinstance(self.run_blockers, tuple) or any(
            not isinstance(item, ResolutionRunBlocker) for item in self.run_blockers
        ):
            raise TypeError("run_blockers must contain ResolutionRunBlocker values")
        if tuple(sorted(set(self.run_blockers), key=lambda item: item.value)) != self.run_blockers:
            raise ValueError("run_blockers must be sorted and unique")
        for decisions, conflict in ((self.exceptions, False), (self.conflicts, True)):
            if not isinstance(decisions, tuple) or any(
                not isinstance(item, ResolutionDecision) or item.conflict is not conflict
                for item in decisions
            ):
                raise ValueError("resolution decisions are invalid")
            ordered = tuple(sorted(decisions, key=lambda item: (item.symbol, item.trade_date)))
            if decisions != ordered:
                raise ValueError("resolution decisions must be deterministically sorted")
            if any(
                report_id not in self.difference_report_ids
                for decision in decisions
                for report_id in decision.difference_report_ids
            ):
                raise ValueError("decision DifferenceReport is missing from run bindings")
            for decision in decisions:
                if decision.created_job_id != self.created_job_id:
                    raise ValueError("decision job must match resolution run job")
                for binding in decision.source_bindings:
                    if binding.status is ResolutionSourceStatus.NOT_CALLED:
                        continue
                    if not any(
                        candidate.upstream is binding.upstream
                        and candidate.batch_id == binding.batch_id
                        and candidate.content_sha256 == binding.content_sha256
                        for candidate in self.input_bindings
                    ):
                        raise ValueError("decision source binding is absent from run inputs")
        decision_keys = tuple(
            (item.symbol, item.trade_date) for item in (*self.exceptions, *self.conflicts)
        )
        if len(set(decision_keys)) != len(decision_keys):
            raise ValueError("exception and conflict decision keys must be globally unique")
        total = self.default_decision_count + len(self.exceptions) + len(self.conflicts)
        if self.candidate_version_id is not None:
            if self.conflicts or self.run_blockers:
                raise ValueError("publishable resolution cannot contain conflicts or blockers")
            if total == 0:
                empty_sources = {
                    item.upstream
                    for item in self.input_bindings
                    if item.status is ResolutionSourceStatus.EMPTY
                }
                if len(empty_sources) < 2 or any(
                    item.status is ResolutionSourceStatus.AVAILABLE for item in self.input_bindings
                ):
                    raise ValueError("zero-row publication requires two-source empty consensus")
        elif not self.conflicts and not self.run_blockers:
            raise ValueError("blocked resolution requires conflicts or run blockers")

    @property
    def default_decision_count(self) -> int:
        return 0 if self.default_summary is None else self.default_summary.decision_count

    @property
    def status(self) -> ResolutionRunStatus:
        if self.candidate_version_id is None:
            return ResolutionRunStatus.BLOCKED
        if self.total_decisions == 0:
            return ResolutionRunStatus.EMPTY_CONSENSUS
        return ResolutionRunStatus.RESOLVED

    @property
    def total_decisions(self) -> int:
        return self.default_decision_count + len(self.exceptions) + len(self.conflicts)

    def summary_dict(self) -> dict[str, object]:
        return {
            "run_id": str(self.run_id),
            "dataset": self.dataset.value,
            "created_job_id": str(self.created_job_id),
            "created_at": self.created_at.to_iso(),
            "rule_version": self.rule_version,
            "threshold_rule_version": self.threshold_rule_version,
            "input_fingerprint": self.input_fingerprint,
            "candidate_version_id": (
                None if self.candidate_version_id is None else str(self.candidate_version_id)
            ),
            "status": self.status.value,
            "input_bindings": [item.to_dict() for item in self.input_bindings],
            "difference_report_ids": [str(item) for item in self.difference_report_ids],
            "default_decision": (
                None if self.default_summary is None else self.default_summary.to_dict()
            ),
            "run_blockers": [item.value for item in self.run_blockers],
            "total_decisions": self.total_decisions,
            "exception_count": len(self.exceptions),
            "conflict_count": len(self.conflicts),
            "blocker_count": len(self.run_blockers),
        }


@dataclass(frozen=True, slots=True)
class ResolutionRunRecord:
    """SQLite-sized immutable summary; row decisions remain only in artifacts."""

    run_id: EntityId
    dataset: DatasetKind
    created_job_id: EntityId
    candidate_version_id: EntityId | None
    created_at: UtcTimestamp
    rule_version: str
    threshold_rule_version: str
    input_fingerprint: str
    artifact_relative_path: str
    manifest_sha256: str
    total_decisions: int
    default_decision_count: int
    exception_count: int
    conflict_count: int
    blocker_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, EntityId) or self.run_id.namespace != "data_resolution":
            raise ValueError("run_id must use data_resolution namespace")
        if self.dataset is not DatasetKind.DAILY_BAR_UNADJUSTED:
            raise ValueError("resolution record supports daily_bar_unadjusted only")
        if not isinstance(self.created_job_id, EntityId) or self.created_job_id.namespace != "job":
            raise ValueError("created_job_id must use job namespace")
        if self.candidate_version_id is not None and (
            not isinstance(self.candidate_version_id, EntityId)
            or self.candidate_version_id.namespace != "data_version"
        ):
            raise ValueError("candidate_version_id must use data_version namespace")
        if not isinstance(self.created_at, UtcTimestamp):
            raise TypeError("created_at must be UtcTimestamp")
        if self.rule_version != RESOLUTION_RULE_VERSION:
            raise ValueError("rule_version is unsupported")
        if self.threshold_rule_version != THRESHOLD_RULE_VERSION:
            raise ValueError("threshold_rule_version is unsupported")
        for value, field in (
            (self.input_fingerprint, "input_fingerprint"),
            (self.manifest_sha256, "manifest_sha256"),
        ):
            if _SHA256.fullmatch(value) is None:
                raise ValueError(f"{field} must be lowercase SHA-256")
        path = PurePosixPath(self.artifact_relative_path)
        if (
            not self.artifact_relative_path
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in self.artifact_relative_path
        ):
            raise ValueError("artifact_relative_path must be a managed relative path")
        for field in (
            "total_decisions",
            "default_decision_count",
            "exception_count",
            "conflict_count",
            "blocker_count",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be non-negative")
        if self.total_decisions != (
            self.default_decision_count + self.exception_count + self.conflict_count
        ):
            raise ValueError("resolution counts do not add up")
        blocked = self.conflict_count > 0 or self.blocker_count > 0
        if (blocked and self.candidate_version_id is not None) or (
            not blocked and self.candidate_version_id is None
        ):
            raise ValueError("candidate binding conflicts with resolution outcome")

    @property
    def status(self) -> ResolutionRunStatus:
        if self.candidate_version_id is None:
            return ResolutionRunStatus.BLOCKED
        if self.total_decisions == 0:
            return ResolutionRunStatus.EMPTY_CONSENSUS
        return ResolutionRunStatus.RESOLVED


def json_key(binding: ResolutionSourceBinding) -> tuple[str, str, str, str]:
    """Stable uniqueness key without serializing third-party values."""
    return (
        binding.upstream.value,
        binding.status.value,
        "" if binding.batch_id is None else str(binding.batch_id),
        binding.content_sha256 or "",
    )


__all__ = [
    "RESOLUTION_RULE_VERSION",
    "CRequestTrace",
    "ResolutionDecision",
    "ResolutionDefaultSummary",
    "ResolutionReasonCode",
    "ResolutionRun",
    "ResolutionRunBlocker",
    "ResolutionRunRecord",
    "ResolutionRunStatus",
    "ResolutionSourceBinding",
    "ResolutionSourceStatus",
]
