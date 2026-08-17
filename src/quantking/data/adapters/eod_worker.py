"""K4 three-role EOD orchestration without a parallel evidence control plane.

Normal tasks collect A (BaoStock) and B (Tencent) over the full range; C
(Sina) is called only for affected spans and stays zero planned/actual when
A/B agree. The whole call plan is preflighted against the frozen endpoint
budget before the first vendor invocation, with zero application-layer retry.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pyarrow as pa

from quantking.common.errors import DatasetVersionNotFoundError
from quantking.common.ids import EntityId, new_entity_id, parse_entity_id
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.canonical_schema import canonical_schema, ensure_sorted, validate_table
from quantking.data.adapters.persistence.sqlite import DataSqliteUnitOfWorkFactory
from quantking.data.adapters.resolution_sources import K4ResolutionSourcePort
from quantking.data.adapters.sources.budget import K4EndpointBudget
from quantking.data.adapters.sources.contracts import SourceAdapterError, SourceErrorCode
from quantking.data.adapters.sources.ingestion import AkshareIngestionService
from quantking.data.adapters.sources.legal_empty import (
    CalendarReferenceIncompleteError,
    K4ExpectedSessionResolver,
)
from quantking.data.adapters.sources.registry import PRODUCTION_UPSTREAMS
from quantking.data.adapters.warehouse.manifest import path_safe_id
from quantking.data.adapters.warehouse.publish_coordinator import (
    CanonicalPublishCoordinator,
    CanonicalPublishCoordinatorRequest,
    compute_content_sha256,
)
from quantking.data.adapters.warehouse.query import CanonicalQueryAdapter
from quantking.data.adapters.warehouse.verifier import verify_raw_directory
from quantking.data.comparison.thresholds import k4_evidence_thresholds
from quantking.data.comparison.verifier import DifferenceReportVerifier
from quantking.data.eod.contracts import EOD_JOB_TYPE, EodReferenceVersion
from quantking.data.eod.fingerprint import eod_input_fingerprint
from quantking.data.eod.frozen import (
    FrozenEodCommand,
    FrozenEodCommandError,
    parse_frozen_eod_command,
)
from quantking.data.eod.frozen import FrozenEodDataset as DatasetPlan
from quantking.data.incremental import FetchChunk
from quantking.data.models import (
    BatchFailureInfo,
    BatchFailureKind,
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    DataUpstream,
    EmptyResultInfo,
    LegalEmptyClassification,
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
from quantking.data.quality.references import PublishedReferenceLoader, ReferenceDataBindingError
from quantking.data.quality.runner import QualityRunner
from quantking.data.resolution.artifacts import (
    ResolutionArtifactWriter,
    VerifiedResolutionArtifact,
    verify_resolution_artifact,
)
from quantking.data.resolution.differences import ResolutionDifferenceArtifactRecorder
from quantking.data.resolution.models import K4_RESOLUTION_RULE_VERSION, ResolutionRunRecord
from quantking.data.resolution.service import ResolutionRequest, SourceResolutionService
from quantking.jobs.jsonl import JsonLinesHandler
from quantking.jobs.models import JobRun, JobStatus
from quantking.jobs.services import JobService
from quantking.jobs.worker import WorkerContext, WorkerRegistry


class EodExecutionError(Exception):
    """Stable orchestration failure; third-party exception text is never persisted."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _VerifiedBase:
    version: DatasetVersion
    table: pa.Table


def _reconstruct(job: JobRun) -> FrozenEodCommand:
    try:
        return parse_frozen_eod_command(
            job_type=job.job_type,
            input_manifest=job.input_manifest,
            config_manifest=job.config_manifest,
            version_manifest=job.version_manifest,
        )
    except FrozenEodCommandError as error:
        raise EodExecutionError("FROZEN_COMMAND_INVALID") from error


class EodWorkerHandler:
    """Execute one daily-bar K4 command from frozen JobRun manifests."""

    def __init__(
        self,
        *,
        ingestions: Mapping[DataUpstream, AkshareIngestionService],
        quality_runner: QualityRunner,
        quality_writer: QualityArtifactWriter,
        publish_coordinator: CanonicalPublishCoordinator,
        data_uow_factory: DataSqliteUnitOfWorkFactory,
        data_root: Path,
        clock: Callable[[], UtcTimestamp],
        query_adapter: CanonicalQueryAdapter | None = None,
        log_root: Path | None = None,
        fault_injector: Callable[[str], None] | None = None,
        reference_loader: PublishedReferenceLoader | None = None,
    ) -> None:
        if set(ingestions) != set(PRODUCTION_UPSTREAMS):
            raise ValueError("EOD requires the exact three-role production ingestions")
        self._ingestions = dict(ingestions)
        self._quality_runner = quality_runner
        self._quality_writer = quality_writer
        self._publish = publish_coordinator
        self._data_uow = data_uow_factory
        self._data_root = data_root
        self._clock = clock
        self._query = query_adapter or CanonicalQueryAdapter(
            data_root=data_root,
            uow_factory=data_uow_factory,
        )
        self._resolution_root = data_root / "artifacts"
        self._log_root = log_root
        self._fault_injector = fault_injector
        self._reference_loader = reference_loader

    def _fault(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    def __call__(self, context: WorkerContext) -> None:
        job = context.service.get_job(context.job_id)
        logger, handler = self._job_logger(job)
        candidate_id: EntityId | None = None
        endpoint_budget: K4EndpointBudget | None = None
        try:
            command = _reconstruct(job)
            if command.resolution_rule_version != K4_RESOLUTION_RULE_VERSION:
                raise EodExecutionError("FROZEN_SOURCE_ROUTE_INVALID")
            endpoint_budget = K4EndpointBudget(
                per_source_limits={upstream: 4 for upstream in PRODUCTION_UPSTREAMS},
                total_limit=command.endpoint_invocation_limit,
            )
            logger.info(
                "EOD endpoint budget frozen",
                extra={
                    "event": "network_budget_planned",
                    "job_id": context.job_id,
                    "endpoint_limit": command.endpoint_invocation_limit,
                    "sina_endpoint_limit": command.c_endpoint_invocation_limit,
                    "estimated_akshare_http_request_limit": command.estimated_http_request_limit,
                    "estimated_baostock_protocol_message_limit": (
                        command.estimated_baostock_protocol_message_limit
                    ),
                    "application_retries": 0,
                },
            )
            plan = self._daily_plan(command)
            chunk_empty_resolver = self._build_chunk_empty_resolver(
                plan,
                coverage_start=command.request.start_date,
                coverage_end=command.request.end_date,
            )
            fingerprint = eod_input_fingerprint(command)
            if self._reuse_published(context, fingerprint=fingerprint):
                logger.info(
                    "EOD reused a strictly verified published version",
                    extra={"event": "eod_reused", "job_id": context.job_id},
                )
                return
            base = None if plan.base_version is None else self._verify_base(plan)
            candidate_id = new_entity_id("data_version")
            created_job_id = parse_entity_id(context.job_id)
            difference_recorder = ResolutionDifferenceArtifactRecorder(
                artifacts_root=self._resolution_root,
                thresholds=k4_evidence_thresholds(),
            )
            resolution_request = ResolutionRequest(
                created_job_id=created_job_id,
                candidate_version_id=candidate_id,
                created_at=self._clock(),
                input_fingerprint=fingerprint,
                symbols=plan.scope,
                start_date=command.request.start_date,
                end_date=command.request.end_date,
            )
            source_port = K4ResolutionSourcePort(
                ingestions=self._ingestions,
                candidate_version_id=candidate_id,
                created_job_id=created_job_id,
                quality_runner=self._quality_runner,
                quality_writer=self._quality_writer,
                code_version=command.code_version,
                adapter_version=command.adapter_version,
                rule_version=command.rule_version,
                reference_data=self._references(plan.references),
                clock=self._clock,
                checkpoint=lambda: self._source_checkpoint(context),
                max_days_per_chunk=command.max_days_per_chunk,
                max_items_per_chunk=command.max_items_per_chunk,
                endpoint_budget=endpoint_budget,
                chunk_empty_resolver=chunk_empty_resolver,
                security_exceptions=plan.security_exceptions,
            )
            resolution = SourceResolutionService(
                sources=source_port,
                differences=difference_recorder,
                thresholds=k4_evidence_thresholds(),
            ).resolve(resolution_request)
            actual_batch_ids = tuple(
                sorted(
                    {
                        binding.batch_id
                        for binding in resolution.run.input_bindings
                        if binding.batch_id is not None
                    },
                    key=str,
                )
            )
            version = DatasetVersion.building(
                version_id=candidate_id,
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                schema_version=plan.schema_version,
                code_version=command.code_version,
                rule_version=command.rule_version,
                parent_version_id=None if base is None else base.version.version_id,
                input_batch_ids=actual_batch_ids,
                created_at=self._clock(),
            )
            artifact = self._write_resolution(resolution.run)
            reports = difference_recorder.verified_reports()
            with self._data_uow() as uow:
                uow.data.save_version(version)
                for quality in source_port.quality_runs:
                    uow.data.save_quality_run(quality)
                uow.data.save_resolution_run(resolution.run, artifact)
                for report in reports:
                    uow.data.save_difference_report(report)
                uow.commit()
            self._fault("after_resolution_summary_commit")
            context.service.add_artifact(
                context.job_id,
                relative_path=(Path("artifacts") / artifact.relative_path).as_posix(),
            )
            if not resolution.publishable or resolution.selected_table is None:
                self._fail_candidate(version, code="RESOLUTION_BLOCKED")
                raise EodExecutionError("RESOLUTION_BLOCKED")
            selected = resolution.selected_table
            table = (
                selected
                if base is None
                else self._merge_base(
                    base.table,
                    selected,
                    symbols=plan.scope,
                    start_date=command.request.start_date,
                    end_date=command.request.end_date,
                )
            )
            empty_result = self._resolved_empty_result(
                table=table,
                source_port=source_port,
            )
            final_quality = self._run_final_quality(
                version=version,
                table=table,
                symbols=plan.scope,
                start_date=command.request.start_date,
                end_date=command.request.end_date,
                references=self._references(plan.references),
                security_exceptions=plan.security_exceptions,
                empty_result=empty_result,
            )
            with self._data_uow() as uow:
                uow.data.save_quality_run(final_quality)
                uow.commit()
            if final_quality.status is not QualityRunStatus.PASSED:
                self._fail_candidate(version, code="FINAL_QUALITY_NOT_PASSED")
                raise EodExecutionError("FINAL_QUALITY_NOT_PASSED")
            with context.non_cancellable("canonical_publish"):
                published = self._publish.coordinate(
                    CanonicalPublishCoordinatorRequest(
                        version=version,
                        tables=(table,),
                        code_version=command.code_version,
                        rule_version=command.rule_version,
                        quality_run_id=final_quality.run_id,
                        empty_result=empty_result,
                        resolution=self._resolution_binding(
                            resolution.run,
                            artifact,
                            reports=reports,
                        ),
                    )
                )
            self._verify_published_binding(
                published.version,
                expected_fingerprint=fingerprint,
                expected_resolution=artifact,
            )
            self._fault("after_publish_before_job_success")
            context.service.add_artifact(
                context.job_id,
                relative_path=published.directory.relative_to(self._data_root).as_posix(),
            )
            context.activity(
                stage="eod:daily_bar_unadjusted:published",
                progress=1.0,
                summary="K4 resolved daily bars published",
                audit_action="eod_dataset_published",
            )
            logger.info(
                "EOD resolution and publish completed",
                extra={
                    "event": "eod_succeeded",
                    "job_id": context.job_id,
                    "version_id": str(published.version.version_id),
                    "resolution_run_id": str(resolution.run.run_id),
                },
            )
        except BaseException as error:
            logger.error(
                "EOD execution failed",
                extra={
                    "event": "eod_failed",
                    "job_id": context.job_id,
                    "error_code": getattr(error, "code", type(error).__name__),
                },
            )
            if candidate_id is not None:
                self._fail_candidate_by_id(candidate_id, code="EOD_FAILED")
            raise
        finally:
            if endpoint_budget is not None:
                logger.info(
                    "S1-08 endpoint budget summary",
                    extra={
                        "event": "network_budget_summary",
                        "job_id": context.job_id,
                        "network_budget": dict(endpoint_budget.summary()),
                    },
                )
            if handler is not None:
                logger.removeHandler(handler)
                handler.close()

    def recover_interrupted(self, service: JobService, job_id: str) -> None:
        """Idempotently fail only this interrupted Job's unpublished candidate."""
        job = service.get_job(job_id)
        if job.job_type != EOD_JOB_TYPE or job.status is not JobStatus.INTERRUPTED:
            return
        with self._data_uow() as uow:
            record = uow.data.find_resolution_run_by_job_id(job_id)
            uow.rollback()
        if record is None:
            return
        candidate_id = self._candidate_for_recovery(record, job_id=job_id)
        if candidate_id is not None:
            self._fail_candidate_by_id(candidate_id, code="INTERRUPTED")

    def _candidate_for_recovery(
        self,
        record: ResolutionRunRecord,
        *,
        job_id: str,
    ) -> EntityId | None:
        artifact = verify_resolution_artifact(
            self._resolution_root / record.artifact_relative_path,
            artifacts_root=self._resolution_root,
        )
        inputs = artifact.summary.get("input_bindings")
        if not isinstance(inputs, list):
            raise EodExecutionError("RESOLUTION_ARTIFACT_INVALID")
        resolution_batch_ids = {
            str(item["batch_id"])
            for item in inputs
            if isinstance(item, dict) and item.get("batch_id") is not None
        }
        with self._data_uow() as uow:
            job_batches = uow.data.list_batches_for_job(job_id)
            versions = uow.data.list_versions(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
            uow.rollback()
        job_batch_ids = {str(item.batch_id) for item in job_batches}
        if resolution_batch_ids != job_batch_ids:
            raise EodExecutionError("INTERRUPTED_BATCH_BINDING_INVALID")
        if record.candidate_version_id is not None:
            matches = tuple(
                item
                for item in versions
                if item.version_id == record.candidate_version_id
                and {str(batch) for batch in item.input_batch_ids} == job_batch_ids
            )
            if len(matches) != 1:
                raise EodExecutionError("INTERRUPTED_CANDIDATE_BINDING_INVALID")
            return record.candidate_version_id
        matches = tuple(
            item
            for item in versions
            if item.status in {DatasetVersionStatus.BUILDING, DatasetVersionStatus.VALIDATED}
            and {str(batch) for batch in item.input_batch_ids} == job_batch_ids
        )
        if len(matches) > 1:
            raise EodExecutionError("INTERRUPTED_CANDIDATE_AMBIGUOUS")
        return None if not matches else matches[0].version_id

    def _reuse_published(self, context: WorkerContext, *, fingerprint: str) -> bool:
        with self._data_uow() as uow:
            record = uow.data.find_resolution_run_by_fingerprint(
                input_fingerprint=fingerprint,
            )
            uow.rollback()
        if record is None or record.candidate_version_id is None:
            return False
        with self._data_uow() as uow:
            version = uow.data.get_version(str(record.candidate_version_id))
            uow.rollback()
        if version.status is not DatasetVersionStatus.PUBLISHED:
            return False
        try:
            artifact = verify_resolution_artifact(
                self._resolution_root / record.artifact_relative_path,
                artifacts_root=self._resolution_root,
            )
            if artifact.manifest_sha256 != record.manifest_sha256:
                raise ValueError("resolution SHA mismatch")
            self._verify_published_binding(
                version,
                expected_fingerprint=fingerprint,
                expected_resolution=artifact,
            )
        except Exception as error:
            raise EodExecutionError("REUSABLE_RESULT_INVALID") from error
        context.service.add_artifact(
            context.job_id,
            relative_path=(Path("artifacts") / record.artifact_relative_path).as_posix(),
        )
        return True

    def _verify_published_binding(
        self,
        version: DatasetVersion,
        *,
        expected_fingerprint: str,
        expected_resolution: VerifiedResolutionArtifact,
    ) -> None:
        verified = self._query.verify_manifest_by_id(
            version_id=str(version.version_id),
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
        )
        binding = verified.manifest.get("resolution")
        if not isinstance(binding, dict) or (
            binding.get("candidate_version_id") != str(version.version_id)
            or binding.get("run_id") != str(expected_resolution.run_id)
            or binding.get("manifest_sha256") != expected_resolution.manifest_sha256
            or binding.get("input_fingerprint") != expected_fingerprint
        ):
            raise EodExecutionError("PUBLISH_RESOLUTION_BINDING_INVALID")
        reports = binding.get("difference_reports")
        if not isinstance(reports, list):
            raise EodExecutionError("PUBLISH_DIFFERENCE_BINDING_INVALID")
        summary_reports = expected_resolution.summary.get("difference_report_ids")
        if (
            not isinstance(summary_reports, list)
            or [item.get("report_id") for item in reports if isinstance(item, dict)]
            != summary_reports
        ):
            raise EodExecutionError("PUBLISH_DIFFERENCE_BINDING_INVALID")
        with self._data_uow() as uow:
            stored_resolution = uow.data.get_resolution_run(str(expected_resolution.run_id))
            input_bindings = expected_resolution.summary.get("input_bindings")
            if not isinstance(input_bindings, list):
                raise EodExecutionError("PUBLISH_RAW_BINDING_INVALID")
            expected_batch_ids = {
                str(item["batch_id"])
                for item in input_bindings
                if isinstance(item, dict) and item.get("batch_id") is not None
            }
            if expected_batch_ids != {str(item) for item in version.input_batch_ids}:
                raise EodExecutionError("PUBLISH_RAW_BINDING_INVALID")
            batches = tuple(uow.data.get_batch(batch_id) for batch_id in sorted(expected_batch_ids))
            stored_reports = {
                item["report_id"]: uow.data.get_difference_report(item["report_id"])
                for item in reports
                if isinstance(item, dict) and isinstance(item.get("report_id"), str)
            }
            uow.rollback()
        if (
            stored_resolution.manifest_sha256 != expected_resolution.manifest_sha256
            or stored_resolution.artifact_relative_path != expected_resolution.relative_path
            or stored_resolution.candidate_version_id != version.version_id
            or stored_resolution.input_fingerprint != expected_fingerprint
        ):
            raise EodExecutionError("PUBLISH_RESOLUTION_DB_BINDING_INVALID")
        created_job_id = str(expected_resolution.summary["created_job_id"])
        for batch in batches:
            if batch.job_run_id != created_job_id:
                raise EodExecutionError("PUBLISH_RAW_JOB_BINDING_INVALID")
            self._verify_raw_batch(batch, expected_code_version=version.code_version)
        difference_verifier = DifferenceReportVerifier(artifacts_root=self._resolution_root)
        for item in reports:
            if not isinstance(item, dict):
                raise EodExecutionError("PUBLISH_DIFFERENCE_BINDING_INVALID")
            report_id = parse_entity_id(str(item["report_id"]))
            report = difference_verifier.verify(report_id=report_id)
            stored = stored_reports.get(str(report_id))
            if (
                report.report_relative_path != item.get("artifact_relative_path")
                or report.manifest_sha256 != item.get("manifest_sha256")
                or report.primary_version_id != version.version_id
                or report.fallback_version_id != version.version_id
                or stored is None
                or stored.report_relative_path != report.report_relative_path
                or stored.report_sha256 != report.report_sha256
            ):
                raise EodExecutionError("PUBLISH_DIFFERENCE_BINDING_INVALID")

    def _verify_raw_batch(self, batch: object, *, expected_code_version: str) -> None:
        from quantking.data.models import IngestionBatch

        if not isinstance(batch, IngestionBatch):
            raise TypeError("batch must be IngestionBatch")
        base = (
            self._data_root
            / "raw"
            / batch.upstream.value.lower()
            / batch.dataset.value
            / path_safe_id(str(batch.batch_id))
        )
        if batch.manifest_files:
            raw = verify_raw_directory(base, data_root=self._data_root)
            actual = {
                (
                    f"{base.relative_to(self._data_root).as_posix()}/{item.relative_path}",
                    item.size,
                    item.sha256,
                )
                for item in raw.files
            }
            expected = {
                (item.relative_path, item.size, item.sha256) for item in batch.manifest_files
            }
            if (
                actual != expected
                or raw.manifest.get("code_version") != expected_code_version
                or raw.manifest.get("source") != batch.upstream.value.lower()
                or raw.manifest.get("batch_id") != path_safe_id(str(batch.batch_id))
            ):
                raise EodExecutionError("PUBLISH_RAW_BINDING_INVALID")
            return
        failure_directory = base.with_name(f"{base.name}.incomplete")
        try:
            failure = json.loads((failure_directory / "failure.json").read_text("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EodExecutionError("PUBLISH_RAW_FAILURE_BINDING_INVALID") from error
        if (
            not isinstance(failure, dict)
            or failure.get("schema_version") != "raw-attempt-failure.v1"
            or failure.get("batch_id") != path_safe_id(str(batch.batch_id))
            or failure.get("source") != batch.upstream.value.lower()
            or failure.get("dataset") != batch.dataset.value
            or failure.get("code_version") != expected_code_version
            or failure.get("adapter_version") != batch.adapter_version
        ):
            raise EodExecutionError("PUBLISH_RAW_FAILURE_BINDING_INVALID")
        try:
            raw_error = SourceErrorCode(str(failure.get("error_code")))
        except ValueError as error:
            raise EodExecutionError("PUBLISH_RAW_FAILURE_BINDING_INVALID") from error
        if (
            batch.failure is None
            or SourceAdapterError(raw_error).failure_kind is not batch.failure.error_type
        ):
            raise EodExecutionError("PUBLISH_RAW_FAILURE_BINDING_INVALID")

    def _write_resolution(self, run: object) -> VerifiedResolutionArtifact:
        from quantking.data.resolution.models import ResolutionRun

        if not isinstance(run, ResolutionRun):
            raise TypeError("run must be ResolutionRun")
        directory = ResolutionArtifactWriter(artifacts_root=self._resolution_root).write(run)
        return verify_resolution_artifact(
            directory,
            artifacts_root=self._resolution_root,
        )

    @staticmethod
    def _resolution_binding(
        run: object,
        artifact: VerifiedResolutionArtifact,
        *,
        reports: tuple[object, ...],
    ) -> dict[str, object]:
        from quantking.data.comparison.verifier import VerifiedDifferenceReport
        from quantking.data.resolution.models import ResolutionRun

        if not isinstance(run, ResolutionRun) or run.candidate_version_id is None:
            raise EodExecutionError("RESOLUTION_NOT_PUBLISHABLE")
        if any(not isinstance(item, VerifiedDifferenceReport) for item in reports):
            raise TypeError("reports must contain VerifiedDifferenceReport values")
        verified_reports = tuple(
            sorted(
                (item for item in reports if isinstance(item, VerifiedDifferenceReport)),
                key=lambda item: str(item.report_id),
            )
        )
        if tuple(item.report_id for item in verified_reports) != run.difference_report_ids:
            raise EodExecutionError("RESOLUTION_DIFFERENCE_BINDING_INVALID")
        return {
            "artifact_relative_path": artifact.relative_path,
            "candidate_version_id": str(run.candidate_version_id),
            "created_job_id": str(run.created_job_id),
            "difference_reports": [
                {
                    "report_id": str(item.report_id),
                    "artifact_relative_path": item.report_relative_path,
                    "manifest_sha256": item.manifest_sha256,
                }
                for item in verified_reports
            ],
            "input_fingerprint": run.input_fingerprint,
            "manifest_sha256": artifact.manifest_sha256,
            "rule_version": run.rule_version,
            "run_id": str(run.run_id),
            "threshold_rule_version": run.threshold_rule_version,
        }

    def _run_final_quality(
        self,
        *,
        version: DatasetVersion,
        table: pa.Table,
        symbols: tuple[str, ...],
        start_date: date,
        end_date: date,
        references: Mapping[str, ReferenceDataBinding],
        security_exceptions: Mapping[str, tuple[tuple[TradingDate, TradingDate], ...]],
        empty_result: EmptyResultInfo | None,
    ) -> QualityRun:
        content_sha = compute_content_sha256(
            (table,),
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            empty_result=empty_result,
        )
        request = QualityRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            version_id=version.version_id,
            rule_version=version.rule_version,
            schema_version=version.schema_version,
            code_version=version.code_version,
            content_sha256=content_sha,
            input_batch_ids=version.input_batch_ids,
            expected_securities=symbols,
            expected_exchanges=tuple(sorted({item.rsplit(".", 1)[-1] for item in symbols})),
            coverage_start=TradingDate(start_date),
            coverage_end=TradingDate(end_date),
            reference_data=references,
            empty_result=empty_result,
            security_exceptions=security_exceptions,
        )
        started = self._clock()
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
            return QualityRun(
                run_id=new_entity_id("data_quality"),
                version_id=version.version_id,
                status=QualityRunStatus.FAILED,
                rule_version=version.rule_version,
                schema_version=version.schema_version,
                content_sha256=content_sha,
                started_at=started,
                ended_at=self._clock(),
                results=(),
                summary_report_relative_path=None,
                failure=BatchFailureInfo(
                    error_type=BatchFailureKind.QUALITY,
                    message="final quality execution failed",
                    retryable=False,
                ),
            )

    @staticmethod
    def _resolved_empty_result(
        *,
        table: pa.Table,
        source_port: K4ResolutionSourcePort,
    ) -> EmptyResultInfo | None:
        if table.num_rows:
            return None
        empty_sources = {
            item.upstream for item in source_port.candidates if item.empty_result is not None
        }
        if len(empty_sources) < 2:
            raise EodExecutionError("EMPTY_CONSENSUS_INVALID")
        return EmptyResultInfo(
            classification=LegalEmptyClassification.NO_TRADING_SESSION,
            reason="two-source no-trading-session consensus",
        )

    @staticmethod
    def _merge_base(
        base: pa.Table,
        selected: pa.Table,
        *,
        symbols: tuple[str, ...],
        start_date: date,
        end_date: date,
    ) -> pa.Table:
        selected_rows = selected.to_pylist()
        if any(
            row.get("code") not in symbols
            or not isinstance(row.get("trade_date"), date)
            or not start_date <= row["trade_date"] <= end_date
            for row in selected_rows
        ):
            raise EodExecutionError("SELECTED_SCOPE_INVALID")
        rows = {
            (row["code"], row["trade_date"]): row
            for row in base.to_pylist()
            if not (row["code"] in symbols and start_date <= row["trade_date"] <= end_date)
        }
        for row in selected_rows:
            rows[(row["code"], row["trade_date"])] = row
        table = ensure_sorted(
            DatasetKind.DAILY_BAR_UNADJUSTED.value,
            pa.Table.from_pylist(
                list(rows.values()),
                schema=canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value),
            ),
        )
        if not validate_table(DatasetKind.DAILY_BAR_UNADJUSTED.value, table).ok:
            raise EodExecutionError("MERGED_SNAPSHOT_INVALID")
        return table

    def _verify_base(self, plan: DatasetPlan) -> _VerifiedBase:
        frozen = plan.base_version
        if frozen is None:
            raise EodExecutionError("BASE_VERSION_INVALID")
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
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
            )
            table = self._query.query_by_id(
                version_id=frozen.version_id,
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
            )
        except Exception as error:
            raise EodExecutionError("BASE_VERSION_INVALID") from error
        if (
            version.status is not DatasetVersionStatus.PUBLISHED
            or version.content_sha256 != frozen.content_sha256
            or version.manifest_sha256 != frozen.manifest_sha256
            or verified.manifest_sha != frozen.manifest_sha256
            or quality is None
            or quality.status is not QualityRunStatus.PASSED
            or quality.version_id != version.version_id
            or quality.content_sha256 != version.content_sha256
        ):
            raise EodExecutionError("BASE_VERSION_INVALID")
        return _VerifiedBase(version=version, table=table)

    def _fail_candidate_by_id(self, version_id: EntityId, *, code: str) -> None:
        try:
            with self._data_uow() as uow:
                version = uow.data.get_version(str(version_id))
                uow.rollback()
        except DatasetVersionNotFoundError:
            return
        self._fail_candidate(version, code=code)

    def _fail_candidate(self, version: DatasetVersion, *, code: str) -> None:
        if version.status not in {
            DatasetVersionStatus.BUILDING,
            DatasetVersionStatus.VALIDATED,
        }:
            return
        failed = transition_dataset_version(
            version,
            DatasetVersionStatus.FAILED,
            at=self._clock(),
            failure=BatchFailureInfo(
                error_type=BatchFailureKind.INTERNAL,
                message=code,
                retryable=False,
            ),
        )
        with self._data_uow() as uow:
            uow.data.save_version(failed)
            uow.commit()

    @staticmethod
    def _references(
        references: Mapping[str, EodReferenceVersion],
    ) -> Mapping[str, ReferenceDataBinding]:
        return {
            key: ReferenceDataBinding(
                dataset=DatasetKind(value.dataset),
                version_id=parse_entity_id(value.version_id),
                content_sha256=value.content_sha256,
            )
            for key, value in references.items()
        }

    def _build_chunk_empty_resolver(
        self,
        plan: DatasetPlan,
        *,
        coverage_start: date,
        coverage_end: date,
    ) -> Callable[[FetchChunk], EmptyResultInfo | None]:
        """Bind the per-chunk legal-empty classifier to the frozen calendar.

        The published calendar is verified (PUBLISHED status, dataset type,
        version id and content SHA) by the reference loader before the first
        network call; a missing, invalid or incomplete binding fails closed
        with a stable error code.
        """
        binding = self._references(plan.references).get("exchange_calendar")
        if binding is None:
            raise EodExecutionError("CALENDAR_REFERENCE_MISSING")
        if self._reference_loader is None:
            raise EodExecutionError("CALENDAR_REFERENCE_LOADER_UNAVAILABLE")
        try:
            loaded = self._reference_loader.load("exchange_calendar", binding)
        except ReferenceDataBindingError as error:
            raise EodExecutionError("CALENDAR_REFERENCE_INVALID") from error
        # The read-back table can carry nullable column metadata; restore the
        # exact canonical schema before strict validation (content was already
        # verified at publish time).
        calendar_table = loaded.table.cast(canonical_schema(DatasetKind.EXCHANGE_CALENDAR.value))
        try:
            resolver = K4ExpectedSessionResolver(
                calendar_table=calendar_table,
                expected_securities=plan.scope,
                security_exceptions=plan.security_exceptions,
                coverage_start=coverage_start,
                coverage_end=coverage_end,
            )
        except CalendarReferenceIncompleteError as error:
            raise EodExecutionError("CALENDAR_REFERENCE_INCOMPLETE") from error
        except ValueError as error:
            raise EodExecutionError("CALENDAR_REFERENCE_INVALID") from error

        def classify(chunk: FetchChunk) -> EmptyResultInfo | None:
            return resolver.classify(
                symbols=chunk.scope,
                start_date=chunk.start.value,
                end_date=chunk.end.value,
            )

        return classify

    @staticmethod
    def _daily_plan(command: FrozenEodCommand) -> DatasetPlan:
        if (
            len(command.datasets) != 1
            or command.datasets[0].dataset is not DatasetKind.DAILY_BAR_UNADJUSTED
            or not command.datasets[0].scope
        ):
            raise EodExecutionError("DAILY_ONLY_CAPABILITY")
        return command.datasets[0]

    @staticmethod
    def _source_checkpoint(context: WorkerContext) -> None:
        context.checkpoint()
        context.heartbeat()

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


def register_eod_handler(registry: WorkerRegistry, handler: EodWorkerHandler) -> None:
    registry.register(EOD_JOB_TYPE, handler)


__all__ = ["EodExecutionError", "EodWorkerHandler", "register_eod_handler"]
