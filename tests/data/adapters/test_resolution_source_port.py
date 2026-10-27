from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.ids import EntityId, new_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.adapters.resolution_sources import ResolutionSourcePort
from quantking.data.adapters.sources.budget import (
    EndpointInvocationBudget,
    NetworkBudgetExceededError,
)
from quantking.data.adapters.sources.ingestion import IngestionOutcome
from quantking.data.incremental import IncrementalFetchPlan
from quantking.data.models import (
    BatchFailureInfo,
    BatchFailureKind,
    DataProvider,
    DatasetKind,
    DataUpstream,
    EmptyResultInfo,
    IngestionBatch,
    IngestionBatchStatus,
    LegalEmptyClassification,
    QualityRunStatus,
    transition_batch,
)
from quantking.data.quality.artifacts import QualityArtifactWriter
from quantking.data.quality.models import (
    S108_DAILY_RULE_VERSION,
    QualityOutcomeResult,
    QualityRequest,
    QualityRunArtifact,
    RuleOutcomeStatus,
)
from quantking.data.quality.runner import QualityRunner
from quantking.data.resolution.models import (
    ResolutionRequestTrace,
    ResolutionSourceStatus,
)

_START = date(2026, 8, 7)
_END = date(2026, 8, 7)
_SYMBOL = "600000.SH"
_NOW = UtcTimestamp.from_iso("2026-08-12T04:00:00Z")
_EMPTY = EmptyResultInfo(
    classification=LegalEmptyClassification.NO_TRADING_SESSION,
    reason="synthetic no trading session",
)
_LIMITS = {
    DataUpstream.BAOSTOCK: 4,
    DataUpstream.TENCENT: 4,
    DataUpstream.SINA: 4,
}


def _provider(upstream: DataUpstream) -> DataProvider:
    return DataProvider.BAOSTOCK if upstream is DataUpstream.BAOSTOCK else DataProvider.AKSHARE


def _table(*, empty: bool = False) -> pa.Table:
    rows = (
        []
        if empty
        else [
            {
                "code": _SYMBOL,
                "trade_date": _START,
                "open": 10.0,
                "high": 10.5,
                "low": 9.9,
                "close": 10.2,
                "pre_close": 10.0,
                "volume": 10_000,
                "amount": 102_000.0,
            }
        ]
    )
    return pa.Table.from_pylist(
        rows,
        schema=canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value),
    )


class _FakeIngestion:
    """Duck-typed AkshareIngestionService for the K4 source port."""

    def __init__(
        self,
        upstream: DataUpstream,
        *,
        status: IngestionBatchStatus = IngestionBatchStatus.SUCCEEDED,
    ) -> None:
        self.upstream = upstream
        self.status = status
        self.calls = 0
        self.batch_ids: list[EntityId] = []

    def run(
        self,
        plan: IncrementalFetchPlan,
        *,
        empty_result: EmptyResultInfo | None = None,
        checkpoint: Callable[[], None] | None = None,
        job_run_id: str | None = None,
        code_version: str = "s1-07k4",
        invocation_guard: Callable[[DataUpstream], None] | None = None,
        chunk_empty_resolver: Callable[[object], EmptyResultInfo | None] | None = None,
    ) -> IngestionOutcome:
        del checkpoint, code_version
        self.calls += 1
        for _chunk in plan.chunks:
            if invocation_guard is not None:
                invocation_guard(self.upstream)
        running = IngestionBatch.running(
            batch_id=plan.batch_id,
            provider=_provider(self.upstream),
            upstream=self.upstream,
            endpoint=f"synthetic-{self.upstream.value.lower()}",
            library_version="0.9.3" if self.upstream is DataUpstream.BAOSTOCK else "1.18.83",
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            adapter_version=plan.adapter_version,
            request_params=plan.request_params,
            started_at=_NOW,
            job_run_id=job_run_id,
        )
        self.batch_ids.append(running.batch_id)
        if self.status in {IngestionBatchStatus.FAILED, IngestionBatchStatus.PARTIAL}:
            batch = transition_batch(
                running,
                self.status,
                at=_NOW,
                row_count=0,
                security_count=0,
                failure=BatchFailureInfo(
                    error_type=(
                        BatchFailureKind.PARTIAL_SOURCE
                        if self.status is IngestionBatchStatus.PARTIAL
                        else BatchFailureKind.NETWORK
                    ),
                    message="synthetic stable failure",
                    retryable=False,
                ),
            )
            return IngestionOutcome(batch=batch, canonical_table=None, raw_directory=None)
        resolved_empty = empty_result
        if chunk_empty_resolver is not None and plan.chunks:
            resolved_empty = chunk_empty_resolver(plan.chunks[0])
        is_empty = resolved_empty is not None
        table = _table(empty=is_empty)
        batch = transition_batch(
            running,
            IngestionBatchStatus.SUCCEEDED,
            at=_NOW,
            row_count=table.num_rows,
            security_count=0 if is_empty else 1,
            empty_result=resolved_empty,
        )
        return IngestionOutcome(batch=batch, canonical_table=table, raw_directory=None)


class _FakeQualityRunner(QualityRunner):
    def __init__(self, status: QualityRunStatus = QualityRunStatus.PASSED) -> None:
        self.status = status
        self.requests: list[QualityRequest] = []

    def run(self, *, request: QualityRequest, table: pa.Table) -> QualityRunArtifact:
        del table
        self.requests.append(request)
        passed = self.status is QualityRunStatus.PASSED
        return QualityRunArtifact(
            run_id=new_entity_id("data_quality"),
            version_id=request.version_id,
            rule_version=request.rule_version,
            schema_version=request.schema_version,
            content_sha256=request.content_sha256,
            started_at=_NOW,
            ended_at=_NOW,
            status=self.status,
            results=(
                QualityOutcomeResult(
                    rule_code="synthetic",
                    status=(RuleOutcomeStatus.PASSED if passed else RuleOutcomeStatus.FAILED),
                    affected_count=0 if passed else 1,
                    summary="synthetic quality outcome",
                    row_evidence_relative_path=None,
                ),
            ),
            failure=None,
        )


class _FakeQualityWriter(QualityArtifactWriter):
    def __init__(self) -> None:
        self.calls = 0

    def write(
        self,
        *,
        outcome: QualityRunArtifact,
        row_evidence_tables: Mapping[str, pa.Table],
    ) -> Path:
        del outcome, row_evidence_tables
        self.calls += 1
        return Path("synthetic-quality")


def _port(
    *,
    ingestions: Mapping[DataUpstream, _FakeIngestion] | None = None,
    budget: EndpointInvocationBudget | None = None,
    quality_status: QualityRunStatus = QualityRunStatus.PASSED,
    empty: bool = False,
) -> tuple[ResolutionSourcePort, EndpointInvocationBudget, _FakeQualityRunner]:
    selected = (
        ingestions
        if ingestions is not None
        else {upstream: _FakeIngestion(upstream) for upstream in _LIMITS}
    )
    selected_budget = budget or EndpointInvocationBudget(
        per_source_limits=_LIMITS,
        total_limit=12,
    )
    quality = _FakeQualityRunner(quality_status)
    port = ResolutionSourcePort(
        ingestions=selected,
        candidate_version_id=new_entity_id("data_version"),
        created_job_id=new_entity_id("job"),
        quality_runner=quality,
        quality_writer=_FakeQualityWriter(),
        code_version="s1-07k4.v1",
        adapter_version="daily-bar-k4.v1",
        rule_version=S108_DAILY_RULE_VERSION,
        reference_data={},
        clock=lambda: _NOW,
        endpoint_budget=selected_budget,
        chunk_empty_resolver=(lambda _chunk: _EMPTY if empty else None),
    )
    return port, selected_budget, quality


def test_mapping_must_be_exact_three_sources_and_non_production_rejected() -> None:
    incomplete = {upstream: _FakeIngestion(upstream) for upstream in _LIMITS}
    incomplete.pop(DataUpstream.SINA)
    with pytest.raises(ValueError, match="exact BAOSTOCK/TENCENT/SINA"):
        _port(ingestions=incomplete)

    with_eastmoney = {upstream: _FakeIngestion(upstream) for upstream in _LIMITS}
    with_eastmoney[DataUpstream.EASTMONEY] = _FakeIngestion(DataUpstream.EASTMONEY)
    with pytest.raises(ValueError, match="exact BAOSTOCK/TENCENT/SINA"):
        _port(ingestions=with_eastmoney)

    port, _budget, _quality = _port()
    with pytest.raises(ValueError, match="non-production"):
        port.fetch(
            upstream=DataUpstream.EASTMONEY,
            symbols=(_SYMBOL,),
            start_date=_START,
            end_date=_END,
            trigger_reason="INVALID",
        )


def test_primary_baostock_and_tencent_freeze_run_quality_and_count_actual() -> None:
    port, budget, quality = _port()
    candidates = tuple(
        port.fetch(
            upstream=upstream,
            symbols=(_SYMBOL,),
            start_date=_START,
            end_date=_END,
            trigger_reason="PRIMARY_COLLECTION",
        )
        for upstream in (DataUpstream.BAOSTOCK, DataUpstream.TENCENT)
    )

    assert all(item.status is ResolutionSourceStatus.AVAILABLE for item in candidates)
    assert all(
        item.version_id == port.quality_runs[index].version_id
        for index, item in enumerate(candidates)
    )
    assert [request.input_batch_ids[0] for request in quality.requests] == [
        item.batch_id for item in candidates
    ]
    summary = budget.summary()
    assert summary["endpoint_actual"] == {
        "BAOSTOCK": 1,
        "TENCENT": 1,
        "SINA": 0,
    }
    assert summary["endpoint_planned"]["SINA"] == 0


def test_sina_requires_atomic_exact_preflight_before_ingestion() -> None:
    ingestions = {item: _FakeIngestion(item) for item in _LIMITS}
    port, budget, _quality = _port(ingestions=ingestions)
    kwargs = {
        "upstream": DataUpstream.SINA,
        "symbols": (_SYMBOL,),
        "start_date": _START,
        "end_date": _END,
        "trigger_reason": "AFFECTED",
    }
    with pytest.raises(NetworkBudgetExceededError):
        port.fetch(**kwargs)
    assert ingestions[DataUpstream.SINA].calls == 0

    trace = ResolutionRequestTrace(
        upstream=DataUpstream.SINA,
        symbol=_SYMBOL,
        start_date=_START,
        end_date=_END,
        trigger_reason="AFFECTED",
    )
    port.preflight_exception_requests(DataUpstream.SINA, (trace,))
    candidate = port.fetch(**kwargs)
    assert candidate.status is ResolutionSourceStatus.AVAILABLE
    assert ingestions[DataUpstream.SINA].calls == 1
    assert budget.summary()["endpoint_actual"]["SINA"] == 1


def test_preflight_rejects_non_sina_and_mismatched_traces() -> None:
    port, _budget, _quality = _port()
    with pytest.raises(ValueError, match="production C \\(SINA\\)"):
        port.preflight_exception_requests(DataUpstream.TENCENT, ())

    tencent_trace = ResolutionRequestTrace(
        upstream=DataUpstream.TENCENT,
        symbol=_SYMBOL,
        start_date=_START,
        end_date=_END,
        trigger_reason="AFFECTED",
    )
    with pytest.raises(ValueError, match="does not match preflight source"):
        port.preflight_exception_requests(DataUpstream.SINA, (tencent_trace,))


@pytest.mark.parametrize(
    ("batch_status", "quality_status", "empty", "expected"),
    [
        (
            IngestionBatchStatus.FAILED,
            QualityRunStatus.PASSED,
            False,
            ResolutionSourceStatus.CALL_FAILED,
        ),
        (
            IngestionBatchStatus.PARTIAL,
            QualityRunStatus.PASSED,
            False,
            ResolutionSourceStatus.CALL_FAILED,
        ),
        (
            IngestionBatchStatus.SUCCEEDED,
            QualityRunStatus.FAILED,
            False,
            ResolutionSourceStatus.QUALITY_FAILED,
        ),
        (
            IngestionBatchStatus.SUCCEEDED,
            QualityRunStatus.PASSED,
            True,
            ResolutionSourceStatus.EMPTY,
        ),
    ],
)
def test_candidate_status_preserves_batch_and_shared_quality_bindings(
    batch_status: IngestionBatchStatus,
    quality_status: QualityRunStatus,
    empty: bool,
    expected: ResolutionSourceStatus,
) -> None:
    ingestion = _FakeIngestion(DataUpstream.BAOSTOCK, status=batch_status)
    ingestions = {upstream: _FakeIngestion(upstream) for upstream in _LIMITS}
    ingestions[DataUpstream.BAOSTOCK] = ingestion
    port, budget, quality = _port(
        ingestions=ingestions,
        quality_status=quality_status,
        empty=empty,
    )
    candidate = port.fetch(
        upstream=DataUpstream.BAOSTOCK,
        symbols=(_SYMBOL,),
        start_date=_START,
        end_date=_END,
        trigger_reason="PRIMARY_COLLECTION",
    )

    assert candidate.status is expected
    assert candidate.batch_id == ingestion.batch_ids[0]
    assert budget.summary()["endpoint_actual"]["BAOSTOCK"] == 1
    if expected in {ResolutionSourceStatus.CALL_FAILED}:
        assert candidate.quality_run_id is None
        assert quality.requests == []
    else:
        assert candidate.quality_run_id == port.quality_runs[0].run_id
        assert quality.requests[0].input_batch_ids == (candidate.batch_id,)
