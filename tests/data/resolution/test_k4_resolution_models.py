from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from quantking.common.ids import EntityId, parse_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.comparison.thresholds import (
    K3_CANDIDATE_RULE_VERSION,
    THRESHOLD_RULE_VERSION,
)
from quantking.data.models import DatasetKind, DataUpstream
from quantking.data.resolution.models import (
    K2_RESOLUTION_RULE_VERSION,
    K3_RESOLUTION_RULE_VERSION,
    K4_RESOLUTION_RULE_VERSION,
    RESOLUTION_RULE_VERSION,
    CRequestTrace,
    ResolutionDecision,
    ResolutionDefaultSummary,
    ResolutionReasonCode,
    ResolutionRequestTrace,
    ResolutionRun,
    ResolutionRunStatus,
    ResolutionSourceBinding,
    ResolutionSourceStatus,
)

_DAY = date(2026, 8, 7)
_CREATED = UtcTimestamp.from_iso("2026-08-12T01:02:03Z")

_A, _B, _C = (
    DataUpstream.BAOSTOCK,
    DataUpstream.TENCENT,
    DataUpstream.SINA,
)


def _id(namespace: str, suffix: int) -> EntityId:
    return parse_entity_id(f"{namespace}:00000000-0000-4000-8000-{suffix:012d}")


def _binding(
    upstream: DataUpstream,
    status: ResolutionSourceStatus,
    suffix: int | None,
) -> ResolutionSourceBinding:
    return ResolutionSourceBinding(
        upstream=upstream,
        status=status,
        batch_id=None if suffix is None else _id("data_batch", suffix),
        content_sha256=None if suffix is None else f"{suffix:x}" * 64,
    )


def _trace(upstream: DataUpstream) -> ResolutionRequestTrace:
    return ResolutionRequestTrace(
        upstream=upstream,
        symbol="600000.SH",
        start_date=_DAY,
        end_date=_DAY,
        trigger_reason="AB_AFFECTED_RANGE",
    )


def _k4_decision(
    reason: ResolutionReasonCode,
    *,
    agreeing: tuple[DataUpstream, ...],
    selected: DataUpstream | None,
    statuses: tuple[ResolutionSourceStatus, ResolutionSourceStatus, ResolutionSourceStatus],
    selected_row_present: bool | None = None,
    request_traces: tuple[ResolutionRequestTrace, ...] = (),
) -> ResolutionDecision:
    bindings = tuple(
        _binding(
            upstream,
            status,
            None if status is ResolutionSourceStatus.NOT_CALLED else suffix,
        )
        for upstream, status, suffix in zip(
            (_A, _B, _C),
            statuses,
            (1, 2, 3),
            strict=True,
        )
    )
    comparable = tuple(
        upstream
        for upstream, status in zip((_A, _B, _C), statuses, strict=True)
        if status in {ResolutionSourceStatus.AVAILABLE, ResolutionSourceStatus.MISSING_ROW}
    )
    return ResolutionDecision(
        symbol="600000.SH",
        trade_date=_DAY,
        source_bindings=bindings,
        compared_upstreams=comparable,
        agreeing_upstreams=agreeing,
        selected_upstream=selected,
        selected_row_present=selected_row_present,
        reason_code=reason,
        rule_version=K4_RESOLUTION_RULE_VERSION,
        difference_report_ids=(_id("data_diff", 11),),
        created_job_id=_id("job", 21),
        created_at=_CREATED,
        request_traces=request_traces,
    )


def test_k4_is_the_active_resolution_rule_version() -> None:
    assert RESOLUTION_RULE_VERSION == K4_RESOLUTION_RULE_VERSION
    assert K4_RESOLUTION_RULE_VERSION == "source-resolution.daily-bar-unadjusted.k4.v1"


def test_k4_default_summary_freezes_three_roles_with_sina_not_called() -> None:
    a = _binding(_A, ResolutionSourceStatus.AVAILABLE, 1)
    b = _binding(_B, ResolutionSourceStatus.AVAILABLE, 2)
    c = _binding(_C, ResolutionSourceStatus.NOT_CALLED, None)
    summary = ResolutionDefaultSummary(
        source_bindings=(a, b, c),
        compared_upstreams=(_A, _B),
        agreeing_upstreams=(_A, _B),
        selected_upstream=_A,
        reason_code=ResolutionReasonCode.AB_AGREE_DEFAULT,
        decision_count=2,
    )

    serialized = summary.to_dict()["source_bindings"]
    assert isinstance(serialized, list)
    assert [item["upstream"] for item in serialized if isinstance(item, dict)] == [
        "BAOSTOCK",
        "TENCENT",
        "SINA",
    ]
    assert summary.to_dict()["selected_upstream"] == "BAOSTOCK"
    assert summary.source_bindings[2].status is ResolutionSourceStatus.NOT_CALLED
    assert summary.source_bindings[2].batch_id is None
    assert summary.source_bindings[2].content_sha256 is None


def test_k4_source_bindings_must_use_the_fixed_three_role_order() -> None:
    with pytest.raises(ValueError, match="fixed source order"):
        ResolutionDecision(
            symbol="600000.SH",
            trade_date=_DAY,
            source_bindings=(
                _binding(_A, ResolutionSourceStatus.AVAILABLE, 1),
                _binding(_B, ResolutionSourceStatus.AVAILABLE, 2),
            ),
            compared_upstreams=(_A, _B),
            agreeing_upstreams=(_A, _B),
            selected_upstream=_A,
            selected_row_present=True,
            reason_code=ResolutionReasonCode.AB_AGREE_A_SELECTED,
            rule_version=K4_RESOLUTION_RULE_VERSION,
            difference_report_ids=(_id("data_diff", 11),),
            created_job_id=_id("job", 21),
            created_at=_CREATED,
        )

    with pytest.raises(ValueError, match="fixed source order"):
        ResolutionDecision(
            symbol="600000.SH",
            trade_date=_DAY,
            source_bindings=(
                _binding(_B, ResolutionSourceStatus.AVAILABLE, 2),
                _binding(_A, ResolutionSourceStatus.AVAILABLE, 1),
                _binding(_C, ResolutionSourceStatus.NOT_CALLED, None),
            ),
            compared_upstreams=(_A, _B),
            agreeing_upstreams=(_A, _B),
            selected_upstream=_A,
            selected_row_present=True,
            reason_code=ResolutionReasonCode.AB_AGREE_A_SELECTED,
            rule_version=K4_RESOLUTION_RULE_VERSION,
            difference_report_ids=(_id("data_diff", 11),),
            created_job_id=_id("job", 21),
            created_at=_CREATED,
        )


@pytest.mark.parametrize(
    ("reason", "agreeing", "selected", "statuses"),
    [
        (
            ResolutionReasonCode.ABC_AGREE_A_SELECTED,
            (_A, _B, _C),
            _A,
            (
                ResolutionSourceStatus.AVAILABLE,
                ResolutionSourceStatus.AVAILABLE,
                ResolutionSourceStatus.AVAILABLE,
            ),
        ),
        (
            ResolutionReasonCode.AB_AGREE_A_SELECTED,
            (_A, _B),
            _A,
            (
                ResolutionSourceStatus.AVAILABLE,
                ResolutionSourceStatus.AVAILABLE,
                ResolutionSourceStatus.NOT_CALLED,
            ),
        ),
        (
            ResolutionReasonCode.AC_AGREE_A_SELECTED,
            (_A, _C),
            _A,
            (
                ResolutionSourceStatus.AVAILABLE,
                ResolutionSourceStatus.MISSING_ROW,
                ResolutionSourceStatus.AVAILABLE,
            ),
        ),
        (
            ResolutionReasonCode.BC_AGREE_B_SELECTED,
            (_B, _C),
            _B,
            (
                ResolutionSourceStatus.MISSING_ROW,
                ResolutionSourceStatus.AVAILABLE,
                ResolutionSourceStatus.AVAILABLE,
            ),
        ),
    ],
)
def test_k4_expected_resolution_mapping_selects_the_frozen_priority(
    reason: ResolutionReasonCode,
    agreeing: tuple[DataUpstream, ...],
    selected: DataUpstream,
    statuses: tuple[ResolutionSourceStatus, ResolutionSourceStatus, ResolutionSourceStatus],
) -> None:
    traces = () if statuses[2] is ResolutionSourceStatus.NOT_CALLED else (_trace(_C),)
    decision = _k4_decision(
        reason,
        agreeing=agreeing,
        selected=selected,
        statuses=statuses,
        selected_row_present=True,
        request_traces=traces,
    )
    assert decision.agreeing_upstreams == agreeing
    assert decision.selected_upstream is selected
    assert decision.threshold_rule_version == THRESHOLD_RULE_VERSION
    assert decision.to_dict()["threshold_rule_version"] == THRESHOLD_RULE_VERSION

    wrong_selected = agreeing[1]
    with pytest.raises(ValueError, match="reason_code"):
        _k4_decision(
            reason,
            agreeing=agreeing,
            selected=wrong_selected,
            statuses=statuses,
            selected_row_present=True,
            request_traces=traces,
        )


def test_k4_conflict_reasons_require_no_selected_source() -> None:
    no_consensus = _k4_decision(
        ResolutionReasonCode.NO_TWO_SOURCE_CONSENSUS,
        agreeing=(),
        selected=None,
        statuses=(
            ResolutionSourceStatus.AVAILABLE,
            ResolutionSourceStatus.AVAILABLE,
            ResolutionSourceStatus.AVAILABLE,
        ),
        selected_row_present=None,
        request_traces=(_trace(_C),),
    )
    assert no_consensus.conflict
    assert no_consensus.selected_upstream is None
    assert no_consensus.agreeing_upstreams == ()

    isolated = _k4_decision(
        ResolutionReasonCode.SINGLE_SOURCE_ROW_ISOLATED,
        agreeing=(),
        selected=None,
        statuses=(
            ResolutionSourceStatus.AVAILABLE,
            ResolutionSourceStatus.MISSING_ROW,
            ResolutionSourceStatus.MISSING_ROW,
        ),
        selected_row_present=None,
        request_traces=(_trace(_C),),
    )
    assert isolated.conflict
    assert isolated.reason_code is ResolutionReasonCode.SINGLE_SOURCE_ROW_ISOLATED

    with pytest.raises(ValueError, match="two-source agreement"):
        _k4_decision(
            ResolutionReasonCode.SINGLE_SOURCE_ROW_ISOLATED,
            agreeing=(_A, _B),
            selected=_A,
            statuses=(
                ResolutionSourceStatus.AVAILABLE,
                ResolutionSourceStatus.AVAILABLE,
                ResolutionSourceStatus.NOT_CALLED,
            ),
            selected_row_present=True,
        )


def test_zero_row_legal_empty_consensus_is_run_level_only() -> None:
    with pytest.raises(ValueError, match="run-level reason only"):
        _k4_decision(
            ResolutionReasonCode.ZERO_ROW_LEGAL_EMPTY_CONSENSUS,
            agreeing=(_A, _B),
            selected=_A,
            statuses=(
                ResolutionSourceStatus.AVAILABLE,
                ResolutionSourceStatus.AVAILABLE,
                ResolutionSourceStatus.NOT_CALLED,
            ),
            selected_row_present=True,
        )


def test_k4_compared_upstreams_exclude_empty_source_status() -> None:
    # SINA returned a whole-call legal EMPTY: it is called but must not join
    # the per-row value comparison.
    decision = _k4_decision(
        ResolutionReasonCode.AB_AGREE_A_SELECTED,
        agreeing=(_A, _B),
        selected=_A,
        statuses=(
            ResolutionSourceStatus.AVAILABLE,
            ResolutionSourceStatus.AVAILABLE,
            ResolutionSourceStatus.EMPTY,
        ),
        selected_row_present=True,
        request_traces=(_trace(_C),),
    )
    assert decision.compared_upstreams == (_A, _B)
    assert _C not in decision.compared_upstreams

    with pytest.raises(ValueError, match="exactly match comparable source states"):
        ResolutionDecision(
            symbol="600000.SH",
            trade_date=_DAY,
            source_bindings=decision.source_bindings,
            compared_upstreams=(_A, _B, _C),
            agreeing_upstreams=(_A, _B),
            selected_upstream=_A,
            selected_row_present=True,
            reason_code=ResolutionReasonCode.AB_AGREE_A_SELECTED,
            rule_version=K4_RESOLUTION_RULE_VERSION,
            difference_report_ids=(_id("data_diff", 11),),
            created_job_id=_id("job", 21),
            created_at=_CREATED,
            request_traces=(_trace(_C),),
        )


def test_k4_request_traces_bind_sina_as_the_only_exceptional_source() -> None:
    with pytest.raises(ValueError, match="NOT_CALLED source cannot carry a request trace"):
        _k4_decision(
            ResolutionReasonCode.AB_AGREE_A_SELECTED,
            agreeing=(_A, _B),
            selected=_A,
            statuses=(
                ResolutionSourceStatus.AVAILABLE,
                ResolutionSourceStatus.AVAILABLE,
                ResolutionSourceStatus.NOT_CALLED,
            ),
            selected_row_present=True,
            request_traces=(_trace(_C),),
        )

    with pytest.raises(ValueError, match="requires one request trace"):
        _k4_decision(
            ResolutionReasonCode.ABC_AGREE_A_SELECTED,
            agreeing=(_A, _B, _C),
            selected=_A,
            statuses=(
                ResolutionSourceStatus.AVAILABLE,
                ResolutionSourceStatus.AVAILABLE,
                ResolutionSourceStatus.AVAILABLE,
            ),
            selected_row_present=True,
            request_traces=(),
        )


def test_threshold_rule_version_is_registry_validated_and_serialized() -> None:
    decision = _k4_decision(
        ResolutionReasonCode.AB_AGREE_A_SELECTED,
        agreeing=(_A, _B),
        selected=_A,
        statuses=(
            ResolutionSourceStatus.AVAILABLE,
            ResolutionSourceStatus.AVAILABLE,
            ResolutionSourceStatus.NOT_CALLED,
        ),
        selected_row_present=True,
    )
    assert decision.threshold_rule_version == THRESHOLD_RULE_VERSION
    assert decision.to_dict()["threshold_rule_version"] == THRESHOLD_RULE_VERSION

    with pytest.raises(ValueError, match="threshold_rule_version is unsupported"):
        replace(
            decision,
            threshold_rule_version="comparison-thresholds.daily-bar-unadjusted.k9.v1",
        )

    candidate = replace(decision, threshold_rule_version=K3_CANDIDATE_RULE_VERSION)
    assert candidate.threshold_rule_version == K3_CANDIDATE_RULE_VERSION


def _k4_empty_run(*, zero_row_reason_code: ResolutionReasonCode | None) -> ResolutionRun:
    a = _binding(_A, ResolutionSourceStatus.EMPTY, 1)
    b = _binding(_B, ResolutionSourceStatus.EMPTY, 2)
    c = _binding(_C, ResolutionSourceStatus.NOT_CALLED, None)
    return ResolutionRun(
        run_id=_id("data_resolution", 31),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        created_job_id=_id("job", 21),
        created_at=_CREATED,
        rule_version=K4_RESOLUTION_RULE_VERSION,
        threshold_rule_version=THRESHOLD_RULE_VERSION,
        input_fingerprint="f" * 64,
        candidate_version_id=_id("data_version", 41),
        input_bindings=(a, b, c),
        difference_report_ids=(),
        default_summary=None,
        run_blockers=(),
        exceptions=(),
        conflicts=(),
        zero_row_reason_code=zero_row_reason_code,
    )


def _k4_resolved_run(*, zero_row_reason_code: ResolutionReasonCode | None = None) -> ResolutionRun:
    a = _binding(_A, ResolutionSourceStatus.AVAILABLE, 1)
    b = _binding(_B, ResolutionSourceStatus.AVAILABLE, 2)
    c = _binding(_C, ResolutionSourceStatus.NOT_CALLED, None)
    summary = ResolutionDefaultSummary(
        source_bindings=(a, b, c),
        compared_upstreams=(_A, _B),
        agreeing_upstreams=(_A, _B),
        selected_upstream=_A,
        reason_code=ResolutionReasonCode.AB_AGREE_DEFAULT,
        decision_count=1,
    )
    return ResolutionRun(
        run_id=_id("data_resolution", 32),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        created_job_id=_id("job", 22),
        created_at=_CREATED,
        rule_version=K4_RESOLUTION_RULE_VERSION,
        threshold_rule_version=THRESHOLD_RULE_VERSION,
        input_fingerprint="f" * 64,
        candidate_version_id=_id("data_version", 42),
        input_bindings=(a, b, c),
        difference_report_ids=(),
        default_summary=summary,
        run_blockers=(),
        exceptions=(),
        conflicts=(),
        zero_row_reason_code=zero_row_reason_code,
    )


def test_k4_zero_row_publication_requires_the_legal_empty_consensus_reason() -> None:
    run = _k4_empty_run(zero_row_reason_code=ResolutionReasonCode.ZERO_ROW_LEGAL_EMPTY_CONSENSUS)
    assert run.status is ResolutionRunStatus.EMPTY_CONSENSUS
    assert run.zero_row_reason_code is ResolutionReasonCode.ZERO_ROW_LEGAL_EMPTY_CONSENSUS
    assert run.summary_dict()["zero_row_reason_code"] == "ZERO_ROW_LEGAL_EMPTY_CONSENSUS"

    with pytest.raises(ValueError, match="K4 zero-row publication requires"):
        _k4_empty_run(zero_row_reason_code=None)


def test_non_empty_k4_resolution_cannot_carry_a_zero_row_reason_code() -> None:
    assert _k4_resolved_run().status is ResolutionRunStatus.RESOLVED
    with pytest.raises(ValueError, match="non-empty K4 resolution cannot carry"):
        _k4_resolved_run(zero_row_reason_code=ResolutionReasonCode.ZERO_ROW_LEGAL_EMPTY_CONSENSUS)


def _k2_empty_run() -> ResolutionRun:
    a = _binding(DataUpstream.EASTMONEY, ResolutionSourceStatus.EMPTY, 1)
    b = _binding(DataUpstream.TENCENT, ResolutionSourceStatus.EMPTY, 2)
    c = _binding(DataUpstream.SINA, ResolutionSourceStatus.NOT_CALLED, None)
    return ResolutionRun(
        run_id=_id("data_resolution", 33),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        created_job_id=_id("job", 23),
        created_at=_CREATED,
        rule_version=K2_RESOLUTION_RULE_VERSION,
        threshold_rule_version=THRESHOLD_RULE_VERSION,
        input_fingerprint="f" * 64,
        candidate_version_id=_id("data_version", 43),
        input_bindings=(a, b, c),
        difference_report_ids=(),
        default_summary=None,
        run_blockers=(),
        exceptions=(),
        conflicts=(),
    )


def test_k2_k3_zero_row_publication_tolerance_is_unchanged() -> None:
    k2 = _k2_empty_run()
    assert k2.status is ResolutionRunStatus.EMPTY_CONSENSUS
    assert "zero_row_reason_code" not in k2.summary_dict()

    with pytest.raises(ValueError, match="bound to the K4 rule version"):
        replace(
            k2,
            zero_row_reason_code=ResolutionReasonCode.ZERO_ROW_LEGAL_EMPTY_CONSENSUS,
        )


# ---------------------------------------------------------------------------
# Historical frozen compatibility: K2/K3 rules still construct and serialize.
# ---------------------------------------------------------------------------


def _four_bindings(
    *, d_status: ResolutionSourceStatus = ResolutionSourceStatus.NOT_CALLED
) -> tuple[ResolutionSourceBinding, ...]:
    return (
        _binding(DataUpstream.BAOSTOCK, ResolutionSourceStatus.AVAILABLE, 1),
        _binding(DataUpstream.EASTMONEY, ResolutionSourceStatus.AVAILABLE, 2),
        _binding(DataUpstream.TENCENT, ResolutionSourceStatus.AVAILABLE, 3),
        _binding(
            DataUpstream.SINA,
            d_status,
            None if d_status is ResolutionSourceStatus.NOT_CALLED else 4,
        ),
    )


def test_k3_four_role_decision_still_round_trips_through_to_dict() -> None:
    decision = ResolutionDecision(
        symbol="600000.SH",
        trade_date=_DAY,
        source_bindings=_four_bindings(),
        compared_upstreams=(
            DataUpstream.BAOSTOCK,
            DataUpstream.EASTMONEY,
            DataUpstream.TENCENT,
        ),
        agreeing_upstreams=(DataUpstream.BAOSTOCK, DataUpstream.TENCENT),
        selected_upstream=DataUpstream.BAOSTOCK,
        selected_row_present=True,
        reason_code=ResolutionReasonCode.AC_AGREE_A_SELECTED,
        rule_version=K3_RESOLUTION_RULE_VERSION,
        difference_report_ids=(_id("data_diff", 11),),
        created_job_id=_id("job", 21),
        created_at=_CREATED,
        request_traces=(_trace(DataUpstream.TENCENT),),
    )

    payload = decision.to_dict()
    assert payload["rule_version"] == K3_RESOLUTION_RULE_VERSION
    assert payload["threshold_rule_version"] == THRESHOLD_RULE_VERSION
    assert "c_request" not in payload
    assert payload["request_traces"] == [_trace(DataUpstream.TENCENT).to_dict()]
    assert [item["upstream"] for item in payload["source_bindings"]] == [
        "BAOSTOCK",
        "EASTMONEY",
        "TENCENT",
        "SINA",
    ]
    assert decision.selected_upstream is DataUpstream.BAOSTOCK


def test_k2_legacy_c_request_keeps_original_json_shape() -> None:
    bindings = (
        _binding(DataUpstream.EASTMONEY, ResolutionSourceStatus.AVAILABLE, 1),
        _binding(DataUpstream.TENCENT, ResolutionSourceStatus.AVAILABLE, 2),
        _binding(DataUpstream.SINA, ResolutionSourceStatus.AVAILABLE, 3),
    )
    trace = CRequestTrace(
        symbol="600000.SH",
        start_date=_DAY,
        end_date=_DAY,
        trigger_reason="FIELD_DIFFERENCE:close",
    )
    decision = ResolutionDecision(
        symbol="600000.SH",
        trade_date=_DAY,
        source_bindings=bindings,
        compared_upstreams=(
            DataUpstream.EASTMONEY,
            DataUpstream.TENCENT,
            DataUpstream.SINA,
        ),
        agreeing_upstreams=(DataUpstream.EASTMONEY, DataUpstream.SINA),
        selected_upstream=DataUpstream.EASTMONEY,
        selected_row_present=True,
        reason_code=ResolutionReasonCode.AC_AGREE_A_SELECTED,
        rule_version=K2_RESOLUTION_RULE_VERSION,
        difference_report_ids=(_id("data_diff", 11),),
        created_job_id=_id("job", 21),
        created_at=_CREATED,
        c_request=trace,
    )

    payload = decision.to_dict()
    assert "request_traces" not in payload
    assert payload["c_request"] == {
        "symbol": "600000.SH",
        "start_date": "2026-08-07",
        "end_date": "2026-08-07",
        "trigger_reason": "FIELD_DIFFERENCE:close",
    }
