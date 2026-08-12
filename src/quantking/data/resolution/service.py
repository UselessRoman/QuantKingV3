"""Deterministic A/B/C arbitration with C restricted to affected symbol/date spans."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Protocol

from quantking.common.ids import EntityId, new_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.canonical_schema import (
    CanonicalTable,
    canonical_table_from_rows,
    validate_table,
)
from quantking.data.comparison.thresholds import THRESHOLD_RULE_VERSION
from quantking.data.content import canonical_content_sha256
from quantking.data.models import (
    DatasetKind,
    DataUpstream,
    EmptyResultInfo,
    LegalEmptyClassification,
)
from quantking.data.resolution.models import (
    RESOLUTION_RULE_VERSION,
    CRequestTrace,
    ResolutionDecision,
    ResolutionDefaultSummary,
    ResolutionReasonCode,
    ResolutionRun,
    ResolutionRunBlocker,
    ResolutionSourceBinding,
    ResolutionSourceStatus,
)

_FIELDS = ("open", "high", "low", "close", "pre_close", "volume", "amount")
_SOURCE_ORDER = (DataUpstream.EASTMONEY, DataUpstream.TENCENT, DataUpstream.SINA)
_USABLE_CALL_STATUSES = {
    ResolutionSourceStatus.AVAILABLE,
    ResolutionSourceStatus.EMPTY,
}
_COMPARABLE_ROW_STATUSES = {
    ResolutionSourceStatus.AVAILABLE,
    ResolutionSourceStatus.MISSING_ROW,
    ResolutionSourceStatus.EMPTY,
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
        if self.status in _USABLE_CALL_STATUSES:
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


class SourceResolutionService:
    """Collect A/B, call C only for affected spans, and resolve every expected row."""

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
            DataUpstream.EASTMONEY,
            symbols=request.symbols,
            start_date=request.start_date,
            end_date=request.end_date,
            trigger_reason="PRIMARY_COLLECTION",
            candidate_version_id=request.candidate_version_id,
        )
        b = self._fetch(
            DataUpstream.TENCENT,
            symbols=request.symbols,
            start_date=request.start_date,
            end_date=request.end_date,
            trigger_reason="SECONDARY_COLLECTION",
            candidate_version_id=request.candidate_version_id,
        )
        rows = {a.upstream: _rows(a), b.upstream: _rows(b)}
        observed_keys = tuple(
            sorted(
                set(rows[DataUpstream.EASTMONEY]) | set(rows[DataUpstream.TENCENT]),
                key=lambda item: (item.symbol, item.trade_date),
            )
        )
        affected: dict[ResolutionKey, str] = {}
        for key in observed_keys:
            reason = _ab_trigger(key, a, b, rows)
            if reason is not None:
                affected[key] = reason
        ab_report = self._record_if_comparable(a, b, observed_keys, request.created_at)
        c_by_key: dict[ResolutionKey, tuple[SourceCandidate, CRequestTrace]] = {}
        c_candidates: list[SourceCandidate] = []
        span_reports: dict[int, tuple[EntityId, ...]] = {}
        global_ab_failure = (
            a.status not in _USABLE_CALL_STATUSES or b.status not in _USABLE_CALL_STATUSES
        )
        if global_ab_failure:
            planned_c_requests = tuple(
                CRequestTrace(
                    symbol=symbol,
                    start_date=request.start_date,
                    end_date=request.end_date,
                    trigger_reason="AB_SOURCE_UNAVAILABLE",
                )
                for symbol in request.symbols
            )
        else:
            planned_c_requests = tuple(
                CRequestTrace(
                    symbol=span_keys[0].symbol,
                    start_date=span_keys[0].trade_date,
                    end_date=span_keys[-1].trade_date,
                    trigger_reason="+".join(sorted({affected[item] for item in span_keys})),
                )
                for span_keys in _affected_spans(observed_keys, frozenset(affected))
            )
        preflight_c = getattr(self._sources, "preflight_c_requests", None)
        if callable(preflight_c):
            preflight_c(planned_c_requests)
        if global_ab_failure:
            full_c: dict[str, tuple[SourceCandidate, CRequestTrace]] = {}
            for symbol in request.symbols:
                trigger = "AB_SOURCE_UNAVAILABLE"
                c = self._fetch(
                    DataUpstream.SINA,
                    symbols=(symbol,),
                    start_date=request.start_date,
                    end_date=request.end_date,
                    trigger_reason=trigger,
                    candidate_version_id=request.candidate_version_id,
                )
                trace = CRequestTrace(
                    symbol=symbol,
                    start_date=request.start_date,
                    end_date=request.end_date,
                    trigger_reason=trigger,
                )
                c_candidates.append(c)
                full_c[symbol] = (c, trace)
            observed_keys = tuple(
                sorted(
                    {
                        *observed_keys,
                        *(key for candidate, _trace in full_c.values() for key in _rows(candidate)),
                    },
                    key=lambda item: (item.symbol, item.trade_date),
                )
            )
            for key in observed_keys:
                affected[key] = "AB_SOURCE_UNAVAILABLE"
                c_by_key[key] = full_c[key.symbol]
            for symbol, (c, _trace) in full_c.items():
                symbol_keys = tuple(key for key in observed_keys if key.symbol == symbol)
                span_reports[id(c)] = self._span_reports(
                    a=a,
                    b=b,
                    c=c,
                    keys=symbol_keys,
                    created_at=request.created_at,
                )
        for span_keys in _affected_spans(observed_keys, frozenset(affected)):
            if span_keys[0] in c_by_key:
                continue
            symbol = span_keys[0].symbol
            span_start = span_keys[0].trade_date
            span_end = span_keys[-1].trade_date
            reasons = sorted({affected[item] for item in span_keys})
            trigger = "+".join(reasons)
            c = self._fetch(
                DataUpstream.SINA,
                symbols=(symbol,),
                start_date=span_start,
                end_date=span_end,
                trigger_reason=trigger,
                candidate_version_id=request.candidate_version_id,
            )
            trace = CRequestTrace(symbol, span_start, span_end, trigger)
            c_candidates.append(c)
            for key in span_keys:
                c_by_key[key] = (c, trace)
            span_reports[id(c)] = self._span_reports(
                a=a,
                b=b,
                c=c,
                keys=span_keys,
                created_at=request.created_at,
            )
        decisions: list[ResolutionDecision] = []
        conflicts: list[ResolutionDecision] = []
        selected_rows: list[dict[str, object]] = []
        default_count = 0
        for key in observed_keys:
            if key not in affected:
                default_count += 1
                selected_rows.append(dict(rows[DataUpstream.EASTMONEY][key]))
                continue
            c, trace = c_by_key[key]
            c_rows = _rows(c)
            bindings = (
                _row_binding(a, key, rows[DataUpstream.EASTMONEY]),
                _row_binding(b, key, rows[DataUpstream.TENCENT]),
                _row_binding(c, key, c_rows),
            )
            row_by_source = {
                DataUpstream.EASTMONEY: rows[DataUpstream.EASTMONEY].get(key),
                DataUpstream.TENCENT: rows[DataUpstream.TENCENT].get(key),
                DataUpstream.SINA: c_rows.get(key),
            }
            compared = tuple(
                item.upstream for item in bindings if item.status in _COMPARABLE_ROW_STATUSES
            )
            agreeing, selected, reason = _agreement(compared, row_by_source)
            decision_reports = tuple(
                sorted(
                    {
                        *(() if ab_report is None else (ab_report,)),
                        *span_reports[id(c)],
                    },
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
                rule_version=RESOLUTION_RULE_VERSION,
                difference_report_ids=decision_reports,
                created_job_id=request.created_job_id,
                created_at=request.created_at,
                c_request=trace,
            )
            if decision.conflict:
                conflicts.append(decision)
            else:
                decisions.append(decision)
                if decision.selected_row_present:
                    if decision.selected_upstream is None:
                        raise ValueError("present decision must select a source")
                    selected_row = row_by_source[decision.selected_upstream]
                    if selected_row is None:
                        raise ValueError("selected source row is unavailable")
                    selected_rows.append(dict(selected_row))
        input_bindings = _input_bindings(a, b, tuple(c_candidates))
        report_ids = tuple(
            sorted(
                {
                    *(() if ab_report is None else (ab_report,)),
                    *(item for reports in span_reports.values() for item in reports),
                },
                key=str,
            )
        )
        empty_consensus = (
            len(
                {
                    item.upstream
                    for item in input_bindings
                    if item.status is ResolutionSourceStatus.EMPTY
                }
            )
            >= 2
        )
        run_blockers: tuple[ResolutionRunBlocker, ...] = ()
        if not observed_keys and not empty_consensus:
            blocker = (
                ResolutionRunBlocker.ALL_SOURCES_UNAVAILABLE
                if not any(item.status is ResolutionSourceStatus.EMPTY for item in input_bindings)
                else ResolutionRunBlocker.INSUFFICIENT_EMPTY_CONSENSUS
            )
            run_blockers = (blocker,)
        candidate_version_id = (
            request.candidate_version_id if not conflicts and not run_blockers else None
        )
        default_summary = None
        if default_count:
            default_summary = ResolutionDefaultSummary(
                source_bindings=(
                    a.binding,
                    b.binding,
                    ResolutionSourceBinding(
                        DataUpstream.SINA,
                        ResolutionSourceStatus.NOT_CALLED,
                        None,
                        None,
                    ),
                ),
                compared_upstreams=(DataUpstream.EASTMONEY, DataUpstream.TENCENT),
                agreeing_upstreams=(DataUpstream.EASTMONEY, DataUpstream.TENCENT),
                selected_upstream=DataUpstream.EASTMONEY,
                reason_code=ResolutionReasonCode.AB_AGREE_DEFAULT,
                decision_count=default_count,
            )
        run = ResolutionRun(
            run_id=new_entity_id("data_resolution"),
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            created_job_id=request.created_job_id,
            created_at=request.created_at,
            rule_version=RESOLUTION_RULE_VERSION,
            threshold_rule_version=THRESHOLD_RULE_VERSION,
            input_fingerprint=request.input_fingerprint,
            candidate_version_id=candidate_version_id,
            input_bindings=input_bindings,
            difference_report_ids=report_ids,
            default_summary=default_summary,
            run_blockers=run_blockers,
            exceptions=tuple(sorted(decisions, key=lambda item: (item.symbol, item.trade_date))),
            conflicts=tuple(sorted(conflicts, key=lambda item: (item.symbol, item.trade_date))),
        )
        table = None
        if candidate_version_id is not None:
            table = canonical_table_from_rows(
                DatasetKind.DAILY_BAR_UNADJUSTED.value,
                selected_rows,
            )
        return ResolutionOutcome(run=run, selected_table=table)

    def _span_reports(
        self,
        *,
        a: SourceCandidate,
        b: SourceCandidate,
        c: SourceCandidate,
        keys: tuple[ResolutionKey, ...],
        created_at: UtcTimestamp,
    ) -> tuple[EntityId, ...]:
        reports: list[EntityId] = []
        for candidate in (a, b):
            report = self._record_if_comparable(candidate, c, keys, created_at)
            if report is not None:
                reports.append(report)
        return tuple(reports)

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
        result = self._sources.fetch(
            upstream=upstream,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            trigger_reason=trigger_reason,
        )
        if (
            not isinstance(result, SourceCandidate)
            or result.upstream is not upstream
            or result.symbols != symbols
            or result.start_date != start_date
            or result.end_date != end_date
            or (
                result.status
                in {
                    ResolutionSourceStatus.AVAILABLE,
                    ResolutionSourceStatus.EMPTY,
                    ResolutionSourceStatus.QUALITY_FAILED,
                }
                and result.version_id != candidate_version_id
            )
        ):
            raise ValueError("source port returned a mismatched request binding")
        return result

    def _record_if_comparable(
        self,
        primary: SourceCandidate,
        fallback: SourceCandidate,
        keys: tuple[ResolutionKey, ...],
        created_at: UtcTimestamp,
    ) -> EntityId | None:
        if (
            primary.status not in _USABLE_CALL_STATUSES
            or fallback.status not in _USABLE_CALL_STATUSES
        ):
            return None
        report_id = self._differences.record(
            primary=primary,
            fallback=fallback,
            keys=keys,
            created_at=created_at,
        )
        if not isinstance(report_id, EntityId) or report_id.namespace != "data_diff":
            raise ValueError("difference port must return a data_diff identifier")
        return report_id


def _rows(candidate: SourceCandidate) -> Mapping[ResolutionKey, Mapping[str, object]]:
    if candidate.status not in _USABLE_CALL_STATUSES or candidate.table is None:
        return MappingProxyType({})
    rows: dict[ResolutionKey, Mapping[str, object]] = {}
    for row in candidate.table.to_pylist():
        key = ResolutionKey(str(row["code"]), row["trade_date"])
        if key in rows:
            raise ValueError("source table contains duplicate resolution key")
        rows[key] = MappingProxyType(dict(row))
    return MappingProxyType(rows)


def _ab_trigger(
    key: ResolutionKey,
    a: SourceCandidate,
    b: SourceCandidate,
    rows: Mapping[DataUpstream, Mapping[ResolutionKey, Mapping[str, object]]],
) -> str | None:
    for candidate in (a, b):
        if candidate.status not in _USABLE_CALL_STATUSES:
            return f"{candidate.upstream.value}_{candidate.status.value}"
    a_row = rows[DataUpstream.EASTMONEY].get(key)
    b_row = rows[DataUpstream.TENCENT].get(key)
    if a_row is None and b_row is None:
        return None
    if a_row is None:
        return "EASTMONEY_MISSING_ROW"
    if b_row is None:
        return "TENCENT_MISSING_ROW"
    different = tuple(field for field in _FIELDS if a_row[field] != b_row[field])
    return None if not different else f"FIELD_DIFFERENCE:{','.join(different)}"


def _affected_spans(
    all_keys: tuple[ResolutionKey, ...],
    affected: frozenset[ResolutionKey],
) -> tuple[tuple[ResolutionKey, ...], ...]:
    spans: list[tuple[ResolutionKey, ...]] = []
    by_symbol: dict[str, list[ResolutionKey]] = {}
    for key in all_keys:
        by_symbol.setdefault(key.symbol, []).append(key)
    for keys in by_symbol.values():
        current: list[ResolutionKey] = []
        for key in keys:
            if key in affected:
                current.append(key)
            elif current:
                spans.append(tuple(current))
                current = []
        if current:
            spans.append(tuple(current))
    return tuple(spans)


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


def _equal(
    left: Mapping[str, object] | None,
    right: Mapping[str, object] | None,
) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return all(left[field] == right[field] for field in _FIELDS)


def _agreement(
    available: tuple[DataUpstream, ...],
    rows: Mapping[DataUpstream, Mapping[str, object] | None],
) -> tuple[tuple[DataUpstream, ...], DataUpstream | None, ResolutionReasonCode]:
    pairs = tuple(
        (left, right)
        for index, left in enumerate(available)
        for right in available[index + 1 :]
        if _equal(rows[left], rows[right])
    )
    if not pairs:
        return (), None, ResolutionReasonCode.NO_TWO_SOURCE_CONSENSUS
    agreeing = tuple(source for source in _SOURCE_ORDER if any(source in pair for pair in pairs))
    selected = agreeing[0]
    if agreeing == _SOURCE_ORDER:
        reason = ResolutionReasonCode.ABC_AGREE_A_SELECTED
    elif DataUpstream.EASTMONEY in agreeing and DataUpstream.TENCENT in agreeing:
        reason = ResolutionReasonCode.AB_AGREE_A_SELECTED
    elif DataUpstream.EASTMONEY in agreeing and DataUpstream.SINA in agreeing:
        reason = ResolutionReasonCode.AC_AGREE_A_SELECTED
    else:
        reason = ResolutionReasonCode.BC_AGREE_B_SELECTED
    return agreeing, selected, reason


def _input_bindings(
    a: SourceCandidate,
    b: SourceCandidate,
    c_candidates: tuple[SourceCandidate, ...],
) -> tuple[ResolutionSourceBinding, ...]:
    bindings = [a.binding, b.binding]
    if c_candidates:
        bindings.extend(item.binding for item in c_candidates)
    else:
        bindings.append(
            ResolutionSourceBinding(
                DataUpstream.SINA,
                ResolutionSourceStatus.NOT_CALLED,
                None,
                None,
            )
        )
    unique = {
        (
            item.upstream.value,
            item.status.value,
            "" if item.batch_id is None else str(item.batch_id),
            item.content_sha256 or "",
        ): item
        for item in bindings
    }
    rank = {source: index for index, source in enumerate(_SOURCE_ORDER)}
    return tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                rank[item.upstream],
                "" if item.batch_id is None else str(item.batch_id),
                item.status.value,
                item.content_sha256 or "",
            ),
        )
    )


__all__ = [
    "ResolutionDifferencePort",
    "ResolutionKey",
    "ResolutionOutcome",
    "ResolutionRequest",
    "ResolutionSourcePort",
    "SourceCandidate",
    "SourceResolutionService",
]
