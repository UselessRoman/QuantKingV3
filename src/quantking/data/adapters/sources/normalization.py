"""Deterministic AKShare daily-bar response normalization."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime

import pyarrow as pa

from quantking.data.adapters.canonical_schema import canonical_schema, ensure_sorted, validate_table
from quantking.data.adapters.sources.contracts import (
    AmountUnit,
    SourceAdapterError,
    SourceErrorCode,
    SupplierResponse,
    VolumeUnit,
)
from quantking.data.models import (
    DatasetKind,
    EmptyResultInfo,
    LegalEmptyClassification,
)
from quantking.data.schemas import canonical_code_pattern

_REQUIRED_FIELDS = (
    "stock_code",
    "date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
)


def required_fields_for(dataset: DatasetKind) -> tuple[str, ...]:
    if dataset is not DatasetKind.DAILY_BAR_UNADJUSTED:
        raise SourceAdapterError(SourceErrorCode.REQUEST_REJECTED)
    return _REQUIRED_FIELDS


def _contract_failure() -> SourceAdapterError:
    return SourceAdapterError(SourceErrorCode.PARSE)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _contract_failure()
    result = float(value)
    if not math.isfinite(result):
        raise _contract_failure()
    return result


def _volume(value: object, unit: VolumeUnit) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _contract_failure()
    if unit is VolumeUnit.LOT_100_SHARES:
        return value * 100
    # UNVERIFIED is intentionally preserved without an inferred conversion.
    return value


def _row(record: Mapping[str, object], response: SupplierResponse) -> dict[str, object]:
    if any(field not in record for field in _REQUIRED_FIELDS):
        raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
    code = record["stock_code"]
    if not isinstance(code, str) or canonical_code_pattern().fullmatch(code) is None:
        raise _contract_failure()
    raw_date = record["date"]
    if not isinstance(raw_date, str):
        raise _contract_failure()
    try:
        trade_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError as error:
        raise _contract_failure() from error
    return {
        "code": code,
        "trade_date": trade_date,
        "open": _number(record["open"]),
        "high": _number(record["high"]),
        "low": _number(record["low"]),
        "close": _number(record["close"]),
        "pre_close": _number(record["pre_close"]),
        "volume": _volume(record["volume"], response.contract.volume_unit),
        "amount": _number(record["amount"]),
    }


def normalize_akshare_response(
    dataset: DatasetKind,
    response: SupplierResponse,
    *,
    empty_result: EmptyResultInfo | None = None,
) -> pa.Table:
    """Return an exact-schema, sorted candidate without guessing unit conversions."""
    if dataset is not DatasetKind.DAILY_BAR_UNADJUSTED:
        raise SourceAdapterError(SourceErrorCode.REQUEST_REJECTED)
    if not isinstance(response, SupplierResponse):
        raise TypeError("response must be a SupplierResponse")
    if response.contract.dataset is not dataset:
        raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
    if response.contract.required_fields != _REQUIRED_FIELDS or not response.contract.unadjusted:
        raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
    if not response.complete:
        raise SourceAdapterError(SourceErrorCode.PARTIAL_RESPONSE)
    if not response.records:
        if (
            empty_result is None
            or empty_result.classification is not LegalEmptyClassification.NO_TRADING_SESSION
        ):
            raise SourceAdapterError(SourceErrorCode.UNEXPECTED_EMPTY)
        table = pa.Table.from_pylist([], schema=canonical_schema(dataset.value))
        validation = validate_table(dataset.value, table, empty_result=empty_result)
        if not validation.ok:
            raise _contract_failure()
        return table
    if (
        response.contract.volume_unit is VolumeUnit.UNVERIFIED
        or response.contract.amount_unit is AmountUnit.UNVERIFIED
    ):
        raise SourceAdapterError(SourceErrorCode.UNIT_UNVERIFIED)
    if response.contract.amount_unit is not AmountUnit.CNY_YUAN:
        raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
    try:
        table = pa.Table.from_pylist(
            [_row(record, response) for record in response.records],
            schema=canonical_schema(dataset.value),
        )
        table = ensure_sorted(dataset.value, table)
        validation = validate_table(dataset.value, table, empty_result=empty_result)
    except SourceAdapterError:
        raise
    except Exception as error:
        raise _contract_failure() from error
    if not validation.ok:
        if any(issue.rule == "unexpected_empty" for issue in validation.issues):
            raise SourceAdapterError(SourceErrorCode.UNEXPECTED_EMPTY)
        raise _contract_failure()
    return table


__all__ = ["normalize_akshare_response", "required_fields_for"]
