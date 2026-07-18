"""Provider-neutral deterministic incremental fetch planning.

The planner has no supplier, filesystem, database, or wall-clock dependency.
Callers provide the exact request boundaries, known coverage, lookback and
chunking policy. A retry uses a new ``batch_id`` while retaining the same
replayable request summary and chunk sequence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from types import MappingProxyType

from quantking.common.ids import EntityId
from quantking.common.time import TradingDate
from quantking.data.models import DatasetKind
from quantking.data.schemas import canonical_code_pattern

_EXCHANGE = re.compile(r"(SZ|SH|BJ)\Z")
_SAFE_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_ADAPTER_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST_PARAM_KEYS = frozenset(
    {
        "adapter_version",
        "dataset",
        "end_inclusive",
        "known_coverage",
        "lookback_days",
        "max_days_per_chunk",
        "max_items_per_chunk",
        "request_end",
        "request_start",
        "scope",
        "start_inclusive",
    }
)


def _validate_scope(dataset: DatasetKind, value: str) -> str:
    if not isinstance(value, str) or _SAFE_SCOPE.fullmatch(value) is None or ".." in value:
        raise ValueError("scope values must be safe managed identifiers")
    if dataset is DatasetKind.EXCHANGE_CALENDAR:
        if _EXCHANGE.fullmatch(value) is None:
            raise ValueError("exchange-calendar scope must use SZ, SH or BJ")
    elif canonical_code_pattern().fullmatch(value) is None:
        raise ValueError("security scope must use canonical 000001.SZ form")
    return value


@dataclass(frozen=True, slots=True)
class CoverageWindow:
    """Known inclusive coverage for one security or exchange."""

    scope: str
    start: TradingDate
    end: TradingDate

    def __post_init__(self) -> None:
        if not isinstance(self.scope, str) or not self.scope:
            raise ValueError("coverage scope must be non-empty text")
        if not isinstance(self.start, TradingDate) or not isinstance(self.end, TradingDate):
            raise TypeError("coverage boundaries must be TradingDate values")
        if self.end.value < self.start.value:
            raise ValueError("coverage end must not precede start")


@dataclass(frozen=True, slots=True)
class IncrementalPlanRequest:
    """All explicit inputs needed to construct one incremental plan."""

    batch_id: EntityId
    dataset: DatasetKind
    scope: tuple[str, ...]
    request_start: TradingDate
    request_end: TradingDate
    start_inclusive: bool
    end_inclusive: bool
    known_coverage: tuple[CoverageWindow, ...]
    lookback_days: int
    max_days_per_chunk: int
    max_items_per_chunk: int
    adapter_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.batch_id, EntityId) or self.batch_id.namespace != "data_batch":
            raise ValueError("batch_id must use the data_batch namespace")
        if not isinstance(self.dataset, DatasetKind):
            raise TypeError("dataset must be a DatasetKind")
        if not isinstance(self.scope, tuple):
            raise TypeError("scope must be a tuple")
        normalized_scope = tuple(
            sorted({_validate_scope(self.dataset, item) for item in self.scope})
        )
        object.__setattr__(self, "scope", normalized_scope)
        if not isinstance(self.request_start, TradingDate) or not isinstance(
            self.request_end, TradingDate
        ):
            raise TypeError("request boundaries must be TradingDate values")
        if self.request_end.value < self.request_start.value:
            raise ValueError("request end must not precede start")
        if not isinstance(self.start_inclusive, bool) or not isinstance(self.end_inclusive, bool):
            raise TypeError("boundary flags must be bool values")
        if not isinstance(self.known_coverage, tuple):
            raise TypeError("known_coverage must be a tuple")
        coverage = tuple(
            sorted(
                self.known_coverage,
                key=lambda item: (item.scope, item.start.value, item.end.value),
            )
        )
        for window in coverage:
            if not isinstance(window, CoverageWindow):
                raise TypeError("known_coverage must contain CoverageWindow values")
            _validate_scope(self.dataset, window.scope)
            if window.scope not in normalized_scope:
                raise ValueError("coverage scope must be present in request scope")
        object.__setattr__(self, "known_coverage", coverage)
        for field in ("lookback_days", "max_days_per_chunk", "max_items_per_chunk"):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{field} must be an integer")
        if self.lookback_days < 0:
            raise ValueError("lookback_days must be non-negative")
        if self.max_days_per_chunk <= 0 or self.max_items_per_chunk <= 0:
            raise ValueError("chunk sizes must be positive")
        if (
            not isinstance(self.adapter_version, str)
            or _ADAPTER_VERSION.fullmatch(self.adapter_version) is None
        ):
            raise ValueError("adapter_version must be a safe stable identifier")


@dataclass(frozen=True, slots=True)
class FetchChunk:
    """One non-empty, inclusive and deterministically ordered supplier request."""

    sequence: int
    dataset: DatasetKind
    scope: tuple[str, ...]
    start: TradingDate
    end: TradingDate
    request_sha256: str
    start_inclusive: bool = True
    end_inclusive: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or self.sequence < 0
        ):
            raise ValueError("sequence must be a non-negative integer")
        if not isinstance(self.dataset, DatasetKind):
            raise TypeError("dataset must be a DatasetKind")
        if not isinstance(self.scope, tuple) or not self.scope:
            raise ValueError("supplier chunks must never have an empty scope")
        normalized_scope = tuple(
            sorted({_validate_scope(self.dataset, value) for value in self.scope})
        )
        if normalized_scope != self.scope:
            raise ValueError("chunk scope must be sorted, unique and canonical")
        if not isinstance(self.start, TradingDate) or not isinstance(self.end, TradingDate):
            raise TypeError("chunk boundaries must be TradingDate values")
        if self.end.value < self.start.value:
            raise ValueError("supplier chunk end must not precede start")
        if not isinstance(self.start_inclusive, bool) or not isinstance(self.end_inclusive, bool):
            raise TypeError("chunk boundary flags must be bool values")
        if not self.start_inclusive or not self.end_inclusive:
            raise ValueError("supplier chunks use inclusive normalized boundaries")
        if (
            not isinstance(self.request_sha256, str)
            or _SHA256.fullmatch(self.request_sha256) is None
        ):
            raise ValueError("chunk request_sha256 must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class IncrementalFetchPlan:
    """A replayable incremental plan plus a fresh attempt identity."""

    batch_id: EntityId
    dataset: DatasetKind
    scope: tuple[str, ...]
    chunks: tuple[FetchChunk, ...]
    adapter_version: str
    request_params: Mapping[str, str]
    request_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.request_params, Mapping):
            raise TypeError("request_params must be a mapping")
        normalized: dict[str, str] = {}
        for key, value in self.request_params.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("request_params must map text to text")
            normalized[key] = value
        object.__setattr__(
            self,
            "request_params",
            MappingProxyType(dict(sorted(normalized.items()))),
        )
        validate_incremental_plan(self)


def _date_range(start: date, end: date) -> tuple[date, ...]:
    if end < start:
        return ()
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _runs(values: set[date]) -> tuple[tuple[date, date], ...]:
    if not values:
        return ()
    ordered = sorted(values)
    result: list[tuple[date, date]] = []
    start = previous = ordered[0]
    for current in ordered[1:]:
        if current != previous + timedelta(days=1):
            result.append((start, previous))
            start = current
        previous = current
    result.append((start, previous))
    return tuple(result)


def _chunks_for_run(start: date, end: date, size: int) -> tuple[tuple[date, date], ...]:
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=size - 1))
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return tuple(chunks)


def _request_dates(request: IncrementalPlanRequest) -> tuple[date, ...]:
    start = request.request_start.value
    end = request.request_end.value
    if not request.start_inclusive:
        start += timedelta(days=1)
    if not request.end_inclusive:
        end -= timedelta(days=1)
    return _date_range(start, end)


def _params(request: IncrementalPlanRequest) -> Mapping[str, str]:
    coverage = [
        {"scope": item.scope, "start": str(item.start), "end": str(item.end)}
        for item in request.known_coverage
    ]
    values = {
        "adapter_version": request.adapter_version,
        "dataset": request.dataset.value,
        "end_inclusive": str(request.end_inclusive).lower(),
        "known_coverage": json.dumps(
            coverage, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ),
        "lookback_days": str(request.lookback_days),
        "max_days_per_chunk": str(request.max_days_per_chunk),
        "max_items_per_chunk": str(request.max_items_per_chunk),
        "request_end": str(request.request_end),
        "request_start": str(request.request_start),
        "scope": json.dumps(
            list(request.scope), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ),
        "start_inclusive": str(request.start_inclusive).lower(),
    }
    return MappingProxyType(dict(sorted(values.items())))


def _request_sha256(params: Mapping[str, str]) -> str:
    payload = json.dumps(
        dict(params), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _build_chunks(
    request: IncrementalPlanRequest,
    *,
    request_sha256: str,
) -> tuple[FetchChunk, ...]:
    requested_dates = set(_request_dates(request))
    intervals: dict[tuple[date, date], list[str]] = {}
    for scope in request.scope:
        covered: set[date] = set()
        for window in request.known_coverage:
            if window.scope != scope:
                continue
            covered.update(_date_range(window.start.value, window.end.value))
        fetch_dates = requested_dates - covered
        covered_in_request = covered & requested_dates
        if request.lookback_days and covered_in_request:
            latest = max(covered_in_request)
            lookback_start = latest - timedelta(days=request.lookback_days - 1)
            fetch_dates.update(
                value
                for value in requested_dates
                if lookback_start <= value <= request.request_end.value
            )
        for run_start, run_end in _runs(fetch_dates):
            for chunk_start, chunk_end in _chunks_for_run(
                run_start, run_end, request.max_days_per_chunk
            ):
                intervals.setdefault((chunk_start, chunk_end), []).append(scope)

    chunks: list[FetchChunk] = []
    for (start, end), scopes in sorted(intervals.items()):
        ordered_scopes = sorted(scopes)
        for offset in range(0, len(ordered_scopes), request.max_items_per_chunk):
            chunk_scope = tuple(ordered_scopes[offset : offset + request.max_items_per_chunk])
            chunks.append(
                FetchChunk(
                    sequence=len(chunks),
                    dataset=request.dataset,
                    scope=chunk_scope,
                    start=TradingDate(start),
                    end=TradingDate(end),
                    request_sha256=request_sha256,
                )
            )
    return tuple(chunks)


def _parse_bool(value: str, *, field: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"{field} must be canonical boolean text")


def _parse_int(value: str, *, field: str) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise ValueError(f"{field} must be canonical non-negative integer text")
    return int(value)


def _request_from_plan(plan: IncrementalFetchPlan) -> IncrementalPlanRequest:
    params = dict(plan.request_params)
    if set(params) != _REQUEST_PARAM_KEYS:
        raise ValueError("request_params must use the generated whitelist contract")
    try:
        dataset = DatasetKind(params["dataset"])
        raw_scope = json.loads(params["scope"])
        raw_coverage = json.loads(params["known_coverage"])
    except (KeyError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("request_params contain invalid generated values") from error
    if not isinstance(raw_scope, list) or any(not isinstance(item, str) for item in raw_scope):
        raise ValueError("request_params scope must be a JSON string list")
    if not isinstance(raw_coverage, list):
        raise ValueError("request_params known_coverage must be a JSON list")
    coverage: list[CoverageWindow] = []
    for item in raw_coverage:
        if not isinstance(item, dict) or set(item) != {"scope", "start", "end"}:
            raise ValueError("request_params coverage entries are invalid")
        if any(not isinstance(item[key], str) for key in ("scope", "start", "end")):
            raise ValueError("request_params coverage values must be text")
        coverage.append(
            CoverageWindow(
                item["scope"],
                TradingDate.from_iso(item["start"]),
                TradingDate.from_iso(item["end"]),
            )
        )
    try:
        request = IncrementalPlanRequest(
            batch_id=plan.batch_id,
            dataset=dataset,
            scope=tuple(raw_scope),
            request_start=TradingDate.from_iso(params["request_start"]),
            request_end=TradingDate.from_iso(params["request_end"]),
            start_inclusive=_parse_bool(params["start_inclusive"], field="start_inclusive"),
            end_inclusive=_parse_bool(params["end_inclusive"], field="end_inclusive"),
            known_coverage=tuple(coverage),
            lookback_days=_parse_int(params["lookback_days"], field="lookback_days"),
            max_days_per_chunk=_parse_int(params["max_days_per_chunk"], field="max_days_per_chunk"),
            max_items_per_chunk=_parse_int(
                params["max_items_per_chunk"], field="max_items_per_chunk"
            ),
            adapter_version=params["adapter_version"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("request_params do not reconstruct a valid request") from error
    if dict(_params(request)) != params:
        raise ValueError("request_params are not in generated canonical form")
    return request


def validate_fetch_chunk(chunk: FetchChunk) -> None:
    """Revalidate a chunk at an adapter or orchestration trust boundary."""
    if not isinstance(chunk, FetchChunk):
        raise TypeError("chunk must be a FetchChunk")
    FetchChunk(
        sequence=chunk.sequence,
        dataset=chunk.dataset,
        scope=chunk.scope,
        start=chunk.start,
        end=chunk.end,
        request_sha256=chunk.request_sha256,
        start_inclusive=chunk.start_inclusive,
        end_inclusive=chunk.end_inclusive,
    )


def validate_incremental_plan(plan: IncrementalFetchPlan) -> None:
    """Reconstruct and compare the complete generated plan contract."""
    if not isinstance(plan, IncrementalFetchPlan):
        raise TypeError("plan must be an IncrementalFetchPlan")
    if not isinstance(plan.batch_id, EntityId) or plan.batch_id.namespace != "data_batch":
        raise ValueError("plan batch_id must use the data_batch namespace")
    if not isinstance(plan.dataset, DatasetKind):
        raise TypeError("plan dataset must be a DatasetKind")
    if not isinstance(plan.scope, tuple):
        raise TypeError("plan scope must be a tuple")
    if not isinstance(plan.chunks, tuple):
        raise TypeError("plan chunks must be a tuple")
    if (
        not isinstance(plan.adapter_version, str)
        or _ADAPTER_VERSION.fullmatch(plan.adapter_version) is None
    ):
        raise ValueError("plan adapter_version must be a safe stable identifier")
    if not isinstance(plan.request_sha256, str) or _SHA256.fullmatch(plan.request_sha256) is None:
        raise ValueError("plan request_sha256 must be lowercase SHA-256")
    request = _request_from_plan(plan)
    if request.dataset is not plan.dataset:
        raise ValueError("plan dataset does not match generated request_params")
    if request.scope != plan.scope:
        raise ValueError("plan scope does not match generated request_params")
    if request.adapter_version != plan.adapter_version:
        raise ValueError("plan adapter_version does not match request_params")
    expected_sha = _request_sha256(plan.request_params)
    if expected_sha != plan.request_sha256:
        raise ValueError("plan request_sha256 does not match request_params")
    expected_chunks = _build_chunks(request, request_sha256=expected_sha)
    if plan.chunks != expected_chunks:
        raise ValueError("plan chunk inventory does not match generated request")
    for chunk in plan.chunks:
        validate_fetch_chunk(chunk)


def build_incremental_plan(request: IncrementalPlanRequest) -> IncrementalFetchPlan:
    """Build deterministic chunks without consulting a supplier or "latest" state."""
    if not isinstance(request, IncrementalPlanRequest):
        raise TypeError("request must be an IncrementalPlanRequest")
    params = _params(request)
    request_sha256 = _request_sha256(params)
    return IncrementalFetchPlan(
        batch_id=request.batch_id,
        dataset=request.dataset,
        scope=request.scope,
        chunks=_build_chunks(request, request_sha256=request_sha256),
        adapter_version=request.adapter_version,
        request_params=params,
        request_sha256=request_sha256,
    )


__all__ = [
    "CoverageWindow",
    "FetchChunk",
    "IncrementalFetchPlan",
    "IncrementalPlanRequest",
    "build_incremental_plan",
    "validate_fetch_chunk",
    "validate_incremental_plan",
]
