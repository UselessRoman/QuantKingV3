"""Stable source-boundary contracts with no vendor or dataframe objects."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from quantking.data.models import BatchFailureKind, DatasetKind


class SourceErrorCode(StrEnum):
    """Stable, redacted source failure categories."""

    NETWORK = "XTQUANT_NETWORK"
    PERMISSION = "XTQUANT_PERMISSION"
    REQUEST_REJECTED = "XTQUANT_REQUEST_REJECTED"
    RESPONSE_CONTRACT = "XTQUANT_RESPONSE_CONTRACT"
    CAPABILITY_UNAVAILABLE = "XTQUANT_CAPABILITY_UNAVAILABLE"
    UNEXPECTED_EMPTY = "XTQUANT_UNEXPECTED_EMPTY"
    PARTIAL_RESPONSE = "XTQUANT_PARTIAL_RESPONSE"
    INTERNAL_SDK = "XTQUANT_INTERNAL_SDK"


_MESSAGES: dict[SourceErrorCode, str] = {
    SourceErrorCode.NETWORK: "XTQuant network or connection failed",
    SourceErrorCode.PERMISSION: "XTQuant permission or authorization failed",
    SourceErrorCode.REQUEST_REJECTED: "XTQuant request was rejected",
    SourceErrorCode.RESPONSE_CONTRACT: "XTQuant response contract is invalid",
    SourceErrorCode.CAPABILITY_UNAVAILABLE: "XTQuant capability is not verified",
    SourceErrorCode.UNEXPECTED_EMPTY: "XTQuant returned an unexpected empty response",
    SourceErrorCode.PARTIAL_RESPONSE: "XTQuant returned a partial response",
    SourceErrorCode.INTERNAL_SDK: "XTQuant internal SDK failure",
}

_FAILURE_KIND: dict[SourceErrorCode, BatchFailureKind] = {
    SourceErrorCode.NETWORK: BatchFailureKind.NETWORK,
    SourceErrorCode.PERMISSION: BatchFailureKind.PERMISSION,
    SourceErrorCode.REQUEST_REJECTED: BatchFailureKind.PARSE,
    SourceErrorCode.RESPONSE_CONTRACT: BatchFailureKind.PARSE,
    SourceErrorCode.CAPABILITY_UNAVAILABLE: BatchFailureKind.INTERNAL,
    SourceErrorCode.UNEXPECTED_EMPTY: BatchFailureKind.UNEXPECTED_EMPTY,
    SourceErrorCode.PARTIAL_RESPONSE: BatchFailureKind.PARTIAL_SOURCE,
    SourceErrorCode.INTERNAL_SDK: BatchFailureKind.INTERNAL,
}

_RETRYABLE: dict[SourceErrorCode, bool] = {
    SourceErrorCode.NETWORK: True,
    SourceErrorCode.PERMISSION: False,
    SourceErrorCode.REQUEST_REJECTED: False,
    SourceErrorCode.RESPONSE_CONTRACT: False,
    SourceErrorCode.CAPABILITY_UNAVAILABLE: False,
    SourceErrorCode.UNEXPECTED_EMPTY: True,
    SourceErrorCode.PARTIAL_RESPONSE: True,
    SourceErrorCode.INTERNAL_SDK: True,
}


class SourceAdapterError(Exception):
    """Public source error whose text never includes vendor exception content."""

    def __init__(self, code: SourceErrorCode) -> None:
        if not isinstance(code, SourceErrorCode):
            raise TypeError("code must be a SourceErrorCode")
        self.code = code
        self.message = _MESSAGES[code]
        self.failure_kind = _FAILURE_KIND[code]
        self.retryable = _RETRYABLE[code]
        super().__init__(self.message)


def classify_source_error(error: BaseException) -> SourceAdapterError:
    """Map an unknown boundary exception without inspecting its sensitive text."""
    if isinstance(error, SourceAdapterError):
        return error
    if isinstance(error, PermissionError):
        return SourceAdapterError(SourceErrorCode.PERMISSION)
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return SourceAdapterError(SourceErrorCode.NETWORK)
    if isinstance(error, (KeyError, TypeError, ValueError)):
        return SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
    return SourceAdapterError(SourceErrorCode.INTERNAL_SDK)


class CodeFormat(StrEnum):
    """Explicit input code encodings accepted by the normalizer."""

    CANONICAL = "000001.SZ"
    EXCHANGE_PREFIX = "SZ000001"


class DateFormat(StrEnum):
    """Explicit exchange-local date encodings."""

    ISO_YYYY_MM_DD = "YYYY-MM-DD"
    COMPACT_YYYYMMDD = "YYYYMMDD"


class VolumeUnit(StrEnum):
    """Explicit supplier volume units; never inferred from values."""

    SHARE = "share"
    LOT_100_SHARES = "lot_100_shares"
    UNVERIFIED = "unverified"


class AmountUnit(StrEnum):
    """Explicit supplier amount units; never inferred from values."""

    CNY_YUAN = "cny_yuan"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class SupplierResponseContract:
    """Verified interpretation attached to one raw supplier response."""

    dataset: DatasetKind
    contract_version: str
    required_fields: tuple[str, ...]
    code_format: CodeFormat
    date_format: DateFormat
    exchange_map: Mapping[str, str]
    volume_unit: VolumeUnit
    amount_unit: AmountUnit
    point_in_time: bool
    unadjusted: bool
    official_status: bool
    adjustment_formula: str | None
    semantics_verified: bool

    def __post_init__(self) -> None:
        if not isinstance(self.dataset, DatasetKind):
            raise TypeError("dataset must be a DatasetKind")
        if not isinstance(self.contract_version, str) or not self.contract_version.strip():
            raise ValueError("contract_version must be non-empty text")
        if not isinstance(self.required_fields, tuple) or not self.required_fields:
            raise ValueError("required_fields must be a non-empty tuple")
        if len(set(self.required_fields)) != len(self.required_fields):
            raise ValueError("required_fields must be unique")
        if any(not isinstance(field, str) or not field for field in self.required_fields):
            raise ValueError("required_fields must contain non-empty text")
        if not isinstance(self.code_format, CodeFormat):
            raise TypeError("code_format must be a CodeFormat")
        if not isinstance(self.date_format, DateFormat):
            raise TypeError("date_format must be a DateFormat")
        if not isinstance(self.volume_unit, VolumeUnit):
            raise TypeError("volume_unit must be a VolumeUnit")
        if not isinstance(self.amount_unit, AmountUnit):
            raise TypeError("amount_unit must be an AmountUnit")
        normalized_exchange: dict[str, str] = {}
        for supplier, canonical in self.exchange_map.items():
            if not isinstance(supplier, str) or not supplier:
                raise ValueError("exchange_map keys must be non-empty text")
            if canonical not in {"SZ", "SH", "BJ"}:
                raise ValueError("exchange_map values must be canonical exchanges")
            normalized_exchange[supplier] = canonical
        object.__setattr__(
            self,
            "exchange_map",
            MappingProxyType(dict(sorted(normalized_exchange.items()))),
        )
        for field in ("point_in_time", "unadjusted", "official_status", "semantics_verified"):
            if not isinstance(getattr(self, field), bool):
                raise TypeError(f"{field} must be a bool")
        if self.adjustment_formula is not None and (
            not isinstance(self.adjustment_formula, str) or not self.adjustment_formula.strip()
        ):
            raise ValueError("adjustment_formula must be non-empty text or None")


@dataclass(frozen=True, slots=True)
class SupplierResponse:
    """Raw bytes and typed record views returned by a source gateway."""

    records: tuple[Mapping[str, object], ...]
    raw_payload: bytes
    contract: SupplierResponseContract
    complete: bool
    partial_reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.records, tuple):
            raise TypeError("records must be a tuple")
        normalized: list[Mapping[str, object]] = []
        for record in self.records:
            if not isinstance(record, Mapping):
                raise TypeError("records must contain mappings")
            normalized.append(MappingProxyType(dict(record)))
        object.__setattr__(self, "records", tuple(normalized))
        if not isinstance(self.raw_payload, (bytes, bytearray)):
            raise TypeError("raw_payload must be bytes")
        object.__setattr__(self, "raw_payload", bytes(self.raw_payload))
        try:
            json.loads(self.raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT) from error
        if not isinstance(self.contract, SupplierResponseContract):
            raise TypeError("contract must be a SupplierResponseContract")
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be a bool")
        if self.complete and self.partial_reason is not None:
            raise ValueError("complete response cannot carry partial_reason")
        if not self.complete and (
            not isinstance(self.partial_reason, str) or not self.partial_reason.strip()
        ):
            raise ValueError("partial response requires a non-empty partial_reason")


__all__ = [
    "AmountUnit",
    "CodeFormat",
    "DateFormat",
    "SourceAdapterError",
    "SourceErrorCode",
    "SupplierResponse",
    "SupplierResponseContract",
    "VolumeUnit",
    "classify_source_error",
]
