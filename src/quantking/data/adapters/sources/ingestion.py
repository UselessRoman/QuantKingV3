"""Provider-neutral Raw-bound daily-bar ingestion orchestration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pyarrow as pa

from quantking.common.errors import InvalidIdError
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.canonical_schema import canonical_schema, ensure_sorted
from quantking.data.adapters.persistence.sqlite import DataSqliteUnitOfWorkFactory
from quantking.data.adapters.sources.budget import NetworkBudgetExceededError
from quantking.data.adapters.sources.contracts import (
    SourceAdapterError,
    SourceErrorCode,
    SupplierResponse,
    SupplierResponseContract,
    classify_source_error,
)
from quantking.data.adapters.sources.normalization import normalize_market_data_response
from quantking.data.adapters.sources.request_window import (
    AKSHARE_LOOKBACK_DAYS,
    fixed_akshare_request_window,
)
from quantking.data.adapters.warehouse.manifest import RawManifestFile, path_safe_id
from quantking.data.adapters.warehouse.raw_store import (
    RawBatchFailureRequest,
    RawBatchPublisher,
    RawBatchPublishRequest,
)
from quantking.data.adapters.warehouse.verifier import (
    VerifiedDirectory,
    VerifiedFile,
    verify_data_root,
    verify_raw_directory,
)
from quantking.data.incremental import (
    FetchChunk,
    IncrementalFetchPlan,
    validate_fetch_chunk,
    validate_incremental_plan,
)
from quantking.data.models import (
    BatchFailureInfo,
    DataProvider,
    DatasetKind,
    DataUpstream,
    EmptyResultInfo,
    IngestionBatch,
    IngestionBatchStatus,
    LegalEmptyClassification,
    ManifestFileEntry,
    transition_batch,
)
from quantking.jobs.worker import JobCancelledAtCheckpoint


class MarketDataGateway(Protocol):
    """Narrow provider port consumed by the ingestion service."""

    @property
    def provider(self) -> DataProvider: ...

    @property
    def upstream(self) -> DataUpstream: ...

    @property
    def endpoint(self) -> str: ...

    def library_version(self) -> str: ...

    def fetch(self, chunk: FetchChunk) -> SupplierResponse: ...


@dataclass(frozen=True, slots=True)
class IngestionOutcome:
    batch: IngestionBatch
    canonical_table: pa.Table | None
    raw_directory: Path | None


class AkshareIngestionService:
    """Persist one batch and rebuild Canonical only from verified Raw bytes."""

    def __init__(
        self,
        *,
        gateway: MarketDataGateway,
        raw_publisher: RawBatchPublisher,
        data_root: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
        clock: Callable[[], UtcTimestamp],
    ) -> None:
        for attribute in ("fetch", "library_version", "provider", "upstream", "endpoint"):
            if not hasattr(gateway, attribute):
                raise TypeError(f"gateway must provide {attribute}")
        self._gateway = gateway
        self._raw_publisher = raw_publisher
        self._data_root = verify_data_root(data_root)
        self._uow_factory = uow_factory
        self._clock = clock

    def run(
        self,
        plan: IncrementalFetchPlan,
        *,
        empty_result: EmptyResultInfo | None = None,
        checkpoint: Callable[[], None] | None = None,
        job_run_id: str | None = None,
        code_version: str = "s1-07k1",
        invocation_guard: Callable[[DataUpstream], None] | None = None,
    ) -> IngestionOutcome:
        validate_incremental_plan(plan)
        if not isinstance(code_version, str) or not code_version.strip():
            raise ValueError("code_version must be non-empty text")
        if plan.dataset is not DatasetKind.DAILY_BAR_UNADJUSTED:
            raise SourceAdapterError(SourceErrorCode.REQUEST_REJECTED)
        started_at = self._now()
        try:
            library_version = self._gateway.library_version()
        except Exception as error:
            raise classify_source_error(error) from error
        running = IngestionBatch.running(
            batch_id=plan.batch_id,
            provider=self._gateway.provider,
            upstream=self._gateway.upstream,
            endpoint=self._gateway.endpoint,
            library_version=library_version,
            dataset=plan.dataset,
            adapter_version=plan.adapter_version,
            request_params={
                **dict(plan.request_params),
                "provider": self._gateway.provider.value,
                "upstream": self._gateway.upstream.value,
                "endpoint": self._gateway.endpoint,
                self._library_version_key(self._gateway.provider): library_version,
            },
            started_at=started_at,
            job_run_id=job_run_id,
        )
        self._insert_running_once(running)
        if not plan.chunks:
            return self._empty_request(
                running,
                plan,
                empty_result=empty_result,
                code_version=code_version,
            )
        responses: list[SupplierResponse] = []
        try:
            for chunk in plan.chunks:
                if checkpoint is not None:
                    checkpoint()
                if invocation_guard is not None:
                    invocation_guard(self._gateway.upstream)
                response = self._validate_response(self._gateway.fetch(chunk), chunk)
                responses.append(response)
                if not response.complete and self._gateway.provider is DataProvider.BAOSTOCK:
                    try:
                        partial_code = SourceErrorCode(str(response.partial_reason))
                    except ValueError:
                        partial_code = SourceErrorCode.INTERNAL_PROVIDER
                    return self._finish_failed(
                        running,
                        plan,
                        SourceAdapterError(partial_code),
                        responses=tuple(responses),
                        code_version=code_version,
                    )
                if checkpoint is not None:
                    checkpoint()
        except JobCancelledAtCheckpoint:
            self._finish_failed(
                running,
                plan,
                SourceAdapterError(SourceErrorCode.INTERNAL_PROVIDER),
                responses=tuple(responses),
                code_version=code_version,
            )
            raise
        except NetworkBudgetExceededError:
            raise
        except Exception as error:
            mapped = classify_source_error(error)
            if responses:
                responses.append(
                    self._partial_response(
                        plan.chunks[len(responses)], responses[0].contract, mapped
                    )
                )
            return self._finish_failed(
                running,
                plan,
                mapped,
                responses=tuple(responses),
                code_version=code_version,
            )
        return self._land_success(
            running,
            plan,
            tuple(responses),
            empty_result=empty_result,
            code_version=code_version,
        )

    def _land_success(
        self,
        running: IngestionBatch,
        plan: IncrementalFetchPlan,
        responses: tuple[SupplierResponse, ...],
        *,
        empty_result: EmptyResultInfo | None,
        code_version: str,
    ) -> IngestionOutcome:
        raw_directory: Path | None = None
        manifest_files: tuple[ManifestFileEntry, ...] = ()
        try:
            raw_directory, verified = self._publish_responses(
                running,
                plan,
                responses,
                code_version=code_version,
            )
            manifest_files = self._manifest_entries(raw_directory, verified.files)
            rebuilt = self._rebuild_responses(verified, plan)
            if rebuilt != responses:
                raise SourceAdapterError(SourceErrorCode.PARSE)
            tables = tuple(
                normalize_market_data_response(plan.dataset, response, empty_result=empty_result)
                for response in rebuilt
            )
            candidate = ensure_sorted(plan.dataset.value, pa.concat_tables(tables))
            succeeded = transition_batch(
                running,
                IngestionBatchStatus.SUCCEEDED,
                at=self._safe_now(running),
                row_count=candidate.num_rows,
                security_count=len(set(candidate.column("code").to_pylist())),
                manifest_files=manifest_files,
                empty_result=empty_result,
            )
            self._save_terminal(succeeded)
            return IngestionOutcome(succeeded, candidate, raw_directory)
        except Exception as error:
            mapped = classify_source_error(error)
            failed = transition_batch(
                running,
                IngestionBatchStatus.FAILED,
                at=self._safe_now(running),
                row_count=sum(len(response.records) for response in responses),
                security_count=len(
                    {
                        str(record["stock_code"])
                        for response in responses
                        for record in response.records
                    }
                ),
                manifest_files=manifest_files,
                failure=self._failure_info(mapped),
            )
            self._save_terminal(failed)
            return IngestionOutcome(failed, None, raw_directory)

    def _empty_request(
        self,
        running: IngestionBatch,
        plan: IncrementalFetchPlan,
        *,
        empty_result: EmptyResultInfo | None,
        code_version: str,
    ) -> IngestionOutcome:
        if (
            empty_result is None
            or empty_result.classification is not LegalEmptyClassification.EMPTY_REQUEST_UNIVERSE
        ):
            return self._finish_failed(
                running,
                plan,
                SourceAdapterError(SourceErrorCode.UNEXPECTED_EMPTY),
                responses=(),
                code_version=code_version,
            )
        candidate = pa.Table.from_pylist([], schema=canonical_schema(plan.dataset.value))
        payload = json.dumps(
            {
                "classification": empty_result.classification.value,
                "reason": empty_result.reason,
                "request_sha256": plan.request_sha256,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        request = RawBatchPublishRequest(
            source=running.upstream.value.lower(),
            dataset=plan.dataset.value,
            batch_id=path_safe_id(str(plan.batch_id)),
            adapter_version=plan.adapter_version,
            request_params=self._lineage_params(
                running,
                plan,
                (),
                payload=payload,
                upstream_row_count=0,
                canonical_row_count=0,
            ),
            files=(RawManifestFile("responses/empty-request.json", payload, 0),),
            created_at=running.started_at.to_iso(),
            code_version=code_version,
        )
        try:
            raw_directory = self._raw_publisher.publish(request)
            verified = verify_raw_directory(raw_directory, data_root=self._data_root)
            manifest_files = self._manifest_entries(raw_directory, verified.files)
        except Exception as error:
            return self._finish_failed(
                running,
                plan,
                classify_source_error(error),
                responses=(),
                code_version=code_version,
            )
        succeeded = transition_batch(
            running,
            IngestionBatchStatus.SUCCEEDED,
            at=self._safe_now(running),
            row_count=0,
            security_count=0,
            manifest_files=manifest_files,
            empty_result=empty_result,
        )
        self._save_terminal(succeeded)
        return IngestionOutcome(succeeded, candidate, raw_directory)

    def _finish_failed(
        self,
        running: IngestionBatch,
        plan: IncrementalFetchPlan,
        error: SourceAdapterError,
        *,
        responses: tuple[SupplierResponse, ...],
        code_version: str,
    ) -> IngestionOutcome:
        if responses:
            try:
                raw_directory, verified = self._publish_responses(
                    running,
                    plan,
                    responses,
                    code_version=code_version,
                )
                manifest_files = self._manifest_entries(raw_directory, verified.files)
                partial = transition_batch(
                    running,
                    IngestionBatchStatus.PARTIAL,
                    at=self._safe_now(running),
                    row_count=sum(len(response.records) for response in responses),
                    security_count=len(
                        {
                            str(record["stock_code"])
                            for response in responses
                            for record in response.records
                        }
                    ),
                    manifest_files=manifest_files,
                    failure=self._failure_info(error),
                )
                self._save_terminal(partial)
                return IngestionOutcome(partial, None, raw_directory)
            except Exception:
                error = SourceAdapterError(SourceErrorCode.INTERNAL_PROVIDER)
        try:
            self._raw_publisher.record_failure(
                RawBatchFailureRequest(
                    source=running.upstream.value.lower(),
                    dataset=plan.dataset.value,
                    batch_id=path_safe_id(str(plan.batch_id)),
                    adapter_version=plan.adapter_version,
                    request_params={
                        "adapter_version": plan.adapter_version,
                        "dataset": plan.dataset.value,
                        "request_sha256": plan.request_sha256,
                        "provider": running.provider.value,
                        "upstream": running.upstream.value,
                        "endpoint": running.endpoint,
                        self._library_version_key(running.provider): running.library_version,
                    },
                    error_code=error.code.value,
                    failed_at=self._safe_now(running).to_iso(),
                    code_version=code_version,
                )
            )
        except Exception:
            error = SourceAdapterError(SourceErrorCode.INTERNAL_PROVIDER)
        failed = transition_batch(
            running,
            IngestionBatchStatus.FAILED,
            at=self._safe_now(running),
            failure=self._failure_info(error),
        )
        self._save_terminal(failed)
        return IngestionOutcome(failed, None, None)

    def _publish_responses(
        self,
        running: IngestionBatch,
        plan: IncrementalFetchPlan,
        responses: tuple[SupplierResponse, ...],
        *,
        code_version: str,
    ) -> tuple[Path, VerifiedDirectory]:
        files = tuple(
            RawManifestFile(
                relative_path=f"responses/chunk-{response.chunk.sequence:05d}.json",
                payload=response.raw_payload,
                row_count=response.upstream_row_count,
            )
            for response in responses
        )
        request = RawBatchPublishRequest(
            source=running.upstream.value.lower(),
            dataset=plan.dataset.value,
            batch_id=path_safe_id(str(plan.batch_id)),
            adapter_version=plan.adapter_version,
            request_params=self._lineage_params(
                running,
                plan,
                responses,
                payload=b"".join(item.payload for item in files),
                upstream_row_count=sum(item.row_count for item in files),
                canonical_row_count=sum(len(response.records) for response in responses),
            ),
            files=files,
            created_at=running.started_at.to_iso(),
            code_version=code_version,
        )
        directory = self._raw_publisher.publish(request)
        return directory, verify_raw_directory(directory, data_root=self._data_root)

    @staticmethod
    def _lineage_params(
        running: IngestionBatch,
        plan: IncrementalFetchPlan,
        responses: tuple[SupplierResponse, ...],
        *,
        payload: bytes,
        upstream_row_count: int,
        canonical_row_count: int,
    ) -> dict[str, str]:
        fetched_at = (
            responses[0].contract.lineage.fetched_at if responses else running.started_at.to_iso()
        )
        request_windows = tuple(response.request_window for response in responses)
        if not request_windows and plan.chunks:
            request_windows = tuple(
                fixed_akshare_request_window(chunk.start.value, chunk.end.value)
                for chunk in plan.chunks
            )
        target_start = (
            min(window.target_start for window in request_windows).isoformat()
            if request_windows
            else "NONE"
        )
        target_end = (
            max(window.target_end for window in request_windows).isoformat()
            if request_windows
            else "NONE"
        )
        upstream_start = (
            min(window.upstream_start for window in request_windows).isoformat()
            if request_windows
            else "NONE"
        )
        upstream_end = (
            max(window.upstream_end for window in request_windows).isoformat()
            if request_windows
            else "NONE"
        )
        return {
            **dict(plan.request_params),
            "provider": running.provider.value,
            "upstream": running.upstream.value,
            "endpoint": running.endpoint,
            AkshareIngestionService._library_version_key(running.provider): running.library_version,
            "fetched_at": fetched_at,
            "request_start": target_start,
            "request_end": target_end,
            "target_start": target_start,
            "target_end": target_end,
            "upstream_start": upstream_start,
            "upstream_end": upstream_end,
            "lookback_days": str(AKSHARE_LOOKBACK_DAYS),
            "row_count": str(upstream_row_count),
            "upstream_row_count": str(upstream_row_count),
            "canonical_row_count": str(canonical_row_count),
            "content_sha256": hashlib.sha256(payload).hexdigest(),
        }

    @staticmethod
    def _validate_response(response: object, chunk: FetchChunk) -> SupplierResponse:
        validate_fetch_chunk(chunk)
        if not isinstance(response, SupplierResponse) or response.chunk != chunk:
            raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
        rebuilt = SupplierResponse.from_raw_payload(response.raw_payload, expected_chunk=chunk)
        if rebuilt != response:
            raise SourceAdapterError(SourceErrorCode.PARSE)
        return rebuilt

    @staticmethod
    def _partial_response(
        chunk: FetchChunk,
        contract: SupplierResponseContract,
        error: SourceAdapterError,
    ) -> SupplierResponse:
        partial_code = (
            SourceErrorCode.BAOSTOCK_PARTIAL_RESPONSE.value
            if contract.lineage.provider is DataProvider.BAOSTOCK
            else SourceErrorCode.PARTIAL_RESPONSE.value
        )
        return SupplierResponse.create_bound(
            chunk=chunk,
            records=(),
            contract=contract,
            complete=False,
            partial_reason=partial_code,
            provider_payload={
                "error_code": error.code.value,
                "request_sha256": chunk.request_sha256,
            },
            request_window=fixed_akshare_request_window(chunk.start.value, chunk.end.value),
        )

    @staticmethod
    def _rebuild_responses(
        verified: VerifiedDirectory,
        plan: IncrementalFetchPlan,
    ) -> tuple[SupplierResponse, ...]:
        chunk_by_name = {
            f"responses/chunk-{chunk.sequence:05d}.json": chunk for chunk in plan.chunks
        }
        rebuilt: list[SupplierResponse] = []
        for item in verified.files:
            chunk = chunk_by_name.get(item.relative_path)
            if chunk is None:
                raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
            response = SupplierResponse.from_raw_payload(
                item.absolute_path.read_bytes(), expected_chunk=chunk
            )
            if item.row_count != response.upstream_row_count:
                raise SourceAdapterError(SourceErrorCode.PARSE)
            rebuilt.append(response)
        return tuple(rebuilt)

    def _now(self) -> UtcTimestamp:
        value = self._clock()
        if not isinstance(value, UtcTimestamp):
            raise TypeError("clock must return UtcTimestamp")
        return value

    def _safe_now(self, running: IngestionBatch) -> UtcTimestamp:
        try:
            return self._now()
        except Exception:
            return running.started_at

    def _insert_running_once(self, batch: IngestionBatch) -> None:
        with self._uow_factory() as uow:
            try:
                uow.data.get_batch(str(batch.batch_id))
            except InvalidIdError:
                uow.data.save_batch(batch)
                uow.commit()
                return
        raise SourceAdapterError(SourceErrorCode.REQUEST_REJECTED)

    def _save_terminal(self, batch: IngestionBatch) -> None:
        with self._uow_factory() as uow:
            uow.data.save_batch(batch)
            uow.commit()

    def _manifest_entries(
        self,
        raw_directory: Path,
        files: tuple[VerifiedFile, ...],
    ) -> tuple[ManifestFileEntry, ...]:
        prefix = raw_directory.relative_to(self._data_root).as_posix()
        return tuple(
            ManifestFileEntry(
                relative_path=f"{prefix}/{item.relative_path}",
                size=item.size,
                sha256=item.sha256,
            )
            for item in files
        )

    @staticmethod
    def _failure_info(error: SourceAdapterError) -> BatchFailureInfo:
        return BatchFailureInfo(
            error_type=error.failure_kind,
            message=error.message,
            retryable=error.retryable,
        )

    @staticmethod
    def _library_version_key(provider: DataProvider) -> str:
        if provider is DataProvider.AKSHARE:
            return "akshare_version"
        if provider is DataProvider.BAOSTOCK:
            return "baostock_version"
        raise SourceAdapterError(SourceErrorCode.REQUEST_REJECTED)


MarketDataIngestionService = AkshareIngestionService


__all__ = [
    "AkshareIngestionService",
    "IngestionOutcome",
    "MarketDataGateway",
    "MarketDataIngestionService",
]
