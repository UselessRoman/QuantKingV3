"""Deterministic fixed-role BaoStock/AKShare four-source resolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from itertools import combinations
from types import MappingProxyType

from quantking.common.ids import EntityId, new_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.canonical_schema import canonical_table_from_rows
from quantking.data.adapters.sources.registry import PRODUCTION_UPSTREAMS
from quantking.data.comparison.thresholds import THRESHOLD_RULE_VERSION
from quantking.data.models import DatasetKind, DataUpstream
from quantking.data.resolution.models import (
    K3_RESOLUTION_RULE_VERSION,
    ResolutionDecision,
    ResolutionDefaultSummary,
    ResolutionReasonCode,
    ResolutionRequestTrace,
    ResolutionRun,
    ResolutionRunBlocker,
    ResolutionSourceBinding,
    ResolutionSourceStatus,
)
from quantking.data.resolution.service import (
    ResolutionDifferencePort,
    ResolutionKey,
    ResolutionOutcome,
    ResolutionRequest,
    ResolutionSourcePort,
    SourceCandidate,
)

_FIELDS = ("open", "high", "low", "close", "pre_close", "volume", "amount")
_SOURCE_ORDER = PRODUCTION_UPSTREAMS
_A, _B, _C, _D = _SOURCE_ORDER
_USABLE = {ResolutionSourceStatus.AVAILABLE, ResolutionSourceStatus.EMPTY}
_COMPARABLE = {
    ResolutionSourceStatus.AVAILABLE,
    ResolutionSourceStatus.EMPTY,
    ResolutionSourceStatus.MISSING_ROW,
}


@dataclass(frozen=True, slots=True)
class _ExceptionalCall:
    candidate: SourceCandidate
    trace: ResolutionRequestTrace


@dataclass(frozen=True, slots=True)
class _ReportScope:
    report_id: EntityId
    keys: frozenset[ResolutionKey]


class K3SourceResolutionService:
    """Resolve A/B normally, then call C and D only for unresolved spans."""

    def __init__(
        self,
        *,
        sources: ResolutionSourcePort,
        differences: ResolutionDifferencePort,
    ) -> None:
        self._sources = sources
        self._differences = differences

    def resolve(self, request: ResolutionRequest) -> ResolutionOutcome:
        if not isinstance(request, ResolutionRequest):
            raise TypeError("request must be ResolutionRequest")
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
            if _pair_agrees(a, b, key, rows_by_call) and rows_by_call[id(a)].get(key) is not None
        }
        affected = keys - default_keys
        reports: list[_ReportScope] = []
        self._record_pair(a, b, tuple(sorted(keys, key=_key_order)), request.created_at, reports)

        c_calls = self._collect_exceptional(
            upstream=_C,
            request=request,
            affected=affected,
            observed_keys=keys,
            full_range=(a.status not in _USABLE or b.status not in _USABLE),
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

        unresolved_after_c = {
            key
            for key in keys
            if key not in default_keys
            and not self._has_consensus(key, (a, b), c_calls, (), rows_by_call)
        }
        empty_before_d = _empty_upstreams((a, b), c_calls, ())
        d_full_range = (a.status not in _USABLE and b.status not in _USABLE) or (
            not keys and len(empty_before_d) < 2
        )
        d_calls = self._collect_exceptional(
            upstream=_D,
            request=request,
            affected=unresolved_after_c,
            observed_keys=keys,
            full_range=d_full_range,
            trigger_reason="ABC_NO_CONSENSUS",
            candidate_version_id=request.candidate_version_id,
        )
        for call in d_calls:
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
            for c_call in c_calls:
                common_scope = {key for key in call_keys if _trace_covers(c_call.trace, key)}
                self._record_pair(
                    c_call.candidate,
                    call.candidate,
                    tuple(sorted(common_scope, key=_key_order)),
                    request.created_at,
                    reports,
                )

        decisions: list[ResolutionDecision] = []
        conflicts: list[ResolutionDecision] = []
        selected_rows = [
            dict(rows_by_call[id(a)][key]) for key in sorted(default_keys, key=_key_order)
        ]
        for key in sorted(keys - default_keys, key=_key_order):
            bindings, row_by_source, traces = self._row_inputs(
                key, a, b, c_calls, d_calls, rows_by_call
            )
            compared = tuple(
                binding.upstream for binding in bindings if binding.status in _COMPARABLE
            )
            agreeing, selected, reason = _agreement(compared, row_by_source)
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
                rule_version=K3_RESOLUTION_RULE_VERSION,
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

        input_bindings = _input_bindings(a, b, c_calls, d_calls)
        empty_upstreams = _empty_upstreams((a, b), c_calls, d_calls)
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
        default_summary = None
        if default_keys:
            default_summary = ResolutionDefaultSummary(
                source_bindings=(
                    a.binding,
                    b.binding,
                    _not_called(_C),
                    _not_called(_D),
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
            rule_version=K3_RESOLUTION_RULE_VERSION,
            threshold_rule_version=THRESHOLD_RULE_VERSION,
            input_fingerprint=request.input_fingerprint,
            candidate_version_id=candidate_version_id,
            input_bindings=input_bindings,
            difference_report_ids=tuple(sorted({report.report_id for report in reports}, key=str)),
            default_summary=default_summary,
            run_blockers=blockers,
            exceptions=tuple(decisions),
            conflicts=tuple(conflicts),
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
        upstream: DataUpstream,
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
                    upstream=upstream,
                )
                for symbol in request.symbols
            )
            if full_range
            else _affected_traces(upstream, affected, observed_keys)
        )
        preflight = getattr(self._sources, "preflight_exception_requests", None)
        if traces and callable(preflight):
            preflight(upstream, traces)
        calls: list[_ExceptionalCall] = []
        for trace in traces:
            candidate = self._fetch(
                upstream,
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
    def _has_consensus(
        key: ResolutionKey,
        base: tuple[SourceCandidate, SourceCandidate],
        c_calls: tuple[_ExceptionalCall, ...],
        d_calls: tuple[_ExceptionalCall, ...],
        rows_by_call: Mapping[int, Mapping[ResolutionKey, Mapping[str, object]]],
    ) -> bool:
        bindings, rows, _traces = K3SourceResolutionService._row_inputs(
            key, base[0], base[1], c_calls, d_calls, rows_by_call
        )
        comparable = tuple(
            binding.upstream for binding in bindings if binding.status in _COMPARABLE
        )
        agreeing, _selected, _reason = _agreement(comparable, rows)
        return len(agreeing) >= 2

    @staticmethod
    def _row_inputs(
        key: ResolutionKey,
        a: SourceCandidate,
        b: SourceCandidate,
        c_calls: tuple[_ExceptionalCall, ...],
        d_calls: tuple[_ExceptionalCall, ...],
        rows_by_call: Mapping[int, Mapping[ResolutionKey, Mapping[str, object]]],
    ) -> tuple[
        tuple[ResolutionSourceBinding, ...],
        Mapping[DataUpstream, Mapping[str, object] | None],
        tuple[ResolutionRequestTrace, ...],
    ]:
        exceptional = {
            _C: _call_for_key(c_calls, key),
            _D: _call_for_key(d_calls, key),
        }
        c_call = exceptional[_C]
        d_call = exceptional[_D]
        candidates: dict[DataUpstream, SourceCandidate | None] = {
            _A: a,
            _B: b,
            _C: None if c_call is None else c_call.candidate,
            _D: None if d_call is None else d_call.candidate,
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
            call = exceptional.get(upstream)
            if call is not None:
                traces.append(call.trace)
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
) -> bool:
    if left.status not in _USABLE or right.status not in _USABLE:
        return False
    return _equal(rows_by_call[id(left)].get(key), rows_by_call[id(right)].get(key))


def _equal(
    left: Mapping[str, object] | None,
    right: Mapping[str, object] | None,
) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return all(left[field] == right[field] for field in _FIELDS)


def _agreement(
    comparable: tuple[DataUpstream, ...],
    rows: Mapping[DataUpstream, Mapping[str, object] | None],
) -> tuple[tuple[DataUpstream, ...], DataUpstream | None, ResolutionReasonCode]:
    pairs = tuple(
        (left, right)
        for left, right in combinations(comparable, 2)
        if _equal(rows[left], rows[right])
    )
    if not pairs:
        return (), None, ResolutionReasonCode.NO_TWO_SOURCE_CONSENSUS
    agreeing = tuple(source for source in _SOURCE_ORDER if any(source in pair for pair in pairs))
    selected = agreeing[0]
    if agreeing == (_A, _B, _C):
        reason = ResolutionReasonCode.ABC_AGREE_A_SELECTED
    elif _A in agreeing and _B in agreeing:
        reason = ResolutionReasonCode.AB_AGREE_A_SELECTED
    elif _A in agreeing and _C in agreeing:
        reason = ResolutionReasonCode.AC_AGREE_A_SELECTED
    elif _B in agreeing and _C in agreeing:
        reason = ResolutionReasonCode.BC_AGREE_B_SELECTED
    elif _A in agreeing and _D in agreeing:
        reason = ResolutionReasonCode.AD_AGREE_A_SELECTED
    elif _B in agreeing and _D in agreeing:
        reason = ResolutionReasonCode.BD_AGREE_B_SELECTED
    else:
        reason = ResolutionReasonCode.CD_AGREE_C_SELECTED
    return agreeing, selected, reason


def _affected_traces(
    upstream: DataUpstream,
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
                    trigger_reason=("AB_AFFECTED_RANGE" if upstream is _C else "ABC_NO_CONSENSUS"),
                    upstream=upstream,
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
    d_calls: tuple[_ExceptionalCall, ...],
) -> frozenset[DataUpstream]:
    candidates = (
        *base,
        *(item.candidate for item in c_calls),
        *(item.candidate for item in d_calls),
    )
    return frozenset(
        candidate.upstream
        for candidate in candidates
        if candidate.status is ResolutionSourceStatus.EMPTY
    )


def _input_bindings(
    a: SourceCandidate,
    b: SourceCandidate,
    c_calls: tuple[_ExceptionalCall, ...],
    d_calls: tuple[_ExceptionalCall, ...],
) -> tuple[ResolutionSourceBinding, ...]:
    bindings = [a.binding, b.binding]
    if c_calls:
        bindings.extend(item.candidate.binding for item in c_calls)
    else:
        bindings.append(_not_called(_C))
    if d_calls:
        bindings.extend(item.candidate.binding for item in d_calls)
    else:
        bindings.append(_not_called(_D))
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


__all__ = ["K3SourceResolutionService"]
