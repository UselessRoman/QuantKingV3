"""Per-Job endpoint invocation budget for the fixed three-role source route.

Normal tasks plan A (BaoStock) and B (Tencent) once per chunk. C (Sina) is
reserved as one complete exceptional set and stays 0 planned/actual whenever
A and B agree. Insufficiency is rejected at planning time — before the first
service call — and every actual call passes the ``before_endpoint`` guard.
There is no application-layer retry.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from quantking.data.adapters.sources.registry import PRODUCTION_UPSTREAMS
from quantking.data.models import DataUpstream

_AKSHARE_HTTP_PER_ENDPOINT = {
    DataUpstream.TENCENT: 3,
    DataUpstream.SINA: 2,
}


class NetworkBudgetExceededError(RuntimeError):
    """Stable fail-closed error raised before an invocation would exceed its budget."""

    code = "NETWORK_BUDGET_EXCEEDED"

    def __init__(self) -> None:
        super().__init__(self.code)


class EndpointInvocationBudget:
    """Three-role endpoint budget with separate protocol/HTTP evidence."""

    def __init__(
        self,
        *,
        per_source_limits: Mapping[DataUpstream, int],
        total_limit: int,
    ) -> None:
        if set(per_source_limits) != set(PRODUCTION_UPSTREAMS):
            raise ValueError("endpoint budget requires exact production upstream limits")
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
        self._exception_expected: list[tuple[str, str, str, int]] | None = None

    def freeze_primary(self, upstream: DataUpstream, *, chunk_count: int) -> None:
        if upstream not in {DataUpstream.BAOSTOCK, DataUpstream.TENCENT}:
            raise ValueError("primary source must be BAOSTOCK or TENCENT")
        if upstream in self._primary_frozen:
            raise ValueError("primary source plan is already frozen")
        self._validate_count(upstream, chunk_count)
        self._planned[upstream] = chunk_count
        self._primary_frozen.add(upstream)

    def preflight_exception(self, requests: tuple[tuple[str, str, str, int], ...]) -> None:
        if self._exception_expected is not None:
            raise ValueError("Sina exception plan is already frozen")
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
        self._validate_count(DataUpstream.SINA, count)
        self._planned[DataUpstream.SINA] = count
        self._exception_expected = list(requests)

    def consume_exception(self, request: tuple[str, str, str, int]) -> None:
        expected = self._exception_expected
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
        tencent_endpoints = self._planned[DataUpstream.TENCENT]
        sina_endpoints = self._planned[DataUpstream.SINA]
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
                "TENCENT": tencent_endpoints * _AKSHARE_HTTP_PER_ENDPOINT[DataUpstream.TENCENT],
                "SINA": sina_endpoints * _AKSHARE_HTTP_PER_ENDPOINT[DataUpstream.SINA],
                "total": (
                    tencent_endpoints * _AKSHARE_HTTP_PER_ENDPOINT[DataUpstream.TENCENT]
                    + sina_endpoints * _AKSHARE_HTTP_PER_ENDPOINT[DataUpstream.SINA]
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


__all__ = ["EndpointInvocationBudget", "NetworkBudgetExceededError"]
