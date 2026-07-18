"""Offline XTQuant ingestion orchestration with Raw and SQLite evidence."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

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
from quantking.data.adapters.warehouse.errors import RawStoreError
from quantking.data.adapters.warehouse.manifest import path_safe_id
from quantking.data.adapters.warehouse.raw_store import (
    RawBatchFailureRequest,
    RawBatchPublisher,
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
        return result


class FailingRawPublisher(RawBatchPublisher):
    def _write_file(self, target: Path, payload: bytes) -> None:
        raise OSError(r"secret path E:\private\raw")


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
    with factory() as uow:
        persisted = uow.data.get_batch(str(plan.batch_id))  # type: ignore[union-attr]
    assert persisted == outcome.batch
    assert gateway.calls == 1


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
    response = _response(complete=False, partial_reason="one requested symbol missing")
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
    assert received[0]["stock_code"] == "000001.SZ"
    assert evidence["error_code"] == SourceErrorCode.NETWORK.value
    assert "SECRET" not in json.dumps(evidence)
    assert "E:\\private" not in json.dumps(evidence)


def test_normalization_failure_keeps_raw_and_cannot_succeed(
    tmp_path: Path,
    persistence: tuple[Path, DataSqliteUnitOfWorkFactory],
) -> None:
    _, factory = persistence
    bad = dict(_response().records[0])
    bad["close"] = float("nan")
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
