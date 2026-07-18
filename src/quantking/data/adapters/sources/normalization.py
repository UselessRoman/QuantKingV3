"""Deterministic supplier-record to Canonical Arrow normalization."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any

import pyarrow as pa

from quantking.data.adapters.canonical_schema import (
    canonical_schema,
    ensure_sorted,
    validate_table,
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
from quantking.data.models import DatasetKind, EmptyResultInfo
from quantking.data.schemas import canonical_code_pattern

_PREFIX_CODE = re.compile(r"(SZ|SH|BJ)(\d{6})\Z")
_FACTOR_FORMULA = "p_adj=p_raw*price_multiplier;v_adj=v_raw*volume_multiplier"

_REQUIRED_FIELDS: dict[DatasetKind, tuple[str, ...]] = {
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


def _capability_failure() -> SourceAdapterError:
    return SourceAdapterError(SourceErrorCode.CAPABILITY_UNAVAILABLE)


def _contract_failure() -> SourceAdapterError:
    return SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)


def _verify_contract(dataset: DatasetKind, contract: SupplierResponseContract) -> None:
    if contract.dataset is not dataset:
        raise _contract_failure()
    if contract.required_fields != _REQUIRED_FIELDS[dataset]:
        raise _contract_failure()
    if not contract.semantics_verified:
        raise _capability_failure()
    if dataset is DatasetKind.SECURITY_MASTER and not contract.point_in_time:
        raise _capability_failure()
    if dataset in {
        DatasetKind.DAILY_BAR_UNADJUSTED,
        DatasetKind.BENCHMARK_INDEX_DAILY,
    }:
        if not contract.unadjusted:
            raise _capability_failure()
        if contract.volume_unit is VolumeUnit.UNVERIFIED:
            raise _capability_failure()
        if contract.amount_unit is not AmountUnit.CNY_YUAN:
            raise _capability_failure()
    if dataset is DatasetKind.ADJUSTMENT_FACTOR and (
        contract.adjustment_formula != _FACTOR_FORMULA
    ):
        raise _capability_failure()
    if dataset is DatasetKind.TRADING_STATUS and not contract.official_status:
        raise _capability_failure()


def _text(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _contract_failure()
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise _contract_failure()
    return value


def _float(value: object, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _contract_failure()
    result = float(value)
    if not math.isfinite(result):
        raise _contract_failure()
    return result


def _date(
    value: object, contract: SupplierResponseContract, *, nullable: bool = False
) -> date | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise _contract_failure()
    date_format = {
        DateFormat.ISO_YYYY_MM_DD: "%Y-%m-%d",
        DateFormat.COMPACT_YYYYMMDD: "%Y%m%d",
    }[contract.date_format]
    try:
        return datetime.strptime(value, date_format).date()
    except ValueError as error:
        raise _contract_failure() from error


def _code(value: object, contract: SupplierResponseContract) -> str:
    if not isinstance(value, str):
        raise _contract_failure()
    if contract.code_format is CodeFormat.CANONICAL:
        result = value
    else:
        matched = _PREFIX_CODE.fullmatch(value)
        if matched is None:
            raise _contract_failure()
        result = f"{matched.group(2)}.{matched.group(1)}"
    if canonical_code_pattern().fullmatch(result) is None:
        raise _contract_failure()
    return result


def _exchange(value: object, contract: SupplierResponseContract) -> str:
    if not isinstance(value, str) or value not in contract.exchange_map:
        raise _contract_failure()
    return contract.exchange_map[value]


def _volume(
    value: object,
    contract: SupplierResponseContract,
    *,
    nullable: bool = False,
) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _contract_failure()
    if contract.volume_unit is VolumeUnit.SHARE:
        return value
    if contract.volume_unit is VolumeUnit.LOT_100_SHARES:
        return value * 100
    raise _capability_failure()


def _required(record: Mapping[str, object], contract: SupplierResponseContract) -> None:
    if any(field not in record for field in contract.required_fields):
        raise _contract_failure()


def _security(
    record: Mapping[str, object], contract: SupplierResponseContract
) -> dict[str, object]:
    code = _code(record["stock_code"], contract)
    return {
        "code": code,
        "exchange": _exchange(record["exchange_id"], contract),
        "name": _text(record["instrument_name"]),
        "board": _text(record["board"]),
        "listing_date": _date(record["open_date"], contract),
        "delisting_date": _date(record["expire_date"], contract, nullable=True),
        "effective_from": _date(record["effective_from"], contract),
        "effective_to": _date(record["effective_to"], contract, nullable=True),
        "listing_status": _text(record["listing_status"]),
    }


def _calendar(
    record: Mapping[str, object], contract: SupplierResponseContract
) -> dict[str, object]:
    return {
        "exchange": _exchange(record["market"], contract),
        "trade_date": _date(record["calendar_date"], contract),
        "is_open": _bool(record["open_flag"]),
    }


def _bar(
    record: Mapping[str, object],
    contract: SupplierResponseContract,
    *,
    nullable_turnover: bool,
) -> dict[str, object]:
    return {
        "code": _code(record["stock_code"], contract),
        "trade_date": _date(record["date"], contract),
        "open": _float(record["open"]),
        "high": _float(record["high"]),
        "low": _float(record["low"]),
        "close": _float(record["close"]),
        "pre_close": _float(record["pre_close"]),
        "volume": _volume(record["volume"], contract, nullable=nullable_turnover),
        "amount": _float(record["amount"], nullable=nullable_turnover),
    }


def _factor(record: Mapping[str, object], contract: SupplierResponseContract) -> dict[str, object]:
    return {
        "code": _code(record["stock_code"], contract),
        "trade_date": _date(record["date"], contract),
        "price_multiplier": _float(record["price_multiplier"]),
        "volume_multiplier": _float(record["volume_multiplier"]),
        "normalization_date": _date(record["normalization_date"], contract),
        "algorithm_version": _text(record["algorithm_version"]),
    }


def _status(record: Mapping[str, object], contract: SupplierResponseContract) -> dict[str, object]:
    return {
        "code": _code(record["stock_code"], contract),
        "trade_date": _date(record["date"], contract),
        "is_trading": _bool(record["is_trading"]),
        "is_suspended": _bool(record["is_suspended"]),
        "is_st": _bool(record["is_st"]),
        "is_star_st": _bool(record["is_star_st"]),
        "is_delisting_period": _bool(record["is_delisting_period"]),
        "limit_up_price": _float(record["limit_up_price"], nullable=True),
        "limit_down_price": _float(record["limit_down_price"], nullable=True),
        "status_reason": _text(record["status_reason"], nullable=True),
    }


_NORMALIZERS: dict[
    DatasetKind,
    Callable[[Mapping[str, object], SupplierResponseContract], dict[str, object]],
] = {
    DatasetKind.SECURITY_MASTER: _security,
    DatasetKind.EXCHANGE_CALENDAR: _calendar,
    DatasetKind.DAILY_BAR_UNADJUSTED: lambda record, contract: _bar(
        record, contract, nullable_turnover=False
    ),
    DatasetKind.ADJUSTMENT_FACTOR: _factor,
    DatasetKind.TRADING_STATUS: _status,
    DatasetKind.BENCHMARK_INDEX_DAILY: lambda record, contract: _bar(
        record, contract, nullable_turnover=True
    ),
}


def required_fields_for(dataset: DatasetKind) -> tuple[str, ...]:
    """Return the exact supplier-boundary record fields for one dataset."""
    if not isinstance(dataset, DatasetKind):
        raise TypeError("dataset must be a DatasetKind")
    return _REQUIRED_FIELDS[dataset]


def normalize_xtquant_response(
    dataset: DatasetKind,
    response: SupplierResponse,
    *,
    empty_result: EmptyResultInfo | None = None,
) -> pa.Table:
    """Return an exact-schema, sorted Canonical candidate or a stable failure."""
    if not isinstance(dataset, DatasetKind):
        raise TypeError("dataset must be a DatasetKind")
    if not isinstance(response, SupplierResponse):
        raise TypeError("response must be a SupplierResponse")
    _verify_contract(dataset, response.contract)
    if not response.complete:
        raise SourceAdapterError(SourceErrorCode.PARTIAL_RESPONSE)
    try:
        rows: list[dict[str, Any]] = []
        normalize = _NORMALIZERS[dataset]
        for record in response.records:
            _required(record, response.contract)
            rows.append(normalize(record, response.contract))
        table = pa.Table.from_pylist(rows, schema=canonical_schema(dataset.value))
        table = ensure_sorted(dataset.value, table)
        validation = validate_table(dataset.value, table, empty_result=empty_result)
        if not validation.ok:
            if any(issue.rule == "unexpected_empty" for issue in validation.issues):
                raise SourceAdapterError(SourceErrorCode.UNEXPECTED_EMPTY)
            raise _contract_failure()
        return table
    except SourceAdapterError:
        raise
    except Exception as error:
        raise _contract_failure() from error


__all__ = ["normalize_xtquant_response", "required_fields_for"]
