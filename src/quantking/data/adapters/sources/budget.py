"""Per-Job AKShare endpoint invocation budget enforced at the source boundary."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from quantking.data.adapters.sources.registry import (
    AKSHARE_RUNTIME_UPSTREAMS,
    PRODUCTION_UPSTREAMS,
)
from quantking.data.models import DataUpstream


class NetworkBudgetExceededError(RuntimeError):
    """Stable fail-closed error raised before an invocation would exceed its budget."""

    code = "NETWORK_BUDGET_EXCEEDED"

    def __init__(self) -> None:
        super().__init__(self.code)


class EndpointInvocationBudget:
    """Track planned and actual calls; C must be reserved as one complete set."""

    def __init__(self, *, total_limit: int, c_limit: int) -> None:
        if total_limit != 12 or c_limit != 4:
            raise ValueError("S1-08 endpoint invocation budget must equal 12/4")
        self._total_limit = total_limit
        self._c_limit = c_limit
        self._planned: Counter[DataUpstream] = Counter()
        self._actual: Counter[DataUpstream] = Counter()
        self._c_expected: list[tuple[str, str, str, int]] | None = None

    def plan_ab(self, upstream: DataUpstream, *, chunk_count: int) -> None:
        if upstream not in {DataUpstream.EASTMONEY, DataUpstream.TENCENT}:
            raise ValueError("plan_ab accepts EASTMONEY or TENCENT only")
        if chunk_count < 0 or self._planned[upstream] != 0:
            raise ValueError("A/B invocation plan is invalid or duplicated")
        if chunk_count > 4 or sum(self._planned.values()) + chunk_count > self._total_limit:
            raise NetworkBudgetExceededError
        self._planned[upstream] = chunk_count

    def preflight_c(self, requests: tuple[tuple[str, str, str, int], ...]) -> None:
        if self._c_expected is not None:
            raise ValueError("C invocation plan was already frozen")
        count = sum(item[3] for item in requests)
        if count > self._c_limit or sum(self._planned.values()) + count > self._total_limit:
            raise NetworkBudgetExceededError
        self._planned[DataUpstream.SINA] = count
        self._c_expected = list(requests)

    def consume_c_plan(self, request: tuple[str, str, str, int]) -> None:
        if self._c_expected is None:
            count = request[3]
            if (
                count > self._c_limit
                or self._planned[DataUpstream.SINA] + count > self._c_limit
                or sum(self._planned.values()) + count > self._total_limit
            ):
                raise NetworkBudgetExceededError
            self._planned[DataUpstream.SINA] += count
            return
        if not self._c_expected or self._c_expected[0] != request:
            raise NetworkBudgetExceededError
        self._c_expected.pop(0)

    def before_endpoint(self, upstream: DataUpstream) -> None:
        if not isinstance(upstream, DataUpstream):
            raise TypeError("upstream must be DataUpstream")
        if (
            self._actual[upstream] >= self._planned[upstream]
            or sum(self._actual.values()) >= self._total_limit
            or (upstream is DataUpstream.SINA and self._actual[DataUpstream.SINA] >= self._c_limit)
        ):
            raise NetworkBudgetExceededError
        self._actual[upstream] += 1

    def summary(self) -> Mapping[str, object]:
        return {
            "limits": {"total": self._total_limit, "sina": self._c_limit},
            "planned": {
                upstream.value: self._planned[upstream] for upstream in AKSHARE_RUNTIME_UPSTREAMS
            },
            "actual": {
                upstream.value: self._actual[upstream] for upstream in AKSHARE_RUNTIME_UPSTREAMS
            },
            "planned_total": sum(self._planned.values()),
            "actual_total": sum(self._actual.values()),
            "application_retries": 0,
        }


class K3EndpointBudget:
    """Frozen four-source endpoint budget with separate protocol/HTTP evidence."""

    def __init__(
        self,
        *,
        per_source_limits: Mapping[DataUpstream, int],
        total_limit: int,
    ) -> None:
        if set(per_source_limits) != set(PRODUCTION_UPSTREAMS):
            raise ValueError("K3 budget requires exact production upstream limits")
        if isinstance(total_limit, bool) or not isinstance(total_limit, int) or total_limit < 0:
            raise ValueError("total_limit must be a non-negative integer")
        limits = dict(per_source_limits)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in limits.values()
        ):
            raise ValueError("per-source limits must be non-negative integers")
        if sum(limits.values()) > total_limit:
            raise ValueError("per-source limits must fit the total limit")
        self._limits = limits
        self._total_limit = total_limit
        self._planned: Counter[DataUpstream] = Counter()
        self._actual: Counter[DataUpstream] = Counter()
        self._primary_frozen: set[DataUpstream] = set()
        self._exception_expected: dict[DataUpstream, list[tuple[str, str, str, int]]] = {}

    def freeze_primary(self, upstream: DataUpstream, *, chunk_count: int) -> None:
        if upstream not in {DataUpstream.BAOSTOCK, DataUpstream.EASTMONEY}:
            raise ValueError("primary source must be BAOSTOCK or EASTMONEY")
        if upstream in self._primary_frozen:
            raise ValueError("primary source plan is already frozen")
        self._validate_count(upstream, chunk_count)
        self._planned[upstream] = chunk_count
        self._primary_frozen.add(upstream)

    def preflight_exception(
        self,
        upstream: DataUpstream,
        requests: tuple[tuple[str, str, str, int], ...],
    ) -> None:
        if upstream not in {DataUpstream.TENCENT, DataUpstream.SINA}:
            raise ValueError("exception source must be TENCENT or SINA")
        if upstream in self._exception_expected:
            raise ValueError("exception source plan is already frozen")
        if not isinstance(requests, tuple) or any(
            not isinstance(item, tuple)
            or len(item) != 4
            or not all(isinstance(value, str) and value for value in item[:3])
            or isinstance(item[3], bool)
            or not isinstance(item[3], int)
            or item[3] < 0
            for item in requests
        ):
            raise ValueError("exception requests are invalid")
        count = sum(item[3] for item in requests)
        self._validate_count(upstream, count)
        self._planned[upstream] = count
        self._exception_expected[upstream] = list(requests)

    def consume_exception(
        self,
        upstream: DataUpstream,
        request: tuple[str, str, str, int],
    ) -> None:
        expected = self._exception_expected.get(upstream)
        if expected is None or not expected or expected[0] != request:
            raise NetworkBudgetExceededError
        expected.pop(0)

    def before_endpoint(self, upstream: DataUpstream) -> None:
        if upstream not in PRODUCTION_UPSTREAMS:
            raise NetworkBudgetExceededError
        if (
            self._actual[upstream] >= self._planned[upstream]
            or self._actual[upstream] >= self._limits[upstream]
            or sum(self._actual.values()) >= self._total_limit
        ):
            raise NetworkBudgetExceededError
        self._actual[upstream] += 1

    def summary(self) -> Mapping[str, object]:
        endpoint_planned = {
            upstream.value: self._planned[upstream] for upstream in PRODUCTION_UPSTREAMS
        }
        endpoint_actual = {
            upstream.value: self._actual[upstream] for upstream in PRODUCTION_UPSTREAMS
        }
        bao_endpoints = self._planned[DataUpstream.BAOSTOCK]
        return {
            "endpoint_limits": {
                "total": self._total_limit,
                "per_source": {
                    upstream.value: self._limits[upstream] for upstream in PRODUCTION_UPSTREAMS
                },
            },
            "endpoint_planned": endpoint_planned,
            "endpoint_actual": endpoint_actual,
            "endpoint_planned_total": sum(self._planned.values()),
            "endpoint_actual_total": sum(self._actual.values()),
            "baostock_protocol_message_upper_bound": {
                "login": bao_endpoints,
                "query": bao_endpoints,
                "logout_attempt": bao_endpoints,
                "total": bao_endpoints * 3,
            },
            "akshare_locked_source_http_upper_bound": {
                "EASTMONEY": self._planned[DataUpstream.EASTMONEY],
                "TENCENT": self._planned[DataUpstream.TENCENT] * 3,
                "SINA": self._planned[DataUpstream.SINA] * 2,
                "total": (
                    self._planned[DataUpstream.EASTMONEY]
                    + self._planned[DataUpstream.TENCENT] * 3
                    + self._planned[DataUpstream.SINA] * 2
                ),
            },
            "application_retries": 0,
        }

    def _validate_count(self, upstream: DataUpstream, count: int) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("chunk count must be a non-negative integer")
        if (
            count > self._limits[upstream]
            or sum(self._planned.values()) + count > self._total_limit
        ):
            raise NetworkBudgetExceededError


__all__ = [
    "EndpointInvocationBudget",
    "K3EndpointBudget",
    "NetworkBudgetExceededError",
]
