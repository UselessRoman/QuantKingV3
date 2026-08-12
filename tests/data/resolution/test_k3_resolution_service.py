from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pyarrow as pa
import pytest

from quantking.common.ids import EntityId, new_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.canonical_schema import canonical_schema, ensure_sorted
from quantking.data.content import canonical_content_sha256
from quantking.data.models import (
    DatasetKind,
    DataUpstream,
    EmptyResultInfo,
    LegalEmptyClassification,
)
from quantking.data.resolution.k3_service import K3SourceResolutionService
from quantking.data.resolution.models import (
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
)

_SYMBOL = "600000.SH"
_START = date(2026, 8, 5)
_END = date(2026, 8, 7)


_EMPTY = EmptyResultInfo(
    classification=LegalEmptyClassification.NO_TRADING_SESSION,
    reason="synthetic no trading session",
)


def _row(day: date, close: float, *, symbol: str = _SYMBOL) -> dict[str, object]:
    return {
        "code": symbol,
        "trade_date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "pre_close": close,
        "volume": 100,
        "amount": 1_000.0,
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
        reject_preflight: DataUpstream | None = None,
    ) -> None:
        self._version_id = version_id
        self._rows = rows
        self._statuses = statuses or {}
        self._reject_preflight = reject_preflight
        self.calls: list[_Call] = []
        self.preflights: list[tuple[DataUpstream, tuple[object, ...], int]] = []

    def preflight_exception_requests(
        self, upstream: DataUpstream, traces: tuple[object, ...]
    ) -> None:
        self.preflights.append((upstream, traces, len(self.calls)))
        if upstream is self._reject_preflight:
            raise RuntimeError("synthetic preflight rejection")

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
        if status is ResolutionSourceStatus.QUALITY_FAILED:
            return SourceCandidate(
                upstream=upstream,
                status=status,
                symbols=symbols,
                start_date=start_date,
                end_date=end_date,
                batch_id=batch_id,
                content_sha256="f" * 64,
                version_id=self._version_id,
                quality_run_id=new_entity_id("data_quality"),
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
    reject_preflight: DataUpstream | None = None,
) -> tuple[ResolutionOutcome, _Sources, _Differences]:
    version_id = new_entity_id("data_version")
    sources = _Sources(version_id, rows, statuses, reject_preflight)
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
    outcome = K3SourceResolutionService(
        sources=sources,
        differences=differences,
    ).resolve(request)
    assert all(call.upstream is not DataUpstream.XTQUANT for call in sources.calls)
    return outcome, sources, differences


def test_ab_agreement_selects_baostock_and_never_calls_c_or_d() -> None:
    rows = [_row(_START, 10.0), _row(_END, 10.5)]
    outcome, sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: rows,
            DataUpstream.EASTMONEY: rows,
        }
    )

    assert [call.upstream for call in sources.calls] == [
        DataUpstream.BAOSTOCK,
        DataUpstream.EASTMONEY,
    ]
    assert outcome.run.status is ResolutionRunStatus.RESOLVED
    assert outcome.run.default_decision_count == 2
    assert outcome.selected_table is not None
    assert outcome.selected_table.num_rows == 2
    assert outcome.run.default_summary is not None
    assert [item.status for item in outcome.run.default_summary.source_bindings] == [
        ResolutionSourceStatus.AVAILABLE,
        ResolutionSourceStatus.AVAILABLE,
        ResolutionSourceStatus.NOT_CALLED,
        ResolutionSourceStatus.NOT_CALLED,
    ]


def test_a_failure_calls_c_full_range_and_bc_consensus_skips_d() -> None:
    rows = [_row(_END, 10.5)]
    outcome, sources, _differences = _resolve(
        {
            DataUpstream.EASTMONEY: rows,
            DataUpstream.TENCENT: rows,
        },
        statuses={DataUpstream.BAOSTOCK: ResolutionSourceStatus.CALL_FAILED},
    )

    assert [call.upstream for call in sources.calls] == [
        DataUpstream.BAOSTOCK,
        DataUpstream.EASTMONEY,
        DataUpstream.TENCENT,
    ]
    assert (sources.calls[-1].start_date, sources.calls[-1].end_date) == (
        _START,
        _END,
    )
    decision = next(item for item in outcome.run.exceptions if item.trade_date == _END)
    assert decision.reason_code is ResolutionReasonCode.BC_AGREE_B_SELECTED
    assert decision.selected_upstream is DataUpstream.EASTMONEY
    assert [trace.upstream for trace in decision.request_traces] == [DataUpstream.TENCENT]


def test_abc_disagreement_calls_d_for_affected_date_and_ad_consensus() -> None:
    outcome, sources, differences = _resolve(
        {
            DataUpstream.BAOSTOCK: [_row(_END, 10.0)],
            DataUpstream.EASTMONEY: [_row(_END, 11.0)],
            DataUpstream.TENCENT: [_row(_END, 12.0)],
            DataUpstream.SINA: [_row(_END, 10.0)],
        }
    )

    assert [call.upstream for call in sources.calls] == [
        DataUpstream.BAOSTOCK,
        DataUpstream.EASTMONEY,
        DataUpstream.TENCENT,
        DataUpstream.SINA,
    ]
    assert (sources.calls[-1].start_date, sources.calls[-1].end_date) == (_END, _END)
    decision = next(item for item in outcome.run.exceptions if item.trade_date == _END)
    assert decision.reason_code is ResolutionReasonCode.AD_AGREE_A_SELECTED
    assert decision.selected_upstream is DataUpstream.BAOSTOCK
    assert [trace.upstream for trace in decision.request_traces] == [
        DataUpstream.TENCENT,
        DataUpstream.SINA,
    ]
    assert {(left, right) for left, right, _keys in differences.calls} == {
        (DataUpstream.BAOSTOCK, DataUpstream.EASTMONEY),
        (DataUpstream.BAOSTOCK, DataUpstream.TENCENT),
        (DataUpstream.EASTMONEY, DataUpstream.TENCENT),
        (DataUpstream.BAOSTOCK, DataUpstream.SINA),
        (DataUpstream.EASTMONEY, DataUpstream.SINA),
        (DataUpstream.TENCENT, DataUpstream.SINA),
    }


def test_ab_double_failure_keeps_c_and_d_on_full_frozen_range() -> None:
    outcome, sources, _differences = _resolve(
        {
            DataUpstream.TENCENT: [_row(_END, 12.0)],
            DataUpstream.SINA: [_row(_END, 10.0)],
        },
        statuses={
            DataUpstream.BAOSTOCK: ResolutionSourceStatus.CALL_FAILED,
            DataUpstream.EASTMONEY: ResolutionSourceStatus.CALL_FAILED,
        },
    )

    exceptional = sources.calls[2:]
    assert [call.upstream for call in exceptional] == [
        DataUpstream.TENCENT,
        DataUpstream.SINA,
    ]
    assert [(call.start_date, call.end_date) for call in exceptional] == [
        (_START, _END),
        (_START, _END),
    ]
    assert outcome.run.status is ResolutionRunStatus.BLOCKED


def test_unaffected_observed_key_splits_c_into_two_exact_spans() -> None:
    middle = date(2026, 8, 6)
    a_rows = [
        _row(_START, 10.0),
        _row(middle, 10.5),
        _row(_END, 11.0),
    ]
    b_rows = [
        _row(_START, 10.1),
        _row(middle, 10.5),
        _row(_END, 11.1),
    ]
    _outcome, sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: a_rows,
            DataUpstream.EASTMONEY: b_rows,
            DataUpstream.TENCENT: a_rows,
        }
    )

    c_calls = [call for call in sources.calls if call.upstream is DataUpstream.TENCENT]
    assert [(call.start_date, call.end_date) for call in c_calls] == [
        (_START, _START),
        (_END, _END),
    ]


@pytest.mark.parametrize(
    ("c_close", "reason", "selected"),
    [
        (10.0, ResolutionReasonCode.AC_AGREE_A_SELECTED, DataUpstream.BAOSTOCK),
        (
            11.0,
            ResolutionReasonCode.BC_AGREE_B_SELECTED,
            DataUpstream.EASTMONEY,
        ),
    ],
)
def test_ab_difference_resolves_with_c_without_d(
    c_close: float,
    reason: ResolutionReasonCode,
    selected: DataUpstream,
) -> None:
    outcome, sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: [_row(_END, 10.0)],
            DataUpstream.EASTMONEY: [_row(_END, 11.0)],
            DataUpstream.TENCENT: [_row(_END, c_close)],
        }
    )

    assert [call.upstream for call in sources.calls] == [
        DataUpstream.BAOSTOCK,
        DataUpstream.EASTMONEY,
        DataUpstream.TENCENT,
    ]
    decision = outcome.run.exceptions[0]
    assert decision.reason_code is reason
    assert decision.selected_upstream is selected
    assert decision.source_bindings[-1].status is ResolutionSourceStatus.NOT_CALLED
    assert [trace.upstream for trace in decision.request_traces] == [DataUpstream.TENCENT]


@pytest.mark.parametrize(
    ("d_close", "reason", "selected"),
    [
        (
            11.0,
            ResolutionReasonCode.BD_AGREE_B_SELECTED,
            DataUpstream.EASTMONEY,
        ),
        (
            12.0,
            ResolutionReasonCode.CD_AGREE_C_SELECTED,
            DataUpstream.TENCENT,
        ),
    ],
)
def test_d_resolves_with_b_or_c(
    d_close: float,
    reason: ResolutionReasonCode,
    selected: DataUpstream,
) -> None:
    outcome, sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: [_row(_END, 10.0)],
            DataUpstream.EASTMONEY: [_row(_END, 11.0)],
            DataUpstream.TENCENT: [_row(_END, 12.0)],
            DataUpstream.SINA: [_row(_END, d_close)],
        }
    )

    assert sources.calls[-1].upstream is DataUpstream.SINA
    decision = outcome.run.exceptions[0]
    assert decision.reason_code is reason
    assert decision.selected_upstream is selected


def test_four_way_disagreement_remains_a_blocking_conflict() -> None:
    outcome, _sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: [_row(_END, 10.0)],
            DataUpstream.EASTMONEY: [_row(_END, 11.0)],
            DataUpstream.TENCENT: [_row(_END, 12.0)],
            DataUpstream.SINA: [_row(_END, 13.0)],
        }
    )

    assert outcome.run.status is ResolutionRunStatus.BLOCKED
    assert outcome.run.candidate_version_id is None
    assert outcome.run.conflicts[0].reason_code is ResolutionReasonCode.NO_TWO_SOURCE_CONSENSUS
    assert outcome.selected_table is None


def test_c_returned_new_boundary_key_drives_d_scope_and_consensus() -> None:
    outcome, sources, _differences = _resolve(
        {
            DataUpstream.EASTMONEY: [_row(_END, 11.0)],
            DataUpstream.TENCENT: [_row(_START, 11.0), _row(_END, 11.0)],
            DataUpstream.SINA: [_row(_START, 11.0)],
        },
        statuses={DataUpstream.BAOSTOCK: ResolutionSourceStatus.CALL_FAILED},
    )

    assert (sources.calls[2].start_date, sources.calls[2].end_date) == (_START, _END)
    assert (sources.calls[3].start_date, sources.calls[3].end_date) == (_START, _START)
    assert {item.trade_date for item in outcome.run.exceptions} == {_START, _END}
    assert outcome.run.total_decisions == 2


def test_d_returned_new_boundary_key_is_included_in_conflicts() -> None:
    outcome, sources, _differences = _resolve(
        {
            DataUpstream.TENCENT: [_row(_END, 12.0)],
            DataUpstream.SINA: [_row(_START, 13.0), _row(_END, 13.0)],
        },
        statuses={
            DataUpstream.BAOSTOCK: ResolutionSourceStatus.CALL_FAILED,
            DataUpstream.EASTMONEY: ResolutionSourceStatus.CALL_FAILED,
        },
    )

    assert sources.calls[-1].upstream is DataUpstream.SINA
    assert {item.trade_date for item in outcome.run.conflicts} == {_START, _END}


@pytest.mark.parametrize(
    "failed_status",
    [ResolutionSourceStatus.CALL_FAILED, ResolutionSourceStatus.QUALITY_FAILED],
)
def test_a_failure_statuses_resolve_via_b_c(
    failed_status: ResolutionSourceStatus,
) -> None:
    row = _row(_END, 11.0)
    outcome, _sources, differences = _resolve(
        {
            DataUpstream.EASTMONEY: [row],
            DataUpstream.TENCENT: [row],
        },
        statuses={DataUpstream.BAOSTOCK: failed_status},
    )

    assert outcome.run.status is ResolutionRunStatus.RESOLVED
    assert outcome.run.exceptions[0].reason_code is ResolutionReasonCode.BC_AGREE_B_SELECTED
    assert {(left, right) for left, right, _keys in differences.calls} == {
        (DataUpstream.EASTMONEY, DataUpstream.TENCENT)
    }


def test_all_sources_failed_blocks_without_keys_or_xtquant() -> None:
    outcome, sources, differences = _resolve(
        {},
        statuses={
            upstream: ResolutionSourceStatus.CALL_FAILED
            for upstream in (
                DataUpstream.BAOSTOCK,
                DataUpstream.EASTMONEY,
                DataUpstream.TENCENT,
                DataUpstream.SINA,
            )
        },
    )

    assert outcome.run.run_blockers == (ResolutionRunBlocker.ALL_SOURCES_UNAVAILABLE,)
    assert outcome.run.total_decisions == 0
    assert differences.calls == []
    assert [call.upstream for call in sources.calls] == [
        DataUpstream.BAOSTOCK,
        DataUpstream.EASTMONEY,
        DataUpstream.TENCENT,
        DataUpstream.SINA,
    ]


def test_missing_a_row_and_c_agree_on_absence_without_selected_row() -> None:
    outcome, _sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: [_row(_START, 10.0)],
            DataUpstream.EASTMONEY: [_row(_START, 10.1), _row(_END, 11.0)],
            DataUpstream.TENCENT: [_row(_START, 10.0)],
        }
    )

    decision = next(item for item in outcome.run.exceptions if item.trade_date == _END)
    assert decision.reason_code is ResolutionReasonCode.AC_AGREE_A_SELECTED
    assert decision.selected_row_present is False
    assert outcome.selected_table is not None
    assert outcome.selected_table.column("trade_date").to_pylist() == [_START]


def test_missing_b_row_and_c_agree_on_absence_with_a_priority() -> None:
    outcome, _sources, _differences = _resolve(
        {
            DataUpstream.BAOSTOCK: [_row(_START, 10.0), _row(_END, 11.0)],
            DataUpstream.EASTMONEY: [_row(_START, 10.1)],
            DataUpstream.TENCENT: [_row(_START, 10.1)],
        }
    )

    decision = next(item for item in outcome.run.exceptions if item.trade_date == _END)
    assert decision.reason_code is ResolutionReasonCode.BC_AGREE_B_SELECTED
    assert decision.selected_row_present is False


def test_two_empty_sources_publish_legal_zero_rows_without_c_or_d() -> None:
    outcome, sources, differences = _resolve(
        {},
        statuses={
            DataUpstream.BAOSTOCK: ResolutionSourceStatus.EMPTY,
            DataUpstream.EASTMONEY: ResolutionSourceStatus.EMPTY,
        },
    )

    assert outcome.run.status is ResolutionRunStatus.EMPTY_CONSENSUS
    assert outcome.selected_table is not None
    assert outcome.selected_table.num_rows == 0
    assert len(sources.calls) == 2
    assert len(differences.calls) == 1


def test_one_empty_and_failed_fallbacks_blocks_as_insufficient_empty() -> None:
    outcome, sources, _differences = _resolve(
        {},
        statuses={
            DataUpstream.BAOSTOCK: ResolutionSourceStatus.EMPTY,
            DataUpstream.EASTMONEY: ResolutionSourceStatus.CALL_FAILED,
            DataUpstream.TENCENT: ResolutionSourceStatus.CALL_FAILED,
            DataUpstream.SINA: ResolutionSourceStatus.CALL_FAILED,
        },
    )

    assert outcome.run.run_blockers == (ResolutionRunBlocker.INSUFFICIENT_EMPTY_CONSENSUS,)
    assert [call.upstream for call in sources.calls] == [
        DataUpstream.BAOSTOCK,
        DataUpstream.EASTMONEY,
        DataUpstream.TENCENT,
        DataUpstream.SINA,
    ]


def test_preflight_rejection_happens_before_first_c_fetch() -> None:
    version_id = new_entity_id("data_version")
    sources = _Sources(
        version_id,
        {
            DataUpstream.BAOSTOCK: [_row(_END, 10.0)],
            DataUpstream.EASTMONEY: [_row(_END, 11.0)],
        },
        reject_preflight=DataUpstream.TENCENT,
    )
    request = ResolutionRequest(
        created_job_id=new_entity_id("job"),
        candidate_version_id=version_id,
        created_at=UtcTimestamp.from_iso("2026-08-12T02:00:00Z"),
        input_fingerprint="a" * 64,
        symbols=(_SYMBOL,),
        start_date=_START,
        end_date=_END,
    )

    with pytest.raises(RuntimeError, match="preflight rejection"):
        K3SourceResolutionService(sources=sources, differences=_Differences()).resolve(request)
    assert [call.upstream for call in sources.calls] == [
        DataUpstream.BAOSTOCK,
        DataUpstream.EASTMONEY,
    ]
    assert sources.preflights[0][2] == 2


def test_d_preflight_rejection_happens_before_first_d_fetch() -> None:
    version_id = new_entity_id("data_version")
    sources = _Sources(
        version_id,
        {
            DataUpstream.BAOSTOCK: [_row(_END, 10.0)],
            DataUpstream.EASTMONEY: [_row(_END, 11.0)],
            DataUpstream.TENCENT: [_row(_END, 12.0)],
        },
        reject_preflight=DataUpstream.SINA,
    )
    request = ResolutionRequest(
        created_job_id=new_entity_id("job"),
        candidate_version_id=version_id,
        created_at=UtcTimestamp.from_iso("2026-08-12T02:00:00Z"),
        input_fingerprint="a" * 64,
        symbols=(_SYMBOL,),
        start_date=_START,
        end_date=_END,
    )

    with pytest.raises(RuntimeError, match="preflight rejection"):
        K3SourceResolutionService(sources=sources, differences=_Differences()).resolve(request)
    assert [call.upstream for call in sources.calls] == [
        DataUpstream.BAOSTOCK,
        DataUpstream.EASTMONEY,
        DataUpstream.TENCENT,
    ]
    assert sources.preflights[-1][2] == 3


@pytest.mark.parametrize(
    "c_status",
    [ResolutionSourceStatus.CALL_FAILED, ResolutionSourceStatus.QUALITY_FAILED],
)
def test_c_failure_can_be_resolved_by_a_and_d(
    c_status: ResolutionSourceStatus,
) -> None:
    outcome, sources, differences = _resolve(
        {
            DataUpstream.BAOSTOCK: [_row(_END, 10.0)],
            DataUpstream.EASTMONEY: [_row(_END, 11.0)],
            DataUpstream.SINA: [_row(_END, 10.0)],
        },
        statuses={DataUpstream.TENCENT: c_status},
    )

    decision = outcome.run.exceptions[0]
    assert decision.reason_code is ResolutionReasonCode.AD_AGREE_A_SELECTED
    assert decision.source_bindings[2].status is c_status
    assert [trace.upstream for trace in decision.request_traces] == [
        DataUpstream.TENCENT,
        DataUpstream.SINA,
    ]
    assert {(left, right) for left, right, _keys in differences.calls} == {
        (DataUpstream.BAOSTOCK, DataUpstream.EASTMONEY),
        (DataUpstream.BAOSTOCK, DataUpstream.SINA),
        (DataUpstream.EASTMONEY, DataUpstream.SINA),
    }
    assert sources.calls[-1].upstream is DataUpstream.SINA


def test_d_call_failure_remains_conflict_with_real_trace_and_no_report() -> None:
    outcome, _sources, differences = _resolve(
        {
            DataUpstream.BAOSTOCK: [_row(_END, 10.0)],
            DataUpstream.EASTMONEY: [_row(_END, 11.0)],
            DataUpstream.TENCENT: [_row(_END, 12.0)],
        },
        statuses={DataUpstream.SINA: ResolutionSourceStatus.CALL_FAILED},
    )

    conflict = outcome.run.conflicts[0]
    assert conflict.source_bindings[-1].status is ResolutionSourceStatus.CALL_FAILED
    assert [trace.upstream for trace in conflict.request_traces] == [
        DataUpstream.TENCENT,
        DataUpstream.SINA,
    ]
    assert all(right is not DataUpstream.SINA for _left, right, _keys in differences.calls)


def test_difference_pair_scopes_follow_exact_exceptional_calls() -> None:
    middle = date(2026, 8, 6)
    a_rows = [_row(_START, 10.0), _row(middle, 10.5), _row(_END, 11.0)]
    b_rows = [_row(_START, 10.1), _row(middle, 10.5), _row(_END, 11.1)]
    _outcome, _sources, differences = _resolve(
        {
            DataUpstream.BAOSTOCK: a_rows,
            DataUpstream.EASTMONEY: b_rows,
            DataUpstream.TENCENT: a_rows,
        }
    )

    ab = next(
        keys
        for left, right, keys in differences.calls
        if (left, right) == (DataUpstream.BAOSTOCK, DataUpstream.EASTMONEY)
    )
    ac_scopes = [
        keys
        for left, right, keys in differences.calls
        if (left, right) == (DataUpstream.BAOSTOCK, DataUpstream.TENCENT)
    ]
    assert [key.trade_date for key in ab] == [_START, middle, _END]
    assert [[key.trade_date for key in keys] for keys in ac_scopes] == [
        [_START],
        [_END],
    ]
