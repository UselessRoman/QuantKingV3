"""Offline EOD Worker composition over the existing Stage 1 data pipeline."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from quantking.common.errors import DatasetVersionNotFoundError
from quantking.common.ids import new_entity_id, parse_entity_id
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.persistence.sqlite import DataSqliteUnitOfWorkFactory
from quantking.data.adapters.sources.capabilities import capability_for
from quantking.data.adapters.sources.ingestion import XtQuantIngestionService
from quantking.data.adapters.warehouse.publish_coordinator import (
    CanonicalPublishCoordinator,
    CanonicalPublishCoordinatorRequest,
    compute_content_sha256,
)
from quantking.data.adapters.warehouse.verifier import verify_canonical_directory
from quantking.data.eod.artifacts import EodEvidenceWriter
from quantking.data.eod.contracts import (
    EOD_CONFIG_SCHEMA,
    EOD_JOB_TYPE,
    EOD_REQUEST_MANIFEST_SCHEMA,
    EodDatasetResult,
    EodPublishIntent,
    EodReferenceVersion,
    EodRunRequest,
    EodRunResult,
    canonical_json,
    load_canonical_json,
    sha256_text,
)
from quantking.data.incremental import (
    CoverageWindow,
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
from quantking.jobs.jsonl import JsonLinesHandler
from quantking.jobs.models import JobRun
from quantking.jobs.worker import JobCancelledAtCheckpoint, WorkerContext, WorkerRegistry


class EodExecutionError(Exception):
    """Stable EOD orchestration failure safe to persist by code only."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _DatasetPlan:
    dataset: DatasetKind
    schema_version: str
    scope: tuple[str, ...]
    known_coverage: tuple[CoverageWindow, ...]
    references: Mapping[str, EodReferenceVersion]


@dataclass(frozen=True, slots=True)
class _FrozenCommand:
    request: EodRunRequest
    request_sha256: str
    config_sha256: str
    code_version: str
    adapter_version: str
    rule_version: str
    lookback_days: int
    max_days_per_chunk: int
    max_items_per_chunk: int
    datasets: tuple[_DatasetPlan, ...]
    evidence_request: Mapping[str, object]


def _required_int(value: object, *, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} is invalid")
    return value


def _reconstruct(job: JobRun) -> _FrozenCommand:
    if job.job_type != EOD_JOB_TYPE:
        raise EodExecutionError("REQUEST_MANIFEST_INVALID", "job type is not EOD")
    if (
        set(job.input_manifest)
        != {
            "schema_version",
            "request_json",
            "request_sha256",
        }
        or job.input_manifest.get("schema_version") != EOD_REQUEST_MANIFEST_SCHEMA
    ):
        raise EodExecutionError("REQUEST_MANIFEST_INVALID", "request manifest fields are invalid")
    request_json = job.input_manifest["request_json"]
    request_sha = job.input_manifest["request_sha256"]
    if sha256_text(request_json) != request_sha:
        raise EodExecutionError("REQUEST_MANIFEST_INVALID", "request SHA does not match")
    try:
        request = EodRunRequest.from_dict(load_canonical_json(request_json))
    except (TypeError, ValueError) as error:
        raise EodExecutionError(
            "REQUEST_MANIFEST_INVALID", "request reconstruction failed"
        ) from error
    if request.publish_intent is not EodPublishIntent.PUBLISH_AFTER_QUALITY:
        raise EodExecutionError("REQUEST_MANIFEST_INVALID", "publish intent is invalid")
    if (
        set(job.config_manifest)
        != {
            "schema_version",
            "config_json",
            "config_sha256",
        }
        or job.config_manifest.get("schema_version") != EOD_CONFIG_SCHEMA
    ):
        raise EodExecutionError("CONFIG_MANIFEST_INVALID", "config manifest fields are invalid")
    config_json = job.config_manifest["config_json"]
    config_sha = job.config_manifest["config_sha256"]
    if sha256_text(config_json) != config_sha:
        raise EodExecutionError("CONFIG_MANIFEST_INVALID", "config SHA does not match")
    try:
        config = load_canonical_json(config_json)
        expected_config_keys = {
            "schema_version",
            "code_version",
            "adapter_version",
            "rule_version",
            "lookback_days",
            "max_days_per_chunk",
            "max_items_per_chunk",
            "datasets",
            "schema_versions",
        }
        if set(config) != expected_config_keys or config["schema_version"] != EOD_CONFIG_SCHEMA:
            raise ValueError("config fields are invalid")
        raw_datasets = config["datasets"]
        raw_schemas = config["schema_versions"]
        if not isinstance(raw_datasets, dict) or not isinstance(raw_schemas, dict):
            raise TypeError("dataset config is invalid")
        if set(raw_datasets) != set(request.datasets) or set(raw_schemas) != set(request.datasets):
            raise ValueError("selected dataset config does not match request")
        plans: list[_DatasetPlan] = []
        for name in request.datasets:
            raw = raw_datasets[name]
            if not isinstance(raw, dict) or set(raw) != {
                "scope",
                "known_coverage",
                "references",
            }:
                raise ValueError("dataset plan fields are invalid")
            raw_scope = raw["scope"]
            raw_coverage = raw["known_coverage"]
            raw_references = raw["references"]
            if (
                not isinstance(raw_scope, list)
                or not isinstance(raw_coverage, list)
                or not isinstance(raw_references, dict)
            ):
                raise TypeError("dataset plan values are invalid")
            coverage: list[CoverageWindow] = []
            for item in raw_coverage:
                if not isinstance(item, dict) or set(item) != {"scope", "start", "end"}:
                    raise ValueError("coverage fields are invalid")
                coverage.append(
                    CoverageWindow(
                        scope=str(item["scope"]),
                        start=TradingDate.from_iso(str(item["start"])),
                        end=TradingDate.from_iso(str(item["end"])),
                    )
                )
            references: dict[str, EodReferenceVersion] = {}
            for key, value in raw_references.items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    raise TypeError("reference config is invalid")
                references[key] = EodReferenceVersion(
                    dataset=str(value.get("dataset")),
                    version_id=str(value.get("version_id")),
                    content_sha256=str(value.get("content_sha256")),
                )
            plans.append(
                _DatasetPlan(
                    dataset=DatasetKind(name),
                    schema_version=str(raw_schemas[name]),
                    scope=tuple(raw_scope),
                    known_coverage=tuple(coverage),
                    references=references,
                )
            )
        versions = {
            "adapter_version": str(config["adapter_version"]),
            "code_version": str(config["code_version"]),
            "rule_version": str(config["rule_version"]),
            "schema_versions_sha256": sha256_text(canonical_json(raw_schemas)),
        }
        if dict(job.version_manifest) != versions:
            raise ValueError("version manifest does not match config")
    except (KeyError, TypeError, ValueError) as error:
        raise EodExecutionError(
            "CONFIG_MANIFEST_INVALID", "config reconstruction failed"
        ) from error
    evidence_request = {
        "schema_version": "eod-request-evidence.v1",
        "request": request.to_dict(),
        "request_sha256": request_sha,
        "config_sha256": config_sha,
        "version_manifest": dict(job.version_manifest),
    }
    return _FrozenCommand(
        request=request,
        request_sha256=request_sha,
        config_sha256=config_sha,
        code_version=str(config["code_version"]),
        adapter_version=str(config["adapter_version"]),
        rule_version=str(config["rule_version"]),
        lookback_days=_required_int(config["lookback_days"], field="lookback_days", minimum=0),
        max_days_per_chunk=_required_int(
            config["max_days_per_chunk"],
            field="max_days_per_chunk",
            minimum=1,
        ),
        max_items_per_chunk=_required_int(
            config["max_items_per_chunk"],
            field="max_items_per_chunk",
            minimum=1,
        ),
        datasets=tuple(plans),
        evidence_request=evidence_request,
    )


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

    def __call__(self, context: WorkerContext) -> None:
        job = context.service.get_job(context.job_id)
        logger, log_handler = self._job_logger(job)
        command: _FrozenCommand | None = None
        completed: list[EodDatasetResult] = []
        try:
            logger.info(
                "EOD execution started",
                extra={"event": "eod_started", "job_id": context.job_id},
            )
            command = _reconstruct(job)
            self._preflight(command)
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
                    published_by_dataset=published_by_dataset,
                )
                completed.append(result)
                published_by_dataset[plan.dataset] = result
                logger.info(
                    "EOD dataset published",
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
                context.activity(
                    stage=f"eod:{plan.dataset.value}:published",
                    progress=(index + 1) / total,
                    summary="EOD dataset published",
                    audit_action="eod_dataset_published",
                )
            run_result = EodRunResult(job_id=context.job_id, datasets=tuple(completed))
            directory = self._evidence.write_success(
                job_id=context.job_id,
                request=command.evidence_request,
                result=run_result.to_dict(),
            )
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
            code = (
                error.code
                if isinstance(error, EodExecutionError)
                else ("CANCELLED" if isinstance(error, JobCancelledAtCheckpoint) else "EOD_FAILED")
            )
            evidence_request = (
                command.evidence_request
                if command is not None
                else {
                    "schema_version": "eod-invalid-request-evidence.v1",
                    "job_id": context.job_id,
                    "request_manifest_valid": False,
                }
            )
            directory = self._evidence.write_failure(
                job_id=context.job_id,
                request=evidence_request,
                code=code,
                message=str(error),
                completed_datasets=tuple(item.to_dict() for item in completed),
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

    def _preflight(self, command: _FrozenCommand) -> None:
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

    def _execute_dataset(
        self,
        context: WorkerContext,
        *,
        command: _FrozenCommand,
        plan: _DatasetPlan,
        published_by_dataset: Mapping[DatasetKind, EodDatasetResult],
    ) -> EodDatasetResult:
        batch_id = new_entity_id("data_batch")
        fetch_plan = build_incremental_plan(
            IncrementalPlanRequest(
                batch_id=batch_id,
                dataset=plan.dataset,
                scope=plan.scope,
                request_start=TradingDate(command.request.start_date),
                request_end=TradingDate(command.request.end_date),
                start_inclusive=True,
                end_inclusive=True,
                known_coverage=plan.known_coverage,
                lookback_days=command.lookback_days,
                max_days_per_chunk=command.max_days_per_chunk,
                max_items_per_chunk=command.max_items_per_chunk,
                adapter_version=command.adapter_version,
            )
        )

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
        table = ingestion.canonical_table
        version = DatasetVersion.building(
            version_id=new_entity_id("data_version"),
            dataset=plan.dataset,
            schema_version=str(plan.schema_version),
            code_version=command.code_version,
            rule_version=command.rule_version,
            parent_version_id=None,
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
                coverage_start=TradingDate(command.request.start_date),
                coverage_end=TradingDate(command.request.end_date),
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
                verified = verify_canonical_directory(
                    published.directory,
                    data_root=self._data_root,
                )
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
            assert stored.content_sha256 is not None
            return EodDatasetResult(
                dataset=plan.dataset.value,
                status="PUBLISHED",
                batch_id=str(batch_id),
                version_id=str(stored.version_id),
                quality_run_id=str(quality_run.run_id),
                content_sha256=stored.content_sha256,
            )
        except BaseException:
            self._fail_nonterminal_version(version)
            raise

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
