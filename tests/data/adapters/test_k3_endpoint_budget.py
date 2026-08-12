from __future__ import annotations

import pytest

from quantking.data.adapters.sources.budget import (
    K3EndpointBudget,
    NetworkBudgetExceededError,
)
from quantking.data.models import DataUpstream

_LIMITS = {
    DataUpstream.BAOSTOCK: 2,
    DataUpstream.EASTMONEY: 2,
    DataUpstream.TENCENT: 2,
    DataUpstream.SINA: 2,
}
_TX_REQUESTS = (
    ("000001.SZ", "2026-08-01", "2026-08-07", 1),
    ("600000.SH", "2026-08-01", "2026-08-07", 1),
)


@pytest.mark.parametrize(
    ("limits", "total"),
    [
        ({**_LIMITS, DataUpstream.XTQUANT: 0}, 8),
        ({key: value for key, value in _LIMITS.items() if key is not DataUpstream.SINA}, 8),
        ({**_LIMITS, DataUpstream.SINA: -1}, 8),
        ({**_LIMITS, DataUpstream.SINA: True}, 8),
        (_LIMITS, -1),
        (_LIMITS, 7),
    ],
)
def test_constructor_rejects_non_exact_or_invalid_limits(
    limits: dict[DataUpstream, int], total: int
) -> None:
    with pytest.raises(ValueError):
        K3EndpointBudget(per_source_limits=limits, total_limit=total)


def test_primary_plan_is_single_freeze_and_fails_before_overflow() -> None:
    budget = K3EndpointBudget(per_source_limits=_LIMITS, total_limit=8)
    budget.freeze_primary(DataUpstream.BAOSTOCK, chunk_count=2)
    budget.freeze_primary(DataUpstream.EASTMONEY, chunk_count=2)

    with pytest.raises(ValueError, match="already frozen"):
        budget.freeze_primary(DataUpstream.BAOSTOCK, chunk_count=1)
    with pytest.raises(ValueError, match="BAOSTOCK or EASTMONEY"):
        budget.freeze_primary(DataUpstream.TENCENT, chunk_count=1)
    overflow = K3EndpointBudget(per_source_limits=_LIMITS, total_limit=8)
    with pytest.raises(NetworkBudgetExceededError):
        overflow.freeze_primary(DataUpstream.BAOSTOCK, chunk_count=3)


def test_exception_preflight_is_atomic_and_consumed_in_frozen_order() -> None:
    budget = K3EndpointBudget(per_source_limits=_LIMITS, total_limit=8)
    budget.preflight_exception(DataUpstream.TENCENT, _TX_REQUESTS)

    with pytest.raises(NetworkBudgetExceededError):
        budget.consume_exception(DataUpstream.TENCENT, _TX_REQUESTS[1])
    budget.consume_exception(DataUpstream.TENCENT, _TX_REQUESTS[0])
    budget.consume_exception(DataUpstream.TENCENT, _TX_REQUESTS[1])
    with pytest.raises(NetworkBudgetExceededError):
        budget.consume_exception(DataUpstream.TENCENT, _TX_REQUESTS[1])
    with pytest.raises(ValueError, match="already frozen"):
        budget.preflight_exception(DataUpstream.TENCENT, ())


def test_exception_overflow_does_not_freeze_partial_plan() -> None:
    budget = K3EndpointBudget(per_source_limits=_LIMITS, total_limit=8)
    oversized = (("000001.SZ", "2026-08-01", "2026-08-07", 3),)
    with pytest.raises(NetworkBudgetExceededError):
        budget.preflight_exception(DataUpstream.SINA, oversized)

    valid = (("000001.SZ", "2026-08-01", "2026-08-07", 2),)
    budget.preflight_exception(DataUpstream.SINA, valid)
    budget.consume_exception(DataUpstream.SINA, valid[0])


def test_actual_endpoint_calls_cannot_exceed_plan_source_total_or_include_xtquant() -> None:
    budget = K3EndpointBudget(per_source_limits=_LIMITS, total_limit=8)
    budget.freeze_primary(DataUpstream.BAOSTOCK, chunk_count=1)
    budget.before_endpoint(DataUpstream.BAOSTOCK)
    with pytest.raises(NetworkBudgetExceededError):
        budget.before_endpoint(DataUpstream.BAOSTOCK)
    with pytest.raises(NetworkBudgetExceededError):
        budget.before_endpoint(DataUpstream.EASTMONEY)
    with pytest.raises(NetworkBudgetExceededError):
        budget.before_endpoint(DataUpstream.XTQUANT)

    tight = K3EndpointBudget(
        per_source_limits={key: 1 for key in _LIMITS},
        total_limit=4,
    )
    for upstream in (DataUpstream.BAOSTOCK, DataUpstream.EASTMONEY):
        tight.freeze_primary(upstream, chunk_count=1)
    for upstream in (DataUpstream.TENCENT, DataUpstream.SINA):
        request = (("600000.SH", "2026-08-07", "2026-08-07", 1),)
        tight.preflight_exception(upstream, request)
    for upstream in _LIMITS:
        tight.before_endpoint(upstream)
    with pytest.raises(NetworkBudgetExceededError):
        tight.before_endpoint(DataUpstream.SINA)


def test_summary_has_exact_four_source_endpoint_and_separate_wire_upper_bounds() -> None:
    budget = K3EndpointBudget(per_source_limits=_LIMITS, total_limit=8)
    budget.freeze_primary(DataUpstream.BAOSTOCK, chunk_count=2)
    budget.freeze_primary(DataUpstream.EASTMONEY, chunk_count=2)
    budget.preflight_exception(DataUpstream.TENCENT, _TX_REQUESTS)
    sina = (("600000.SH", "2026-08-07", "2026-08-07", 1),)
    budget.preflight_exception(DataUpstream.SINA, sina)
    budget.before_endpoint(DataUpstream.BAOSTOCK)
    budget.before_endpoint(DataUpstream.EASTMONEY)
    budget.before_endpoint(DataUpstream.TENCENT)

    summary = budget.summary()
    expected_keys = {"BAOSTOCK", "EASTMONEY", "TENCENT", "SINA"}
    planned = summary["endpoint_planned"]
    actual = summary["endpoint_actual"]
    assert isinstance(planned, dict)
    assert isinstance(actual, dict)
    assert set(planned) == expected_keys
    assert set(actual) == expected_keys
    assert "XTQUANT" not in str(summary)
    assert summary["endpoint_planned"] == {
        "BAOSTOCK": 2,
        "EASTMONEY": 2,
        "TENCENT": 2,
        "SINA": 1,
    }
    assert summary["endpoint_actual"] == {
        "BAOSTOCK": 1,
        "EASTMONEY": 1,
        "TENCENT": 1,
        "SINA": 0,
    }
    assert summary["baostock_protocol_message_upper_bound"] == {
        "login": 2,
        "query": 2,
        "logout_attempt": 2,
        "total": 6,
    }
    assert summary["akshare_locked_source_http_upper_bound"] == {
        "EASTMONEY": 2,
        "TENCENT": 6,
        "SINA": 2,
        "total": 10,
    }
    assert summary["application_retries"] == 0
