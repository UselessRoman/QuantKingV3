"""Lazy, one-call AKShare daily-bar adapters for three independent upstreams."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import ModuleType
from typing import Protocol

from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.adapters.sources.contracts import (
    AmountUnit,
    SourceAdapterError,
    SourceErrorCode,
    SourceLineage,
    SupplierResponse,
    SupplierResponseContract,
    VolumeUnit,
    classify_source_error,
)
from quantking.data.adapters.sources.parameters import DailyBarRequest, endpoint_parameters
from quantking.data.adapters.sources.registry import SourceSpec, source_spec
from quantking.data.adapters.sources.request_window import AkshareRequestWindow
from quantking.data.adapters.sources.units import resolve_returned_units
from quantking.data.incremental import FetchChunk, validate_fetch_chunk
from quantking.data.models import DataProvider, DataUpstream


class DataFrameLike(Protocol):
    @property
    def columns(self) -> object: ...

    @property
    def empty(self) -> object: ...

    def to_dict(self, orient: str) -> object: ...


@dataclass(frozen=True, slots=True)
class AkshareEndpointResponse:
    """One immutable endpoint result before the Raw envelope is assembled."""

    request: DailyBarRequest
    request_window: AkshareRequestWindow
    lineage: SourceLineage
    records: tuple[Mapping[str, object], ...]
    provider_payload: tuple[Mapping[str, object], ...]
    raw_payload: bytes
    row_count: int
    content_sha256: str
    volume_unit: VolumeUnit
    amount_unit: AmountUnit
    unit_policy_version: str
    complete: bool
    partial_reason: str | None

    @property
    def units_verified(self) -> bool:
        return (
            self.volume_unit is not VolumeUnit.UNVERIFIED
            and self.amount_unit is not AmountUnit.UNVERIFIED
        )


_COLUMN_MAP: dict[DataUpstream, Mapping[str, str]] = {
    DataUpstream.EASTMONEY: {
        "trade_date": "日期",
        "open": "开盘",
        "high": "最高",
        "low": "最低",
        "close": "收盘",
        "volume": "成交量",
        "amount": "成交额",
    },
    DataUpstream.TENCENT: {
        "trade_date": "date",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
        "amount": "amount",
    },
    DataUpstream.SINA: {
        "trade_date": "date",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
        "amount": "amount",
    },
}

_PRE_CLOSE_COLUMN: dict[DataUpstream, str] = {
    DataUpstream.EASTMONEY: "昨收",
    DataUpstream.TENCENT: "pre_close",
    DataUpstream.SINA: "pre_close",
}


def _default_loader() -> ModuleType:
    # Import occurs only inside an explicit adapter call. Importing QuantKing remains offline.
    return importlib.import_module("akshare")


def _plain_scalar(value: object) -> object:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise SourceAdapterError(SourceErrorCode.PARSE)
        return value
    item = getattr(value, "item", None)
    if callable(item):
        return _plain_scalar(item())
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        rendered = isoformat()
        if isinstance(rendered, str):
            return rendered[:10]
    raise SourceAdapterError(SourceErrorCode.PARSE)


def _records_from_frame(frame: object) -> tuple[dict[str, object], ...]:
    if not hasattr(frame, "columns") or not hasattr(frame, "to_dict"):
        raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
    try:
        raw = frame.to_dict(orient="records")
    except Exception as error:
        raise classify_source_error(error) from error
    if not isinstance(raw, list) or any(not isinstance(row, Mapping) for row in raw):
        raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
    return tuple({str(key): _plain_scalar(value) for key, value in row.items()} for row in raw)


def _date_value(value: object) -> str:
    plain = _plain_scalar(value)
    if not isinstance(plain, str):
        raise SourceAdapterError(SourceErrorCode.PARSE)
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(plain[:10], pattern).date().isoformat()
        except ValueError:
            continue
    raise SourceAdapterError(SourceErrorCode.PARSE)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceAdapterError(SourceErrorCode.PARSE)
    result = float(value)
    if not math.isfinite(result):
        raise SourceAdapterError(SourceErrorCode.PARSE)
    return result


def _volume(value: object) -> int:
    result = _number(value)
    if not result.is_integer():
        raise SourceAdapterError(SourceErrorCode.PARSE)
    return int(result)


def _stable_exception(error: BaseException) -> SourceAdapterError:
    # The classifier walks cause/context and known exception-valued attributes.
    return classify_source_error(error)


def _library_version(module: object) -> str:
    try:
        version = vars(module).get("__version__")
    except TypeError as error:
        raise SourceAdapterError(SourceErrorCode.INTERNAL_PROVIDER) from error
    if not isinstance(version, str) or not version.strip():
        raise SourceAdapterError(SourceErrorCode.INTERNAL_PROVIDER)
    return version


class AkshareDailyBarAdapter:
    """Bind one AKShare endpoint to one independently traceable upstream."""

    def __init__(
        self,
        upstream: DataUpstream,
        *,
        module_loader: Callable[[], object] = _default_loader,
        clock: Callable[[], UtcTimestamp] = utc_now,
    ) -> None:
        self.spec: SourceSpec = source_spec(upstream)
        self._module_loader = module_loader
        self._clock = clock
        self._module: object | None = None

    def library_version(self) -> str:
        """Load the provider locally without making an upstream request."""
        module = self._load_module()
        return _library_version(module)

    def _load_module(self) -> object:
        if self._module is None:
            try:
                self._module = self._module_loader()
            except Exception as error:
                mapped = _stable_exception(error)
                raise mapped from error
        return self._module

    def fetch(self, request: DailyBarRequest) -> AkshareEndpointResponse:
        """Make exactly one endpoint call, with no fallback and no retry."""
        if not isinstance(request, DailyBarRequest):
            raise TypeError("request must be a DailyBarRequest")
        request_window = request.request_window
        try:
            module = self._load_module()
            endpoint = getattr(module, self.spec.endpoint)
            library_version = _library_version(module)
            unit_policy = resolve_returned_units(
                self.spec.upstream,
                request.symbol,
                library_version,
            )
            if not unit_policy.verified:
                raise SourceAdapterError(SourceErrorCode.UNIT_UNVERIFIED)
            frame = endpoint(**endpoint_parameters(self.spec.upstream, request))
        except Exception as error:
            mapped = _stable_exception(error)
            raise mapped from error
        rows = _records_from_frame(frame)
        if not rows:
            fetched_at = self._clock()
            if not isinstance(fetched_at, UtcTimestamp):
                raise TypeError("clock must return UtcTimestamp")
            raw_payload = b"[]"
            return AkshareEndpointResponse(
                request=request,
                request_window=request_window,
                lineage=SourceLineage(
                    provider=self.spec.provider,
                    upstream=self.spec.upstream,
                    endpoint=self.spec.endpoint,
                    library_version=library_version,
                    fetched_at=fetched_at.to_iso(),
                ),
                records=(),
                provider_payload=(),
                raw_payload=raw_payload,
                row_count=0,
                content_sha256=hashlib.sha256(raw_payload).hexdigest(),
                volume_unit=unit_policy.volume_unit,
                amount_unit=unit_policy.amount_unit,
                unit_policy_version=unit_policy.policy_version,
                complete=True,
                partial_reason=None,
            )
        expected = _COLUMN_MAP[self.spec.upstream]
        available = set(rows[0])
        if any(column not in available for column in expected.values()):
            raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
        parsed: list[dict[str, object]] = []
        for row in rows:
            if set(row) != available:
                raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
            parsed.append(
                {
                    "stock_code": request.symbol,
                    "date": _date_value(row[expected["trade_date"]]),
                    "open": _number(row[expected["open"]]),
                    "high": _number(row[expected["high"]]),
                    "low": _number(row[expected["low"]]),
                    "close": _number(row[expected["close"]]),
                    "volume": _volume(row[expected["volume"]]),
                    "amount": _number(row[expected["amount"]]),
                }
            )
        parsed.sort(key=lambda row: (str(row["stock_code"]), str(row["date"])))
        if any(
            not (
                request_window.upstream_start.isoformat()
                <= str(row["date"])
                <= request_window.upstream_end.isoformat()
            )
            for row in parsed
        ):
            raise SourceAdapterError(SourceErrorCode.REQUEST_REJECTED)
        pre_close_column = _PRE_CLOSE_COLUMN[self.spec.upstream]
        direct_pre_close = pre_close_column in available
        canonical: list[dict[str, object]] = []
        target_row_count = 0
        previous_close: float | None = None
        for row, provider_row in zip(
            parsed,
            sorted(rows, key=lambda item: _date_value(item[expected["trade_date"]])),
            strict=True,
        ):
            in_target = (
                request.start_date.isoformat() <= str(row["date"]) <= request.end_date.isoformat()
            )
            if in_target:
                target_row_count += 1
            if direct_pre_close:
                pre_close = _number(provider_row[pre_close_column])
            elif previous_close is not None:
                pre_close = previous_close
            else:
                previous_close = _number(row["close"])
                continue
            if in_target:
                canonical.append({**row, "pre_close": pre_close})
            previous_close = _number(row["close"])
        complete = len(canonical) == target_row_count
        partial_reason = None if complete else SourceErrorCode.PARTIAL_RESPONSE.value
        fetched_at = self._clock()
        if not isinstance(fetched_at, UtcTimestamp):
            raise TypeError("clock must return UtcTimestamp")
        raw_payload = json.dumps(
            rows,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return AkshareEndpointResponse(
            request=request,
            request_window=request_window,
            lineage=SourceLineage(
                provider=self.spec.provider,
                upstream=self.spec.upstream,
                endpoint=self.spec.endpoint,
                library_version=library_version,
                fetched_at=fetched_at.to_iso(),
            ),
            records=tuple(canonical),
            provider_payload=tuple(rows),
            raw_payload=raw_payload,
            row_count=len(rows),
            content_sha256=hashlib.sha256(raw_payload).hexdigest(),
            volume_unit=unit_policy.volume_unit,
            amount_unit=unit_policy.amount_unit,
            unit_policy_version=unit_policy.policy_version,
            complete=complete,
            partial_reason=partial_reason,
        )


class AkshareMarketDataGateway:
    """Translate one single-symbol EOD chunk into a Raw-bound response."""

    def __init__(self, adapter: AkshareDailyBarAdapter) -> None:
        if not isinstance(adapter, AkshareDailyBarAdapter):
            raise TypeError("adapter must be an AkshareDailyBarAdapter")
        self.adapter = adapter

    @property
    def provider(self) -> DataProvider:
        return DataProvider.AKSHARE

    @property
    def upstream(self) -> DataUpstream:
        return self.adapter.spec.upstream

    @property
    def endpoint(self) -> str:
        return self.adapter.spec.endpoint

    def library_version(self) -> str:
        return self.adapter.library_version()

    def fetch(self, chunk: FetchChunk) -> SupplierResponse:
        validate_fetch_chunk(chunk)
        if chunk.dataset is not self.adapter.spec.dataset or len(chunk.scope) != 1:
            raise SourceAdapterError(SourceErrorCode.REQUEST_REJECTED)
        result = self.adapter.fetch(
            DailyBarRequest(
                symbol=chunk.scope[0],
                start_date=chunk.start.value,
                end_date=chunk.end.value,
            )
        )
        return SupplierResponse.create_bound(
            chunk=chunk,
            records=result.records,
            contract=SupplierResponseContract(
                dataset=chunk.dataset,
                contract_version="akshare-daily-bar.v1",
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
                volume_unit=result.volume_unit,
                amount_unit=result.amount_unit,
                unadjusted=True,
                lineage=result.lineage,
            ),
            complete=result.complete,
            partial_reason=result.partial_reason,
            provider_payload={
                "records": list(result.provider_payload),
                "row_count": result.row_count,
                "content_sha256": result.content_sha256,
                "unit_policy_version": result.unit_policy_version,
                "request": {
                    "symbol": result.request.symbol,
                    "target_start": result.request_window.target_start.isoformat(),
                    "target_end": result.request_window.target_end.isoformat(),
                    "upstream_start": result.request_window.upstream_start.isoformat(),
                    "upstream_end": result.request_window.upstream_end.isoformat(),
                    "lookback_days": result.request_window.lookback_days,
                },
            },
            request_window=result.request_window,
            upstream_row_count=result.row_count,
        )


__all__ = [
    "AkshareDailyBarAdapter",
    "AkshareEndpointResponse",
    "AkshareMarketDataGateway",
]
