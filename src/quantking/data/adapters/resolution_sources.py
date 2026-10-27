"""Production source port: Raw first, source quality in memory, no retries.

A (BaoStock) and B (Tencent) are primary calls per chunk. C (Sina) is
preflighted as one complete exceptional set and remains unused whenever A/B
agree. Every actual call passes the endpoint budget guard before the first
vendor invocation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date

import pyarrow as pa

from quantking.common.ids import EntityId, new_entity_id
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.sources.budget import EndpointInvocationBudget
from quantking.data.adapters.sources.ingestion import AkshareIngestionService, ChunkEmptyResolver
from quantking.data.adapters.sources.registry import PRODUCTION_UPSTREAMS
from quantking.data.adapters.warehouse.publish_coordinator import compute_content_sha256
from quantking.data.incremental import (
    IncrementalFetchPlan,
    IncrementalPlanRequest,
    build_incremental_plan,
)
from quantking.data.models import (
    BatchFailureInfo,
    BatchFailureKind,
    DatasetKind,
    DataUpstream,
    EmptyResultInfo,
    IngestionBatchStatus,
    QualityResult,
    QualityRun,
    QualityRunStatus,
)
from quantking.data.quality.artifacts import QualityArtifactWriter
from quantking.data.quality.models import (
    QualityRequest,
    ReferenceDataBinding,
    RuleOutcomeStatus,
)
from quantking.data.quality.runner import QualityRunner
from quantking.data.resolution.models import ResolutionRequestTrace, ResolutionSourceStatus
from quantking.data.resolution.service import SourceCandidate
from quantking.data.schemas import descriptor_for

SecurityExceptions = Mapping[str, tuple[tuple[TradingDate, TradingDate], ...]]


class ResolutionSourcePort:
    """Collect one requested upstream range and retain source quality until version creation.

    Raw batches are persisted by ``AkshareIngestionService`` immediately. Quality
    artifacts and immutable ``QualityRun`` domain objects are produced next, but the
    runs are deliberately not inserted into SQLite until the orchestrator knows every
    actual A/B/C batch and can create the single shared candidate DatasetVersion.
    """

    def __init__(
        self,
        *,
        ingestions: Mapping[DataUpstream, AkshareIngestionService],
        candidate_version_id: EntityId,
        created_job_id: EntityId,
        quality_runner: QualityRunner,
        quality_writer: QualityArtifactWriter,
        code_version: str,
        adapter_version: str,
        rule_version: str,
        reference_data: Mapping[str, ReferenceDataBinding],
        clock: Callable[[], UtcTimestamp],
        endpoint_budget: EndpointInvocationBudget,
        checkpoint: Callable[[], None] | None = None,
        chunk_empty_resolver: ChunkEmptyResolver | None = None,
        security_exceptions: SecurityExceptions | None = None,
        max_days_per_chunk: int = 31,
        max_items_per_chunk: int = 1,
    ) -> None:
        if set(ingestions) != set(PRODUCTION_UPSTREAMS):
            raise ValueError("source port requires exact BAOSTOCK/TENCENT/SINA ingestions")
        if (
            not isinstance(candidate_version_id, EntityId)
            or candidate_version_id.namespace != "data_version"
        ):
            raise ValueError("candidate_version_id must use data_version namespace")
        if not isinstance(created_job_id, EntityId) or created_job_id.namespace != "job":
            raise ValueError("created_job_id must use job namespace")
        if not isinstance(endpoint_budget, EndpointInvocationBudget):
            raise TypeError("endpoint_budget must be EndpointInvocationBudget")
        self._ingestions = dict(ingestions)
        self._candidate_version_id = candidate_version_id
        self._created_job_id = created_job_id
        self._quality_runner = quality_runner
        self._quality_writer = quality_writer
        self._code_version = code_version
        self._adapter_version = adapter_version
        self._rule_version = rule_version
        self._reference_data = dict(reference_data)
        self._clock = clock
        self._checkpoint = checkpoint
        self._chunk_empty_resolver = chunk_empty_resolver
        self._security_exceptions = security_exceptions
        self._max_days_per_chunk = max_days_per_chunk
        self._max_items_per_chunk = max_items_per_chunk
        self._endpoint_budget = endpoint_budget
        self._quality_runs: list[QualityRun] = []
        self._candidates: list[SourceCandidate] = []

    @property
    def quality_runs(self) -> tuple[QualityRun, ...]:
        return tuple(self._quality_runs)

    @property
    def candidates(self) -> tuple[SourceCandidate, ...]:
        return tuple(self._candidates)

    def fetch(
        self,
        *,
        upstream: DataUpstream,
        symbols: tuple[str, ...],
        start_date: date,
        end_date: date,
        trigger_reason: str,
    ) -> SourceCandidate:
        if upstream not in PRODUCTION_UPSTREAMS:
            raise ValueError("source port rejects non-production upstreams")
        batch_id = new_entity_id("data_batch")
        plan = self._plan(
            batch_id=batch_id,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
        )
        if upstream in {DataUpstream.BAOSTOCK, DataUpstream.TENCENT}:
            self._endpoint_budget.freeze_primary(upstream, chunk_count=len(plan.chunks))
        else:
            self._endpoint_budget.consume_exception(
                (symbols[0], start_date.isoformat(), end_date.isoformat(), len(plan.chunks))
            )
        del trigger_reason
        outcome = self._ingestions[upstream].run(
            plan,
            checkpoint=self._checkpoint,
            job_run_id=str(self._created_job_id),
            code_version=self._code_version,
            invocation_guard=self._endpoint_budget.before_endpoint,
            chunk_empty_resolver=self._chunk_empty_resolver,
        )
        if (
            outcome.batch.status is not IngestionBatchStatus.SUCCEEDED
            or outcome.canonical_table is None
        ):
            candidate = SourceCandidate(
                upstream=upstream,
                status=ResolutionSourceStatus.CALL_FAILED,
                symbols=symbols,
                start_date=start_date,
                end_date=end_date,
                batch_id=outcome.batch.batch_id,
                content_sha256=None,
                version_id=None,
                quality_run_id=None,
                table=None,
                empty_result=None,
            )
            self._candidates.append(candidate)
            return candidate
        table = outcome.canonical_table
        source_empty = outcome.batch.empty_result
        content_sha = compute_content_sha256(
            (table,),
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            empty_result=source_empty,
        )
        quality = self._run_quality(
            batch_id=batch_id,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            table=table,
            content_sha=content_sha,
            empty_result=source_empty,
        )
        self._quality_runs.append(quality)
        if quality.status is not QualityRunStatus.PASSED:
            candidate = SourceCandidate(
                upstream=upstream,
                status=ResolutionSourceStatus.QUALITY_FAILED,
                symbols=symbols,
                start_date=start_date,
                end_date=end_date,
                batch_id=batch_id,
                content_sha256=content_sha,
                version_id=self._candidate_version_id,
                quality_run_id=quality.run_id,
                table=None,
                empty_result=None,
            )
            self._candidates.append(candidate)
            return candidate
        candidate = SourceCandidate(
            upstream=upstream,
            status=(
                ResolutionSourceStatus.EMPTY
                if source_empty is not None
                else ResolutionSourceStatus.AVAILABLE
            ),
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            batch_id=batch_id,
            content_sha256=content_sha,
            version_id=self._candidate_version_id,
            quality_run_id=quality.run_id,
            table=table,
            empty_result=source_empty,
        )
        self._candidates.append(candidate)
        return candidate

    def _plan(
        self,
        *,
        batch_id: EntityId,
        symbols: tuple[str, ...],
        start_date: date,
        end_date: date,
    ) -> IncrementalFetchPlan:
        return build_incremental_plan(
            IncrementalPlanRequest(
                batch_id=batch_id,
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                scope=symbols,
                request_start=TradingDate(start_date),
                request_end=TradingDate(end_date),
                start_inclusive=True,
                end_inclusive=True,
                known_coverage=(),
                lookback_days=0,
                max_days_per_chunk=self._max_days_per_chunk,
                max_items_per_chunk=self._max_items_per_chunk,
                adapter_version=self._adapter_version,
            )
        )

    def preflight_exception_requests(
        self,
        upstream: DataUpstream,
        requests: tuple[ResolutionRequestTrace, ...],
    ) -> None:
        if upstream is not DataUpstream.SINA:
            raise ValueError("exception preflight requires production C (SINA)")
        if any(request.upstream is not upstream for request in requests):
            raise ValueError("request trace upstream does not match preflight source")
        frozen: list[tuple[str, str, str, int]] = []
        for request in requests:
            plan = self._plan(
                batch_id=new_entity_id("data_batch"),
                symbols=(request.symbol,),
                start_date=request.start_date,
                end_date=request.end_date,
            )
            frozen.append(
                (
                    request.symbol,
                    request.start_date.isoformat(),
                    request.end_date.isoformat(),
                    len(plan.chunks),
                )
            )
        self._endpoint_budget.preflight_exception(tuple(frozen))

    def _run_quality(
        self,
        *,
        batch_id: EntityId,
        symbols: tuple[str, ...],
        start_date: date,
        end_date: date,
        table: pa.Table,
        content_sha: str,
        empty_result: EmptyResultInfo | None,
    ) -> QualityRun:
        started = self._clock()
        request = QualityRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            version_id=self._candidate_version_id,
            rule_version=self._rule_version,
            schema_version=descriptor_for(DatasetKind.DAILY_BAR_UNADJUSTED.value).schema_version,
            code_version=self._code_version,
            content_sha256=content_sha,
            input_batch_ids=(batch_id,),
            expected_securities=symbols,
            expected_exchanges=tuple(sorted({symbol.rsplit(".", 1)[-1] for symbol in symbols})),
            coverage_start=TradingDate(start_date),
            coverage_end=TradingDate(end_date),
            reference_data=self._reference_data,
            empty_result=empty_result,
            security_exceptions=self._security_exceptions,
        )
        try:
            artifact = self._quality_runner.run(request=request, table=table)
            self._quality_writer.write(outcome=artifact, row_evidence_tables={})
            return QualityRun(
                run_id=artifact.run_id,
                version_id=artifact.version_id,
                status=artifact.status,
                rule_version=artifact.rule_version,
                schema_version=artifact.schema_version,
                content_sha256=artifact.content_sha256,
                started_at=artifact.started_at,
                ended_at=artifact.ended_at,
                results=tuple(
                    QualityResult(
                        rule_code=item.rule_code,
                        passed=item.status is RuleOutcomeStatus.PASSED,
                        affected_count=item.affected_count,
                        summary=item.summary,
                        report_relative_path=item.row_evidence_relative_path,
                    )
                    for item in artifact.results
                    if item.status is not RuleOutcomeStatus.SKIPPED
                ),
                summary_report_relative_path=None,
                failure=artifact.failure,
            )
        except Exception:
            ended = self._clock()
            return QualityRun(
                run_id=new_entity_id("data_quality"),
                version_id=self._candidate_version_id,
                status=QualityRunStatus.FAILED,
                rule_version=self._rule_version,
                schema_version=descriptor_for(
                    DatasetKind.DAILY_BAR_UNADJUSTED.value
                ).schema_version,
                content_sha256=content_sha,
                started_at=started,
                ended_at=ended,
                results=(),
                summary_report_relative_path=None,
                failure=BatchFailureInfo(
                    error_type=BatchFailureKind.QUALITY,
                    message="source quality execution failed",
                    retryable=False,
                ),
            )


__all__ = [
    "ResolutionSourcePort",
    "SecurityExceptions",
]
