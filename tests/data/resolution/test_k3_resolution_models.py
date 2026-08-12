from __future__ import annotations

from datetime import date

import pytest

from quantking.common.ids import EntityId, parse_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.models import DataUpstream
from quantking.data.resolution.models import (
    K2_RESOLUTION_RULE_VERSION,
    K3_RESOLUTION_RULE_VERSION,
    CRequestTrace,
    ResolutionDecision,
    ResolutionDefaultSummary,
    ResolutionReasonCode,
    ResolutionRequestTrace,
    ResolutionSourceBinding,
    ResolutionSourceStatus,
)

_DAY = date(2026, 8, 7)
_CREATED = UtcTimestamp.from_iso("2026-08-12T01:02:03Z")


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


def _trace(upstream: DataUpstream) -> ResolutionRequestTrace:
    return ResolutionRequestTrace(
        upstream=upstream,
        symbol="600000.SH",
        start_date=_DAY,
        end_date=_DAY,
        trigger_reason="AFFECTED_SPAN",
    )


def test_k3_default_freezes_four_roles_and_c_d_not_called() -> None:
    bindings = (
        _binding(DataUpstream.BAOSTOCK, ResolutionSourceStatus.AVAILABLE, 1),
        _binding(DataUpstream.EASTMONEY, ResolutionSourceStatus.AVAILABLE, 2),
        _binding(DataUpstream.TENCENT, ResolutionSourceStatus.NOT_CALLED, None),
        _binding(DataUpstream.SINA, ResolutionSourceStatus.NOT_CALLED, None),
    )
    summary = ResolutionDefaultSummary(
        source_bindings=bindings,
        compared_upstreams=(DataUpstream.BAOSTOCK, DataUpstream.EASTMONEY),
        agreeing_upstreams=(DataUpstream.BAOSTOCK, DataUpstream.EASTMONEY),
        selected_upstream=DataUpstream.BAOSTOCK,
        reason_code=ResolutionReasonCode.AB_AGREE_DEFAULT,
        decision_count=2,
    )

    serialized = summary.to_dict()["source_bindings"]
    assert isinstance(serialized, list)
    assert [item["upstream"] for item in serialized if isinstance(item, dict)] == [
        "BAOSTOCK",
        "EASTMONEY",
        "TENCENT",
        "SINA",
    ]


def test_k3_exception_requires_c_trace_and_preserves_a_priority() -> None:
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

    assert decision.selected_upstream is DataUpstream.BAOSTOCK
    assert decision.to_dict()["request_traces"] == [_trace(DataUpstream.TENCENT).to_dict()]
    with pytest.raises(ValueError, match="requires one request trace"):
        ResolutionDecision(
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
        )


def test_k3_conflict_requires_both_called_c_d_traces() -> None:
    decision = ResolutionDecision(
        symbol="600000.SH",
        trade_date=_DAY,
        source_bindings=_four_bindings(d_status=ResolutionSourceStatus.AVAILABLE),
        compared_upstreams=(
            DataUpstream.BAOSTOCK,
            DataUpstream.EASTMONEY,
            DataUpstream.TENCENT,
            DataUpstream.SINA,
        ),
        agreeing_upstreams=(),
        selected_upstream=None,
        selected_row_present=None,
        reason_code=ResolutionReasonCode.NO_TWO_SOURCE_CONSENSUS,
        rule_version=K3_RESOLUTION_RULE_VERSION,
        difference_report_ids=(_id("data_diff", 11),),
        created_job_id=_id("job", 21),
        created_at=_CREATED,
        request_traces=(
            _trace(DataUpstream.TENCENT),
            _trace(DataUpstream.SINA),
        ),
    )

    assert decision.conflict
    with pytest.raises(ValueError, match="requires one request trace"):
        ResolutionDecision(
            symbol="600000.SH",
            trade_date=_DAY,
            source_bindings=_four_bindings(d_status=ResolutionSourceStatus.AVAILABLE),
            compared_upstreams=(
                DataUpstream.BAOSTOCK,
                DataUpstream.EASTMONEY,
                DataUpstream.TENCENT,
                DataUpstream.SINA,
            ),
            agreeing_upstreams=(),
            selected_upstream=None,
            selected_row_present=None,
            reason_code=ResolutionReasonCode.NO_TWO_SOURCE_CONSENSUS,
            rule_version=K3_RESOLUTION_RULE_VERSION,
            difference_report_ids=(_id("data_diff", 11),),
            created_job_id=_id("job", 21),
            created_at=_CREATED,
            request_traces=(_trace(DataUpstream.TENCENT),),
        )


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
