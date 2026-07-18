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
        if not isinstance(self.adapter_version, str) or not self.adapter_version.strip():
            raise ValueError("adapter_version must be non-empty text")


@dataclass(frozen=True, slots=True)
class FetchChunk:
    """One non-empty, inclusive and deterministically ordered supplier request."""

    sequence: int
    dataset: DatasetKind
    scope: tuple[str, ...]
    start: TradingDate
    end: TradingDate
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
        if not self.scope:
            raise ValueError("supplier chunks must never have an empty scope")
        if self.end.value < self.start.value:
            raise ValueError("supplier chunk end must not precede start")


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


def build_incremental_plan(request: IncrementalPlanRequest) -> IncrementalFetchPlan:
    """Build deterministic chunks without consulting a supplier or "latest" state."""
    if not isinstance(request, IncrementalPlanRequest):
        raise TypeError("request must be an IncrementalPlanRequest")
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
                )
            )
    params = _params(request)
    payload = json.dumps(
        dict(params), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return IncrementalFetchPlan(
        batch_id=request.batch_id,
        dataset=request.dataset,
        scope=request.scope,
        chunks=tuple(chunks),
        adapter_version=request.adapter_version,
        request_params=params,
        request_sha256=hashlib.sha256(payload).hexdigest(),
    )


__all__ = [
    "CoverageWindow",
    "FetchChunk",
    "IncrementalFetchPlan",
    "IncrementalPlanRequest",
    "build_incremental_plan",
]
