"""Offline XTQuant ingestion orchestration with Raw and SQLite evidence."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.errors import InvalidPathError
from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.adapters.sources.contracts import (
    AmountUnit,
    CodeFormat,
    DateFormat,
    SourceAdapterError,
    SourceErrorCode,
    SupplierResponse,
    SupplierResponseContract,
    VolumeUnit,
)
from quantking.data.adapters.sources.ingestion import XtQuantIngestionService
from quantking.data.adapters.sources.normalization import normalize_xtquant_response
from quantking.data.adapters.sources.xtquant import XtQuantMarketDataGateway
from quantking.data.adapters.warehouse.errors import RawStoreError
from quantking.data.adapters.warehouse.manifest import path_safe_id
from quantking.data.adapters.warehouse.raw_store import (
    RawBatchFailureRequest,
    RawBatchPublisher,
    RawBatchPublishRequest,
)
from quantking.data.incremental import IncrementalPlanRequest, build_incremental_plan
from quantking.data.models import (
    BatchFailureKind,
    DatasetKind,
    EmptyResultInfo,
    IngestionBatchStatus,
    LegalEmptyClassification,
)
from quantking.jobs.adapters.persistence.sqlite import initialize_database

_START = UtcTimestamp(datetime(2026, 7, 18, 1, 0, tzinfo=UTC))
_END = UtcTimestamp(datetime(2026, 7, 18, 1, 1, tzinfo=UTC))


class FakeGateway:
    def __init__(
        self,
        responses: tuple[SupplierResponse | BaseException, ...] = (),
        *,
        error: BaseException | None = None,
    ) -> None:
        self.responses = list(responses)
        self.error = error
        self.calls = 0

    def fetch(self, _chunk: object) -> SupplierResponse:
        self.calls += 1
        if self.error is not None:
            raise self.error
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, SupplierResponse) and result.chunk is None:
            return SupplierResponse.create_bound(
                chunk=_chunk,  # type: ignore[arg-type]
                records=result.records,
                contract=result.contract,
                complete=result.complete,
                partial_reason=result.partial_reason,
                provider_payload={
                    "synthetic_records": [dict(record) for record in result.records],
                },
            )
        return result


class FailingRawPublisher(RawBatchPublisher):
    def _write_file(self, target: Path, payload: bytes) -> None:
        raise OSError(r"secret path E:\private\raw")


class SerializationFailingRawPublisher(RawBatchPublisher):
    def _write_input_listing(
        self,
        directory: Path,
        request: RawBatchPublishRequest,
    ) -> None:
        raise TypeError(r"SECRET E:\private serialization")


class RewritingRawPublisher(RawBatchPublisher):
    def publish(self, request: RawBatchPublishRequest) -> Path:
        first = request.files[0]
        envelope = json.loads(first.payload)
        envelope["response"]["records"][0]["close"] = 10.1
        payload = json.dumps(
            envelope,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        changed_file = replace(first, payload=payload)
        return super().publish(replace(request, files=(changed_file, *request.files[1:])))


class WrongRowCountRawPublisher(RawBatchPublisher):
    def publish(self, request: RawBatchPublishRequest) -> Path:
        first = replace(request.files[0], row_count=request.files[0].row_count + 1)
        return super().publish(replace(request, files=(first, *request.files[1:])))


@pytest.fixture()
def persistence(
    tmp_path: Path,
) -> tuple[Path, DataSqliteUnitOfWorkFactory]:
    database = tmp_path / "metadata.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    yield database, DataSqliteUnitOfWorkFactory(engine)
    engine.dispose()


def _clock() -> object:
    values = iter((_START, _END, _END, _END))
    return lambda: next(values, _END)


def _plan(
    *,
    batch_id: object | None = None,
    scope: tuple[str, ...] = ("000001.SZ",),
) -> object:
    return build_incremental_plan(
        IncrementalPlanRequest(
            batch_id=batch_id or new_entity_id("data_batch"),  # type: ignore[arg-type]
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            scope=scope,
            request_start=TradingDate(date(2026, 7, 17)),
            request_end=TradingDate(date(2026, 7, 17)),
            start_inclusive=True,
            end_inclusive=True,
            known_coverage=(),
            lookback_days=0,
            max_days_per_chunk=1,
            max_items_per_chunk=1,
            adapter_version="xtquant-s1-03.v1",
        )
    )


def _calendar_plan(*, batch_id: object | None = None) -> object:
    return build_incremental_plan(
        IncrementalPlanRequest(
            batch_id=batch_id or new_entity_id("data_batch"),  # type: ignore[arg-type]
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            scope=("SH",),
            request_start=TradingDate(date(2026, 7, 17)),
            request_end=TradingDate(date(2026, 7, 18)),
            start_inclusive=True,
            end_inclusive=True,
            known_coverage=(),
            lookback_days=0,
            max_days_per_chunk=2,
            max_items_per_chunk=1,
            adapter_version="xtquant-s1-03.v1",
        )
    )


def _contract() -> SupplierResponseContract:
    return SupplierResponseContract(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        contract_version="xtquant-synthetic.v1",
        required_fields=(
            "stock_code",
            "date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "volume",
            "amount",
        ),
        code_format=CodeFormat.CANONICAL,
        date_format=DateFormat.ISO_YYYY_MM_DD,
        exchange_map={"SZ": "SZ", "SH": "SH", "BJ": "BJ"},
        volume_unit=VolumeUnit.SHARE,
        amount_unit=AmountUnit.CNY_YUAN,
        point_in_time=False,
        unadjusted=True,
        official_status=False,
        adjustment_formula=None,
        semantics_verified=True,
    )


def _response(
    *,
    records: tuple[dict[str, object], ...] | None = None,
    complete: bool = True,
    partial_reason: str | None = None,
) -> SupplierResponse:
    default = (
        {
            "stock_code": "000001.SZ",
            "date": "2026-07-17",
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "pre_close": 9.9,
            "volume": 10_000,
            "amount": 102_000.0,
        },
    )
    payload = json.dumps(
        list(default if records is None else records),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return SupplierResponse(
        records=default if records is None else records,
        raw_payload=payload,
        contract=_contract(),
        complete=complete,
        partial_reason=partial_reason,
    )


def _service(
    tmp_path: Path,
    factory: DataSqliteUnitOfWorkFactory,
    gateway: FakeGateway,
    *,
    publisher: RawBatchPublisher | None = None,
) -> XtQuantIngestionService:
    return XtQuantIngestionService(
        gateway=gateway,
        raw_publisher=publisher or RawBatchPublisher(data_root=tmp_path),
        data_root=tmp_path,
        uow_factory=factory,
        clock=_clock(),  # type: ignore[arg-type]
    )


def test_success_persists_raw_manifest_complete_request_and_candidate(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    gateway = FakeGateway((_response(),))
    plan = _plan()
    outcome = _service(tmp_path, factory, gateway).run(plan)  # type: ignore[arg-type]

    assert outcome.batch.status is IngestionBatchStatus.SUCCEEDED
    assert outcome.canonical_table is not None
    assert outcome.canonical_table.num_rows == 1
    assert outcome.raw_directory is not None
    assert (outcome.raw_directory / "manifest.json").is_file()
    assert (outcome.raw_directory / "COMPLETE").is_file()
    listing = json.loads((outcome.raw_directory / "input_listing.json").read_text(encoding="utf-8"))
    assert listing["request_params"] == dict(plan.request_params)  # type: ignore[union-attr]
    manifest = json.loads((outcome.raw_directory / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["manifest_sha256"]) == 64
    assert len(manifest["files"][0]["sha256"]) == 64
    raw_envelope = json.loads(
        (outcome.raw_directory / manifest["files"][0]["relative_path"]).read_text(encoding="utf-8")
    )
    assert manifest["files"][0]["row_count"] == len(raw_envelope["response"]["records"])
    with factory() as uow:
        persisted = uow.data.get_batch(str(plan.batch_id))  # type: ignore[union-attr]
    assert persisted == outcome.batch
    assert gateway.calls == 1


def test_candidate_is_rebuildable_from_verified_raw_bytes(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    plan = _plan()
    outcome = _service(tmp_path, factory, FakeGateway((_response(),))).run(
        plan  # type: ignore[arg-type]
    )
    assert outcome.canonical_table is not None
    assert outcome.raw_directory is not None
    payload = (outcome.raw_directory / "responses" / "chunk-00000.json").read_bytes()
    rebuilt = SupplierResponse.from_raw_payload(
        payload,
        expected_chunk=plan.chunks[0],  # type: ignore[union-attr]
    )
    replayed = normalize_xtquant_response(
        DatasetKind.DAILY_BAR_UNADJUSTED,
        rebuilt,
    )
    assert replayed.equals(outcome.canonical_table)


def test_valid_but_rewritten_raw_is_rejected_against_gateway_records(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    outcome = _service(
        tmp_path,
        factory,
        FakeGateway((_response(),)),
        publisher=RewritingRawPublisher(data_root=tmp_path),
    ).run(_plan())  # type: ignore[arg-type]
    assert outcome.batch.status is IngestionBatchStatus.FAILED
    assert outcome.batch.failure is not None
    assert outcome.batch.failure.error_type is BatchFailureKind.PARSE
    assert outcome.canonical_table is None
    assert outcome.raw_directory is not None


def test_manifest_row_count_must_equal_records_inside_raw(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    outcome = _service(
        tmp_path,
        factory,
        FakeGateway((_response(),)),
        publisher=WrongRowCountRawPublisher(data_root=tmp_path),
    ).run(_plan())  # type: ignore[arg-type]
    assert outcome.batch.status is IngestionBatchStatus.FAILED
    assert outcome.batch.failure is not None
    assert outcome.batch.failure.error_type is BatchFailureKind.PARSE
    assert outcome.canonical_table is None


def test_raw_object_and_nonempty_records_mismatch_is_rejected() -> None:
    records = _response().records
    with pytest.raises(SourceAdapterError) as exc_info:
        SupplierResponse(
            records=records,
            raw_payload=b"{}",
            contract=_contract(),
            complete=True,
            partial_reason=None,
        )
    assert exc_info.value.code is SourceErrorCode.RESPONSE_CONTRACT


@pytest.mark.parametrize(
    "mutation",
    ("sequence", "scope", "date_range", "request_hash"),
)
def test_response_binding_must_match_expected_chunk(
    mutation: str,
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    plan = _plan()
    expected = plan.chunks[0]  # type: ignore[union-attr]
    if mutation == "sequence":
        wrong = replace(expected, sequence=expected.sequence + 1)
    elif mutation == "scope":
        wrong = replace(expected, scope=("600000.SH",))
    elif mutation == "date_range":
        wrong = replace(
            expected,
            start=TradingDate(date(2026, 7, 18)),
            end=TradingDate(date(2026, 7, 18)),
        )
    else:
        wrong = replace(expected, request_sha256="0" * 64)
    base = _response()
    response = SupplierResponse.create_bound(
        chunk=wrong,
        records=base.records,
        contract=base.contract,
        complete=True,
        partial_reason=None,
        provider_payload={"synthetic": True},
    )
    outcome = _service(tmp_path, factory, FakeGateway((response,))).run(
        plan  # type: ignore[arg-type]
    )
    assert outcome.batch.status is IngestionBatchStatus.FAILED
    assert outcome.batch.failure is not None
    assert outcome.batch.failure.error_type is BatchFailureKind.PARSE


def test_response_dataset_must_match_expected_chunk(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence

    class EmptyCalendarModule:
        @staticmethod
        def get_trading_dates(*_args: object, **_kwargs: object) -> list[object]:
            return []

    calendar_plan = _calendar_plan()
    response = XtQuantMarketDataGateway(module_loader=lambda: EmptyCalendarModule()).fetch(
        calendar_plan.chunks[0]
    )  # type: ignore[union-attr]
    outcome = _service(tmp_path, factory, FakeGateway((response,))).run(
        _plan()  # type: ignore[arg-type]
    )
    assert outcome.batch.status is IngestionBatchStatus.FAILED
    assert outcome.batch.failure is not None
    assert outcome.batch.failure.error_type is BatchFailureKind.PARSE


@pytest.mark.parametrize(
    "mutation",
    (
        {"stock_code": "600000.SH"},
        {"date": "2026-07-18"},
    ),
)
def test_candidate_outside_plan_scope_or_date_fails(
    mutation: dict[str, object],
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    record = dict(_response().records[0])
    record.update(mutation)
    outcome = _service(
        tmp_path,
        factory,
        FakeGateway((_response(records=(record,)),)),
    ).run(_plan())  # type: ignore[arg-type]
    assert outcome.batch.status is IngestionBatchStatus.FAILED
    assert outcome.batch.failure is not None
    assert outcome.batch.failure.error_type is BatchFailureKind.PARSE


def test_gateway_wrong_return_type_becomes_failed_terminal(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    plan = _plan()
    outcome = _service(
        tmp_path,
        factory,
        FakeGateway((object(),)),  # type: ignore[arg-type]
    ).run(plan)  # type: ignore[arg-type]
    assert outcome.batch.status is IngestionBatchStatus.FAILED
    with factory() as uow:
        persisted = uow.data.get_batch(str(plan.batch_id))  # type: ignore[union-attr]
    assert persisted.status is IngestionBatchStatus.FAILED


def test_gateway_failure_before_payload_preserves_managed_failure_evidence(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    raw = PermissionError(r"account=123456 token=SECRET E:\private\qmt")
    gateway = FakeGateway(error=raw)
    plan = _plan()
    outcome = _service(tmp_path, factory, gateway).run(plan)  # type: ignore[arg-type]

    assert outcome.batch.status is IngestionBatchStatus.FAILED
    assert outcome.batch.failure is not None
    assert outcome.batch.failure.error_type is BatchFailureKind.PERMISSION
    assert "SECRET" not in outcome.batch.failure.message
    failure_dir = (
        tmp_path
        / "raw"
        / "xtquant"
        / DatasetKind.DAILY_BAR_UNADJUSTED.value
        / f"{path_safe_id(str(plan.batch_id))}.incomplete"  # type: ignore[union-attr]
    )
    record = json.loads((failure_dir / "failure.json").read_text(encoding="utf-8"))
    rendered = json.dumps(record)
    assert record["error_code"] == SourceErrorCode.PERMISSION.value
    assert "SECRET" not in rendered
    assert "E:\\private" not in rendered
    assert (failure_dir / "input_listing.json").is_file()
    listing = json.loads((failure_dir / "input_listing.json").read_text(encoding="utf-8"))
    assert set(listing["request_params"]) == {
        "adapter_version",
        "dataset",
        "request_sha256",
    }
    assert "SECRET" not in json.dumps(listing)


def test_failed_same_parameters_can_retry_with_new_batch_id(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    first_plan = _plan()
    failed = _service(
        tmp_path,
        factory,
        FakeGateway(error=TimeoutError("supplier secret")),
    ).run(first_plan)  # type: ignore[arg-type]
    retry_plan = _plan()
    succeeded = _service(tmp_path, factory, FakeGateway((_response(),))).run(
        retry_plan  # type: ignore[arg-type]
    )

    assert failed.batch.status is IngestionBatchStatus.FAILED
    assert failed.batch.failure is not None
    assert failed.batch.failure.error_type is BatchFailureKind.NETWORK
    assert failed.batch.failure.retryable
    assert succeeded.batch.status is IngestionBatchStatus.SUCCEEDED
    assert first_plan.batch_id != retry_plan.batch_id  # type: ignore[union-attr]
    assert first_plan.request_params == retry_plan.request_params  # type: ignore[union-attr]


def test_terminal_batch_id_never_calls_gateway_again(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    plan = _plan()
    _service(tmp_path, factory, FakeGateway((_response(),))).run(plan)  # type: ignore[arg-type]
    second_gateway = FakeGateway((_response(),))
    with pytest.raises(SourceAdapterError) as exc_info:
        _service(tmp_path, factory, second_gateway).run(plan)  # type: ignore[arg-type]
    assert exc_info.value.code is SourceErrorCode.REQUEST_REJECTED
    assert second_gateway.calls == 0


def test_partial_response_keeps_raw_but_never_returns_candidate(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    response = _response(
        complete=False,
        partial_reason=SourceErrorCode.PARTIAL_RESPONSE.value,
    )
    outcome = _service(tmp_path, factory, FakeGateway((response,))).run(
        _plan()  # type: ignore[arg-type]
    )
    assert outcome.batch.status is IngestionBatchStatus.PARTIAL
    assert outcome.batch.failure is not None
    assert outcome.batch.failure.error_type is BatchFailureKind.PARTIAL_SOURCE
    assert outcome.canonical_table is None
    assert outcome.raw_directory is not None
    assert (outcome.raw_directory / "COMPLETE").is_file()


def test_later_chunk_failure_keeps_received_raw_and_becomes_partial(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    raw_error = TimeoutError(r"token=SECRET E:\private\qmt")
    gateway = FakeGateway((_response(), raw_error))
    plan = _plan(scope=("000001.SZ", "600000.SH"))
    outcome = _service(tmp_path, factory, gateway).run(plan)  # type: ignore[arg-type]

    assert gateway.calls == 2
    assert outcome.batch.status is IngestionBatchStatus.PARTIAL
    assert outcome.batch.failure is not None
    assert outcome.batch.failure.error_type is BatchFailureKind.PARTIAL_SOURCE
    assert outcome.canonical_table is None
    assert outcome.raw_directory is not None
    received = json.loads(
        (outcome.raw_directory / "responses" / "chunk-00000.json").read_text(encoding="utf-8")
    )
    evidence = json.loads(
        (outcome.raw_directory / "responses" / "chunk-00001.json").read_text(encoding="utf-8")
    )
    assert received["response"]["records"][0]["stock_code"] == "000001.SZ"
    assert evidence["provider_payload"]["error_code"] == SourceErrorCode.NETWORK.value
    assert "SECRET" not in json.dumps(evidence)
    assert "E:\\private" not in json.dumps(evidence)


def test_normalization_failure_keeps_raw_and_cannot_succeed(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    bad = dict(_response().records[0])
    bad["high"] = 10.0
    bad["close"] = 10.2
    outcome = _service(tmp_path, factory, FakeGateway((_response(records=(bad,)),))).run(
        _plan()  # type: ignore[arg-type]
    )
    assert outcome.batch.status is IngestionBatchStatus.FAILED
    assert outcome.batch.failure is not None
    assert outcome.batch.failure.error_type is BatchFailureKind.PARSE
    assert outcome.canonical_table is None
    assert outcome.raw_directory is not None
    assert (outcome.raw_directory / "COMPLETE").is_file()


def test_raw_failure_leaves_incomplete_and_no_success_batch(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    plan = _plan()
    outcome = _service(
        tmp_path,
        factory,
        FakeGateway((_response(),)),
        publisher=FailingRawPublisher(data_root=tmp_path),
    ).run(plan)  # type: ignore[arg-type]
    assert outcome.batch.status is IngestionBatchStatus.FAILED
    assert outcome.batch.failure is not None
    assert outcome.batch.failure.error_type is BatchFailureKind.INTERNAL
    assert outcome.raw_directory is None
    incomplete = (
        tmp_path
        / "raw"
        / "xtquant"
        / DatasetKind.DAILY_BAR_UNADJUSTED.value
        / f"{path_safe_id(str(plan.batch_id))}.incomplete"  # type: ignore[union-attr]
    )
    assert (incomplete / "failure.json").is_file()
    assert not incomplete.with_name(incomplete.name.removesuffix(".incomplete")).exists()


def test_raw_serialization_failure_becomes_failed_terminal(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    plan = _plan()
    outcome = _service(
        tmp_path,
        factory,
        FakeGateway((_response(),)),
        publisher=SerializationFailingRawPublisher(data_root=tmp_path),
    ).run(plan)  # type: ignore[arg-type]
    assert outcome.batch.status is IngestionBatchStatus.FAILED
    with factory() as uow:
        persisted = uow.data.get_batch(str(plan.batch_id))  # type: ignore[union-attr]
    assert persisted.status is IngestionBatchStatus.FAILED


def test_request_construction_failure_becomes_failed_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence

    def fail_request(*_args: object, **_kwargs: object) -> RawBatchPublishRequest:
        raise ValueError(r"SECRET E:\private request")

    monkeypatch.setattr(XtQuantIngestionService, "_raw_request", staticmethod(fail_request))
    plan = _plan()
    outcome = _service(tmp_path, factory, FakeGateway((_response(),))).run(
        plan  # type: ignore[arg-type]
    )
    assert outcome.batch.status is IngestionBatchStatus.FAILED
    with factory() as uow:
        persisted = uow.data.get_batch(str(plan.batch_id))  # type: ignore[union-attr]
    assert persisted.status is IngestionBatchStatus.FAILED


def test_unexpected_standardizer_exception_becomes_failed_with_raw(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence

    def fail_standardization(*_args: object, **_kwargs: object) -> pa.Table:
        raise RuntimeError(r"SECRET E:\private standardizer")

    monkeypatch.setattr(
        "quantking.data.adapters.sources.ingestion.normalize_xtquant_response",
        fail_standardization,
    )
    plan = _plan()
    outcome = _service(tmp_path, factory, FakeGateway((_response(),))).run(
        plan  # type: ignore[arg-type]
    )
    assert outcome.batch.status is IngestionBatchStatus.FAILED
    assert outcome.raw_directory is not None
    assert outcome.batch.failure is not None
    assert outcome.batch.failure.error_type is BatchFailureKind.INTERNAL
    assert "SECRET" not in outcome.batch.failure.message


def test_legal_empty_universe_succeeds_without_supplier_call(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    gateway = FakeGateway()
    empty = EmptyResultInfo(
        LegalEmptyClassification.EMPTY_REQUEST_UNIVERSE,
        "validated request universe contains no instruments",
    )
    outcome = _service(tmp_path, factory, gateway).run(
        _plan(scope=()),  # type: ignore[arg-type]
        empty_result=empty,
    )
    assert outcome.batch.status is IngestionBatchStatus.SUCCEEDED
    assert outcome.batch.empty_result == empty
    assert outcome.canonical_table is not None
    assert outcome.canonical_table.num_rows == 0
    assert gateway.calls == 0
    assert outcome.raw_directory is not None


def test_unexpected_empty_response_is_failed_not_success(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    outcome = _service(
        tmp_path,
        factory,
        FakeGateway((_response(records=()),)),
    ).run(_plan())  # type: ignore[arg-type]
    assert outcome.batch.status is IngestionBatchStatus.FAILED
    assert outcome.batch.failure is not None
    assert outcome.batch.failure.error_type is BatchFailureKind.UNEXPECTED_EMPTY


def test_empty_calendar_requires_explicit_legal_empty_evidence(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence

    class EmptyCalendarModule:
        @staticmethod
        def get_trading_dates(*_args: object, **_kwargs: object) -> list[object]:
            return []

    gateway = XtQuantMarketDataGateway(module_loader=lambda: EmptyCalendarModule())
    failed_plan = _calendar_plan()
    failed = XtQuantIngestionService(
        gateway=gateway,
        raw_publisher=RawBatchPublisher(data_root=tmp_path),
        data_root=tmp_path,
        uow_factory=factory,
        clock=_clock(),  # type: ignore[arg-type]
    ).run(failed_plan)  # type: ignore[arg-type]
    assert failed.batch.status is IngestionBatchStatus.FAILED
    assert failed.batch.failure is not None
    assert failed.batch.failure.error_type is BatchFailureKind.UNEXPECTED_EMPTY

    legal_plan = _calendar_plan()
    legal = EmptyResultInfo(
        LegalEmptyClassification.NO_TRADING_SESSION,
        "exchange calendar evidence confirms no session in the explicit range",
    )
    succeeded = XtQuantIngestionService(
        gateway=gateway,
        raw_publisher=RawBatchPublisher(data_root=tmp_path),
        data_root=tmp_path,
        uow_factory=factory,
        clock=_clock(),  # type: ignore[arg-type]
    ).run(legal_plan, empty_result=legal)  # type: ignore[arg-type]
    assert succeeded.batch.status is IngestionBatchStatus.SUCCEEDED
    assert succeeded.canonical_table is not None
    assert succeeded.canonical_table.num_rows == 0


def test_orchestrator_revalidates_tampered_plan_before_gateway_call(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    plan = _plan()
    object.__setattr__(plan, "request_sha256", "0" * 64)
    gateway = FakeGateway((_response(),))
    with pytest.raises(ValueError):
        _service(tmp_path, factory, gateway).run(plan)  # type: ignore[arg-type]
    assert gateway.calls == 0


def test_ingestion_never_creates_or_publishes_dataset_version(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    database, factory = persistence
    _service(tmp_path, factory, FakeGateway((_response(),))).run(
        _plan()  # type: ignore[arg-type]
    )
    with sqlite3.connect(database) as connection:
        count = connection.execute("SELECT COUNT(*) FROM dataset_versions").fetchone()
    assert count == (0,)


def test_illegal_scope_is_rejected_before_any_managed_write(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    with pytest.raises(ValueError):
        _plan(scope=("../escape",))
    assert not (tmp_path / "raw").exists()
    assert factory is not None


def test_managed_failure_api_is_immutable_and_rejects_unsafe_id(tmp_path: Path) -> None:
    publisher = RawBatchPublisher(data_root=tmp_path)
    request = RawBatchFailureRequest(
        source="xtquant",
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
        batch_id="data_batch--11111111-1111-4111-8111-111111111111",
        adapter_version="xtquant-s1-03.v1",
        request_params={"request_sha256": "a" * 64},
        error_code=SourceErrorCode.NETWORK.value,
        failed_at=_END.to_iso(),
        code_version="s1-03",
    )
    directory = publisher.record_failure(request)
    before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    with pytest.raises(RawStoreError, match="already has immutable evidence"):
        publisher.record_failure(request)
    after = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    assert after == before
    with pytest.raises(InvalidPathError, match="batch_id"):
        RawBatchFailureRequest(
            source="xtquant",
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
            batch_id="../escape",
            adapter_version="xtquant-s1-03.v1",
            request_params={},
            error_code=SourceErrorCode.NETWORK.value,
        )
    assert not (tmp_path / "escape").exists()
    with pytest.raises(InvalidPathError, match="failed_at"):
        RawBatchFailureRequest(
            source="xtquant",
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
            batch_id="data_batch--22222222-2222-4222-8222-222222222222",
            adapter_version="xtquant-s1-03.v1",
            request_params={},
            error_code=SourceErrorCode.NETWORK.value,
            failed_at="not-a-utc-timestamp",
        )
    with pytest.raises(InvalidPathError, match="request_params"):
        RawBatchFailureRequest(
            source="xtquant",
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
            batch_id="data_batch--33333333-3333-4333-8333-333333333333",
            adapter_version="xtquant-s1-03.v1",
            request_params={"token": r"SECRET E:\private"},
            error_code=SourceErrorCode.NETWORK.value,
            failed_at=_END.to_iso(),
        )


def test_s1_03_source_tree_contains_no_later_stage_implementations() -> None:
    root = (
        Path(__file__).resolve().parents[3] / "src" / "quantking" / "data" / "adapters" / "sources"
    )
    rendered = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(root.rglob("*.py"))
    ).casefold()
    for forbidden in (
        "baostock",
        "akshare",
        "tushare",
        "canonicalpublishcoordinator",
        "sourceoverride",
        "qualityrun",
        "fastapi",
        "xttrader",
    ):
        assert forbidden not in rendered
