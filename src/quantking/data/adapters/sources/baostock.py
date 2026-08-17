"""Process-isolated BaoStock daily-bar adapter for the fixed Stage 1 A source."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import version

from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.adapters.sources.baostock_worker import (
    BAOSTOCK_DISTRIBUTION_VERSION,
    BAOSTOCK_FIELDS,
)
from quantking.data.adapters.sources.contracts import (
    AmountUnit,
    SourceAdapterError,
    SourceErrorCode,
    SourceLineage,
    SupplierResponse,
    SupplierResponseContract,
    VolumeUnit,
)
from quantking.data.adapters.sources.parameters import DailyBarRequest
from quantking.data.adapters.sources.units import BAOSTOCK_UNIT_POLICY_VERSION
from quantking.data.incremental import FetchChunk, validate_fetch_chunk
from quantking.data.models import DataProvider, DatasetKind, DataUpstream

BAOSTOCK_ENDPOINT = "query_history_k_data_plus"
BAOSTOCK_ADAPTER_VERSION = "baostock-daily-bar.v1"
BAOSTOCK_SESSION_TIMEOUT_SECONDS = 20.0

_FIELD_INDEX = {field: index for index, field in enumerate(BAOSTOCK_FIELDS)}


@dataclass(frozen=True, slots=True)
class BaostockSessionResult:
    rows: tuple[tuple[str, ...], ...]
    distribution_version: str
    runtime_version: str
    login_messages: int
    query_messages: int
    logout_messages: int
    complete: bool
    partial_reason: str | None


@dataclass(frozen=True, slots=True)
class BaostockEndpointResponse:
    request: DailyBarRequest
    lineage: SourceLineage
    records: tuple[Mapping[str, object], ...]
    provider_payload: Mapping[str, object]
    upstream_row_count: int
    complete: bool
    partial_reason: str | None


class BaostockSessionRunner:
    """Run each vendor session in a new process with one parent-enforced timeout."""

    def __init__(
        self,
        *,
        timeout_seconds: float = BAOSTOCK_SESSION_TIMEOUT_SECONDS,
        command: Sequence[str] | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        self.timeout_seconds = float(timeout_seconds)
        self._command = (
            tuple(command)
            if command is not None
            else (
                sys.executable,
                "-I",
                "-m",
                "quantking.data.adapters.sources.baostock_worker",
            )
        )
        if not self._command or any(
            not isinstance(item, str) or not item for item in self._command
        ):
            raise ValueError("command must contain non-empty strings")

    def run(self, request: DailyBarRequest) -> BaostockSessionResult:
        if not isinstance(request, DailyBarRequest):
            raise TypeError("request must be a DailyBarRequest")
        window = request.request_window
        exchange, digits = request.symbol.split(".")[1].lower(), request.symbol[:6]
        payload = json.dumps(
            {
                "code": f"{exchange}.{digits}",
                "start_date": window.upstream_start.isoformat(),
                "end_date": window.upstream_end.isoformat(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            completed = subprocess.run(
                self._command,
                input=payload,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise SourceAdapterError(SourceErrorCode.BAOSTOCK_TIMEOUT) from error
        except OSError as error:
            raise SourceAdapterError(SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER) from error
        if completed.returncode != 0:
            raise SourceAdapterError(SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER)
        try:
            value = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise SourceAdapterError(SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER) from error
        if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
            raise SourceAdapterError(SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER)
        error_code = value.get("error_code")
        if not isinstance(error_code, str):
            raise SourceAdapterError(SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER)
        partial = error_code == SourceErrorCode.BAOSTOCK_PARTIAL_RESPONSE.value
        if not value["ok"] and not partial:
            try:
                stable_code = SourceErrorCode(error_code)
            except ValueError as error:
                raise SourceAdapterError(SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER) from error
            if not stable_code.value.startswith("BAOSTOCK_"):
                raise SourceAdapterError(SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER)
            raise SourceAdapterError(stable_code)
        raw_rows = value.get("rows")
        if not isinstance(raw_rows, list):
            raise SourceAdapterError(SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT)
        rows: list[tuple[str, ...]] = []
        for row in raw_rows:
            if (
                not isinstance(row, list)
                or len(row) != len(BAOSTOCK_FIELDS)
                or any(not isinstance(item, str) for item in row)
            ):
                raise SourceAdapterError(SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT)
            rows.append(tuple(row))
        distribution_version = value.get("distribution_version", BAOSTOCK_DISTRIBUTION_VERSION)
        runtime_version = value.get("runtime_version", "")
        login_messages = value.get("login_messages")
        query_messages = value.get("query_messages")
        logout_messages = value.get("logout_messages")
        if partial:
            distribution_version = BAOSTOCK_DISTRIBUTION_VERSION
            runtime_version = "00.9.30"
        if distribution_version != BAOSTOCK_DISTRIBUTION_VERSION or runtime_version != "00.9.30":
            raise SourceAdapterError(SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER)
        if isinstance(login_messages, bool) or not isinstance(login_messages, int):
            raise SourceAdapterError(SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER)
        if isinstance(query_messages, bool) or not isinstance(query_messages, int):
            raise SourceAdapterError(SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER)
        if isinstance(logout_messages, bool) or not isinstance(logout_messages, int):
            raise SourceAdapterError(SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER)
        if login_messages != 1 or query_messages < 1 or logout_messages != 1:
            raise SourceAdapterError(SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER)
        return BaostockSessionResult(
            rows=tuple(rows),
            distribution_version=distribution_version,
            runtime_version=runtime_version,
            login_messages=login_messages,
            query_messages=query_messages,
            logout_messages=logout_messages,
            complete=not partial,
            partial_reason=error_code if partial else None,
        )


def _number(value: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise SourceAdapterError(SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT) from error
    if not math.isfinite(result):
        raise SourceAdapterError(SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT)
    return result


def _volume(value: str) -> int:
    number = _number(value)
    if not number.is_integer() or number < 0:
        raise SourceAdapterError(SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT)
    return int(number)


def _date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as error:
        raise SourceAdapterError(SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT) from error


class BaostockDailyBarAdapter:
    """Parse one isolated BaoStock session without importing the vendor in this process."""

    def __init__(
        self,
        *,
        runner: BaostockSessionRunner | None = None,
        clock: Callable[[], UtcTimestamp] = utc_now,
    ) -> None:
        self._runner = runner or BaostockSessionRunner()
        self._clock = clock

    def library_version(self) -> str:
        installed = version("baostock")
        if installed != BAOSTOCK_DISTRIBUTION_VERSION:
            raise SourceAdapterError(SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER)
        return installed

    def fetch(self, request: DailyBarRequest) -> BaostockEndpointResponse:
        result = self._runner.run(request)
        parsed: list[tuple[dict[str, object], Mapping[str, str]]] = []
        expected_code = f"{request.symbol[7:].lower()}.{request.symbol[:6]}"
        for raw_row in result.rows:
            provider_row = dict(zip(BAOSTOCK_FIELDS, raw_row, strict=True))
            if provider_row["code"].lower() != expected_code:
                raise SourceAdapterError(SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT)
            if provider_row["adjustflag"] != "3":
                raise SourceAdapterError(SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT)
            if provider_row["tradestatus"] not in {"0", "1"} or provider_row["isST"] not in {
                "0",
                "1",
            }:
                raise SourceAdapterError(SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT)
            if provider_row["tradestatus"] == "0":
                # Suspended-day placeholder (K4C-R3-R1 diagnostic, 2026-08-16):
                # BaoStock fills OHLC with the last close and leaves
                # volume/amount empty; such a row carries no bar and must not
                # surface as a held row or as schema drift.
                if provider_row["volume"] == "" and provider_row["amount"] == "":
                    continue
                raise SourceAdapterError(SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT)
            if provider_row["volume"] == "" or provider_row["amount"] == "":
                raise SourceAdapterError(SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT)
            parsed.append(
                (
                    {
                        "stock_code": request.symbol,
                        "date": _date(provider_row["date"]),
                        "open": _number(provider_row["open"]),
                        "high": _number(provider_row["high"]),
                        "low": _number(provider_row["low"]),
                        "close": _number(provider_row["close"]),
                        "volume": _volume(provider_row["volume"]),
                        "amount": _number(provider_row["amount"]),
                    },
                    provider_row,
                )
            )
        parsed.sort(key=lambda pair: str(pair[0]["date"]))
        if len({str(row["date"]) for row, _raw in parsed}) != len(parsed):
            raise SourceAdapterError(SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT)
        window = request.request_window
        if any(
            not window.upstream_start.isoformat()
            <= str(row["date"])
            <= window.upstream_end.isoformat()
            for row, _raw in parsed
        ):
            raise SourceAdapterError(SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT)
        canonical: list[dict[str, object]] = []
        previous_close: float | None = None
        target_rows = 0
        for row, _provider_row in parsed:
            in_target = (
                request.start_date.isoformat() <= str(row["date"]) <= request.end_date.isoformat()
            )
            if in_target:
                target_rows += 1
                if previous_close is not None:
                    canonical.append({**row, "pre_close": previous_close})
            close_value = row["close"]
            if isinstance(close_value, bool) or not isinstance(close_value, (int, float)):
                raise SourceAdapterError(SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT)
            previous_close = float(close_value)
        complete = result.complete and len(canonical) == target_rows
        partial_reason = None if complete else SourceErrorCode.BAOSTOCK_PARTIAL_RESPONSE.value
        fetched_at = self._clock()
        if not isinstance(fetched_at, UtcTimestamp):
            raise TypeError("clock must return UtcTimestamp")
        return BaostockEndpointResponse(
            request=request,
            records=tuple(canonical),
            lineage=SourceLineage(
                provider=DataProvider.BAOSTOCK,
                upstream=DataUpstream.BAOSTOCK,
                endpoint=BAOSTOCK_ENDPOINT,
                library_version=result.distribution_version,
                fetched_at=fetched_at.to_iso(),
            ),
            complete=complete,
            partial_reason=partial_reason,
            provider_payload={
                "fields": list(BAOSTOCK_FIELDS),
                "rows": [list(row) for row in result.rows],
                "response_metadata": {
                    "distribution_version": result.distribution_version,
                    "runtime_version": result.runtime_version,
                    "login_messages": result.login_messages,
                    "query_messages": result.query_messages,
                    "logout_messages": result.logout_messages,
                    "error_code": "0" if complete else partial_reason,
                    "unit_policy_version": BAOSTOCK_UNIT_POLICY_VERSION,
                },
            },
            upstream_row_count=len(result.rows),
        )


class BaostockMarketDataGateway:
    def __init__(self, adapter: BaostockDailyBarAdapter) -> None:
        if not isinstance(adapter, BaostockDailyBarAdapter):
            raise TypeError("adapter must be a BaostockDailyBarAdapter")
        self.adapter = adapter

    @property
    def provider(self) -> DataProvider:
        return DataProvider.BAOSTOCK

    @property
    def upstream(self) -> DataUpstream:
        return DataUpstream.BAOSTOCK

    @property
    def endpoint(self) -> str:
        return BAOSTOCK_ENDPOINT

    def library_version(self) -> str:
        return self.adapter.library_version()

    def fetch(self, chunk: FetchChunk) -> SupplierResponse:
        validate_fetch_chunk(chunk)
        if chunk.dataset is not DatasetKind.DAILY_BAR_UNADJUSTED or len(chunk.scope) != 1:
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
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                contract_version=BAOSTOCK_ADAPTER_VERSION,
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
                volume_unit=VolumeUnit.SHARE,
                amount_unit=AmountUnit.CNY_YUAN,
                unadjusted=True,
                lineage=result.lineage,
            ),
            complete=result.complete,
            partial_reason=result.partial_reason,
            provider_payload=result.provider_payload,
            request_window=result.request.request_window,
            upstream_row_count=result.upstream_row_count,
        )


__all__ = [
    "BAOSTOCK_ADAPTER_VERSION",
    "BAOSTOCK_ENDPOINT",
    "BAOSTOCK_SESSION_TIMEOUT_SECONDS",
    "BAOSTOCK_UNIT_POLICY_VERSION",
    "BaostockDailyBarAdapter",
    "BaostockEndpointResponse",
    "BaostockMarketDataGateway",
    "BaostockSessionResult",
    "BaostockSessionRunner",
]
