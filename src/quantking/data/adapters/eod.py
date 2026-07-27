"""Offline EOD Worker composition over the existing Stage 1 data pipeline."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pyarrow as pa

from quantking.common.errors import DatasetVersionNotFoundError
from quantking.common.ids import new_entity_id, parse_entity_id
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.canonical_schema import (
    canonical_schema,
    ensure_sorted,
    validate_table,
)
from quantking.data.adapters.persistence.sqlite import DataSqliteUnitOfWorkFactory
from quantking.data.adapters.sources.capabilities import capability_for
from quantking.data.adapters.sources.ingestion import XtQuantIngestionService
from quantking.data.adapters.warehouse.publish_coordinator import (
    CanonicalPublishCoordinator,
    CanonicalPublishCoordinatorRequest,
    compute_content_sha256,
)
from quantking.data.adapters.warehouse.query import CanonicalQueryAdapter
from quantking.data.adapters.warehouse.verifier import verify_canonical_directory
from quantking.data.eod.artifacts import (
    EodEvidenceConflictError,
    EodEvidenceWriter,
    verify_eod_completed_results,
    verify_eod_success_evidence_deep,
    verify_eod_terminal_failure,
)
from quantking.data.eod.contracts import (
    EOD_JOB_TYPE,
    EodDatasetResult,
    EodRunResult,
    canonical_json,
    sha256_text,
)
from quantking.data.eod.frozen import (
    FrozenEodCommand as _FrozenCommand,
)
from quantking.data.eod.frozen import (
    FrozenEodCommandError,
    parse_frozen_eod_command,
)
from quantking.data.eod.frozen import (
    FrozenEodDataset as _DatasetPlan,
)
from quantking.data.incremental import (
    CoverageWindow,
    FetchChunk,
    IncrementalPlanRequest,
    build_incremental_plan,
)
from quantking.data.models import (
    BatchFailureInfo,
    BatchFailureKind,
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    IngestionBatchStatus,
    QualityResult,
    QualityRun,
    QualityRunStatus,
    transition_dataset_version,
)
from quantking.data.quality.artifacts import QualityArtifactWriter
from quantking.data.quality.models import (
    QualityRequest,
    ReferenceDataBinding,
    RuleOutcomeStatus,
)
from quantking.data.quality.runner import QualityRunner
from quantking.data.schemas import descriptor_for
from quantking.jobs.jsonl import JsonLinesHandler
from quantking.jobs.models import JobRun, JobStatus
from quantking.jobs.services import JobService
from quantking.jobs.worker import JobCancelledAtCheckpoint, WorkerContext, WorkerRegistry


class EodExecutionError(Exception):
    """Stable EOD orchestration failure safe to persist by code only."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _PublishedDatasetFailure(EodExecutionError):
    def __init__(self, result: EodDatasetResult, error: BaseException) -> None:
        self.result = result
        super().__init__(
            "POST_PUBLISH_FAILURE",
            f"published dataset recovery required after {type(error).__name__}",
        )


@dataclass(frozen=True, slots=True)
class _VerifiedBase:
    version: DatasetVersion
    table: pa.Table
    known_coverage: tuple[CoverageWindow, ...]


def _reconstruct(job: JobRun) -> _FrozenCommand:
    try:
        return parse_frozen_eod_command(
            job_type=job.job_type,
            input_manifest=job.input_manifest,
            config_manifest=job.config_manifest,
            version_manifest=job.version_manifest,
        )
    except FrozenEodCommandError as error:
        code = "REQUEST_MANIFEST_INVALID" if "request" in str(error) else "CONFIG_MANIFEST_INVALID"
        raise EodExecutionError(code, "request and config reconstruction failed") from error


def _invalid_evidence_request(job: JobRun) -> Mapping[str, object]:
    return {
        "schema_version": "eod-invalid-request-evidence.v1",
        "job_id": job.job_id,
        "request_manifest_valid": False,
        "manifest_fingerprints": {
            "input_manifest_sha256": sha256_text(canonical_json(dict(job.input_manifest))),
            "config_manifest_sha256": sha256_text(canonical_json(dict(job.config_manifest))),
            "version_manifest_sha256": sha256_text(canonical_json(dict(job.version_manifest))),
        },
    }


class EodWorkerHandler:
    """Execute one persisted EOD command without retaining enqueue-time objects."""

    def __init__(
        self,
        *,
        ingestion: XtQuantIngestionService,
        quality_runner: QualityRunner,
        quality_writer: QualityArtifactWriter,
        publish_coordinator: CanonicalPublishCoordinator,
        data_uow_factory: DataSqliteUnitOfWorkFactory,
        evidence_writer: EodEvidenceWriter,
        data_root: Path,
        clock: Callable[[], UtcTimestamp],
        capability_check: Callable[[DatasetKind], bool] | None = None,
        log_root: Path | None = None,
        query_adapter: CanonicalQueryAdapter | None = None,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._ingestion = ingestion
        self._quality_runner = quality_runner
        self._quality_writer = quality_writer
        self._publish = publish_coordinator
        self._data_uow = data_uow_factory
        self._evidence = evidence_writer
        self._data_root = data_root
        self._clock = clock
        self._capability_check = capability_check or (
            lambda dataset: capability_for(dataset).available
        )
        self._log_root = log_root
        self._query = query_adapter or CanonicalQueryAdapter(
            data_root=data_root,
            uow_factory=data_uow_factory,
        )
        self._fault_injector = fault_injector

    def _fault(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    def on_success_commit_failure(
        self,
        context: WorkerContext,
        error: BaseException,
    ) -> None:
        """Preserve an auditable INTERRUPTED outcome after final Job commit failure."""
        del error
        job = context.service.get_job(context.job_id)
        if job.status is JobStatus.SUCCEEDED:
            return
        if job.status not in {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.INTERRUPTED}:
            raise EodExecutionError(
                "FINAL_SUCCESS_COMMIT_CONFLICT",
                "final Job state conflicts with published evidence",
            )
        command = _reconstruct(job)
        completed = list(
            self._evidence.load_checkpoints(
                job_id=context.job_id,
                request_sha256_value=command.request_sha256,
                config_sha256=command.config_sha256,
            )
        )
        for result in completed:
            self._verify_completed_result(result, command=command)
        directory = self._evidence.write_failure(
            job_id=context.job_id,
            request=command.evidence_request,
            code="FINAL_SUCCESS_COMMIT_FAILED",
            message="final Job success commit failed",
            terminal_status=(
                JobStatus.INTERRUPTED.value if job.status is JobStatus.RUNNING else job.status.value
            ),
            failed_at=self._clock(),
            request_sha256_value=command.request_sha256,
            completed_datasets=tuple(item.to_dict() for item in completed),
            replace_uncommitted_success=True,
        )
        relative_path = directory.relative_to(self._data_root).as_posix()
        if job.status is JobStatus.RUNNING:
            context.service.add_artifact(context.job_id, relative_path=relative_path)
        else:
            context.service.add_recovery_artifact(context.job_id, relative_path=relative_path)

    def recover_interrupted(self, service: JobService, job_id: str) -> None:
        """Materialize authoritative failure evidence after explicit stale recovery."""
        job = service.get_job(job_id)
        if job.job_type != EOD_JOB_TYPE or job.status is not JobStatus.INTERRUPTED:
            return
        try:
            command = _reconstruct(job)
        except EodExecutionError:
            request = _invalid_evidence_request(job)
            request_sha = "0" * 64
            completed: tuple[EodDatasetResult, ...] = ()
        else:
            request = command.evidence_request
            request_sha = command.request_sha256
            completed = self._evidence.load_checkpoints(
                job_id=job_id,
                request_sha256_value=command.request_sha256,
                config_sha256=command.config_sha256,
            )
            for result in completed:
                self._verify_completed_result(result, command=command)
        directory = self._evidence.coordinate_interrupted_failure(
            job=job,
            request=request,
            failed_at=job.ended_at or self._clock(),
            request_sha256_value=request_sha,
            completed_datasets=completed,
            data_root=self._data_root,
            uow_factory=self._data_uow,
        )
        service.add_recovery_artifact(
            job_id,
            relative_path=directory.relative_to(self._data_root).as_posix(),
        )
        verify_eod_terminal_failure(
            directory,
            artifacts_root=directory.parent,
            data_root=self._data_root,
            uow_factory=self._data_uow,
            job=service.get_job(job_id),
        )

    def __call__(self, context: WorkerContext) -> None:
        job = context.service.get_job(context.job_id)
        logger, log_handler = self._job_logger(job)
        command: _FrozenCommand | None = None
        completed: list[EodDatasetResult] = []
        success_written = False
        try:
            logger.info(
                "EOD execution started",
                extra={"event": "eod_started", "job_id": context.job_id},
            )
            command = _reconstruct(job)
            bases = self._preflight(command)
            total = len(command.datasets)
            published_by_dataset: dict[DatasetKind, EodDatasetResult] = {}
            for index, plan in enumerate(command.datasets):
                context.checkpoint()
                context.progress(
                    stage=f"eod:{plan.dataset.value}:ingestion",
                    progress=index / total,
                    summary="EOD dataset ingestion started",
                )
                result = self._execute_dataset(
                    context,
                    command=command,
                    plan=plan,
                    verified_base=bases.get(plan.dataset),
                    published_by_dataset=published_by_dataset,
                )
                completed.append(result)
                published_by_dataset[plan.dataset] = result
                logger.info(
                    "EOD dataset completed",
                    extra={
                        "event": "eod_dataset_published",
                        "job_id": context.job_id,
                        "dataset": plan.dataset.value,
                        "batch_id": result.batch_id,
                        "version_id": result.version_id,
                        "quality_run_id": result.quality_run_id,
                    },
                )
                context.checkpoint()
                self._fault("before_job_event_audit")
                context.activity(
                    stage=f"eod:{plan.dataset.value}:published",
                    progress=(index + 1) / total,
                    summary="EOD dataset completed",
                    audit_action="eod_dataset_published",
                )
            run_result = EodRunResult(
                job_id=context.job_id,
                datasets=tuple(completed),
                expected_datasets=command.request.datasets,
            )
            directory = self._evidence.write_success(
                job_id=context.job_id,
                request=command.evidence_request,
                result=run_result.to_dict(),
            )
            success_written = True
            verify_eod_success_evidence_deep(
                directory,
                artifacts_root=directory.parent,
                data_root=self._data_root,
                uow_factory=self._data_uow,
            )
            self._fault("after_success_evidence_before_job_succeeded")
            context.service.add_artifact(
                context.job_id,
                relative_path=directory.relative_to(self._data_root).as_posix(),
            )
            logger.info(
                "EOD execution completed",
                extra={
                    "event": "eod_succeeded",
                    "job_id": context.job_id,
                    "dataset_count": len(completed),
                },
            )
        except BaseException as error:
            if isinstance(error, _PublishedDatasetFailure) and all(
                item.dataset != error.result.dataset for item in completed
            ):
                completed.append(error.result)
            if command is not None:
                for recovered in self._evidence.load_checkpoints(
                    job_id=context.job_id,
                    request_sha256_value=command.request_sha256,
                    config_sha256=command.config_sha256,
                ):
                    if all(item.dataset != recovered.dataset for item in completed):
                        self._verify_completed_result(recovered, command=command)
                        completed.append(recovered)
                order = {name: index for index, name in enumerate(command.request.datasets)}
                completed.sort(key=lambda item: order[item.dataset])
            code = (
                error.code
                if isinstance(error, EodExecutionError)
                else ("CANCELLED" if isinstance(error, JobCancelledAtCheckpoint) else "EOD_FAILED")
            )
            evidence_request = (
                command.evidence_request if command is not None else _invalid_evidence_request(job)
            )
            terminal_status = (
                "CANCELLED" if isinstance(error, JobCancelledAtCheckpoint) else "FAILED"
            )
            failure_request_sha = command.request_sha256 if command is not None else "0" * 64
            directory = self._evidence.write_failure(
                job_id=context.job_id,
                request=evidence_request,
                code=code,
                message=str(error),
                terminal_status=terminal_status,
                failed_at=self._clock(),
                request_sha256_value=failure_request_sha,
                completed_datasets=tuple(item.to_dict() for item in completed),
                replace_uncommitted_success=success_written,
            )
            context.service.add_artifact(
                context.job_id,
                relative_path=directory.relative_to(self._data_root).as_posix(),
            )
            logger.error(
                "EOD execution failed",
                extra={
                    "event": "eod_failed",
                    "job_id": context.job_id,
                    "error_type": type(error).__name__,
                    "completed_dataset_count": len(completed),
                },
            )
            raise
        finally:
            if log_handler is not None:
                logger.removeHandler(log_handler)
                log_handler.close()

    def _job_logger(self, job: JobRun) -> tuple[logging.Logger, JsonLinesHandler | None]:
        logger = logging.getLogger(f"quantking.eod.{job.job_id}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if self._log_root is None:
            logger.addHandler(logging.NullHandler())
            return logger, None
        handler = JsonLinesHandler(self._log_root, job.log_id)
        logger.addHandler(handler)
        return logger, handler

    def _preflight(
        self,
        command: _FrozenCommand,
    ) -> Mapping[DatasetKind, _VerifiedBase]:
        bases: dict[DatasetKind, _VerifiedBase] = {}
        for plan in command.datasets:
            if not self._capability_check(plan.dataset):
                raise EodExecutionError(
                    "CAPABILITY_UNAVAILABLE",
                    f"XTQuant capability is unavailable for {plan.dataset.value}",
                )
            if not plan.scope:
                raise EodExecutionError(
                    "CAPABILITY_UNAVAILABLE",
                    f"frozen scope is unavailable for {plan.dataset.value}",
                )
            if plan.base_version is not None:
                bases[plan.dataset] = self._verify_base(plan)
        return bases

    def _execute_dataset(
        self,
        context: WorkerContext,
        *,
        command: _FrozenCommand,
        plan: _DatasetPlan,
        verified_base: _VerifiedBase | None,
        published_by_dataset: Mapping[DatasetKind, EodDatasetResult],
    ) -> EodDatasetResult:
        def bind_recovery_artifact(job_id: str, path: Path) -> None:
            context.service.add_recovery_artifact(
                job_id,
                relative_path=path.relative_to(self._data_root).as_posix(),
            )

        reusable = self._evidence.find_reusable_published(
            request_sha256_value=command.request_sha256,
            config_sha256=command.config_sha256,
            dataset=plan.dataset.value,
            job_lookup=context.service.get_job,
            recovered_at=self._clock(),
            bind_recovery_artifact=bind_recovery_artifact,
            data_root=self._data_root,
            uow_factory=self._data_uow,
        )
        if reusable is not None:
            expected_parent = (
                None if verified_base is None else str(verified_base.version.version_id)
            )
            if reusable.parent_version_id != expected_parent:
                raise EodExecutionError(
                    "RETRY_RESULT_CONFLICT",
                    "reusable published result has different lineage",
                )
            self._verify_completed_result(reusable, command=command)
            self._evidence.write_checkpoint(
                job_id=context.job_id,
                request_sha256_value=command.request_sha256,
                config_sha256=command.config_sha256,
                result=reusable,
            )
            return reusable
        batch_id = new_entity_id("data_batch")
        known_coverage = () if verified_base is None else verified_base.known_coverage
        fetch_plan = build_incremental_plan(
            IncrementalPlanRequest(
                batch_id=batch_id,
                dataset=plan.dataset,
                scope=plan.scope,
                request_start=TradingDate(command.request.start_date),
                request_end=TradingDate(command.request.end_date),
                start_inclusive=True,
                end_inclusive=True,
                known_coverage=known_coverage,
                lookback_days=command.lookback_days,
                max_days_per_chunk=command.max_days_per_chunk,
                max_items_per_chunk=command.max_items_per_chunk,
                adapter_version=command.adapter_version,
            )
        )
        if not fetch_plan.chunks:
            if verified_base is None:
                raise EodExecutionError(
                    "NO_CHANGE_WITHOUT_BASE",
                    "an empty incremental plan requires a verified base",
                )
            result = self._result_from_version(
                verified_base.version,
                status="REUSED_UNCHANGED",
                batch_id=None,
            )
            self._evidence.write_checkpoint(
                job_id=context.job_id,
                request_sha256_value=command.request_sha256,
                config_sha256=command.config_sha256,
                result=result,
            )
            return result

        def chunk_checkpoint() -> None:
            context.checkpoint()
            context.heartbeat()

        ingestion = self._ingestion.run(fetch_plan, checkpoint=chunk_checkpoint)
        if (
            ingestion.batch.status is not IngestionBatchStatus.SUCCEEDED
            or ingestion.canonical_table is None
        ):
            code = (
                ingestion.batch.failure.error_type.value
                if ingestion.batch.failure is not None
                else "INGESTION_FAILED"
            )
            raise EodExecutionError(code, "ingestion did not produce a complete candidate")
        candidate = ingestion.canonical_table
        if verified_base is None:
            self._prove_initial_coverage(command=command, plan=plan, table=candidate)
            table = candidate
        else:
            table = self._merge_snapshot(
                plan.dataset,
                base=verified_base.table,
                candidate=candidate,
                chunks=fetch_plan.chunks,
            )
        version = DatasetVersion.building(
            version_id=new_entity_id("data_version"),
            dataset=plan.dataset,
            schema_version=str(plan.schema_version),
            code_version=command.code_version,
            rule_version=command.rule_version,
            parent_version_id=(None if verified_base is None else verified_base.version.version_id),
            input_batch_ids=(batch_id,),
            created_at=self._clock(),
        )
        with self._data_uow() as uow:
            uow.data.save_version(version)
            uow.commit()
        try:
            context.checkpoint()
            context.progress(
                stage=f"eod:{plan.dataset.value}:quality",
                progress=context.service.get_job(context.job_id).progress,
                summary="EOD quality evaluation started",
            )
            references = self._references(plan, published_by_dataset)
            content_sha = compute_content_sha256((table,), dataset=plan.dataset)
            coverage_start, coverage_end = self._table_coverage(table, plan.dataset)
            quality_request = QualityRequest(
                dataset=plan.dataset,
                version_id=version.version_id,
                rule_version=command.rule_version,
                schema_version=version.schema_version,
                code_version=command.code_version,
                content_sha256=content_sha,
                input_batch_ids=version.input_batch_ids,
                expected_securities=(
                    () if plan.dataset is DatasetKind.EXCHANGE_CALENDAR else plan.scope
                ),
                expected_exchanges=(
                    plan.scope
                    if plan.dataset is DatasetKind.EXCHANGE_CALENDAR
                    else tuple(sorted({item.rsplit(".", 1)[-1] for item in plan.scope}))
                ),
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                reference_data=references,
                empty_result=ingestion.batch.empty_result,
            )
            outcome = self._quality_runner.run(request=quality_request, table=table)
            self._quality_writer.write(outcome=outcome, row_evidence_tables={})
            quality_run = QualityRun(
                run_id=outcome.run_id,
                version_id=outcome.version_id,
                status=outcome.status,
                rule_version=outcome.rule_version,
                schema_version=outcome.schema_version,
                content_sha256=outcome.content_sha256,
                started_at=outcome.started_at,
                ended_at=outcome.ended_at,
                results=tuple(
                    QualityResult(
                        rule_code=item.rule_code,
                        passed=item.status is RuleOutcomeStatus.PASSED,
                        affected_count=item.affected_count,
                        summary=item.summary,
                        report_relative_path=item.row_evidence_relative_path,
                    )
                    for item in outcome.results
                    if item.status is not RuleOutcomeStatus.SKIPPED
                ),
                summary_report_relative_path=None,
                failure=outcome.failure,
            )
            with self._data_uow() as uow:
                uow.data.save_quality_run(quality_run)
                uow.commit()
            context.checkpoint()
            if quality_run.status is not QualityRunStatus.PASSED:
                raise EodExecutionError("QUALITY_REJECTED", "quality did not pass")
            with context.non_cancellable("canonical_publish"):
                published = self._publish.coordinate(
                    CanonicalPublishCoordinatorRequest(
                        version=version,
                        tables=(table,),
                        code_version=command.code_version,
                        rule_version=command.rule_version,
                        quality_run_id=quality_run.run_id,
                        empty_result=ingestion.batch.empty_result,
                    )
                )
                self._fault("after_coordinator_return")
                verified = verify_canonical_directory(
                    published.directory,
                    data_root=self._data_root,
                )
                self._fault("after_warehouse_verify")
                stored = self._get_version(str(version.version_id))
                if (
                    stored.status is not DatasetVersionStatus.PUBLISHED
                    or verified.manifest.get("version_id") != str(stored.version_id)
                    or verified.manifest.get("content_sha256") != stored.content_sha256
                    or verified.manifest_sha != stored.manifest_sha256
                ):
                    raise EodExecutionError(
                        "PUBLISH_CREDENTIAL_MISMATCH",
                        "published credentials do not match",
                    )
            result = self._result_from_version(
                stored,
                status="PUBLISHED_NEW",
                batch_id=str(batch_id),
            )
            self._fault("before_checkpoint_write")
            self._evidence.write_checkpoint(
                job_id=context.job_id,
                request_sha256_value=command.request_sha256,
                config_sha256=command.config_sha256,
                result=result,
            )
            self._fault("after_checkpoint_write")
            return result
        except BaseException as error:
            self._fail_nonterminal_version(version)
            try:
                stored = self._get_version(str(version.version_id))
            except DatasetVersionNotFoundError:
                raise
            if stored.status is DatasetVersionStatus.PUBLISHED:
                result = self._result_from_version(
                    stored,
                    status="PUBLISHED_NEW",
                    batch_id=str(batch_id),
                )
                self._evidence.write_checkpoint(
                    job_id=context.job_id,
                    request_sha256_value=command.request_sha256,
                    config_sha256=command.config_sha256,
                    result=result,
                )
                raise _PublishedDatasetFailure(result, error) from error
            raise

    def _verify_base(self, plan: _DatasetPlan) -> _VerifiedBase:
        frozen = plan.base_version
        if frozen is None:
            raise EodExecutionError("BASE_VERSION_INVALID", "base_version is required")
        try:
            with self._data_uow() as uow:
                version = uow.data.get_version(frozen.version_id)
                quality = (
                    None
                    if version.quality_run_id is None
                    else uow.data.get_quality_run(str(version.quality_run_id))
                )
                uow.rollback()
            verified = self._query.verify_manifest_by_id(
                version_id=frozen.version_id,
                dataset=plan.dataset.value,
            )
            table = self._query.query_by_id(
                version_id=frozen.version_id,
                dataset=plan.dataset.value,
            )
            actual_coverage = self._coverage_by_scope(
                table,
                dataset=plan.dataset,
                scope=plan.scope,
            )
        except Exception as error:
            raise EodExecutionError(
                "BASE_VERSION_INVALID",
                "base version could not be strictly verified",
            ) from error
        if (
            version.status is not DatasetVersionStatus.PUBLISHED
            or version.dataset is not plan.dataset
            or version.schema_version != frozen.schema_version
            or version.content_sha256 != frozen.content_sha256
            or version.manifest_sha256 != frozen.manifest_sha256
            or verified.manifest_sha != frozen.manifest_sha256
            or verified.manifest.get("content_sha256") != frozen.content_sha256
            or actual_coverage != frozen.coverage
            or tuple((item.scope, str(item.start), str(item.end)) for item in plan.known_coverage)
            != actual_coverage
            or quality is None
            or quality.status is not QualityRunStatus.PASSED
            or quality.version_id != version.version_id
            or quality.rule_version != version.rule_version
            or quality.schema_version != version.schema_version
            or quality.content_sha256 != version.content_sha256
        ):
            raise EodExecutionError(
                "BASE_VERSION_INVALID",
                "base version credentials do not match",
            )
        return _VerifiedBase(
            version=version,
            table=table,
            known_coverage=tuple(
                CoverageWindow(
                    scope=item[0],
                    start=TradingDate.from_iso(item[1]),
                    end=TradingDate.from_iso(item[2]),
                )
                for item in actual_coverage
            ),
        )

    @staticmethod
    def _coverage_by_scope(
        table: pa.Table,
        *,
        dataset: DatasetKind,
        scope: tuple[str, ...],
    ) -> tuple[tuple[str, str, str], ...]:
        date_field = "effective_from" if dataset is DatasetKind.SECURITY_MASTER else "trade_date"
        scope_field = "exchange" if dataset is DatasetKind.EXCHANGE_CALENDAR else "code"
        if date_field not in table.column_names or scope_field not in table.column_names:
            raise EodExecutionError(
                "BASE_VERSION_INVALID",
                "base table cannot derive incremental coverage",
            )
        dates_by_scope: dict[str, list[date]] = {item: [] for item in scope}
        for row in table.select([scope_field, date_field]).to_pylist():
            raw_scope = row[scope_field]
            raw_date = row[date_field]
            if (
                isinstance(raw_scope, str)
                and raw_scope in dates_by_scope
                and isinstance(raw_date, date)
            ):
                dates_by_scope[raw_scope].append(raw_date)
        if any(not values for values in dates_by_scope.values()):
            raise EodExecutionError(
                "BASE_VERSION_INVALID",
                "base table does not cover every configured scope",
            )
        return tuple(
            (
                item,
                min(dates_by_scope[item]).isoformat(),
                max(dates_by_scope[item]).isoformat(),
            )
            for item in sorted(dates_by_scope)
        )

    def _prove_initial_coverage(
        self,
        *,
        command: _FrozenCommand,
        plan: _DatasetPlan,
        table: pa.Table,
    ) -> None:
        actual = self._coverage_by_scope(
            table,
            dataset=plan.dataset,
            scope=plan.scope,
        )
        requested_start = command.request.start_date.isoformat()
        requested_end = command.request.end_date.isoformat()
        if any(start > requested_start or end < requested_end for _, start, end in actual):
            raise EodExecutionError(
                "INITIAL_COVERAGE_INCOMPLETE",
                "initial candidate does not cover the complete requested range",
            )

    @staticmethod
    def _merge_snapshot(
        dataset: DatasetKind,
        *,
        base: pa.Table,
        candidate: pa.Table,
        chunks: tuple[FetchChunk, ...],
    ) -> pa.Table:
        descriptor = canonical_schema(dataset.value)
        if not chunks or any(chunk.dataset is not dataset for chunk in chunks):
            raise EodExecutionError(
                "MERGED_SNAPSHOT_INVALID",
                "actual fetch chunk inventory is invalid",
            )
        base = ensure_sorted(dataset.value, base)
        candidate = ensure_sorted(dataset.value, candidate)
        validation = validate_table(dataset.value, candidate)
        if not validation.ok:
            raise EodExecutionError(
                "MERGED_SNAPSHOT_INVALID",
                "candidate violates the Canonical contract",
            )
        date_field = "effective_from" if dataset is DatasetKind.SECURITY_MASTER else "trade_date"
        scope_field = "exchange" if dataset is DatasetKind.EXCHANGE_CALENDAR else "code"
        candidate_rows = candidate.to_pylist()
        for chunk in chunks:
            observed = {
                row[scope_field]
                for row in candidate_rows
                if row[scope_field] in chunk.scope
                and chunk.start.value <= row[date_field] <= chunk.end.value
            }
            if observed != set(chunk.scope):
                raise EodExecutionError(
                    "MERGED_SNAPSHOT_INVALID",
                    "candidate does not cover every fetched scope",
                )
        for row in candidate_rows:
            if not any(
                row[scope_field] in chunk.scope
                and chunk.start.value <= row[date_field] <= chunk.end.value
                for chunk in chunks
            ):
                raise EodExecutionError(
                    "MERGED_SNAPSHOT_INVALID",
                    "candidate contains a record outside the fetched slices",
                )
        key_fields = descriptor_for(dataset.value).primary_key
        rows: dict[tuple[object, ...], dict[str, object]] = {}
        for row in base.to_pylist():
            if any(
                row[scope_field] in chunk.scope
                and chunk.start.value <= row[date_field] <= chunk.end.value
                for chunk in chunks
            ):
                continue
            key = tuple(row[field] for field in key_fields)
            rows[key] = row
        for row in candidate_rows:
            key = tuple(row[field] for field in key_fields)
            rows[key] = row
        merged = pa.Table.from_pylist(list(rows.values()), schema=descriptor)
        merged = ensure_sorted(dataset.value, merged)
        validation = validate_table(dataset.value, merged)
        if not validation.ok:
            raise EodExecutionError(
                "MERGED_SNAPSHOT_INVALID",
                "merged full snapshot violates the Canonical contract",
            )
        return merged

    @staticmethod
    def _table_coverage(
        table: pa.Table,
        dataset: DatasetKind,
    ) -> tuple[TradingDate, TradingDate]:
        date_field = "effective_from" if dataset is DatasetKind.SECURITY_MASTER else "trade_date"
        if date_field not in table.column_names:
            raise EodExecutionError(
                "CANDIDATE_COVERAGE_INVALID",
                "candidate has no canonical date field",
            )
        dates = [item for item in table.column(date_field).to_pylist() if item is not None]
        if not dates:
            raise EodExecutionError(
                "CANDIDATE_COVERAGE_INVALID",
                "candidate has no coverage dates",
            )
        return TradingDate(min(dates)), TradingDate(max(dates))

    def _result_from_version(
        self,
        version: DatasetVersion,
        *,
        status: str,
        batch_id: str | None,
    ) -> EodDatasetResult:
        if (
            version.status is not DatasetVersionStatus.PUBLISHED
            or version.quality_run_id is None
            or version.content_sha256 is None
            or version.manifest_sha256 is None
        ):
            raise EodExecutionError(
                "PUBLISH_CREDENTIAL_MISMATCH",
                "published result credentials are incomplete",
            )
        result = EodDatasetResult(
            dataset=version.dataset.value,
            status=status,
            batch_id=batch_id,
            version_id=str(version.version_id),
            quality_run_id=str(version.quality_run_id),
            content_sha256=version.content_sha256,
            manifest_sha256=version.manifest_sha256,
            parent_version_id=(
                None if version.parent_version_id is None else str(version.parent_version_id)
            ),
        )
        self._verify_completed_result(result)
        return result

    def _verify_completed_result(
        self,
        result: EodDatasetResult,
        *,
        command: _FrozenCommand | None = None,
    ) -> None:
        if command is not None:
            try:
                verify_eod_completed_results(
                    command,
                    (result,),
                    data_root=self._data_root,
                    uow_factory=self._data_uow,
                )
            except EodEvidenceConflictError as error:
                raise EodExecutionError(
                    "PUBLISH_CREDENTIAL_MISMATCH",
                    "completed result does not match the frozen command",
                ) from error
            return
        with self._data_uow() as uow:
            version = uow.data.get_version(result.version_id)
            quality = uow.data.get_quality_run(result.quality_run_id)
            uow.rollback()
        verified = self._query.verify_manifest_by_id(
            version_id=result.version_id,
            dataset=result.dataset,
        )
        if (
            version.status is not DatasetVersionStatus.PUBLISHED
            or version.dataset.value != result.dataset
            or version.content_sha256 != result.content_sha256
            or version.manifest_sha256 != result.manifest_sha256
            or (None if version.parent_version_id is None else str(version.parent_version_id))
            != result.parent_version_id
            or quality.status is not QualityRunStatus.PASSED
            or quality.version_id != version.version_id
            or quality.rule_version != version.rule_version
            or quality.schema_version != version.schema_version
            or quality.content_sha256 != result.content_sha256
            or verified.manifest_sha != result.manifest_sha256
        ):
            raise EodExecutionError(
                "PUBLISH_CREDENTIAL_MISMATCH",
                "completed result credentials do not match",
            )

    def _references(
        self,
        plan: _DatasetPlan,
        published_by_dataset: Mapping[DatasetKind, EodDatasetResult],
    ) -> Mapping[str, ReferenceDataBinding]:
        references: dict[str, ReferenceDataBinding] = {
            key: ReferenceDataBinding(
                dataset=DatasetKind(reference.dataset),
                version_id=parse_entity_id(reference.version_id),
                content_sha256=reference.content_sha256,
            )
            for key, reference in plan.references.items()
        }
        if (
            plan.dataset
            in {
                DatasetKind.DAILY_BAR_UNADJUSTED,
                DatasetKind.ADJUSTMENT_FACTOR,
                DatasetKind.TRADING_STATUS,
                DatasetKind.BENCHMARK_INDEX_DAILY,
            }
            and "exchange_calendar" not in references
            and DatasetKind.EXCHANGE_CALENDAR in published_by_dataset
        ):
            result = published_by_dataset[DatasetKind.EXCHANGE_CALENDAR]
            references["exchange_calendar"] = ReferenceDataBinding(
                dataset=DatasetKind.EXCHANGE_CALENDAR,
                version_id=parse_entity_id(result.version_id),
                content_sha256=result.content_sha256,
            )
        return references

    def _get_version(self, version_id: str) -> DatasetVersion:
        with self._data_uow() as uow:
            version = uow.data.get_version(version_id)
            uow.rollback()
            return version

    def _fail_nonterminal_version(self, version: DatasetVersion) -> None:
        try:
            stored = self._get_version(str(version.version_id))
        except DatasetVersionNotFoundError:
            return
        if stored.status not in {
            DatasetVersionStatus.BUILDING,
            DatasetVersionStatus.VALIDATED,
        }:
            return
        failed = transition_dataset_version(
            stored,
            DatasetVersionStatus.FAILED,
            at=self._clock(),
            failure=BatchFailureInfo(
                error_type=BatchFailureKind.INTERNAL,
                message="EOD dataset execution failed; inspect managed evidence",
                retryable=False,
            ),
        )
        with self._data_uow() as uow:
            uow.data.save_version(failed)
            uow.commit()


def register_eod_handler(registry: WorkerRegistry, handler: EodWorkerHandler) -> None:
    """Register the one fixed EOD job type."""
    registry.register(EOD_JOB_TYPE, handler)


__all__ = [
    "EodExecutionError",
    "EodWorkerHandler",
    "register_eod_handler",
]
