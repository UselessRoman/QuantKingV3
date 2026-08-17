from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pyarrow as pa

from quantking.common.ids import EntityId, new_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.canonical_schema import canonical_schema, ensure_sorted
from quantking.data.comparison.thresholds import (
    ThresholdSet,
    ThresholdSpec,
    default_thresholds,
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
    ResolutionReasonCode,
    ResolutionRunBlocker,
    ResolutionRunStatus,
    ResolutionSourceStatus,
)
from quantking.data.resolution.service import (
    ResolutionKey,
    ResolutionOutcome,
    ResolutionRequest,
    SourceCandidate,
    SourceResolutionService,
)

_SYMBOL = "600000.SH"
_START = date(2026, 8, 5)
_END = date(2026, 8, 7)

_EMPTY = EmptyResultInfo(
    classification=LegalEmptyClassification.NO_TRADING_SESSION,
    reason="synthetic no trading session",
)


def _row(
    day: date,
    close: float,
    *,
    symbol: str = _SYMBOL,
    volume: int = 100,
    amount: float = 1_000.0,
) -> dict[str, object]:
    return {
        "code": symbol,
        "trade_date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "pre_close": close,
        "volume": volume,
        "amount": amount,
    }


def _table(rows: list[dict[str, object]]) -> pa.Table:
    return ensure_sorted(
        DatasetKind.DAILY_BAR_UNADJUSTED.value,
        pa.Table.from_pylist(
            rows,
            schema=canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value),
        ),
    )


@dataclass(frozen=True, slots=True)
class _Call:
    upstream: DataUpstream
    symbols: tuple[str, ...]
    start_date: date
    end_date: date
    trigger_reason: str


class _Sources:
    def __init__(
        self,
        version_id: EntityId,
        rows: dict[DataUpstream, list[dict[str, object]]],
        statuses: dict[DataUpstream, ResolutionSourceStatus] | None = None,
    ) -> None:
        self._version_id = version_id
        self._rows = rows
        self._statuses = statuses or {}
        self.calls: list[_Call] = []
        self.preflights: list[tuple[DataUpstream, tuple[object, ...], int]] = []

    def preflight_exception_requests(
        self, upstream: DataUpstream, traces: tuple[object, ...]
    ) -> None:
        self.preflights.append((upstream, traces, len(self.calls)))

    def fetch(
        self,
        *,
        upstream: DataUpstream,
        symbols: tuple[str, ...],
        start_date: date,
        end_date: date,
        trigger_reason: str,
    ) -> SourceCandidate:
        self.calls.append(_Call(upstream, symbols, start_date, end_date, trigger_reason))
        status = self._statuses.get(upstream, ResolutionSourceStatus.AVAILABLE)
        batch_id = new_entity_id("data_batch")
        if status is ResolutionSourceStatus.CALL_FAILED:
            return SourceCandidate(
                upstream=upstream,
                status=status,
                symbols=symbols,
                start_date=start_date,
                end_date=end_date,
                batch_id=batch_id,
                content_sha256=None,
                version_id=None,
                quality_run_id=None,
                table=None,
                empty_result=None,
            )
        rows: list[dict[str, object]] = []
        for row in self._rows.get(upstream, []):
            row_symbol = row["code"]
            row_date = row["trade_date"]
            if not isinstance(row_symbol, str) or not isinstance(row_date, date):
                raise TypeError("synthetic row key is invalid")
            if row_symbol in symbols and start_date <= row_date <= end_date:
                rows.append(row)
        # A landed AVAILABLE call with zero rows cannot exist in production
        # (zero-row SUCCEEDED batches require a legal empty classification),
        # so an empty span resolves to the EMPTY call status.
        if status is ResolutionSourceStatus.AVAILABLE and not rows:
            status = ResolutionSourceStatus.EMPTY
        table = _table(rows)
        empty_result = _EMPTY if status is ResolutionSourceStatus.EMPTY else None
        content_sha = canonical_content_sha256(
            DatasetKind.DAILY_BAR_UNADJUSTED,
            [table],
            empty_result=empty_result,
        )
        return SourceCandidate(
            upstream=upstream,
            status=status,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            batch_id=batch_id,
            content_sha256=content_sha,
            version_id=self._version_id,
            quality_run_id=new_entity_id("data_quality"),
            table=table,
            empty_result=empty_result,
        )


class _Differences:
    def __init__(self) -> None:
        self.calls: list[tuple[DataUpstream, DataUpstream, tuple[ResolutionKey, ...]]] = []

    def record(
        self,
        *,
        primary: SourceCandidate,
        fallback: SourceCandidate,
        keys: tuple[ResolutionKey, ...],
        created_at: UtcTimestamp,
    ) -> EntityId:
        del created_at
        self.calls.append((primary.upstream, fallback.upstream, keys))
        return new_entity_id("data_diff")


def _resolve(
    rows: dict[DataUpstream, list[dict[str, object]]],
    *,
    statuses: dict[DataUpstream, ResolutionSourceStatus] | None = None,
    symbols: tuple[str, ...] = (_SYMBOL,),
    thresholds: ThresholdSet | None = None,
) -> tuple[ResolutionOutcome, _Sources, _Differences]:
    version_id = new_entity_id("data_version")
    sources = _Sources(version_id, rows, statuses)
    differences = _Differences()
    request = ResolutionRequest(
        created_job_id=new_entity_id("job"),
        candidate_version_id=version_id,
        created_at=UtcTimestamp.from_iso("2026-08-12T02:00:00Z"),
        input_fingerprint="a" * 64,
        symbols=symbols,
        start_date=_START,
        end_date=_END,
    )
    outcome = SourceResolutionService(
        sources=sources,
        differences=differences,
        thresholds=thresholds if thresholds is not None else default_thresholds(),
    ).resolve(request)
    return outcome, sources, differences


def test_ab_agreement_selects_a_and_never_calls_c() -> None:
    a = [_row(_START, 10.0), _row(_END, 11.0)]
    b = [_row(_START, 10.0), _row(_END, 11.0)]
    outcome, sources, _differences = _resolve({DataUpstream.BAOSTOCK: a, DataUpstream.TENCENT: b})

    assert outcome.publishable is True
    assert outcome.selected_table is not None
    assert outcome.selected_table.num_rows == 2
    assert outcome.run.rule_version == K4_RESOLUTION_RULE_VERSION
    assert outcome.run.threshold_rule_version == default_thresholds().rule_version
    assert outcome.run.status is ResolutionRunStatus.RESOLVED
    assert outcome.run.conflicts == ()
    assert outcome.run.zero_row_reason_code is None
    assert outcome.run.default_summary is not None
    assert outcome.run.default_summary.selected_upstream is DataUpstream.BAOSTOCK
    assert outcome.run.default_summary.compared_upstreams == (
        DataUpstream.BAOSTOCK,
        DataUpstream.TENCENT,
    )
    assert tuple(item.upstream for item in outcome.run.default_summary.source_bindings) == (
        DataUpstream.BAOSTOCK,
        DataUpstream.TENCENT,
        DataUpstream.SINA,
    )
    sina_binding = outcome.run.default_summary.source_bindings[2]
    assert sina_binding.status is ResolutionSourceStatus.NOT_CALLED
    # C planned/actual both zero when A/B agree.
    assert [call.upstream for call in sources.calls] == [
        DataUpstream.BAOSTOCK,
        DataUpstream.TENCENT,
    ]


def test_ab_disagreement_calls_c_for_affected_span_and_selects_ac() -> None:
    a = [_row(_START, 10.0), _row(_END, 11.0)]
    b = [_row(_START, 10.0), _row(_END, 12.0)]
    c = [_row(_START, 10.0), _row(_END, 11.0)]
    outcome, sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: a,
            DataUpstream.TENCENT: b,
            DataUpstream.SINA: c,
        }
    )

    assert outcome.publishable is True
    assert {call.upstream for call in sources.calls} == {
        DataUpstream.BAOSTOCK,
        DataUpstream.TENCENT,
        DataUpstream.SINA,
    }
    sina_calls = [call for call in sources.calls if call.upstream is DataUpstream.SINA]
    assert len(sina_calls) == 1
    assert sina_calls[0].start_date == _END and sina_calls[0].end_date == _END
    assert len(outcome.run.exceptions) == 1
    decision = outcome.run.exceptions[0]
    assert decision.trade_date == _END
    assert decision.reason_code is ResolutionReasonCode.AC_AGREE_A_SELECTED
    assert decision.selected_upstream is DataUpstream.BAOSTOCK
    assert outcome.run.conflicts == ()
    assert outcome.run.default_summary is not None
    assert outcome.run.default_summary.decision_count == 1  # the agreeing _START row
    assert len(sources.preflights) == 1
    assert sources.preflights[0][0] is DataUpstream.SINA


def test_missing_a_row_arbitrates_bc_and_selects_b() -> None:
    a = [_row(_END, 11.0)]
    b = [_row(_START, 10.0), _row(_END, 11.0)]
    c = [_row(_START, 10.0), _row(_END, 11.0)]
    outcome, _sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: a,
            DataUpstream.TENCENT: b,
            DataUpstream.SINA: c,
        }
    )

    assert outcome.publishable is True
    assert outcome.selected_table is not None
    assert outcome.selected_table.num_rows == 2
    decisions = outcome.run.exceptions
    assert len(decisions) == 1
    assert decisions[0].trade_date == _START
    assert decisions[0].reason_code is ResolutionReasonCode.BC_AGREE_B_SELECTED
    assert decisions[0].selected_upstream is DataUpstream.TENCENT
    assert decisions[0].threshold_rule_version == default_thresholds().rule_version


def test_exactly_one_held_row_is_isolated_not_silently_dropped() -> None:
    # A holds d1 only; B and C hold d2 only (AVAILABLE, missing d1). For d1
    # exactly one actual source holds the row -> isolated, never dropped.
    a = [_row(_START, 10.0)]
    b = [_row(_END, 11.0)]
    c = [_row(_END, 11.0)]
    outcome, sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: a,
            DataUpstream.TENCENT: b,
            DataUpstream.SINA: c,
        }
    )

    assert any(call.upstream is DataUpstream.SINA for call in sources.calls)
    assert outcome.publishable is False
    assert outcome.selected_table is None
    assert outcome.run.status is ResolutionRunStatus.BLOCKED
    isolated = [conflict for conflict in outcome.run.conflicts if conflict.trade_date == _START]
    assert len(isolated) == 1
    conflict = isolated[0]
    assert conflict.reason_code is ResolutionReasonCode.SINGLE_SOURCE_ROW_ISOLATED
    assert conflict.agreeing_upstreams == ()
    assert conflict.selected_upstream is None


def test_common_missing_rows_never_form_a_value_consensus() -> None:
    # B and C both miss the d1 row while A holds it: their common absence must
    # not produce a BC "agreement" that drops the row.
    a = [_row(_START, 10.0)]
    b = [_row(_END, 11.0)]
    c = [_row(_END, 11.0)]
    outcome, _sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: a,
            DataUpstream.TENCENT: b,
            DataUpstream.SINA: c,
        }
    )
    isolated = [conflict for conflict in outcome.run.conflicts if conflict.trade_date == _START]
    assert len(isolated) == 1
    assert isolated[0].reason_code is ResolutionReasonCode.SINGLE_SOURCE_ROW_ISOLATED


def test_two_held_disagreeing_rows_are_a_conflict() -> None:
    a = [_row(_START, 10.0)]
    b = [_row(_START, 10.5)]
    c = [_row(_END, 11.0)]  # AVAILABLE but misses the disputed key
    outcome, _sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: a,
            DataUpstream.TENCENT: b,
            DataUpstream.SINA: c,
        }
    )

    assert outcome.publishable is False
    disputed = [conflict for conflict in outcome.run.conflicts if conflict.trade_date == _START]
    assert len(disputed) == 1
    assert disputed[0].reason_code is ResolutionReasonCode.NO_TWO_SOURCE_CONSENSUS


def test_two_empty_sources_publish_legal_zero_rows_with_independent_reason() -> None:
    outcome, sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: [],
            DataUpstream.TENCENT: [],
        },
        statuses={
            DataUpstream.BAOSTOCK: ResolutionSourceStatus.EMPTY,
            DataUpstream.TENCENT: ResolutionSourceStatus.EMPTY,
        },
    )

    assert outcome.publishable is True
    assert outcome.selected_table is not None
    assert outcome.selected_table.num_rows == 0
    assert outcome.run.status is ResolutionRunStatus.EMPTY_CONSENSUS
    assert outcome.run.zero_row_reason_code is ResolutionReasonCode.ZERO_ROW_LEGAL_EMPTY_CONSENSUS
    assert outcome.run.total_decisions == 0
    assert outcome.run.conflicts == ()
    # C stays untouched for a clean two-source empty consensus.
    assert [call.upstream for call in sources.calls] == [
        DataUpstream.BAOSTOCK,
        DataUpstream.TENCENT,
    ]
    assert not any(decision for decision in outcome.run.exceptions)


def test_call_failure_with_two_empty_sources_still_forms_zero_row_consensus() -> None:
    outcome, _sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: [],
            DataUpstream.TENCENT: [],
            DataUpstream.SINA: [],
        },
        statuses={
            DataUpstream.BAOSTOCK: ResolutionSourceStatus.EMPTY,
            DataUpstream.TENCENT: ResolutionSourceStatus.CALL_FAILED,
            DataUpstream.SINA: ResolutionSourceStatus.EMPTY,
        },
    )

    assert outcome.publishable is True
    assert outcome.run.zero_row_reason_code is ResolutionReasonCode.ZERO_ROW_LEGAL_EMPTY_CONSENSUS
    assert outcome.selected_table is not None and outcome.selected_table.num_rows == 0


def test_single_empty_source_blocks_with_insufficient_consensus() -> None:
    outcome, _sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: [],
            DataUpstream.TENCENT: [],
            DataUpstream.SINA: [],
        },
        statuses={
            DataUpstream.BAOSTOCK: ResolutionSourceStatus.EMPTY,
            DataUpstream.TENCENT: ResolutionSourceStatus.CALL_FAILED,
            DataUpstream.SINA: ResolutionSourceStatus.CALL_FAILED,
        },
    )

    assert outcome.publishable is False
    assert outcome.run.run_blockers == (ResolutionRunBlocker.INSUFFICIENT_EMPTY_CONSENSUS,)
    assert outcome.run.zero_row_reason_code is None


def test_ab_global_failure_with_bc_agreement_is_publishable() -> None:
    outcome, sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: [],
            DataUpstream.TENCENT: [_row(_START, 10.0)],
            DataUpstream.SINA: [_row(_START, 10.0)],
        },
        statuses={
            DataUpstream.BAOSTOCK: ResolutionSourceStatus.CALL_FAILED,
            DataUpstream.TENCENT: ResolutionSourceStatus.AVAILABLE,
            DataUpstream.SINA: ResolutionSourceStatus.AVAILABLE,
        },
    )

    assert outcome.publishable is True  # B and C agree on the single held row
    assert outcome.selected_table is not None
    assert outcome.selected_table.num_rows == 1
    assert outcome.run.exceptions[0].reason_code is ResolutionReasonCode.BC_AGREE_B_SELECTED
    # full-range C calls carry the global trigger
    sina_calls = [call for call in sources.calls if call.upstream is DataUpstream.SINA]
    assert sina_calls and all(call.trigger_reason == "AB_SOURCE_UNAVAILABLE" for call in sina_calls)


def test_global_failure_with_single_held_row_is_isolated_not_a_keyerror() -> None:
    # Regression: a CALL_FAILED source never enters row_by_source, and the
    # held-row classification must not raise while classifying the others.
    outcome, _sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: [],
            DataUpstream.TENCENT: [_row(_START, 10.0)],
            DataUpstream.SINA: [_row(_END, 11.0)],
        },
        statuses={
            DataUpstream.BAOSTOCK: ResolutionSourceStatus.CALL_FAILED,
            DataUpstream.TENCENT: ResolutionSourceStatus.AVAILABLE,
            DataUpstream.SINA: ResolutionSourceStatus.AVAILABLE,
        },
    )

    assert outcome.publishable is False
    assert len(outcome.run.conflicts) == 2
    assert all(
        conflict.reason_code is ResolutionReasonCode.SINGLE_SOURCE_ROW_ISOLATED
        for conflict in outcome.run.conflicts
    )


def test_resolution_consumes_the_shared_versioned_threshold_set() -> None:
    from quantking.data.comparison.thresholds import K3_CANDIDATE_RULE_VERSION

    # A non-production candidate set registered in the version registry: the
    # resolution layer must consume its field thresholds, not raw equality.
    tolerant = ThresholdSet(
        rule_version=K3_CANDIDATE_RULE_VERSION,
        thresholds={
            DatasetKind.DAILY_BAR_UNADJUSTED: {
                "open": ThresholdSpec(absolute=Decimal("0"), relative=Decimal("0")),
                "high": ThresholdSpec(absolute=Decimal("0"), relative=Decimal("0")),
                "low": ThresholdSpec(absolute=Decimal("0"), relative=Decimal("0")),
                "close": ThresholdSpec(absolute=Decimal("0"), relative=Decimal("0")),
                "pre_close": ThresholdSpec(absolute=Decimal("0"), relative=Decimal("0")),
                "volume": ThresholdSpec(absolute=Decimal("1"), relative=Decimal("0")),
                "amount": ThresholdSpec(absolute=Decimal("1"), relative=Decimal("0")),
            },
        },
    )
    a = [_row(_START, 10.0, volume=100)]
    b = [_row(_START, 10.0, volume=101)]
    outcome, sources, _differences = _resolve(
        {DataUpstream.BAOSTOCK: a, DataUpstream.TENCENT: b},
        thresholds=tolerant,
    )
    assert outcome.publishable is True
    assert outcome.run.threshold_rule_version == tolerant.rule_version
    assert outcome.run.default_summary is not None
    assert [call.upstream for call in sources.calls] == [
        DataUpstream.BAOSTOCK,
        DataUpstream.TENCENT,
    ]

    # Under the strict production thresholds the same rows conflict; C holds
    # a third disagreeing row so the decision stays NO_TWO_SOURCE_CONSENSUS.
    c = [_row(_START, 10.0, volume=102)]
    strict_outcome, _sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: a,
            DataUpstream.TENCENT: b,
            DataUpstream.SINA: c,
        },
    )
    assert strict_outcome.publishable is False
    conflict = strict_outcome.run.conflicts[0]
    assert conflict.reason_code is ResolutionReasonCode.NO_TWO_SOURCE_CONSENSUS


def test_default_summary_rejects_fabricated_c_binding() -> None:
    outcome, _sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: [_row(_START, 10.0)],
            DataUpstream.TENCENT: [_row(_START, 10.0)],
        }
    )
    summary = outcome.run.default_summary
    assert summary is not None
    assert summary.reason_code is ResolutionReasonCode.AB_AGREE_DEFAULT
    assert summary.decision_count == 1
    assert summary.source_bindings[2].status is ResolutionSourceStatus.NOT_CALLED
    assert summary.source_bindings[2].batch_id is None
    assert summary.source_bindings[2].content_sha256 is None
