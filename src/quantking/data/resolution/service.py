"""Deterministic ADR-021 three-role resolution: BaoStock A, Tencent B, Sina C.

Normal tasks collect A and B over the full request range. C (Sina) is called
only for affected symbol/date spans — or the full range when A or B is
unusable — and stays 0 planned/actual when A and B agree. Row comparison uses
the same versioned Decimal ``ThresholdSet`` as the DifferenceReport layer;
common missing rows never form a value consensus, a row held by exactly one
source is isolated, and multi-source legal whole-range empty results carry an
independent zero-row reason code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from itertools import combinations
from types import MappingProxyType
from typing import Protocol

from quantking.common.ids import EntityId, new_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.canonical_schema import (
    CanonicalTable,
    canonical_table_from_rows,
    validate_table,
)
from quantking.data.comparison.thresholds import (
    ThresholdSet,
    default_thresholds,
    rows_within_threshold,
)
from quantking.data.content import canonical_content_sha256
from quantking.data.models import (
    DatasetKind,
    DataUpstream,
    EmptyResultInfo,
    LegalEmptyClassification,
)
from quantking.data.resolution.models import (
    K4_RESOLUTION_RULE_VERSION,
    ResolutionDecision,
    ResolutionDefaultSummary,
    ResolutionReasonCode,
    ResolutionRequestTrace,
    ResolutionRun,
    ResolutionRunBlocker,
    ResolutionSourceBinding,
    ResolutionSourceStatus,
)

_FIELDS = ("open", "high", "low", "close", "pre_close", "volume", "amount")
_SOURCE_ORDER = (
    DataUpstream.BAOSTOCK,
    DataUpstream.TENCENT,
    DataUpstream.SINA,
)
_A, _B, _C = _SOURCE_ORDER
_USABLE = {ResolutionSourceStatus.AVAILABLE, ResolutionSourceStatus.EMPTY}
_COMPARABLE_ROW = {
    ResolutionSourceStatus.AVAILABLE,
    ResolutionSourceStatus.MISSING_ROW,
}


@dataclass(frozen=True, slots=True)
class ResolutionKey:
    symbol: str
    trade_date: date

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("symbol must be non-empty text")
        if not isinstance(self.trade_date, date):
            raise TypeError("trade_date must be a date")


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    """One actual source call outcome; failed calls never carry invented payload SHA."""

    upstream: DataUpstream
    status: ResolutionSourceStatus
    symbols: tuple[str, ...]
    start_date: date
    end_date: date
    batch_id: EntityId | None
    content_sha256: str | None
    version_id: EntityId | None
    quality_run_id: EntityId | None
    table: CanonicalTable | None
    empty_result: EmptyResultInfo | None

    def __post_init__(self) -> None:
        if not isinstance(self.upstream, DataUpstream):
            raise TypeError("upstream must be DataUpstream")
        if self.status is ResolutionSourceStatus.NOT_CALLED:
            raise ValueError("SourceCandidate represents an actual call")
        if self.status is ResolutionSourceStatus.MISSING_ROW:
            raise ValueError("MISSING_ROW is derived per row, not returned by a source call")
        if (
            not isinstance(self.symbols, tuple)
            or not self.symbols
            or tuple(sorted(set(self.symbols))) != self.symbols
        ):
            raise ValueError("symbols must be a sorted unique non-empty tuple")
        if not isinstance(self.start_date, date) or not isinstance(self.end_date, date):
            raise TypeError("request dates must be date values")
        if self.start_date > self.end_date:
            raise ValueError("request start must not follow end")
        ResolutionSourceBinding(
            upstream=self.upstream,
            status=self.status,
            batch_id=self.batch_id,
            content_sha256=self.content_sha256,
        )
        if self.version_id is not None and (
            not isinstance(self.version_id, EntityId) or self.version_id.namespace != "data_version"
        ):
            raise ValueError("version_id must use data_version namespace")
        if self.quality_run_id is not None and (
            not isinstance(self.quality_run_id, EntityId)
            or self.quality_run_id.namespace != "data_quality"
        ):
            raise ValueError("quality_run_id must use data_quality namespace")
        if self.quality_run_id is not None and self.version_id is None:
            raise ValueError("quality_run_id requires version_id")
        if self.status in _USABLE:
            if self.table is None or self.version_id is None or self.quality_run_id is None:
                raise ValueError("usable candidate requires table/version/quality bindings")
            if self.status is ResolutionSourceStatus.AVAILABLE and self.empty_result is not None:
                raise ValueError("AVAILABLE source candidate cannot carry empty_result")
            if self.status is ResolutionSourceStatus.EMPTY:
                if not isinstance(self.empty_result, EmptyResultInfo):
                    raise ValueError("EMPTY source candidate requires EmptyResultInfo")
                if (
                    self.empty_result.classification
                    is not LegalEmptyClassification.NO_TRADING_SESSION
                ):
                    raise ValueError("EMPTY source candidate requires NO_TRADING_SESSION")
            validation = validate_table(
                DatasetKind.DAILY_BAR_UNADJUSTED.value,
                self.table,
                empty_result=self.empty_result,
            )
            if not validation.ok:
                raise ValueError("usable source table is not canonical")
            actual = canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED,
                [self.table],
                empty_result=self.empty_result,
            )
            if actual != self.content_sha256:
                raise ValueError("candidate content SHA does not match canonical table")
            if self.status is ResolutionSourceStatus.AVAILABLE and self.table.num_rows == 0:
                raise ValueError("AVAILABLE source candidate must contain rows")
            if self.status is ResolutionSourceStatus.EMPTY and self.table.num_rows != 0:
                raise ValueError("EMPTY source candidate must not contain rows")
            for row in self.table.select(["code", "trade_date"]).to_pylist():
                if row["code"] not in self.symbols:
                    raise ValueError("source table contains a symbol outside its request")
                if not self.start_date <= row["trade_date"] <= self.end_date:
                    raise ValueError("source table contains a date outside its request")
        else:
            if self.empty_result is not None:
                raise ValueError("failed source candidate cannot carry empty_result")
            if self.table is not None:
                raise ValueError("non-AVAILABLE source candidate must not expose rows")
            if self.status is ResolutionSourceStatus.QUALITY_FAILED and (
                self.version_id is None or self.quality_run_id is None
            ):
                raise ValueError("QUALITY_FAILED requires version and quality bindings")

    @property
    def binding(self) -> ResolutionSourceBinding:
        return ResolutionSourceBinding(
            upstream=self.upstream,
            status=self.status,
            batch_id=self.batch_id,
            content_sha256=self.content_sha256,
        )


class ResolutionSourcePort(Protocol):
    def fetch(
        self,
        *,
        upstream: DataUpstream,
        symbols: tuple[str, ...],
        start_date: date,
        end_date: date,
        trigger_reason: str,
    ) -> SourceCandidate: ...


class ResolutionDifferencePort(Protocol):
    def record(
        self,
        *,
        primary: SourceCandidate,
        fallback: SourceCandidate,
        keys: tuple[ResolutionKey, ...],
        created_at: UtcTimestamp,
    ) -> EntityId: ...


@dataclass(frozen=True, slots=True)
class ResolutionRequest:
    created_job_id: EntityId
    candidate_version_id: EntityId
    created_at: UtcTimestamp
    input_fingerprint: str
    symbols: tuple[str, ...]
    start_date: date
    end_date: date

    def __post_init__(self) -> None:
        if not isinstance(self.created_job_id, EntityId) or self.created_job_id.namespace != "job":
            raise ValueError("created_job_id must use job namespace")
        if (
            not isinstance(self.candidate_version_id, EntityId)
            or self.candidate_version_id.namespace != "data_version"
        ):
            raise ValueError("candidate_version_id must use data_version namespace")
        if not isinstance(self.created_at, UtcTimestamp):
            raise TypeError("created_at must be UtcTimestamp")
        if len(self.input_fingerprint) != 64 or any(
            item not in "0123456789abcdef" for item in self.input_fingerprint
        ):
            raise ValueError("input_fingerprint must be lowercase SHA-256")
        if (
            not isinstance(self.symbols, tuple)
            or not self.symbols
            or tuple(sorted(set(self.symbols))) != self.symbols
        ):
            raise ValueError("symbols must be a sorted unique non-empty tuple")
        if not isinstance(self.start_date, date) or not isinstance(self.end_date, date):
            raise TypeError("request dates must be date values")
        if self.start_date > self.end_date:
            raise ValueError("request start must not follow end")


@dataclass(frozen=True, slots=True)
class ResolutionOutcome:
    run: ResolutionRun
    selected_table: CanonicalTable | None

    @property
    def publishable(self) -> bool:
        return self.selected_table is not None and self.run.candidate_version_id is not None


@dataclass(frozen=True, slots=True)
class _ExceptionalCall:
    candidate: SourceCandidate
    trace: ResolutionRequestTrace


@dataclass(frozen=True, slots=True)
class _ReportScope:
    report_id: EntityId
    keys: frozenset[ResolutionKey]


class SourceResolutionService:
    """Collect A/B, call C only for affected spans, and resolve every held row."""

    def __init__(
        self,
        *,
        sources: ResolutionSourcePort,
        differences: ResolutionDifferencePort,
        thresholds: ThresholdSet | None = None,
    ) -> None:
        self._sources = sources
        self._differences = differences
        self._thresholds = thresholds or default_thresholds()
        if not isinstance(self._thresholds, ThresholdSet):
            raise TypeError("thresholds must be a ThresholdSet")

    def resolve(self, request: ResolutionRequest) -> ResolutionOutcome:
        if not isinstance(request, ResolutionRequest):
            raise TypeError("request must be ResolutionRequest")
        thresholds = self._thresholds
        a = self._fetch(
            _A,
            symbols=request.symbols,
            start_date=request.start_date,
            end_date=request.end_date,
            trigger_reason="PRIMARY_COLLECTION",
            candidate_version_id=request.candidate_version_id,
        )
        b = self._fetch(
            _B,
            symbols=request.symbols,
            start_date=request.start_date,
            end_date=request.end_date,
            trigger_reason="SECONDARY_COLLECTION",
            candidate_version_id=request.candidate_version_id,
        )
        rows_by_call: dict[int, Mapping[ResolutionKey, Mapping[str, object]]] = {
            id(a): _rows(a),
            id(b): _rows(b),
        }
        keys = set(rows_by_call[id(a)]) | set(rows_by_call[id(b)])
        default_keys = {
            key
            for key in keys
            if _pair_agrees(a, b, key, rows_by_call, thresholds)
            and rows_by_call[id(a)].get(key) is not None
        }
        affected = keys - default_keys
        reports: list[_ReportScope] = []
        self._record_pair(a, b, tuple(sorted(keys, key=_key_order)), request.created_at, reports)

        full_range = a.status not in _USABLE or b.status not in _USABLE
        c_calls = self._collect_exceptional(
            request=request,
            affected=affected,
            observed_keys=keys,
            full_range=full_range,
            trigger_reason="AB_SOURCE_UNAVAILABLE",
            candidate_version_id=request.candidate_version_id,
        )
        for call in c_calls:
            rows_by_call[id(call.candidate)] = _rows(call.candidate)
            call_keys = set(rows_by_call[id(call.candidate)]) | {
                key for key in keys if _trace_covers(call.trace, key)
            }
            keys.update(call_keys)
            for primary in (a, b):
                self._record_pair(
                    primary,
                    call.candidate,
                    tuple(sorted(call_keys, key=_key_order)),
                    request.created_at,
                    reports,
                )

        decisions: list[ResolutionDecision] = []
        conflicts: list[ResolutionDecision] = []
        selected_rows = [
            dict(rows_by_call[id(a)][key]) for key in sorted(default_keys, key=_key_order)
        ]
        for key in sorted(keys - default_keys, key=_key_order):
            bindings, row_by_source, traces = self._row_inputs(key, a, b, c_calls, rows_by_call)
            compared = tuple(
                binding.upstream for binding in bindings if binding.status in _COMPARABLE_ROW
            )
            agreeing, selected, reason = _agreement(compared, row_by_source, thresholds)
            if reason is None:
                held = tuple(
                    source for source in _SOURCE_ORDER if row_by_source.get(source) is not None
                )
                reason = (
                    ResolutionReasonCode.SINGLE_SOURCE_ROW_ISOLATED
                    if len(held) == 1
                    else ResolutionReasonCode.NO_TWO_SOURCE_CONSENSUS
                )
            decision_report_ids = tuple(
                sorted(
                    {report.report_id for report in reports if key in report.keys},
                    key=str,
                )
            )
            decision = ResolutionDecision(
                symbol=key.symbol,
                trade_date=key.trade_date,
                source_bindings=bindings,
                compared_upstreams=compared,
                agreeing_upstreams=agreeing,
                selected_upstream=selected,
                selected_row_present=(
                    None if selected is None else row_by_source[selected] is not None
                ),
                reason_code=reason,
                rule_version=K4_RESOLUTION_RULE_VERSION,
                threshold_rule_version=thresholds.rule_version,
                difference_report_ids=decision_report_ids,
                created_job_id=request.created_job_id,
                created_at=request.created_at,
                request_traces=traces,
            )
            if decision.conflict:
                conflicts.append(decision)
            else:
                decisions.append(decision)
                if decision.selected_row_present:
                    if selected is None:
                        raise ValueError("selected row is unavailable")
                    selected_row = row_by_source[selected]
                    if selected_row is None:
                        raise ValueError("selected row is unavailable")
                    selected_rows.append(dict(selected_row))

        input_bindings = _input_bindings(a, b, c_calls)
        empty_upstreams = _empty_upstreams((a, b), c_calls)
        blockers: tuple[ResolutionRunBlocker, ...] = ()
        if not keys and len(empty_upstreams) < 2:
            blockers = (
                ResolutionRunBlocker.INSUFFICIENT_EMPTY_CONSENSUS
                if empty_upstreams
                else ResolutionRunBlocker.ALL_SOURCES_UNAVAILABLE,
            )
        candidate_version_id = (
            request.candidate_version_id if not conflicts and not blockers else None
        )
        zero_row_reason: ResolutionReasonCode | None = None
        if candidate_version_id is not None and not keys:
            zero_row_reason = ResolutionReasonCode.ZERO_ROW_LEGAL_EMPTY_CONSENSUS
        default_summary = None
        if default_keys:
            default_summary = ResolutionDefaultSummary(
                source_bindings=(
                    a.binding,
                    b.binding,
                    _not_called(_C),
                ),
                compared_upstreams=(_A, _B),
                agreeing_upstreams=(_A, _B),
                selected_upstream=_A,
                reason_code=ResolutionReasonCode.AB_AGREE_DEFAULT,
                decision_count=len(default_keys),
            )
        run = ResolutionRun(
            run_id=new_entity_id("data_resolution"),
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            created_job_id=request.created_job_id,
            created_at=request.created_at,
            rule_version=K4_RESOLUTION_RULE_VERSION,
            threshold_rule_version=thresholds.rule_version,
            input_fingerprint=request.input_fingerprint,
            candidate_version_id=candidate_version_id,
            input_bindings=input_bindings,
            difference_report_ids=tuple(sorted({report.report_id for report in reports}, key=str)),
            default_summary=default_summary,
            run_blockers=blockers,
            exceptions=tuple(decisions),
            conflicts=tuple(conflicts),
            zero_row_reason_code=zero_row_reason,
        )
        table = None
        if candidate_version_id is not None:
            table = canonical_table_from_rows(
                DatasetKind.DAILY_BAR_UNADJUSTED.value,
                selected_rows,
            )
        return ResolutionOutcome(run=run, selected_table=table)

    def _collect_exceptional(
        self,
        *,
        request: ResolutionRequest,
        affected: set[ResolutionKey],
        observed_keys: set[ResolutionKey],
        full_range: bool,
        trigger_reason: str,
        candidate_version_id: EntityId,
    ) -> tuple[_ExceptionalCall, ...]:
        traces = (
            tuple(
                ResolutionRequestTrace(
                    symbol=symbol,
                    start_date=request.start_date,
                    end_date=request.end_date,
                    trigger_reason=trigger_reason,
                    upstream=_C,
                )
                for symbol in request.symbols
            )
            if full_range
            else _affected_traces(affected, observed_keys)
        )
        preflight = getattr(self._sources, "preflight_exception_requests", None)
        if traces and callable(preflight):
            preflight(_C, traces)
        calls: list[_ExceptionalCall] = []
        for trace in traces:
            candidate = self._fetch(
                _C,
                symbols=(trace.symbol,),
                start_date=trace.start_date,
                end_date=trace.end_date,
                trigger_reason=trace.trigger_reason,
                candidate_version_id=candidate_version_id,
            )
            calls.append(_ExceptionalCall(candidate, trace))
        return tuple(calls)

    def _fetch(
        self,
        upstream: DataUpstream,
        *,
        symbols: tuple[str, ...],
        start_date: date,
        end_date: date,
        trigger_reason: str,
        candidate_version_id: EntityId,
    ) -> SourceCandidate:
        candidate = self._sources.fetch(
            upstream=upstream,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            trigger_reason=trigger_reason,
        )
        if (
            not isinstance(candidate, SourceCandidate)
            or candidate.upstream is not upstream
            or candidate.symbols != symbols
            or candidate.start_date != start_date
            or candidate.end_date != end_date
            or (
                candidate.status
                in {
                    ResolutionSourceStatus.AVAILABLE,
                    ResolutionSourceStatus.EMPTY,
                    ResolutionSourceStatus.QUALITY_FAILED,
                }
                and candidate.version_id != candidate_version_id
            )
        ):
            raise ValueError("source port returned a mismatched request binding")
        return candidate

    def _record_pair(
        self,
        left: SourceCandidate,
        right: SourceCandidate,
        keys: tuple[ResolutionKey, ...],
        created_at: UtcTimestamp,
        reports: list[_ReportScope],
    ) -> None:
        if left.status not in _USABLE or right.status not in _USABLE:
            return
        report_id = self._differences.record(
            primary=left,
            fallback=right,
            keys=keys,
            created_at=created_at,
        )
        if not isinstance(report_id, EntityId) or report_id.namespace != "data_diff":
            raise ValueError("difference port must return a data_diff identifier")
        reports.append(_ReportScope(report_id, frozenset(keys)))

    @staticmethod
    def _row_inputs(
        key: ResolutionKey,
        a: SourceCandidate,
        b: SourceCandidate,
        c_calls: tuple[_ExceptionalCall, ...],
        rows_by_call: Mapping[int, Mapping[ResolutionKey, Mapping[str, object]]],
    ) -> tuple[
        tuple[ResolutionSourceBinding, ...],
        Mapping[DataUpstream, Mapping[str, object] | None],
        tuple[ResolutionRequestTrace, ...],
    ]:
        c_call = _call_for_key(c_calls, key)
        candidates: dict[DataUpstream, SourceCandidate | None] = {
            _A: a,
            _B: b,
            _C: None if c_call is None else c_call.candidate,
        }
        bindings: list[ResolutionSourceBinding] = []
        rows: dict[DataUpstream, Mapping[str, object] | None] = {}
        traces: list[ResolutionRequestTrace] = []
        for upstream in _SOURCE_ORDER:
            candidate = candidates[upstream]
            if candidate is None:
                bindings.append(_not_called(upstream))
                continue
            candidate_rows = rows_by_call[id(candidate)]
            bindings.append(_row_binding(candidate, key, candidate_rows))
            if candidate.status in _USABLE:
                rows[upstream] = candidate_rows.get(key)
            if upstream is _C and c_call is not None:
                traces.append(c_call.trace)
        return tuple(bindings), MappingProxyType(rows), tuple(traces)


def _rows(candidate: SourceCandidate) -> Mapping[ResolutionKey, Mapping[str, object]]:
    if candidate.status not in _USABLE or candidate.table is None:
        return MappingProxyType({})
    rows: dict[ResolutionKey, Mapping[str, object]] = {}
    for row in candidate.table.to_pylist():
        key = ResolutionKey(str(row["code"]), row["trade_date"])
        if key in rows:
            raise ValueError("source table contains duplicate resolution key")
        rows[key] = MappingProxyType(dict(row))
    return MappingProxyType(rows)


def _row_binding(
    candidate: SourceCandidate,
    key: ResolutionKey,
    rows: Mapping[ResolutionKey, Mapping[str, object]],
) -> ResolutionSourceBinding:
    status = candidate.status
    if status is ResolutionSourceStatus.AVAILABLE and key not in rows:
        status = ResolutionSourceStatus.MISSING_ROW
    return ResolutionSourceBinding(
        upstream=candidate.upstream,
        status=status,
        batch_id=candidate.batch_id,
        content_sha256=candidate.content_sha256,
    )


def _not_called(upstream: DataUpstream) -> ResolutionSourceBinding:
    return ResolutionSourceBinding(
        upstream=upstream,
        status=ResolutionSourceStatus.NOT_CALLED,
        batch_id=None,
        content_sha256=None,
    )


def _pair_agrees(
    left: SourceCandidate,
    right: SourceCandidate,
    key: ResolutionKey,
    rows_by_call: Mapping[int, Mapping[ResolutionKey, Mapping[str, object]]],
    thresholds: ThresholdSet,
) -> bool:
    if left.status not in _USABLE or right.status not in _USABLE:
        return False
    left_row = rows_by_call[id(left)].get(key)
    right_row = rows_by_call[id(right)].get(key)
    if left_row is None or right_row is None:
        return False
    return rows_within_threshold(thresholds, left_row, right_row, _FIELDS)


def _agreement(
    comparable: tuple[DataUpstream, ...],
    rows: Mapping[DataUpstream, Mapping[str, object] | None],
    thresholds: ThresholdSet,
) -> tuple[tuple[DataUpstream, ...], DataUpstream | None, ResolutionReasonCode | None]:
    """Agree only on actually-held rows under the shared ThresholdSet.

    A missing side never agrees with anything — including another missing
    side — so common absence cannot form a value consensus.
    """
    held = tuple(source for source in comparable if rows[source] is not None)
    pairs = tuple(
        (left, right)
        for left, right in combinations(held, 2)
        if rows_within_threshold(thresholds, rows[left], rows[right], _FIELDS)
    )
    if not pairs:
        return (), None, None
    agreeing = tuple(source for source in _SOURCE_ORDER if any(source in pair for pair in pairs))
    selected = agreeing[0]
    if agreeing == _SOURCE_ORDER:
        reason = ResolutionReasonCode.ABC_AGREE_A_SELECTED
    elif _A in agreeing and _B in agreeing:
        reason = ResolutionReasonCode.AB_AGREE_A_SELECTED
    elif _A in agreeing and _C in agreeing:
        reason = ResolutionReasonCode.AC_AGREE_A_SELECTED
    else:
        reason = ResolutionReasonCode.BC_AGREE_B_SELECTED
    return agreeing, selected, reason


def _affected_traces(
    affected: set[ResolutionKey],
    observed_keys: set[ResolutionKey],
) -> tuple[ResolutionRequestTrace, ...]:
    traces: list[ResolutionRequestTrace] = []
    by_symbol: dict[str, list[ResolutionKey]] = {}
    for key in sorted(observed_keys, key=_key_order):
        by_symbol.setdefault(key.symbol, []).append(key)
    for symbol, keys in by_symbol.items():
        current: list[ResolutionKey] = []
        spans: list[tuple[ResolutionKey, ...]] = []
        for key in keys:
            if key in affected:
                current.append(key)
            elif current:
                spans.append(tuple(current))
                current = []
        if current:
            spans.append(tuple(current))
        for span in spans:
            traces.append(
                ResolutionRequestTrace(
                    symbol=symbol,
                    start_date=span[0].trade_date,
                    end_date=span[-1].trade_date,
                    trigger_reason="AB_AFFECTED_RANGE",
                    upstream=_C,
                )
            )
    return tuple(traces)


def _trace_covers(trace: ResolutionRequestTrace, key: ResolutionKey) -> bool:
    return trace.symbol == key.symbol and trace.start_date <= key.trade_date <= trace.end_date


def _call_for_key(
    calls: tuple[_ExceptionalCall, ...], key: ResolutionKey
) -> _ExceptionalCall | None:
    matches = tuple(call for call in calls if _trace_covers(call.trace, key))
    if len(matches) > 1:
        raise ValueError("exceptional request traces overlap")
    return None if not matches else matches[0]


def _empty_upstreams(
    base: tuple[SourceCandidate, SourceCandidate],
    c_calls: tuple[_ExceptionalCall, ...],
) -> frozenset[DataUpstream]:
    candidates = (*base, *(item.candidate for item in c_calls))
    return frozenset(
        candidate.upstream
        for candidate in candidates
        if candidate.status is ResolutionSourceStatus.EMPTY
    )


def _input_bindings(
    a: SourceCandidate,
    b: SourceCandidate,
    c_calls: tuple[_ExceptionalCall, ...],
) -> tuple[ResolutionSourceBinding, ...]:
    bindings = [a.binding, b.binding]
    if c_calls:
        bindings.extend(item.candidate.binding for item in c_calls)
    else:
        bindings.append(_not_called(_C))
    unique = {
        (
            binding.upstream.value,
            binding.status.value,
            "" if binding.batch_id is None else str(binding.batch_id),
            binding.content_sha256 or "",
        ): binding
        for binding in bindings
    }
    rank = {source: index for index, source in enumerate(_SOURCE_ORDER)}
    return tuple(
        sorted(
            unique.values(),
            key=lambda binding: (
                rank[binding.upstream],
                "" if binding.batch_id is None else str(binding.batch_id),
                binding.status.value,
                binding.content_sha256 or "",
            ),
        )
    )


def _key_order(key: ResolutionKey) -> tuple[str, date]:
    return key.symbol, key.trade_date


__all__ = [
    "ResolutionDifferencePort",
    "ResolutionKey",
    "ResolutionOutcome",
    "ResolutionRequest",
    "ResolutionSourcePort",
    "SourceCandidate",
    "SourceResolutionService",
]
