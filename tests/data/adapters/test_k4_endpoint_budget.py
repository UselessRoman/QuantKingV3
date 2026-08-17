from __future__ import annotations

import pytest

from quantking.data.adapters.sources.budget import (
    K4EndpointBudget,
    NetworkBudgetExceededError,
)
from quantking.data.models import DataUpstream

_LIMITS = {
    DataUpstream.BAOSTOCK: 2,
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
        # Missing one production upstream.
        ({DataUpstream.BAOSTOCK: 2, DataUpstream.TENCENT: 2}, 6),
        # Retired upstream must not join the three-role limits.
        ({**_LIMITS, DataUpstream.EASTMONEY: 0}, 6),
        # Negative per-source limit.
        (
            {
                **{k: v for k, v in _LIMITS.items() if k is not DataUpstream.SINA},
                DataUpstream.SINA: -1,
            },
            6,
        ),
        # Boolean per-source limit.
        (
            {
                **{k: v for k, v in _LIMITS.items() if k is not DataUpstream.SINA},
                DataUpstream.SINA: True,
            },
            6,
        ),
        # Negative total.
        (_LIMITS, -1),
        # Total smaller than the sum of per-source limits.
        (_LIMITS, 5),
    ],
)
def test_constructor_rejects_non_exact_or_invalid_limits(
    limits: dict[DataUpstream, int], total: int
) -> None:
    with pytest.raises(ValueError):
        K4EndpointBudget(per_source_limits=limits, total_limit=total)


def test_primary_plan_is_single_freeze_and_rejects_non_primary() -> None:
    budget = K4EndpointBudget(per_source_limits=_LIMITS, total_limit=6)
    budget.freeze_primary(DataUpstream.BAOSTOCK, chunk_count=2)
    budget.freeze_primary(DataUpstream.TENCENT, chunk_count=2)

    with pytest.raises(ValueError, match="already frozen"):
        budget.freeze_primary(DataUpstream.BAOSTOCK, chunk_count=1)
    with pytest.raises(ValueError, match="BAOSTOCK or TENCENT"):
        budget.freeze_primary(DataUpstream.SINA, chunk_count=1)
    with pytest.raises(ValueError, match="BAOSTOCK or TENCENT"):
        budget.freeze_primary(DataUpstream.EASTMONEY, chunk_count=1)


def test_primary_plan_fails_before_per_source_overflow() -> None:
    overflow = K4EndpointBudget(per_source_limits=_LIMITS, total_limit=6)
    with pytest.raises(NetworkBudgetExceededError):
        overflow.freeze_primary(DataUpstream.BAOSTOCK, chunk_count=3)


def test_exception_preflight_is_frozen_once_sums_counts_and_orders() -> None:
    budget = K4EndpointBudget(per_source_limits=_LIMITS, total_limit=6)
    budget.preflight_exception(_TX_REQUESTS)

    with pytest.raises(ValueError, match="already frozen"):
        budget.preflight_exception(())
    with pytest.raises(NetworkBudgetExceededError):
        budget.consume_exception(_TX_REQUESTS[1])  # wrong order
    budget.consume_exception(_TX_REQUESTS[0])
    budget.consume_exception(_TX_REQUESTS[1])
    with pytest.raises(NetworkBudgetExceededError):
        budget.consume_exception(_TX_REQUESTS[0])  # exhausted

    assert budget.summary()["endpoint_planned"]["SINA"] == 2


@pytest.mark.parametrize(
    "requests",
    [
        (("a", "b"),),  # wrong tuple length
        (("", "2026-08-01", "2026-08-07", 1),),  # empty first field
        (("600000.SH", "", "2026-08-07", 1),),  # empty second field
        (("600000.SH", "2026-08-01", "2026-08-07", -1),),  # negative count
        (("600000.SH", "2026-08-01", "2026-08-07", True),),  # boolean count
        (("600000.SH", "2026-08-01", "2026-08-07", "1"),),  # non-int count
    ],
)
def test_exception_preflight_rejects_bad_tuples_without_freezing(
    requests: tuple[tuple[str, str, str, int], ...],
) -> None:
    budget = K4EndpointBudget(per_source_limits=_LIMITS, total_limit=6)
    with pytest.raises(ValueError):
        budget.preflight_exception(requests)

    assert budget.summary()["endpoint_planned"]["SINA"] == 0


def test_exception_overflow_does_not_freeze_partial_plan() -> None:
    budget = K4EndpointBudget(per_source_limits=_LIMITS, total_limit=6)
    oversized = (("000001.SZ", "2026-08-01", "2026-08-07", 3),)
    with pytest.raises(NetworkBudgetExceededError):
        budget.preflight_exception(oversized)

    valid = (("000001.SZ", "2026-08-01", "2026-08-07", 2),)
    budget.preflight_exception(valid)
    budget.consume_exception(valid[0])


def test_before_endpoint_guards_plan_and_rejects_non_production() -> None:
    budget = K4EndpointBudget(per_source_limits=_LIMITS, total_limit=6)
    budget.freeze_primary(DataUpstream.BAOSTOCK, chunk_count=1)
    budget.before_endpoint(DataUpstream.BAOSTOCK)
    with pytest.raises(NetworkBudgetExceededError):
        budget.before_endpoint(DataUpstream.BAOSTOCK)  # actual >= planned
    with pytest.raises(NetworkBudgetExceededError):
        budget.before_endpoint(DataUpstream.EASTMONEY)  # retired upstream
    with pytest.raises(NetworkBudgetExceededError):
        budget.before_endpoint(DataUpstream.XTQUANT)  # legacy upstream
    with pytest.raises(NetworkBudgetExceededError):
        budget.before_endpoint(DataUpstream.TENCENT)  # unplanned (0 >= 0)


def test_actual_calls_cannot_exceed_combined_total() -> None:
    tight = K4EndpointBudget(
        per_source_limits={
            DataUpstream.BAOSTOCK: 1,
            DataUpstream.TENCENT: 1,
            DataUpstream.SINA: 1,
        },
        total_limit=3,
    )
    tight.freeze_primary(DataUpstream.BAOSTOCK, chunk_count=1)
    tight.freeze_primary(DataUpstream.TENCENT, chunk_count=1)
    tight.preflight_exception((("600000.SH", "2026-08-07", "2026-08-07", 1),))
    for upstream in (DataUpstream.BAOSTOCK, DataUpstream.TENCENT, DataUpstream.SINA):
        tight.before_endpoint(upstream)

    assert tight.summary()["endpoint_actual_total"] == 3
    with pytest.raises(NetworkBudgetExceededError):
        tight.before_endpoint(DataUpstream.SINA)


def test_sina_stays_zero_planned_and_actual_when_a_and_b_agree() -> None:
    budget = K4EndpointBudget(per_source_limits=_LIMITS, total_limit=6)
    budget.freeze_primary(DataUpstream.BAOSTOCK, chunk_count=2)
    budget.freeze_primary(DataUpstream.TENCENT, chunk_count=2)
    budget.before_endpoint(DataUpstream.BAOSTOCK)
    budget.before_endpoint(DataUpstream.BAOSTOCK)
    budget.before_endpoint(DataUpstream.TENCENT)
    budget.before_endpoint(DataUpstream.TENCENT)

    summary = budget.summary()
    assert summary["endpoint_planned"]["SINA"] == 0
    assert summary["endpoint_actual"]["SINA"] == 0


def test_summary_has_exact_three_source_keys_and_http_upper_bounds() -> None:
    budget = K4EndpointBudget(per_source_limits=_LIMITS, total_limit=6)
    budget.freeze_primary(DataUpstream.BAOSTOCK, chunk_count=2)
    budget.freeze_primary(DataUpstream.TENCENT, chunk_count=2)
    sina = (("600000.SH", "2026-08-07", "2026-08-07", 1),)
    budget.preflight_exception(sina)
    budget.before_endpoint(DataUpstream.BAOSTOCK)
    budget.before_endpoint(DataUpstream.TENCENT)

    summary = budget.summary()
    assert set(summary) == {
        "endpoint_limits",
        "endpoint_planned",
        "endpoint_actual",
        "endpoint_planned_total",
        "endpoint_actual_total",
        "baostock_protocol_message_upper_bound",
        "akshare_locked_source_http_upper_bound",
        "application_retries",
    }
    planned = summary["endpoint_planned"]
    actual = summary["endpoint_actual"]
    assert isinstance(planned, dict)
    assert isinstance(actual, dict)
    assert set(planned) == {"BAOSTOCK", "TENCENT", "SINA"}
    assert set(actual) == {"BAOSTOCK", "TENCENT", "SINA"}
    assert "EASTMONEY" not in str(summary)
    assert "XTQUANT" not in str(summary)
    assert summary["endpoint_limits"] == {
        "total": 6,
        "per_source": {"BAOSTOCK": 2, "TENCENT": 2, "SINA": 2},
    }
    assert summary["endpoint_planned"] == {"BAOSTOCK": 2, "TENCENT": 2, "SINA": 1}
    assert summary["endpoint_actual"] == {"BAOSTOCK": 1, "TENCENT": 1, "SINA": 0}
    assert summary["endpoint_planned_total"] == 5
    assert summary["endpoint_actual_total"] == 2
    assert summary["baostock_protocol_message_upper_bound"] == {
        "login": 2,
        "query": 2,
        "logout_attempt": 2,
        "total": 6,
    }
    assert summary["akshare_locked_source_http_upper_bound"] == {
        "TENCENT": 6,  # 2 planned * 3 HTTP requests per Tencent endpoint
        "SINA": 2,  # 1 planned * 2 HTTP requests per Sina endpoint
        "total": 8,
    }
    assert summary["application_retries"] == 0
