from __future__ import annotations

from dataclasses import dataclass, replace
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
from quantking.data.resolution import (
    ResolutionKey,
    ResolutionReasonCode,
    ResolutionRequest,
    ResolutionRunBlocker,
    ResolutionRunStatus,
    ResolutionSourceStatus,
    SourceCandidate,
    SourceResolutionService,
)

_START = date(2026, 8, 5)
_END = date(2026, 8, 7)
_SYMBOL = "600000.SH"
_EMPTY = EmptyResultInfo(
    classification=LegalEmptyClassification.NO_TRADING_SESSION,
    reason="explicit no trading session",
)


def _row(trade_date: date, *, close: float = 10.5) -> dict[str, object]:
    return {
        "code": _SYMBOL,
        "trade_date": trade_date,
        "open": 10.0,
        "high": 11.0,
        "low": 9.5,
        "close": close,
        "pre_close": 10.0,
        "volume": 1_000_000,
        "amount": 10_500_000.0,
    }


def _table(rows: list[dict[str, object]]) -> pa.Table:
    return ensure_sorted(
        DatasetKind.DAILY_BAR_UNADJUSTED.value,
        pa.Table.from_pylist(
            rows,
            schema=canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value),
        ),
    )


@dataclass(frozen=True)
class _Call:
    upstream: DataUpstream
    symbols: tuple[str, ...]
    start_date: date
    end_date: date
    trigger_reason: str


class _Sources:
    def __init__(
        self,
        *,
        candidate_version_id: EntityId,
        rows: dict[DataUpstream, list[dict[str, object]]],
        statuses: dict[DataUpstream, ResolutionSourceStatus] | None = None,
    ) -> None:
        self._version = candidate_version_id
        self._rows = rows
        self._statuses = statuses or {}
        self.calls: list[_Call] = []

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
        configured = self._statuses.get(upstream, ResolutionSourceStatus.AVAILABLE)
        batch = new_entity_id("data_batch")
        if configured is ResolutionSourceStatus.CALL_FAILED:
            return SourceCandidate(
                upstream=upstream,
                status=configured,
                symbols=symbols,
                start_date=start_date,
                end_date=end_date,
                batch_id=batch,
                content_sha256=None,
                version_id=None,
                quality_run_id=None,
                table=None,
                empty_result=None,
            )
        if configured is ResolutionSourceStatus.QUALITY_FAILED:
            return SourceCandidate(
                upstream=upstream,
                status=configured,
                symbols=symbols,
                start_date=start_date,
                end_date=end_date,
                batch_id=batch,
                content_sha256="f" * 64,
                version_id=self._version,
                quality_run_id=new_entity_id("data_quality"),
                table=None,
                empty_result=None,
            )
        filtered = [
            item
            for item in self._rows.get(upstream, [])
            if item["code"] in symbols and start_date <= item["trade_date"] <= end_date
        ]
        table = _table(filtered)
        status = configured
        empty_result = _EMPTY if status is ResolutionSourceStatus.EMPTY else None
        sha = canonical_content_sha256(
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
            batch_id=batch,
            content_sha256=sha,
            version_id=self._version,
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


def _request(version: EntityId) -> ResolutionRequest:
    return ResolutionRequest(
        created_job_id=new_entity_id("job"),
        candidate_version_id=version,
        created_at=UtcTimestamp.from_iso("2026-08-09T10:00:00Z"),
        input_fingerprint="a" * 64,
        symbols=(_SYMBOL,),
        start_date=_START,
        end_date=_END,
    )


def _resolve(
    rows: dict[DataUpstream, list[dict[str, object]]],
    *,
    statuses: dict[DataUpstream, ResolutionSourceStatus] | None = None,
) -> tuple[object, _Sources, _Differences]:
    version = new_entity_id("data_version")
    sources = _Sources(candidate_version_id=version, rows=rows, statuses=statuses)
    differences = _Differences()
    result = SourceResolutionService(sources=sources, differences=differences).resolve(
        _request(version)
    )
    return result, sources, differences


def test_ab_identical_uses_default_summary_and_never_calls_c() -> None:
    rows = [_row(_START), _row(_END)]
    result, sources, differences = _resolve(
        {
            DataUpstream.EASTMONEY: rows,
            DataUpstream.TENCENT: rows,
        }
    )

    assert [item.upstream for item in sources.calls] == [
        DataUpstream.EASTMONEY,
        DataUpstream.TENCENT,
    ]
    assert result.run.status is ResolutionRunStatus.RESOLVED
    assert result.run.default_decision_count == 2
    assert result.run.exceptions == ()
    assert result.selected_table.num_rows == 2
    assert [(left, right) for left, right, _ in differences.calls] == [
        (DataUpstream.EASTMONEY, DataUpstream.TENCENT)
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("open", 9.9),
        ("high", 11.1),
        ("low", 9.4),
        ("close", 10.6),
        ("pre_close", 9.9),
        ("volume", 1_000_001),
        ("amount", 10_500_001.0),
    ],
)
def test_any_nonzero_field_difference_calls_c_for_exact_affected_date(
    field: str,
    value: object,
) -> None:
    a_row = _row(_END)
    b_row = {**a_row, field: value}
    result, sources, _differences = _resolve(
        {
            DataUpstream.EASTMONEY: [a_row],
            DataUpstream.TENCENT: [b_row],
            DataUpstream.SINA: [a_row],
        }
    )

    c_call = sources.calls[-1]
    assert c_call.upstream is DataUpstream.SINA
    assert (c_call.start_date, c_call.end_date) == (_END, _END)
    assert c_call.trigger_reason == f"FIELD_DIFFERENCE:{field}"
    decision = result.run.exceptions[0]
    assert decision.reason_code is ResolutionReasonCode.AC_AGREE_A_SELECTED
    assert decision.selected_row_present is True


@pytest.mark.parametrize(
    ("failed", "status", "reason", "selected"),
    [
        (
            DataUpstream.EASTMONEY,
            ResolutionSourceStatus.CALL_FAILED,
            ResolutionReasonCode.BC_AGREE_B_SELECTED,
            DataUpstream.TENCENT,
        ),
        (
            DataUpstream.TENCENT,
            ResolutionSourceStatus.QUALITY_FAILED,
            ResolutionReasonCode.AC_AGREE_A_SELECTED,
            DataUpstream.EASTMONEY,
        ),
    ],
)
def test_one_ab_failure_calls_c_for_full_frozen_range_and_resolves(
    failed: DataUpstream,
    status: ResolutionSourceStatus,
    reason: ResolutionReasonCode,
    selected: DataUpstream,
) -> None:
    survivor = DataUpstream.TENCENT if failed is DataUpstream.EASTMONEY else DataUpstream.EASTMONEY
    row = _row(_END)
    result, sources, _differences = _resolve(
        {survivor: [row], DataUpstream.SINA: [row]},
        statuses={failed: status},
    )

    c_call = sources.calls[-1]
    assert (c_call.start_date, c_call.end_date) == (_START, _END)
    assert result.run.default_summary is None
    assert result.run.exceptions[0].reason_code is reason
    assert result.run.exceptions[0].selected_upstream is selected


def test_global_failure_c_union_includes_dates_outside_survivor_rows() -> None:
    c_only = _row(_START)
    common = _row(_END)
    result, sources, _differences = _resolve(
        {
            DataUpstream.TENCENT: [common],
            DataUpstream.SINA: [c_only, common],
        },
        statuses={DataUpstream.EASTMONEY: ResolutionSourceStatus.CALL_FAILED},
    )

    assert (sources.calls[-1].start_date, sources.calls[-1].end_date) == (_START, _END)
    assert {(item.trade_date, item.conflict) for item in result.run.conflicts} == {(_START, True)}
    assert {item.trade_date for item in result.run.exceptions} == {_END}
    assert result.selected_table is None


@pytest.mark.parametrize("missing", [DataUpstream.EASTMONEY, DataUpstream.TENCENT])
def test_two_sources_can_agree_on_absence_and_omit_the_row(missing: DataUpstream) -> None:
    other_date = _START
    target = _END
    a_rows = [_row(other_date), _row(target)]
    b_rows = [_row(other_date), _row(target)]
    if missing is DataUpstream.EASTMONEY:
        a_rows = [_row(other_date)]
    else:
        b_rows = [_row(other_date)]
    result, _sources, _differences = _resolve(
        {
            DataUpstream.EASTMONEY: a_rows,
            DataUpstream.TENCENT: b_rows,
        },
        statuses={DataUpstream.SINA: ResolutionSourceStatus.EMPTY},
    )

    decision = result.run.exceptions[0]
    expected = (
        ResolutionReasonCode.AC_AGREE_A_SELECTED
        if missing is DataUpstream.EASTMONEY
        else ResolutionReasonCode.BC_AGREE_B_SELECTED
    )
    assert decision.reason_code is expected
    assert decision.selected_row_present is False
    assert result.selected_table.num_rows == 1
    assert result.selected_table.column("trade_date").to_pylist() == [other_date]


def test_empty_available_empty_presence_consensus_produces_zero_rows() -> None:
    result, _sources, differences = _resolve(
        {DataUpstream.TENCENT: [_row(_END)]},
        statuses={
            DataUpstream.EASTMONEY: ResolutionSourceStatus.EMPTY,
            DataUpstream.SINA: ResolutionSourceStatus.EMPTY,
        },
    )

    decision = result.run.exceptions[0]
    assert decision.reason_code is ResolutionReasonCode.AC_AGREE_A_SELECTED
    assert decision.selected_row_present is False
    assert result.selected_table.num_rows == 0
    assert len(differences.calls) == 3


def test_two_empty_sources_publish_legal_zero_rows_without_calling_c() -> None:
    result, sources, differences = _resolve(
        {},
        statuses={
            DataUpstream.EASTMONEY: ResolutionSourceStatus.EMPTY,
            DataUpstream.TENCENT: ResolutionSourceStatus.EMPTY,
        },
    )

    assert len(sources.calls) == 2
    assert result.run.status is ResolutionRunStatus.EMPTY_CONSENSUS
    assert result.run.total_decisions == 0
    assert result.run.default_summary is None
    assert result.selected_table.num_rows == 0
    assert len(differences.calls) == 1


def test_all_sources_failed_blocks_without_inventing_trade_dates() -> None:
    result, sources, differences = _resolve(
        {},
        statuses={source: ResolutionSourceStatus.CALL_FAILED for source in DataUpstream},
    )

    assert len(sources.calls) == 3
    assert result.run.status is ResolutionRunStatus.BLOCKED
    assert result.run.total_decisions == 0
    assert result.run.run_blockers == (ResolutionRunBlocker.ALL_SOURCES_UNAVAILABLE,)
    assert result.run.exceptions == result.run.conflicts == ()
    assert result.selected_table is None
    assert differences.calls == []


def test_sparse_differences_make_two_precise_c_calls() -> None:
    middle = date(2026, 8, 6)
    a_rows = [_row(_START), _row(middle), _row(_END)]
    b_rows = [
        {**_row(_START), "close": 10.6},
        _row(middle),
        {**_row(_END), "volume": 1_000_001},
    ]
    _result, sources, _differences = _resolve(
        {
            DataUpstream.EASTMONEY: a_rows,
            DataUpstream.TENCENT: b_rows,
            DataUpstream.SINA: a_rows,
        }
    )

    c_calls = [item for item in sources.calls if item.upstream is DataUpstream.SINA]
    assert [(item.start_date, item.end_date) for item in c_calls] == [
        (_START, _START),
        (_END, _END),
    ]


def test_source_candidate_rejects_wrong_version_and_rows_outside_request() -> None:
    version = new_entity_id("data_version")
    sources = _Sources(
        candidate_version_id=new_entity_id("data_version"),
        rows={
            DataUpstream.EASTMONEY: [_row(_END)],
            DataUpstream.TENCENT: [_row(_END)],
        },
    )
    with pytest.raises(ValueError, match="mismatched request binding"):
        SourceResolutionService(sources=sources, differences=_Differences()).resolve(
            _request(version)
        )

    valid = _Sources(candidate_version_id=version, rows={})
    table = _table([_row(_END)])
    with pytest.raises(ValueError, match="outside its request"):
        SourceCandidate(
            upstream=DataUpstream.EASTMONEY,
            status=ResolutionSourceStatus.AVAILABLE,
            symbols=(_SYMBOL,),
            start_date=_START,
            end_date=date(2026, 8, 6),
            batch_id=new_entity_id("data_batch"),
            content_sha256=canonical_content_sha256(DatasetKind.DAILY_BAR_UNADJUSTED, [table]),
            version_id=version,
            quality_run_id=new_entity_id("data_quality"),
            table=table,
            empty_result=None,
        )
    del valid


@pytest.mark.parametrize(
    "classification",
    [
        LegalEmptyClassification.OUTSIDE_LISTING_RANGE,
        LegalEmptyClassification.EMPTY_REQUEST_UNIVERSE,
    ],
)
def test_empty_candidate_rejects_non_session_empty_classification(
    classification: LegalEmptyClassification,
) -> None:
    table = _table([])
    empty_result = EmptyResultInfo(classification=classification, reason="not a session empty")
    with pytest.raises(ValueError, match="NO_TRADING_SESSION"):
        SourceCandidate(
            upstream=DataUpstream.EASTMONEY,
            status=ResolutionSourceStatus.EMPTY,
            symbols=(_SYMBOL,),
            start_date=_START,
            end_date=_END,
            batch_id=new_entity_id("data_batch"),
            content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED,
                [table],
                empty_result=empty_result,
            ),
            version_id=new_entity_id("data_version"),
            quality_run_id=new_entity_id("data_quality"),
            table=table,
            empty_result=empty_result,
        )


def test_selected_presence_contract_rejects_tampering() -> None:
    row = _row(_END)
    result, _sources, _differences = _resolve(
        {
            DataUpstream.EASTMONEY: [row],
            DataUpstream.TENCENT: [{**row, "close": 10.6}],
            DataUpstream.SINA: [row],
        }
    )
    with pytest.raises(ValueError, match="selected_row_present"):
        replace(result.run.exceptions[0], selected_row_present=False)
