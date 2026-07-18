"""Stable source-boundary contracts with no vendor or dataframe objects."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from quantking.data.incremental import FetchChunk, validate_fetch_chunk
from quantking.data.models import BatchFailureKind, DatasetKind

_RAW_RESPONSE_SCHEMA_VERSION = "xtquant-raw-response.v1"


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


def _contract_payload(contract: SupplierResponseContract) -> dict[str, object]:
    return {
        "dataset": contract.dataset.value,
        "contract_version": contract.contract_version,
        "required_fields": list(contract.required_fields),
        "code_format": contract.code_format.value,
        "date_format": contract.date_format.value,
        "exchange_map": dict(contract.exchange_map),
        "volume_unit": contract.volume_unit.value,
        "amount_unit": contract.amount_unit.value,
        "point_in_time": contract.point_in_time,
        "unadjusted": contract.unadjusted,
        "official_status": contract.official_status,
        "adjustment_formula": contract.adjustment_formula,
        "semantics_verified": contract.semantics_verified,
    }


def _contract_from_payload(value: object) -> SupplierResponseContract:
    keys = {
        "dataset",
        "contract_version",
        "required_fields",
        "code_format",
        "date_format",
        "exchange_map",
        "volume_unit",
        "amount_unit",
        "point_in_time",
        "unadjusted",
        "official_status",
        "adjustment_formula",
        "semantics_verified",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
    required_fields = value["required_fields"]
    exchange_map = value["exchange_map"]
    if not isinstance(required_fields, list) or any(
        not isinstance(field, str) for field in required_fields
    ):
        raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
    if not isinstance(exchange_map, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in exchange_map.items()
    ):
        raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
    try:
        return SupplierResponseContract(
            dataset=DatasetKind(value["dataset"]),
            contract_version=value["contract_version"],
            required_fields=tuple(required_fields),
            code_format=CodeFormat(value["code_format"]),
            date_format=DateFormat(value["date_format"]),
            exchange_map=exchange_map,
            volume_unit=VolumeUnit(value["volume_unit"]),
            amount_unit=AmountUnit(value["amount_unit"]),
            point_in_time=value["point_in_time"],
            unadjusted=value["unadjusted"],
            official_status=value["official_status"],
            adjustment_formula=value["adjustment_formula"],
            semantics_verified=value["semantics_verified"],
        )
    except (TypeError, ValueError) as error:
        raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT) from error


def _chunk_payload(chunk: FetchChunk) -> dict[str, object]:
    return {
        "sequence": chunk.sequence,
        "dataset": chunk.dataset.value,
        "scope": list(chunk.scope),
        "start": str(chunk.start),
        "end": str(chunk.end),
        "start_inclusive": chunk.start_inclusive,
        "end_inclusive": chunk.end_inclusive,
        "request_sha256": chunk.request_sha256,
    }


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _load_canonical_json(payload: bytes) -> object:
    try:
        decoded = payload.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT) from error
    if canonical != payload:
        raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
    return value


def _plain_records(records: tuple[Mapping[str, object], ...]) -> list[dict[str, object]]:
    try:
        payload = json.dumps(
            [dict(record) for record in records],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        value = _load_canonical_json(payload)
    except (TypeError, ValueError) as error:
        raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT) from error
    if not isinstance(value, list) or any(not isinstance(record, dict) for record in value):
        raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
    return value


@dataclass(frozen=True, slots=True)
class SupplierResponse:
    """Raw bytes and typed record views returned by a source gateway."""

    records: tuple[Mapping[str, object], ...]
    raw_payload: bytes
    contract: SupplierResponseContract
    complete: bool
    partial_reason: str | None
    chunk: FetchChunk | None = None

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
        if self.partial_reason is not None and self.partial_reason not in {
            code.value for code in SourceErrorCode
        }:
            raise ValueError("partial_reason must be a stable source error code")
        raw = _load_canonical_json(self.raw_payload)
        expected_records = _plain_records(self.records)
        if self.chunk is None:
            if raw != expected_records:
                raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
            return
        validate_fetch_chunk(self.chunk)
        expected_keys = {
            "schema_version",
            "binding",
            "contract",
            "response",
            "provider_payload",
        }
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        response = raw["response"]
        if not isinstance(response, dict) or set(response) != {
            "complete",
            "partial_reason",
            "records",
        }:
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        if raw["schema_version"] != _RAW_RESPONSE_SCHEMA_VERSION:
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        if raw["binding"] != _chunk_payload(self.chunk):
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        if _contract_from_payload(raw["contract"]) != self.contract:
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        if response["records"] != expected_records:
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        if response["complete"] is not self.complete:
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        if response["partial_reason"] != self.partial_reason:
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)

    @classmethod
    def create_bound(
        cls,
        *,
        chunk: FetchChunk,
        records: tuple[Mapping[str, object], ...],
        contract: SupplierResponseContract,
        complete: bool,
        partial_reason: str | None,
        provider_payload: object,
    ) -> SupplierResponse:
        """Create the only response form eligible for Raw ingestion."""
        validate_fetch_chunk(chunk)
        if contract.dataset is not chunk.dataset:
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        plain_records = _plain_records(records)
        envelope = {
            "schema_version": _RAW_RESPONSE_SCHEMA_VERSION,
            "binding": _chunk_payload(chunk),
            "contract": _contract_payload(contract),
            "response": {
                "complete": complete,
                "partial_reason": partial_reason,
                "records": plain_records,
            },
            "provider_payload": provider_payload,
        }
        try:
            raw_payload = json.dumps(
                envelope,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT) from error
        return cls(
            records=tuple(plain_records),
            raw_payload=raw_payload,
            contract=contract,
            complete=complete,
            partial_reason=partial_reason,
            chunk=chunk,
        )

    @classmethod
    def from_raw_payload(
        cls,
        raw_payload: bytes,
        *,
        expected_chunk: FetchChunk,
    ) -> SupplierResponse:
        """Rebuild a typed response solely from verified Raw bytes."""
        validate_fetch_chunk(expected_chunk)
        raw = _load_canonical_json(raw_payload)
        if not isinstance(raw, dict):
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
        try:
            contract = _contract_from_payload(raw["contract"])
            response = raw["response"]
            if not isinstance(response, dict):
                raise TypeError("response must be an object")
            records = response["records"]
            if not isinstance(records, list) or any(
                not isinstance(record, dict) for record in records
            ):
                raise TypeError("records must be an object list")
            complete = response["complete"]
            partial_reason = response["partial_reason"]
        except (KeyError, TypeError) as error:
            raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT) from error
        return cls(
            records=tuple(records),
            raw_payload=raw_payload,
            contract=contract,
            complete=complete,
            partial_reason=partial_reason,
            chunk=expected_chunk,
        )


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
