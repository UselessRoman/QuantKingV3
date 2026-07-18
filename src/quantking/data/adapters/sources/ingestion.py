"""S1-03 Raw-bound ingestion orchestration.

The service persists one RUNNING batch, performs supplier and filesystem work
outside metadata transactions, rebuilds every normalization record from the
verified Raw bytes, and then writes exactly one immutable terminal state. It
never creates or publishes a DatasetVersion.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
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
    DatasetKind,
    DataSource,
    EmptyResultInfo,
    IngestionBatch,
    IngestionBatchStatus,
    LegalEmptyClassification,
    ManifestFileEntry,
    transition_batch,
)

_PREFIX_CODE = re.compile(r"(SZ|SH|BJ)(\d{6})\Z")
_EMPTY_REQUEST_SCHEMA_VERSION = "xtquant-empty-request.v1"


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
    """Execute one deterministic plan with verified Raw as the source of truth."""

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
        validate_incremental_plan(plan)
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
        try:
            if not plan.chunks:
                return self._run_empty_universe(running, plan, empty_result=empty_result)
            return self._fetch_and_land(running, plan, empty_result=empty_result)
        except Exception as error:
            return self._fail_before_payload(
                running,
                plan,
                classify_source_error(error),
            )

    def _fetch_and_land(
        self,
        running: IngestionBatch,
        plan: IncrementalFetchPlan,
        *,
        empty_result: EmptyResultInfo | None,
    ) -> IngestionOutcome:
        responses: list[SupplierResponse] = []
        for chunk in plan.chunks:
            try:
                raw_response = self._gateway.fetch(chunk)
                response = self._validate_gateway_response(raw_response, chunk)
            except Exception as error:
                mapped = classify_source_error(error)
                if not responses:
                    return self._fail_before_payload(running, plan, mapped)
                responses.append(
                    self._partial_failure_evidence(
                        chunk,
                        responses[0].contract,
                        mapped,
                    )
                )
                return self._land_and_normalize(
                    running,
                    plan,
                    tuple(responses),
                    empty_result=empty_result,
                )
            responses.append(response)
        return self._land_and_normalize(
            running,
            plan,
            tuple(responses),
            empty_result=empty_result,
        )

    def _run_empty_universe(
        self,
        running: IngestionBatch,
        plan: IncrementalFetchPlan,
        *,
        empty_result: EmptyResultInfo | None,
    ) -> IngestionOutcome:
        if (
            empty_result is None
            or empty_result.classification is not LegalEmptyClassification.EMPTY_REQUEST_UNIVERSE
        ):
            return self._fail_before_payload(
                running,
                plan,
                SourceAdapterError(SourceErrorCode.UNEXPECTED_EMPTY),
            )
        evidence: dict[str, object] = {
            "schema_version": _EMPTY_REQUEST_SCHEMA_VERSION,
            "dataset": plan.dataset.value,
            "request_sha256": plan.request_sha256,
            "classification": empty_result.classification.value,
            "records": [],
        }
        payload = json.dumps(
            evidence,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        raw_directory: Path | None = None
        manifest_files: tuple[ManifestFileEntry, ...] = ()
        try:
            request = self._raw_request(
                running,
                plan,
                (
                    RawManifestFile(
                        relative_path="responses/empty-request.json",
                        payload=payload,
                        row_count=0,
                    ),
                ),
            )
            raw_directory = self._raw_publisher.publish(request)
            verified = verify_raw_directory(raw_directory, data_root=self._data_root)
            manifest_files = self._manifest_entries(raw_directory, verified.files)
            if len(verified.files) != 1:
                raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
            raw_file = verified.files[0]
            if raw_file.relative_path != "responses/empty-request.json" or raw_file.row_count != 0:
                raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
            if raw_file.absolute_path.read_bytes() != payload:
                raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
            self._validate_manifest_binding(verified, plan)
            candidate = normalize_xtquant_response(
                plan.dataset,
                self._empty_normalization_response(plan.dataset),
                empty_result=empty_result,
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
        except Exception as error:
            mapped = classify_source_error(error)
            if raw_directory is None:
                return self._fail_before_payload(running, plan, mapped)
            return self._failed_with_raw(
                running,
                mapped,
                raw_directory=raw_directory,
                row_count=0,
                security_count=0,
                manifest_files=manifest_files,
            )

    def _land_and_normalize(
        self,
        running: IngestionBatch,
        plan: IncrementalFetchPlan,
        responses: tuple[SupplierResponse, ...],
        *,
        empty_result: EmptyResultInfo | None,
    ) -> IngestionOutcome:
        raw_directory: Path | None = None
        manifest_files: tuple[ManifestFileEntry, ...] = ()
        row_count = 0
        security_count = 0
        try:
            validated = self._validate_response_inventory(plan, responses)
            files = tuple(
                RawManifestFile(
                    relative_path=f"responses/chunk-{chunk.sequence:05d}.json",
                    payload=response.raw_payload,
                    row_count=len(response.records),
                )
                for chunk, response in zip(plan.chunks, validated, strict=True)
            )
            request = self._raw_request(running, plan, files)
            raw_directory = self._raw_publisher.publish(request)
            verified = verify_raw_directory(raw_directory, data_root=self._data_root)
            manifest_files = self._manifest_entries(raw_directory, verified.files)
            row_count = sum(item.row_count for item in verified.files)
            self._validate_manifest_binding(verified, plan)
            rebuilt = self._rebuild_responses(verified, plan)
            if rebuilt != validated:
                raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
            self._validate_record_boundaries(rebuilt, empty_result=empty_result)
            row_count = sum(len(response.records) for response in rebuilt)
            security_count = self._security_count(rebuilt)
            if any(not response.complete for response in rebuilt):
                return self._partial_with_raw(
                    running,
                    raw_directory=raw_directory,
                    row_count=row_count,
                    security_count=security_count,
                    manifest_files=manifest_files,
                )
            candidate = normalize_xtquant_response(
                plan.dataset,
                self._combine_responses(rebuilt),
                empty_result=empty_result,
            )
            succeeded = transition_batch(
                running,
                IngestionBatchStatus.SUCCEEDED,
                at=self._safe_now(running),
                row_count=candidate.num_rows,
                security_count=self._candidate_security_count(candidate),
                manifest_files=manifest_files,
                empty_result=empty_result,
            )
            self._save_terminal(succeeded)
            return IngestionOutcome(succeeded, candidate, raw_directory)
        except Exception as error:
            mapped = classify_source_error(error)
            if raw_directory is None:
                return self._fail_before_payload(running, plan, mapped)
            return self._failed_with_raw(
                running,
                mapped,
                raw_directory=raw_directory,
                row_count=row_count,
                security_count=security_count,
                manifest_files=manifest_files,
            )

    @staticmethod
    def _validate_gateway_response(
        response: object,
        chunk: FetchChunk,
    ) -> SupplierResponse:
        validate_fetch_chunk(chunk)
        if not isinstance(response, SupplierResponse):
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        if response.chunk != chunk:
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        rebuilt = SupplierResponse.from_raw_payload(
            response.raw_payload,
            expected_chunk=chunk,
        )
        if rebuilt != response:
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        return rebuilt

    def _validate_response_inventory(
        self,
        plan: IncrementalFetchPlan,
        responses: tuple[SupplierResponse, ...],
    ) -> tuple[SupplierResponse, ...]:
        validate_incremental_plan(plan)
        if not isinstance(responses, tuple) or len(responses) != len(plan.chunks):
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        rebuilt: list[SupplierResponse] = []
        for chunk, response in zip(plan.chunks, responses, strict=True):
            rebuilt.append(self._validate_gateway_response(response, chunk))
        return tuple(rebuilt)

    def _rebuild_responses(
        self,
        verified: VerifiedDirectory,
        plan: IncrementalFetchPlan,
    ) -> tuple[SupplierResponse, ...]:
        expected_names = tuple(
            f"responses/chunk-{chunk.sequence:05d}.json" for chunk in plan.chunks
        )
        if tuple(item.relative_path for item in verified.files) != expected_names:
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        rebuilt: list[SupplierResponse] = []
        for chunk, item in zip(plan.chunks, verified.files, strict=True):
            response = SupplierResponse.from_raw_payload(
                item.absolute_path.read_bytes(),
                expected_chunk=chunk,
            )
            if item.row_count != len(response.records):
                raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
            rebuilt.append(response)
        return tuple(rebuilt)

    @staticmethod
    def _validate_manifest_binding(
        verified: VerifiedDirectory,
        plan: IncrementalFetchPlan,
    ) -> None:
        manifest = verified.manifest
        expected = {
            "source": "xtquant",
            "dataset": plan.dataset.value,
            "batch_id": path_safe_id(str(plan.batch_id)),
            "adapter_version": plan.adapter_version,
            "request_params": dict(plan.request_params),
        }
        for key, value in expected.items():
            if manifest.get(key) != value:
                raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)

    def _validate_record_boundaries(
        self,
        responses: tuple[SupplierResponse, ...],
        *,
        empty_result: EmptyResultInfo | None,
    ) -> None:
        all_empty = all(not response.records for response in responses)
        has_partial = any(not response.complete for response in responses)
        if all_empty and empty_result is None and not has_partial:
            raise SourceAdapterError(SourceErrorCode.UNEXPECTED_EMPTY)
        for response in responses:
            chunk = response.chunk
            if chunk is None or response.contract.dataset is not chunk.dataset:
                raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
            observed: set[str] = set()
            for record in response.records:
                scope = self._record_scope(record, response.contract)
                if scope not in chunk.scope:
                    raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
                observed.add(scope)
                record_date = self._record_date(record, response.contract)
                if record_date < chunk.start.value or record_date > chunk.end.value:
                    raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
            if response.complete and response.records and observed != set(chunk.scope):
                raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
            if response.complete and not response.records and not all_empty:
                raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)

    @staticmethod
    def _record_scope(
        record: Mapping[str, object],
        contract: SupplierResponseContract,
    ) -> str:
        if contract.dataset is DatasetKind.EXCHANGE_CALENDAR:
            raw_market = record.get("market")
            if not isinstance(raw_market, str) or raw_market not in contract.exchange_map:
                raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
            return contract.exchange_map[raw_market]
        raw_code = record.get("stock_code")
        if not isinstance(raw_code, str):
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        if contract.code_format is CodeFormat.CANONICAL:
            return raw_code
        matched = _PREFIX_CODE.fullmatch(raw_code)
        if matched is None:
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        return f"{matched.group(2)}.{matched.group(1)}"

    @staticmethod
    def _record_date(
        record: Mapping[str, object],
        contract: SupplierResponseContract,
    ) -> date:
        field = {
            DatasetKind.SECURITY_MASTER: "effective_from",
            DatasetKind.EXCHANGE_CALENDAR: "calendar_date",
        }.get(contract.dataset, "date")
        value = record.get(field)
        if not isinstance(value, str):
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        date_format = {
            DateFormat.ISO_YYYY_MM_DD: "%Y-%m-%d",
            DateFormat.COMPACT_YYYYMMDD: "%Y%m%d",
        }[contract.date_format]
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError as error:
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT) from error

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
                    request_params={
                        "adapter_version": plan.adapter_version,
                        "dataset": plan.dataset.value,
                        "request_sha256": plan.request_sha256,
                    },
                    error_code=error.code.value,
                    failed_at=self._safe_now(running).to_iso(),
                    code_version="s1-03-r1",
                )
            )
        except Exception:
            error = SourceAdapterError(SourceErrorCode.INTERNAL_SDK)
        failed = transition_batch(
            running,
            IngestionBatchStatus.FAILED,
            at=self._safe_now(running),
            failure=self._failure_info(error),
        )
        self._save_terminal(failed)
        return IngestionOutcome(failed, None, None)

    def _failed_with_raw(
        self,
        running: IngestionBatch,
        error: SourceAdapterError,
        *,
        raw_directory: Path,
        row_count: int,
        security_count: int,
        manifest_files: tuple[ManifestFileEntry, ...],
    ) -> IngestionOutcome:
        failed = transition_batch(
            running,
            IngestionBatchStatus.FAILED,
            at=self._safe_now(running),
            row_count=row_count,
            security_count=security_count,
            manifest_files=manifest_files,
            failure=self._failure_info(error),
        )
        self._save_terminal(failed)
        return IngestionOutcome(failed, None, raw_directory)

    def _partial_with_raw(
        self,
        running: IngestionBatch,
        *,
        raw_directory: Path,
        row_count: int,
        security_count: int,
        manifest_files: tuple[ManifestFileEntry, ...],
    ) -> IngestionOutcome:
        error = SourceAdapterError(SourceErrorCode.PARTIAL_RESPONSE)
        partial = transition_batch(
            running,
            IngestionBatchStatus.PARTIAL,
            at=self._safe_now(running),
            row_count=row_count,
            security_count=security_count,
            manifest_files=manifest_files,
            failure=self._failure_info(error),
        )
        self._save_terminal(partial)
        return IngestionOutcome(partial, None, raw_directory)

    @staticmethod
    def _failure_info(error: SourceAdapterError) -> BatchFailureInfo:
        return BatchFailureInfo(
            error_type=error.failure_kind,
            message=error.message,
            retryable=error.retryable,
        )

    def _save_terminal(self, batch: IngestionBatch) -> None:
        with self._uow_factory() as uow:
            uow.data.save_batch(batch)
            uow.commit()

    @staticmethod
    def _raw_request(
        running: IngestionBatch,
        plan: IncrementalFetchPlan,
        files: tuple[RawManifestFile, ...],
    ) -> RawBatchPublishRequest:
        return RawBatchPublishRequest(
            source="xtquant",
            dataset=plan.dataset.value,
            batch_id=path_safe_id(str(plan.batch_id)),
            adapter_version=plan.adapter_version,
            request_params=plan.request_params,
            files=files,
            created_at=running.started_at.to_iso(),
            code_version="s1-03-r1",
        )

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
    def _combine_responses(
        responses: tuple[SupplierResponse, ...],
    ) -> SupplierResponse:
        first = responses[0]
        if any(response.contract != first.contract for response in responses[1:]):
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        records = tuple(record for response in responses for record in response.records)
        payload = json.dumps(
            [dict(record) for record in records],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return SupplierResponse(
            records=records,
            raw_payload=payload,
            contract=first.contract,
            complete=True,
            partial_reason=None,
        )

    @staticmethod
    def _partial_failure_evidence(
        chunk: FetchChunk,
        contract: SupplierResponseContract,
        error: SourceAdapterError,
    ) -> SupplierResponse:
        return SupplierResponse.create_bound(
            chunk=chunk,
            records=(),
            contract=contract,
            complete=False,
            partial_reason=SourceErrorCode.PARTIAL_RESPONSE.value,
            provider_payload={
                "error_code": error.code.value,
                "message": error.message,
                "request_sha256": chunk.request_sha256,
            },
        )

    @staticmethod
    def _empty_normalization_response(dataset: DatasetKind) -> SupplierResponse:
        contract = SupplierResponseContract(
            dataset=dataset,
            contract_version="xtquant-empty-request.v1",
            required_fields=required_fields_for(dataset),
            code_format=CodeFormat.CANONICAL,
            date_format=DateFormat.ISO_YYYY_MM_DD,
            exchange_map={"SZ": "SZ", "SH": "SH", "BJ": "BJ"},
            volume_unit=VolumeUnit.SHARE,
            amount_unit=AmountUnit.CNY_YUAN,
            point_in_time=dataset is DatasetKind.SECURITY_MASTER,
            unadjusted=dataset
            in {DatasetKind.DAILY_BAR_UNADJUSTED, DatasetKind.BENCHMARK_INDEX_DAILY},
            official_status=dataset is DatasetKind.TRADING_STATUS,
            adjustment_formula=(
                "p_adj=p_raw*price_multiplier;v_adj=v_raw*volume_multiplier"
                if dataset is DatasetKind.ADJUSTMENT_FACTOR
                else None
            ),
            semantics_verified=True,
        )
        return SupplierResponse(
            records=(),
            raw_payload=b"[]",
            contract=contract,
            complete=True,
            partial_reason=None,
        )


__all__ = ["IngestionOutcome", "MarketDataGateway", "XtQuantIngestionService"]
