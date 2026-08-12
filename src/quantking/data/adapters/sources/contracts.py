"""Stable AKShare source-boundary contracts without dataframe objects."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from quantking.data.adapters.sources.registry import PROVIDER_UPSTREAM_PAIRS
from quantking.data.adapters.sources.request_window import AkshareRequestWindow
from quantking.data.incremental import FetchChunk, validate_fetch_chunk
from quantking.data.models import BatchFailureKind, DataProvider, DatasetKind, DataUpstream

_AKSHARE_RAW_RESPONSE_SCHEMA_VERSION = "akshare-raw-response.v1"
_MARKET_DATA_RAW_RESPONSE_SCHEMA_VERSION = "market-data-raw-response.v2"


class SourceErrorCode(StrEnum):
    """Stable, redacted failure categories exposed outside the adapter."""

    NETWORK = "AKSHARE_NETWORK"
    RATE_LIMITED = "AKSHARE_RATE_LIMITED"
    REQUEST_REJECTED = "AKSHARE_REQUEST_REJECTED"
    SCHEMA_DRIFT = "AKSHARE_SCHEMA_DRIFT"
    PARSE = "AKSHARE_PARSE"
    UNIT_UNVERIFIED = "AKSHARE_UNIT_UNVERIFIED"
    UNEXPECTED_EMPTY = "AKSHARE_UNEXPECTED_EMPTY"
    PARTIAL_RESPONSE = "AKSHARE_PARTIAL_RESPONSE"
    INTERNAL_PROVIDER = "AKSHARE_INTERNAL_PROVIDER"
    BAOSTOCK_LOGIN_FAILED = "BAOSTOCK_LOGIN_FAILED"
    BAOSTOCK_QUERY_FAILED = "BAOSTOCK_QUERY_FAILED"
    BAOSTOCK_SERVICE_REJECTED = "BAOSTOCK_SERVICE_REJECTED"
    BAOSTOCK_TIMEOUT = "BAOSTOCK_TIMEOUT"
    BAOSTOCK_DISCONNECTED = "BAOSTOCK_DISCONNECTED"
    BAOSTOCK_UNEXPECTED_EMPTY = "BAOSTOCK_UNEXPECTED_EMPTY"
    BAOSTOCK_PARTIAL_RESPONSE = "BAOSTOCK_PARTIAL_RESPONSE"
    BAOSTOCK_SCHEMA_DRIFT = "BAOSTOCK_SCHEMA_DRIFT"
    BAOSTOCK_LOGOUT_FAILED = "BAOSTOCK_LOGOUT_FAILED"
    BAOSTOCK_INTERNAL_PROVIDER = "BAOSTOCK_INTERNAL_PROVIDER"


class NetworkReason(StrEnum):
    """Stable, redacted reason for a network-boundary failure."""

    PROXY = "proxy"
    REMOTE_DISCONNECTED = "remote_disconnected"
    DNS = "dns"
    TLS = "tls"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    UNKNOWN = "unknown"


_MESSAGES: dict[SourceErrorCode, str] = {
    SourceErrorCode.NETWORK: "AKShare upstream network request failed",
    SourceErrorCode.RATE_LIMITED: "AKShare upstream rate limit was reached",
    SourceErrorCode.REQUEST_REJECTED: "AKShare request was rejected",
    SourceErrorCode.SCHEMA_DRIFT: "AKShare upstream response schema changed",
    SourceErrorCode.PARSE: "AKShare upstream response could not be parsed",
    SourceErrorCode.UNIT_UNVERIFIED: "AKShare volume or amount unit is not verified",
    SourceErrorCode.UNEXPECTED_EMPTY: "AKShare upstream returned an unexpected empty response",
    SourceErrorCode.PARTIAL_RESPONSE: "AKShare upstream returned a partial response",
    SourceErrorCode.INTERNAL_PROVIDER: "AKShare provider failed internally",
    SourceErrorCode.BAOSTOCK_LOGIN_FAILED: "BaoStock login failed",
    SourceErrorCode.BAOSTOCK_QUERY_FAILED: "BaoStock query failed",
    SourceErrorCode.BAOSTOCK_SERVICE_REJECTED: "BaoStock service rejected the request",
    SourceErrorCode.BAOSTOCK_TIMEOUT: "BaoStock session exceeded its hard timeout",
    SourceErrorCode.BAOSTOCK_DISCONNECTED: "BaoStock service connection was interrupted",
    SourceErrorCode.BAOSTOCK_UNEXPECTED_EMPTY: "BaoStock returned an unexpected empty response",
    SourceErrorCode.BAOSTOCK_PARTIAL_RESPONSE: "BaoStock returned a partial response",
    SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT: "BaoStock response schema changed",
    SourceErrorCode.BAOSTOCK_LOGOUT_FAILED: "BaoStock logout failed",
    SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER: "BaoStock provider failed internally",
}

_FAILURE_KIND: dict[SourceErrorCode, BatchFailureKind] = {
    SourceErrorCode.NETWORK: BatchFailureKind.NETWORK,
    SourceErrorCode.RATE_LIMITED: BatchFailureKind.NETWORK,
    SourceErrorCode.REQUEST_REJECTED: BatchFailureKind.PARSE,
    SourceErrorCode.SCHEMA_DRIFT: BatchFailureKind.PARSE,
    SourceErrorCode.PARSE: BatchFailureKind.PARSE,
    SourceErrorCode.UNIT_UNVERIFIED: BatchFailureKind.PARSE,
    SourceErrorCode.UNEXPECTED_EMPTY: BatchFailureKind.UNEXPECTED_EMPTY,
    SourceErrorCode.PARTIAL_RESPONSE: BatchFailureKind.PARTIAL_SOURCE,
    SourceErrorCode.INTERNAL_PROVIDER: BatchFailureKind.INTERNAL,
    SourceErrorCode.BAOSTOCK_LOGIN_FAILED: BatchFailureKind.NETWORK,
    SourceErrorCode.BAOSTOCK_QUERY_FAILED: BatchFailureKind.NETWORK,
    SourceErrorCode.BAOSTOCK_SERVICE_REJECTED: BatchFailureKind.PERMISSION,
    SourceErrorCode.BAOSTOCK_TIMEOUT: BatchFailureKind.NETWORK,
    SourceErrorCode.BAOSTOCK_DISCONNECTED: BatchFailureKind.NETWORK,
    SourceErrorCode.BAOSTOCK_UNEXPECTED_EMPTY: BatchFailureKind.UNEXPECTED_EMPTY,
    SourceErrorCode.BAOSTOCK_PARTIAL_RESPONSE: BatchFailureKind.PARTIAL_SOURCE,
    SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT: BatchFailureKind.PARSE,
    SourceErrorCode.BAOSTOCK_LOGOUT_FAILED: BatchFailureKind.NETWORK,
    SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER: BatchFailureKind.INTERNAL,
}

_RETRYABLE: dict[SourceErrorCode, bool] = {
    SourceErrorCode.NETWORK: True,
    SourceErrorCode.RATE_LIMITED: True,
    SourceErrorCode.REQUEST_REJECTED: False,
    SourceErrorCode.SCHEMA_DRIFT: False,
    SourceErrorCode.PARSE: False,
    SourceErrorCode.UNIT_UNVERIFIED: False,
    SourceErrorCode.UNEXPECTED_EMPTY: False,
    SourceErrorCode.PARTIAL_RESPONSE: True,
    SourceErrorCode.INTERNAL_PROVIDER: False,
    SourceErrorCode.BAOSTOCK_LOGIN_FAILED: True,
    SourceErrorCode.BAOSTOCK_QUERY_FAILED: True,
    SourceErrorCode.BAOSTOCK_SERVICE_REJECTED: False,
    SourceErrorCode.BAOSTOCK_TIMEOUT: True,
    SourceErrorCode.BAOSTOCK_DISCONNECTED: True,
    SourceErrorCode.BAOSTOCK_UNEXPECTED_EMPTY: False,
    SourceErrorCode.BAOSTOCK_PARTIAL_RESPONSE: True,
    SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT: False,
    SourceErrorCode.BAOSTOCK_LOGOUT_FAILED: True,
    SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER: False,
}

_PARTIAL_CODES = frozenset(
    {SourceErrorCode.PARTIAL_RESPONSE.value, SourceErrorCode.BAOSTOCK_PARTIAL_RESPONSE.value}
)


class SourceAdapterError(Exception):
    """Public adapter error whose message never includes third-party text."""

    def __init__(
        self,
        code: SourceErrorCode,
        *,
        network_reason: NetworkReason | None = None,
    ) -> None:
        if not isinstance(code, SourceErrorCode):
            raise TypeError("code must be a SourceErrorCode")
        if network_reason is not None and not isinstance(network_reason, NetworkReason):
            raise TypeError("network_reason must be a NetworkReason or None")
        if code is SourceErrorCode.NETWORK:
            network_reason = network_reason or NetworkReason.UNKNOWN
        elif network_reason is not None:
            raise ValueError("network_reason is valid only for network failures")
        self.code = code
        self.network_reason = network_reason
        self.message = _MESSAGES[code]
        self.failure_kind = _FAILURE_KIND[code]
        self.retryable = _RETRYABLE[code]
        super().__init__(self.message)


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    """Return a cycle-safe exception chain without inspecting unstable messages."""
    pending = [error]
    result: list[BaseException] = []
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(current)
        for attribute in ("__cause__", "__context__", "reason", "original_error"):
            try:
                nested = getattr(current, attribute, None)
            except Exception:
                continue
            if isinstance(nested, BaseException):
                pending.append(nested)
    return tuple(result)


def _is_rate_limited(chain: tuple[BaseException, ...]) -> bool:
    for error in chain:
        if type(error).__name__ in {"RateLimitError", "TooManyRequests"}:
            return True
        try:
            response = getattr(error, "response", None)
            status_code = getattr(response, "status_code", None)
        except Exception:
            continue
        if status_code == 429:
            return True
    return False


def _network_reason(chain: tuple[BaseException, ...]) -> NetworkReason | None:
    type_names = {type(error).__name__ for error in chain}
    if "ProxyError" in type_names:
        return NetworkReason.PROXY
    if type_names & {"RemoteDisconnected", "RemoteDisconnectedError"}:
        return NetworkReason.REMOTE_DISCONNECTED
    if type_names & {"gaierror", "DNSLookupError", "NameResolutionError"}:
        return NetworkReason.DNS
    if type_names & {"SSLError", "SSLCertVerificationError", "CertificateError"}:
        return NetworkReason.TLS
    if type_names & {
        "Timeout",
        "TimeoutError",
        "ReadTimeout",
        "ConnectTimeout",
        "ReadTimeoutError",
        "ConnectTimeoutError",
    }:
        return NetworkReason.TIMEOUT
    if type_names & {
        "ConnectionError",
        "NewConnectionError",
        "ProtocolError",
        "MaxRetryError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "BrokenPipeError",
    }:
        return NetworkReason.CONNECTION
    if any(isinstance(error, OSError) for error in chain):
        return NetworkReason.UNKNOWN
    return None


def classify_source_error(error: BaseException) -> SourceAdapterError:
    """Map boundary failures without leaking unstable exception details."""
    if isinstance(error, SourceAdapterError):
        return error
    chain = _exception_chain(error)
    if _is_rate_limited(chain):
        return SourceAdapterError(SourceErrorCode.RATE_LIMITED)
    network_reason = _network_reason(chain)
    if network_reason is not None:
        return SourceAdapterError(SourceErrorCode.NETWORK, network_reason=network_reason)
    if isinstance(error, KeyError):
        return SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
    if isinstance(error, (TypeError, ValueError)):
        return SourceAdapterError(SourceErrorCode.PARSE)
    return SourceAdapterError(SourceErrorCode.INTERNAL_PROVIDER)


class VolumeUnit(StrEnum):
    """Observed upstream volume unit resolved by the locked package policy."""

    SHARE = "share"
    LOT_100_SHARES = "lot_100_shares"
    UNVERIFIED = "unverified"


class AmountUnit(StrEnum):
    """Observed upstream amount unit resolved by the locked package policy."""

    CNY_YUAN = "cny_yuan"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class SourceLineage:
    """Provider/upstream facts bound to one endpoint response."""

    provider: DataProvider
    upstream: DataUpstream
    endpoint: str
    library_version: str
    fetched_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.provider, DataProvider):
            raise TypeError("provider must be a DataProvider")
        if not isinstance(self.upstream, DataUpstream):
            raise TypeError("upstream must be a DataUpstream")
        if (self.provider, self.upstream) not in PROVIDER_UPSTREAM_PAIRS:
            raise ValueError("provider/upstream pair is not a fixed Stage 1 source")
        for field in ("endpoint", "library_version", "fetched_at"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be non-empty text")


@dataclass(frozen=True, slots=True)
class SupplierResponseContract:
    """Verified interpretation attached to one unadjusted daily-bar response."""

    dataset: DatasetKind
    contract_version: str
    required_fields: tuple[str, ...]
    volume_unit: VolumeUnit
    amount_unit: AmountUnit
    unadjusted: bool
    lineage: SourceLineage

    def __post_init__(self) -> None:
        if self.dataset is not DatasetKind.DAILY_BAR_UNADJUSTED:
            raise ValueError("AKShare production contract supports daily_bar_unadjusted only")
        if not isinstance(self.contract_version, str) or not self.contract_version.strip():
            raise ValueError("contract_version must be non-empty text")
        if not isinstance(self.required_fields, tuple) or not self.required_fields:
            raise ValueError("required_fields must be a non-empty tuple")
        if len(set(self.required_fields)) != len(self.required_fields):
            raise ValueError("required_fields must be unique")
        if not isinstance(self.volume_unit, VolumeUnit):
            raise TypeError("volume_unit must be a VolumeUnit")
        if not isinstance(self.amount_unit, AmountUnit):
            raise TypeError("amount_unit must be an AmountUnit")
        if self.unadjusted is not True:
            raise ValueError("daily bars must be explicitly unadjusted")
        if not isinstance(self.lineage, SourceLineage):
            raise TypeError("lineage must be SourceLineage")


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


def _contract_payload(contract: SupplierResponseContract) -> dict[str, object]:
    return {
        "dataset": contract.dataset.value,
        "contract_version": contract.contract_version,
        "required_fields": list(contract.required_fields),
        "volume_unit": contract.volume_unit.value,
        "amount_unit": contract.amount_unit.value,
        "unadjusted": contract.unadjusted,
        "lineage": {
            "provider": contract.lineage.provider.value,
            "upstream": contract.lineage.upstream.value,
            "endpoint": contract.lineage.endpoint,
            "library_version": contract.lineage.library_version,
            "fetched_at": contract.lineage.fetched_at,
        },
    }


def _contract_from_payload(value: object) -> SupplierResponseContract:
    if not isinstance(value, dict) or set(value) != {
        "dataset",
        "contract_version",
        "required_fields",
        "volume_unit",
        "amount_unit",
        "unadjusted",
        "lineage",
    }:
        raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
    lineage = value["lineage"]
    required = value["required_fields"]
    if not isinstance(lineage, dict) or set(lineage) != {
        "provider",
        "upstream",
        "endpoint",
        "library_version",
        "fetched_at",
    }:
        raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
    try:
        return SupplierResponseContract(
            dataset=DatasetKind(value["dataset"]),
            contract_version=str(value["contract_version"]),
            required_fields=tuple(required),
            volume_unit=VolumeUnit(value["volume_unit"]),
            amount_unit=AmountUnit(value["amount_unit"]),
            unadjusted=value["unadjusted"],
            lineage=SourceLineage(
                provider=DataProvider(lineage["provider"]),
                upstream=DataUpstream(lineage["upstream"]),
                endpoint=str(lineage["endpoint"]),
                library_version=str(lineage["library_version"]),
                fetched_at=str(lineage["fetched_at"]),
            ),
        )
    except (TypeError, ValueError) as error:
        raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT) from error


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
        value = json.loads(
            payload.decode("utf-8"),
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
        raise SourceAdapterError(SourceErrorCode.PARSE) from error
    if canonical != payload:
        raise SourceAdapterError(SourceErrorCode.PARSE)
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
        raise SourceAdapterError(SourceErrorCode.PARSE) from error
    if not isinstance(value, list) or any(not isinstance(record, dict) for record in value):
        raise SourceAdapterError(SourceErrorCode.PARSE)
    return value


@dataclass(frozen=True, slots=True)
class SupplierResponse:
    """Canonicalizable records plus immutable provider payload and lineage."""

    records: tuple[Mapping[str, object], ...]
    raw_payload: bytes
    contract: SupplierResponseContract
    complete: bool
    partial_reason: str | None
    chunk: FetchChunk
    upstream_row_count: int
    request_window: AkshareRequestWindow

    def __post_init__(self) -> None:
        normalized = tuple(MappingProxyType(dict(record)) for record in self.records)
        object.__setattr__(self, "records", normalized)
        object.__setattr__(self, "raw_payload", bytes(self.raw_payload))
        if not isinstance(self.contract, SupplierResponseContract):
            raise TypeError("contract must be a SupplierResponseContract")
        if not isinstance(self.request_window, AkshareRequestWindow):
            raise TypeError("request_window must be an AkshareRequestWindow")
        if (
            self.request_window.target_start != self.chunk.start.value
            or self.request_window.target_end != self.chunk.end.value
        ):
            raise SourceAdapterError(SourceErrorCode.PARSE)
        if (
            isinstance(self.upstream_row_count, bool)
            or not isinstance(self.upstream_row_count, int)
            or self.upstream_row_count < len(self.records)
        ):
            raise ValueError("upstream_row_count must cover all canonicalizable records")
        validate_fetch_chunk(self.chunk)
        if self.contract.dataset is not self.chunk.dataset:
            raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
        if self.complete and self.partial_reason is not None:
            raise ValueError("complete response cannot carry partial_reason")
        if not self.complete and self.partial_reason not in _PARTIAL_CODES:
            raise ValueError("partial response requires the stable partial code")
        rebuilt = self.from_raw_payload(self.raw_payload, expected_chunk=self.chunk)
        if (
            rebuilt.records != self.records
            or rebuilt.contract != self.contract
            or rebuilt.upstream_row_count != self.upstream_row_count
            or rebuilt.request_window != self.request_window
        ):
            raise SourceAdapterError(SourceErrorCode.PARSE)

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
        request_window: AkshareRequestWindow,
        upstream_row_count: int | None = None,
    ) -> SupplierResponse:
        validate_fetch_chunk(chunk)
        if not isinstance(request_window, AkshareRequestWindow):
            raise TypeError("request_window must be an AkshareRequestWindow")
        if (
            request_window.target_start != chunk.start.value
            or request_window.target_end != chunk.end.value
        ):
            raise ValueError("request_window target must match chunk")
        plain = _plain_records(records)
        upstream_rows = len(plain) if upstream_row_count is None else upstream_row_count
        if (
            isinstance(upstream_rows, bool)
            or not isinstance(upstream_rows, int)
            or upstream_rows < len(plain)
        ):
            raise ValueError("upstream_row_count must cover all canonicalizable records")
        envelope = {
            "schema_version": (
                _AKSHARE_RAW_RESPONSE_SCHEMA_VERSION
                if contract.lineage.provider is DataProvider.AKSHARE
                else _MARKET_DATA_RAW_RESPONSE_SCHEMA_VERSION
            ),
            "binding": _chunk_payload(chunk),
            "request_window": request_window.to_payload(),
            "contract": _contract_payload(contract),
            "response": {
                "complete": complete,
                "partial_reason": partial_reason,
                "records": plain,
                "upstream_row_count": upstream_rows,
            },
            "provider_payload": provider_payload,
        }
        try:
            payload = json.dumps(
                envelope,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise SourceAdapterError(SourceErrorCode.PARSE) from error
        return cls(
            records=tuple(plain),
            raw_payload=payload,
            contract=contract,
            complete=complete,
            partial_reason=partial_reason,
            chunk=chunk,
            upstream_row_count=upstream_rows,
            request_window=request_window,
        )

    @classmethod
    def from_raw_payload(
        cls,
        payload: bytes,
        *,
        expected_chunk: FetchChunk,
    ) -> SupplierResponse:
        value = _load_canonical_json(payload)
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "binding",
            "request_window",
            "contract",
            "response",
            "provider_payload",
        }:
            raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
        if value["schema_version"] not in {
            _AKSHARE_RAW_RESPONSE_SCHEMA_VERSION,
            _MARKET_DATA_RAW_RESPONSE_SCHEMA_VERSION,
        }:
            raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
        if value["binding"] != _chunk_payload(expected_chunk):
            raise SourceAdapterError(SourceErrorCode.PARSE)
        try:
            request_window = AkshareRequestWindow.from_payload(value["request_window"])
        except (TypeError, ValueError) as error:
            raise SourceAdapterError(SourceErrorCode.PARSE) from error
        if (
            request_window.target_start != expected_chunk.start.value
            or request_window.target_end != expected_chunk.end.value
        ):
            raise SourceAdapterError(SourceErrorCode.PARSE)
        response = value["response"]
        if not isinstance(response, dict) or set(response) != {
            "complete",
            "partial_reason",
            "records",
            "upstream_row_count",
        }:
            raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
        records = response["records"]
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise SourceAdapterError(SourceErrorCode.PARSE)
        complete = response["complete"]
        partial_reason = response["partial_reason"]
        upstream_row_count = response["upstream_row_count"]
        if not isinstance(complete, bool):
            raise SourceAdapterError(SourceErrorCode.PARSE)
        if partial_reason is not None and not isinstance(partial_reason, str):
            raise SourceAdapterError(SourceErrorCode.PARSE)
        if complete and partial_reason is not None:
            raise SourceAdapterError(SourceErrorCode.PARSE)
        if not complete and partial_reason not in _PARTIAL_CODES:
            raise SourceAdapterError(SourceErrorCode.PARSE)
        if (
            isinstance(upstream_row_count, bool)
            or not isinstance(upstream_row_count, int)
            or upstream_row_count < len(records)
        ):
            raise SourceAdapterError(SourceErrorCode.PARSE)
        obj = object.__new__(cls)
        object.__setattr__(obj, "records", tuple(MappingProxyType(record) for record in records))
        object.__setattr__(obj, "raw_payload", bytes(payload))
        object.__setattr__(obj, "contract", _contract_from_payload(value["contract"]))
        if (
            value["schema_version"] == _AKSHARE_RAW_RESPONSE_SCHEMA_VERSION
            and obj.contract.lineage.provider is not DataProvider.AKSHARE
        ):
            raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
        object.__setattr__(obj, "complete", complete)
        object.__setattr__(obj, "partial_reason", partial_reason)
        object.__setattr__(obj, "chunk", expected_chunk)
        object.__setattr__(obj, "upstream_row_count", upstream_row_count)
        object.__setattr__(obj, "request_window", request_window)
        return obj


__all__ = [
    "AmountUnit",
    "NetworkReason",
    "SourceAdapterError",
    "SourceErrorCode",
    "SourceLineage",
    "SupplierResponse",
    "SupplierResponseContract",
    "VolumeUnit",
    "classify_source_error",
]
