"""Six XTQuant response contracts map deterministically to Canonical Arrow."""

from __future__ import annotations

import json
import math
from datetime import date

import pyarrow as pa
import pytest

from quantking.data.adapters.canonical_schema import canonical_schema, validate_table
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
from quantking.data.adapters.sources.normalization import normalize_xtquant_response
from quantking.data.models import (
    DatasetKind,
    EmptyResultInfo,
    LegalEmptyClassification,
)

_FACTOR_FORMULA = "p_adj=p_raw*price_multiplier;v_adj=v_raw*volume_multiplier"

_FIELDS: dict[DatasetKind, tuple[str, ...]] = {
    DatasetKind.SECURITY_MASTER: (
        "stock_code",
        "exchange_id",
        "instrument_name",
        "board",
        "open_date",
        "expire_date",
        "effective_from",
        "effective_to",
        "listing_status",
    ),
    DatasetKind.EXCHANGE_CALENDAR: ("market", "calendar_date", "open_flag"),
    DatasetKind.DAILY_BAR_UNADJUSTED: (
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
    DatasetKind.ADJUSTMENT_FACTOR: (
        "stock_code",
        "date",
        "price_multiplier",
        "volume_multiplier",
        "normalization_date",
        "algorithm_version",
    ),
    DatasetKind.TRADING_STATUS: (
        "stock_code",
        "date",
        "is_trading",
        "is_suspended",
        "is_st",
        "is_star_st",
        "is_delisting_period",
        "limit_up_price",
        "limit_down_price",
        "status_reason",
    ),
    DatasetKind.BENCHMARK_INDEX_DAILY: (
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
}


def _contract(dataset: DatasetKind, **changes: object) -> SupplierResponseContract:
    values: dict[str, object] = {
        "dataset": dataset,
        "contract_version": "xtquant-synthetic.v1",
        "required_fields": _FIELDS[dataset],
        "code_format": CodeFormat.EXCHANGE_PREFIX,
        "date_format": DateFormat.COMPACT_YYYYMMDD,
        "exchange_map": {"XSHE": "SZ", "XSHG": "SH", "BSE": "BJ", "SH": "SH"},
        "volume_unit": VolumeUnit.LOT_100_SHARES,
        "amount_unit": AmountUnit.CNY_YUAN,
        "point_in_time": dataset is DatasetKind.SECURITY_MASTER,
        "unadjusted": dataset
        in {DatasetKind.DAILY_BAR_UNADJUSTED, DatasetKind.BENCHMARK_INDEX_DAILY},
        "official_status": dataset is DatasetKind.TRADING_STATUS,
        "adjustment_formula": (
            _FACTOR_FORMULA if dataset is DatasetKind.ADJUSTMENT_FACTOR else None
        ),
        "semantics_verified": True,
    }
    values.update(changes)
    return SupplierResponseContract(**values)  # type: ignore[arg-type]


def _rows(dataset: DatasetKind) -> tuple[dict[str, object], ...]:
    if dataset is DatasetKind.SECURITY_MASTER:
        return (
            {
                "stock_code": "SZ000001",
                "exchange_id": "XSHE",
                "instrument_name": "平安银行",
                "board": "MAIN",
                "open_date": "19910403",
                "expire_date": None,
                "effective_from": "20260701",
                "effective_to": None,
                "listing_status": "LISTED",
            },
        )
    if dataset is DatasetKind.EXCHANGE_CALENDAR:
        return (
            {"market": "XSHG", "calendar_date": "20260718", "open_flag": False},
            {"market": "XSHG", "calendar_date": "20260717", "open_flag": True},
        )
    if dataset is DatasetKind.DAILY_BAR_UNADJUSTED:
        return (
            {
                "stock_code": "SZ000001",
                "date": "20260718",
                "open": 10.1,
                "high": 10.8,
                "low": 10.0,
                "close": 10.5,
                "pre_close": 10.0,
                "volume": 123,
                "amount": 1_291_500.0,
            },
            {
                "stock_code": "SZ000001",
                "date": "20260717",
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.0,
                "pre_close": 9.9,
                "volume": 100,
                "amount": 1_000_000.0,
            },
        )
    if dataset is DatasetKind.ADJUSTMENT_FACTOR:
        return (
            {
                "stock_code": "SH600000",
                "date": "20260718",
                "price_multiplier": 1.25,
                "volume_multiplier": 0.8,
                "normalization_date": "20260718",
                "algorithm_version": "qk-factor.v1",
            },
        )
    if dataset is DatasetKind.TRADING_STATUS:
        return (
            {
                "stock_code": "SZ000001",
                "date": "20260717",
                "is_trading": True,
                "is_suspended": False,
                "is_st": True,
                "is_star_st": True,
                "is_delisting_period": False,
                "limit_up_price": 10.5,
                "limit_down_price": 8.6,
                "status_reason": "*ST",
            },
            {
                "stock_code": "SH600000",
                "date": "20260717",
                "is_trading": False,
                "is_suspended": True,
                "is_st": False,
                "is_star_st": False,
                "is_delisting_period": False,
                "limit_up_price": None,
                "limit_down_price": None,
                "status_reason": "official suspension",
            },
            {
                "stock_code": "BJ430047",
                "date": "20260717",
                "is_trading": False,
                "is_suspended": False,
                "is_st": False,
                "is_star_st": False,
                "is_delisting_period": True,
                "limit_up_price": None,
                "limit_down_price": None,
                "status_reason": "delisting period",
            },
        )
    return (
        {
            "stock_code": "SH000300",
            "date": "20260717",
            "open": 4000.0,
            "high": 4050.0,
            "low": 3980.0,
            "close": 4020.0,
            "pre_close": 3990.0,
            "volume": None,
            "amount": None,
        },
    )


def _response(
    dataset: DatasetKind,
    *,
    rows: tuple[dict[str, object], ...] | None = None,
    contract: SupplierResponseContract | None = None,
) -> SupplierResponse:
    records = _rows(dataset) if rows is None else rows
    return SupplierResponse(
        records=records,
        raw_payload=json.dumps(
            [dict(record) for record in records],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
        contract=_contract(dataset) if contract is None else contract,
        complete=True,
        partial_reason=None,
    )


def test_opaque_or_executable_raw_payload_is_rejected() -> None:
    with pytest.raises(SourceAdapterError) as exc_info:
        SupplierResponse(
            records=_rows(DatasetKind.DAILY_BAR_UNADJUSTED),
            raw_payload=b"\x80\x04pickle",
            contract=_contract(DatasetKind.DAILY_BAR_UNADJUSTED),
            complete=True,
            partial_reason=None,
        )
    assert exc_info.value.code is SourceErrorCode.RESPONSE_CONTRACT


@pytest.mark.parametrize("dataset", tuple(DatasetKind))
def test_six_responses_have_exact_schema_sorting_and_unique_keys(dataset: DatasetKind) -> None:
    table = normalize_xtquant_response(dataset, _response(dataset))
    assert table.schema.equals(canonical_schema(dataset.value), check_metadata=True)
    assert validate_table(dataset.value, table).ok


def test_code_exchange_date_volume_and_amount_are_converted() -> None:
    table = normalize_xtquant_response(
        DatasetKind.DAILY_BAR_UNADJUSTED,
        _response(DatasetKind.DAILY_BAR_UNADJUSTED),
    )
    assert table.column("code").to_pylist() == ["000001.SZ", "000001.SZ"]
    assert table.column("trade_date").to_pylist() == [
        date(2026, 7, 17),
        date(2026, 7, 18),
    ]
    assert table.column("volume").to_pylist() == [10_000, 12_300]
    assert table.column("amount").to_pylist() == [1_000_000.0, 1_291_500.0]


def test_security_master_keeps_listing_boundaries_and_point_in_time_dates() -> None:
    table = normalize_xtquant_response(
        DatasetKind.SECURITY_MASTER,
        _response(DatasetKind.SECURITY_MASTER),
    )
    assert table.to_pylist() == [
        {
            "code": "000001.SZ",
            "exchange": "SZ",
            "name": "平安银行",
            "board": "MAIN",
            "listing_date": date(1991, 4, 3),
            "delisting_date": None,
            "effective_from": date(2026, 7, 1),
            "effective_to": None,
            "listing_status": "LISTED",
        }
    ]


def test_adjustment_formula_base_date_and_algorithm_are_preserved() -> None:
    table = normalize_xtquant_response(
        DatasetKind.ADJUSTMENT_FACTOR,
        _response(DatasetKind.ADJUSTMENT_FACTOR),
    )
    row = table.to_pylist()[0]
    assert row["price_multiplier"] == 1.25
    assert row["volume_multiplier"] == 0.8
    assert row["normalization_date"] == date(2026, 7, 18)
    assert row["algorithm_version"] == "qk-factor.v1"


@pytest.mark.parametrize(
    ("dataset", "mutation", "expected_code"),
    (
        (
            DatasetKind.DAILY_BAR_UNADJUSTED,
            lambda row: row.pop("close"),
            SourceErrorCode.RESPONSE_CONTRACT,
        ),
        (
            DatasetKind.DAILY_BAR_UNADJUSTED,
            lambda row: row.__setitem__("volume", "100"),
            SourceErrorCode.RESPONSE_CONTRACT,
        ),
        (
            DatasetKind.DAILY_BAR_UNADJUSTED,
            lambda row: row.__setitem__("close", math.nan),
            SourceErrorCode.RESPONSE_CONTRACT,
        ),
        (
            DatasetKind.DAILY_BAR_UNADJUSTED,
            lambda row: row.__setitem__("amount", math.inf),
            SourceErrorCode.RESPONSE_CONTRACT,
        ),
        (
            DatasetKind.TRADING_STATUS,
            lambda row: row.__setitem__("is_suspended", True),
            SourceErrorCode.RESPONSE_CONTRACT,
        ),
    ),
)
def test_missing_wrong_nonfinite_and_contradictory_records_fail(
    dataset: DatasetKind,
    mutation: object,
    expected_code: SourceErrorCode,
) -> None:
    row = dict(_rows(dataset)[0])
    mutation(row)  # type: ignore[operator]
    with pytest.raises(SourceAdapterError) as exc_info:
        normalize_xtquant_response(dataset, _response(dataset, rows=(row,)))
    assert exc_info.value.code is expected_code


def test_duplicate_primary_key_fails_instead_of_choosing_a_row() -> None:
    row = dict(_rows(DatasetKind.DAILY_BAR_UNADJUSTED)[0])
    with pytest.raises(SourceAdapterError) as exc_info:
        normalize_xtquant_response(
            DatasetKind.DAILY_BAR_UNADJUSTED,
            _response(DatasetKind.DAILY_BAR_UNADJUSTED, rows=(row, dict(row))),
        )
    assert exc_info.value.code is SourceErrorCode.RESPONSE_CONTRACT


@pytest.mark.parametrize(
    ("dataset", "contract"),
    (
        (
            DatasetKind.DAILY_BAR_UNADJUSTED,
            _contract(
                DatasetKind.DAILY_BAR_UNADJUSTED,
                volume_unit=VolumeUnit.UNVERIFIED,
            ),
        ),
        (
            DatasetKind.SECURITY_MASTER,
            _contract(DatasetKind.SECURITY_MASTER, point_in_time=False),
        ),
        (
            DatasetKind.TRADING_STATUS,
            _contract(DatasetKind.TRADING_STATUS, official_status=False),
        ),
        (
            DatasetKind.ADJUSTMENT_FACTOR,
            _contract(
                DatasetKind.ADJUSTMENT_FACTOR,
                adjustment_formula="unknown",
            ),
        ),
    ),
)
def test_unverified_units_or_semantics_are_never_guessed(
    dataset: DatasetKind, contract: SupplierResponseContract
) -> None:
    with pytest.raises(SourceAdapterError) as exc_info:
        normalize_xtquant_response(dataset, _response(dataset, contract=contract))
    assert exc_info.value.code is SourceErrorCode.CAPABILITY_UNAVAILABLE


def test_legal_empty_and_unexpected_empty_are_distinct() -> None:
    response = _response(DatasetKind.EXCHANGE_CALENDAR, rows=())
    with pytest.raises(SourceAdapterError) as unexpected:
        normalize_xtquant_response(DatasetKind.EXCHANGE_CALENDAR, response)
    assert unexpected.value.code is SourceErrorCode.UNEXPECTED_EMPTY

    empty = EmptyResultInfo(
        LegalEmptyClassification.NO_TRADING_SESSION,
        "authorized calendar proof identifies no trading session",
    )
    table = normalize_xtquant_response(
        DatasetKind.EXCHANGE_CALENDAR,
        response,
        empty_result=empty,
    )
    assert table.num_rows == 0
    assert table.schema.equals(
        pa.schema(canonical_schema(DatasetKind.EXCHANGE_CALENDAR.value)),
        check_metadata=True,
    )
