"""S1-03 Raw-plus-batch ingestion orchestration.

This service stops at a schema-validated Canonical candidate table. It never
creates, validates, advances, or publishes a dataset version.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pyarrow as pa

from quantking.common.errors import InvalidIdError
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.persistence.sqlite import DataSqliteUnitOfWorkFactory
from quantking.data.adapters.sources.contracts import (
    AmountUnit,
    CodeFormat,
    DateFormat,
    SourceAdapterError,
    SourceErrorCode,
    SupplierResponse,
    SupplierResponseContract,
    VolumeUnit,
    classify_source_error,
)
from quantking.data.adapters.sources.normalization import (
    normalize_xtquant_response,
    required_fields_for,
)
from quantking.data.adapters.warehouse.errors import RawStoreError
from quantking.data.adapters.warehouse.manifest import RawManifestFile, path_safe_id
from quantking.data.adapters.warehouse.raw_store import (
    RawBatchFailureRequest,
    RawBatchPublisher,
    RawBatchPublishRequest,
)
from quantking.data.adapters.warehouse.verifier import (
    VerifiedFile,
    verify_data_root,
    verify_raw_directory,
)
from quantking.data.incremental import FetchChunk, IncrementalFetchPlan
from quantking.data.models import (
    BatchFailureInfo,
    DataSource,
    EmptyResultInfo,
    IngestionBatch,
    IngestionBatchStatus,
    LegalEmptyClassification,
    ManifestFileEntry,
    transition_batch,
)


class MarketDataGateway(Protocol):
    """The narrow provider port consumed by the ingestion service."""

    def fetch(self, chunk: FetchChunk) -> SupplierResponse: ...


@dataclass(frozen=True, slots=True)
class IngestionOutcome:
    """One terminal batch and its optional in-memory candidate."""

    batch: IngestionBatch
    canonical_table: pa.Table | None
    raw_directory: Path | None


class XtQuantIngestionService:
    """Execute one deterministic plan with short metadata transactions."""

    def __init__(
        self,
        *,
        gateway: MarketDataGateway,
        raw_publisher: RawBatchPublisher,
        data_root: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
        clock: Callable[[], UtcTimestamp],
    ) -> None:
        if not hasattr(gateway, "fetch"):
            raise TypeError("gateway must provide fetch")
        if not isinstance(raw_publisher, RawBatchPublisher):
            raise TypeError("raw_publisher must be a RawBatchPublisher")
        if not isinstance(uow_factory, DataSqliteUnitOfWorkFactory):
            raise TypeError("uow_factory must be a DataSqliteUnitOfWorkFactory")
        if not callable(clock):
            raise TypeError("clock must be callable")
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
    ) -> IngestionOutcome:
        if not isinstance(plan, IncrementalFetchPlan):
            raise TypeError("plan must be an IncrementalFetchPlan")
        started_at = self._now()
        running = IngestionBatch.running(
            batch_id=plan.batch_id,
            source=DataSource.XTQUANT,
            dataset=plan.dataset,
            adapter_version=plan.adapter_version,
            request_params=plan.request_params,
            started_at=started_at,
        )
        self._insert_running_once(running)

        if not plan.chunks:
            if (
                empty_result is None
                or empty_result.classification
                is not LegalEmptyClassification.EMPTY_REQUEST_UNIVERSE
            ):
                error = SourceAdapterError(SourceErrorCode.UNEXPECTED_EMPTY)
                return self._fail_before_payload(running, plan, error)
            response = self._empty_response(plan)
            return self._land_and_normalize(
                running,
                plan,
                (response,),
                empty_result=empty_result,
            )

        responses: list[SupplierResponse] = []
        for chunk in plan.chunks:
            try:
                response = self._gateway.fetch(chunk)
            except Exception as error:
                mapped = classify_source_error(error)
                if responses:
                    responses.append(self._partial_failure_evidence(plan, responses[0], mapped))
                    return self._land_and_normalize(
                        running,
                        plan,
                        tuple(responses),
                        empty_result=empty_result,
                    )
                return self._fail_before_payload(running, plan, mapped)
            responses.append(response)
        return self._land_and_normalize(
            running,
            plan,
            tuple(responses),
            empty_result=empty_result,
        )

    def _now(self) -> UtcTimestamp:
        value = self._clock()
        if not isinstance(value, UtcTimestamp):
            raise TypeError("clock must return UtcTimestamp")
        return value

    def _insert_running_once(self, batch: IngestionBatch) -> None:
        with self._uow_factory() as uow:
            try:
                uow.data.get_batch(str(batch.batch_id))
            except InvalidIdError:
                uow.data.save_batch(batch)
                uow.commit()
                return
        raise SourceAdapterError(SourceErrorCode.REQUEST_REJECTED)

    def _fail_before_payload(
        self,
        running: IngestionBatch,
        plan: IncrementalFetchPlan,
        error: SourceAdapterError,
    ) -> IngestionOutcome:
        try:
            self._raw_publisher.record_failure(
                RawBatchFailureRequest(
                    source="xtquant",
                    dataset=plan.dataset.value,
                    batch_id=path_safe_id(str(plan.batch_id)),
                    adapter_version=plan.adapter_version,
                    request_params=plan.request_params,
                    error_code=error.code.value,
                    failed_at=self._now().to_iso(),
                    code_version="s1-03",
                )
            )
        except RawStoreError:
            error = SourceAdapterError(SourceErrorCode.INTERNAL_SDK)
        failed = transition_batch(
            running,
            IngestionBatchStatus.FAILED,
            at=self._now(),
            failure=BatchFailureInfo(
                error_type=error.failure_kind,
                message=error.message,
                retryable=error.retryable,
            ),
        )
        self._save_terminal(failed)
        return IngestionOutcome(failed, None, None)

    def _land_and_normalize(
        self,
        running: IngestionBatch,
        plan: IncrementalFetchPlan,
        responses: tuple[SupplierResponse, ...],
        *,
        empty_result: EmptyResultInfo | None,
    ) -> IngestionOutcome:
        files = tuple(
            RawManifestFile(
                relative_path=f"responses/chunk-{index:05d}.json",
                payload=response.raw_payload,
                row_count=len(response.records),
            )
            for index, response in enumerate(responses)
        )
        request = RawBatchPublishRequest(
            source="xtquant",
            dataset=plan.dataset.value,
            batch_id=path_safe_id(str(plan.batch_id)),
            adapter_version=plan.adapter_version,
            request_params=plan.request_params,
            files=files,
            created_at=running.started_at.to_iso(),
            code_version="s1-03",
        )
        try:
            raw_directory = self._raw_publisher.publish(request)
            verified = verify_raw_directory(raw_directory, data_root=self._data_root)
        except Exception:
            error = SourceAdapterError(SourceErrorCode.INTERNAL_SDK)
            failed = transition_batch(
                running,
                IngestionBatchStatus.FAILED,
                at=self._now(),
                failure=BatchFailureInfo(
                    error_type=error.failure_kind,
                    message=error.message,
                    retryable=error.retryable,
                ),
            )
            self._save_terminal(failed)
            return IngestionOutcome(failed, None, None)

        manifest_files = self._manifest_entries(raw_directory, verified.files)
        row_count = sum(len(response.records) for response in responses)
        security_count = self._security_count(responses)
        if any(not response.complete for response in responses):
            error = SourceAdapterError(SourceErrorCode.PARTIAL_RESPONSE)
            partial = transition_batch(
                running,
                IngestionBatchStatus.PARTIAL,
                at=self._now(),
                row_count=row_count,
                security_count=security_count,
                manifest_files=manifest_files,
                failure=BatchFailureInfo(
                    error_type=error.failure_kind,
                    message=error.message,
                    retryable=error.retryable,
                ),
            )
            self._save_terminal(partial)
            return IngestionOutcome(partial, None, raw_directory)

        try:
            combined = self._combine_responses(responses)
            candidate = normalize_xtquant_response(
                plan.dataset,
                combined,
                empty_result=empty_result,
            )
        except SourceAdapterError as error:
            failed = transition_batch(
                running,
                IngestionBatchStatus.FAILED,
                at=self._now(),
                row_count=row_count,
                security_count=security_count,
                manifest_files=manifest_files,
                failure=BatchFailureInfo(
                    error_type=error.failure_kind,
                    message=error.message,
                    retryable=error.retryable,
                ),
            )
            self._save_terminal(failed)
            return IngestionOutcome(failed, None, raw_directory)

        succeeded = transition_batch(
            running,
            IngestionBatchStatus.SUCCEEDED,
            at=self._now(),
            row_count=candidate.num_rows,
            security_count=self._candidate_security_count(candidate),
            manifest_files=manifest_files,
            empty_result=empty_result,
        )
        self._save_terminal(succeeded)
        return IngestionOutcome(succeeded, candidate, raw_directory)

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
        entries: list[ManifestFileEntry] = []
        for item in files:
            relative_path = item.relative_path
            entries.append(
                ManifestFileEntry(
                    relative_path=f"{prefix}/{relative_path}",
                    size=item.size,
                    sha256=item.sha256,
                )
            )
        return tuple(entries)

    @staticmethod
    def _security_count(responses: tuple[SupplierResponse, ...]) -> int:
        identifiers: set[str] = set()
        for response in responses:
            for record in response.records:
                value = record.get("stock_code", record.get("market"))
                if isinstance(value, str):
                    identifiers.add(value)
        return len(identifiers)

    @staticmethod
    def _candidate_security_count(table: pa.Table) -> int:
        for field in ("code", "exchange"):
            if field in table.column_names:
                return len(set(table.column(field).to_pylist()))
        return 0

    @staticmethod
    def _combine_responses(responses: tuple[SupplierResponse, ...]) -> SupplierResponse:
        first = responses[0]
        if any(response.contract != first.contract for response in responses[1:]):
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        records = tuple(record for response in responses for record in response.records)
        return SupplierResponse(
            records=records,
            raw_payload=b"[]",
            contract=first.contract,
            complete=True,
            partial_reason=None,
        )

    @staticmethod
    def _partial_failure_evidence(
        plan: IncrementalFetchPlan,
        previous: SupplierResponse,
        error: SourceAdapterError,
    ) -> SupplierResponse:
        payload = json.dumps(
            {
                "error_code": error.code.value,
                "message": error.message,
                "request_sha256": plan.request_sha256,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return SupplierResponse(
            records=(),
            raw_payload=payload,
            contract=previous.contract,
            complete=False,
            partial_reason=error.code.value,
        )

    @staticmethod
    def _empty_response(plan: IncrementalFetchPlan) -> SupplierResponse:
        contract = SupplierResponseContract(
            dataset=plan.dataset,
            contract_version="xtquant-empty-request.v1",
            required_fields=required_fields_for(plan.dataset),
            code_format=CodeFormat.CANONICAL,
            date_format=DateFormat.ISO_YYYY_MM_DD,
            exchange_map={"SZ": "SZ", "SH": "SH", "BJ": "BJ"},
            volume_unit=VolumeUnit.SHARE,
            amount_unit=AmountUnit.CNY_YUAN,
            point_in_time=plan.dataset.value == "security_master",
            unadjusted=plan.dataset.value in {"daily_bar_unadjusted", "benchmark_index_daily"},
            official_status=plan.dataset.value == "trading_status",
            adjustment_formula=(
                "p_adj=p_raw*price_multiplier;v_adj=v_raw*volume_multiplier"
                if plan.dataset.value == "adjustment_factor"
                else None
            ),
            semantics_verified=True,
        )
        payload = json.dumps(
            {
                "classification": LegalEmptyClassification.EMPTY_REQUEST_UNIVERSE.value,
                "request_sha256": plan.request_sha256,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return SupplierResponse(
            records=(),
            raw_payload=payload,
            contract=contract,
            complete=True,
            partial_reason=None,
        )


__all__ = ["IngestionOutcome", "MarketDataGateway", "XtQuantIngestionService"]
