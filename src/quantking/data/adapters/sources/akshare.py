"""Lazy, one-call AKShare daily-bar adapters for the fixed B/C upstreams.

Every vendor call runs in a controlled subprocess with one parent-enforced
wall-clock hard timeout and zero application-layer retry. Canonical
``pre_close`` is always derived from the previous row ``close`` of the
complete unadjusted upstream series (including the 15-day lookback context);
any vendor reference pre_close stays in the Raw provider payload only.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import ModuleType

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
from quantking.data.adapters.sources.parameters import DailyBarRequest
from quantking.data.adapters.sources.registry import SourceSpec, source_spec
from quantking.data.adapters.sources.request_window import AkshareRequestWindow
from quantking.data.adapters.sources.units import (
    AKSHARE_UNIT_POLICY_VERSION,
    ReturnedUnitPolicy,
    resolve_returned_units,
)
from quantking.data.incremental import FetchChunk, validate_fetch_chunk
from quantking.data.models import DataProvider, DataUpstream

AKSHARE_SESSION_TIMEOUT_SECONDS = 60.0
AKSHARE_ADAPTER_VERSION = "akshare-daily-bar.v2"

_COLUMN_MAP: dict[DataUpstream, Mapping[str, str]] = {
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


def _load_module() -> ModuleType:
    # Import occurs only inside an explicit adapter call. Importing QuantKing
    # remains offline; the vendor call itself runs in a subprocess.
    import importlib

    return importlib.import_module("akshare")


def _date_value(value: object) -> str:
    if not isinstance(value, str):
        raise SourceAdapterError(SourceErrorCode.PARSE)
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value[:10], pattern).date().isoformat()
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


def _library_version(module: object) -> str:
    try:
        version = vars(module).get("__version__")
    except TypeError as error:
        raise SourceAdapterError(SourceErrorCode.INTERNAL_PROVIDER) from error
    if not isinstance(version, str) or not version.strip():
        raise SourceAdapterError(SourceErrorCode.INTERNAL_PROVIDER)
    return version


@dataclass(frozen=True, slots=True)
class AkshareSessionEnvelope:
    """Validated result of one controlled worker session."""

    rows: tuple[Mapping[str, object], ...]
    library_version: str
    upstream_row_count: int


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


class AkshareSessionRunner:
    """Run each vendor endpoint call in a new process with one hard timeout."""

    def __init__(
        self,
        *,
        timeout_seconds: float = AKSHARE_SESSION_TIMEOUT_SECONDS,
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
                "quantking.data.adapters.sources.akshare_worker",
            )
        )
        if not self._command or any(
            not isinstance(item, str) or not item for item in self._command
        ):
            raise ValueError("command must contain non-empty strings")

    def run(self, request: DailyBarRequest, upstream: DataUpstream) -> AkshareSessionEnvelope:
        if not isinstance(request, DailyBarRequest):
            raise TypeError("request must be a DailyBarRequest")
        payload = json.dumps(
            {
                "upstream": upstream.value,
                "symbol": request.symbol,
                "start_date": request.start_date.isoformat(),
                "end_date": request.end_date.isoformat(),
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
            raise SourceAdapterError(SourceErrorCode.NETWORK) from error
        except OSError as error:
            raise SourceAdapterError(SourceErrorCode.INTERNAL_PROVIDER) from error
        if completed.returncode != 0:
            raise SourceAdapterError(SourceErrorCode.INTERNAL_PROVIDER)
        try:
            value = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise SourceAdapterError(SourceErrorCode.INTERNAL_PROVIDER) from error
        if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
            raise SourceAdapterError(SourceErrorCode.INTERNAL_PROVIDER)
        if not value["ok"]:
            error_code = value.get("error_code")
            if not isinstance(error_code, str):
                raise SourceAdapterError(SourceErrorCode.INTERNAL_PROVIDER)
            try:
                stable_code = SourceErrorCode(error_code)
            except ValueError as error:
                raise SourceAdapterError(SourceErrorCode.INTERNAL_PROVIDER) from error
            raise SourceAdapterError(stable_code)
        rows_value = value.get("rows")
        library_version = value.get("library_version")
        row_count = value.get("row_count")
        if (
            not isinstance(rows_value, list)
            or any(not isinstance(item, dict) for item in rows_value)
            or not isinstance(library_version, str)
            or not library_version
            or isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or row_count < len(rows_value)
        ):
            raise SourceAdapterError(SourceErrorCode.INTERNAL_PROVIDER)
        return AkshareSessionEnvelope(
            rows=tuple({str(key): item for key, item in row.items()} for row in rows_value),
            library_version=library_version,
            upstream_row_count=row_count,
        )


class AkshareDailyBarAdapter:
    """Bind one AKShare endpoint to one independently traceable upstream."""

    def __init__(
        self,
        upstream: DataUpstream,
        *,
        runner: AkshareSessionRunner | None = None,
        module_loader: Callable[[], object] = _load_module,
        clock: Callable[[], UtcTimestamp] = utc_now,
    ) -> None:
        self.spec: SourceSpec = source_spec(upstream)
        self._runner = runner or AkshareSessionRunner()
        self._module_loader = module_loader
        self._clock = clock
        self._module: object | None = None

    def library_version(self) -> str:
        """Load the provider locally without making an upstream request."""
        if self._module is None:
            try:
                self._module = self._module_loader()
            except Exception as error:
                raise classify_source_error(error) from error
        return _library_version(self._module)

    def fetch(self, request: DailyBarRequest) -> AkshareEndpointResponse:
        """Make exactly one endpoint call through the worker, with no retry."""
        if not isinstance(request, DailyBarRequest):
            raise TypeError("request must be a DailyBarRequest")
        request_window = request.request_window
        library_version = self.library_version()
        unit_policy = resolve_returned_units(
            self.spec.upstream,
            request.symbol,
            library_version,
        )
        if not unit_policy.verified:
            raise SourceAdapterError(SourceErrorCode.UNIT_UNVERIFIED)
        envelope = self._runner.run(request, self.spec.upstream)
        if envelope.library_version != library_version:
            raise SourceAdapterError(SourceErrorCode.INTERNAL_PROVIDER)
        provider_rows = envelope.rows
        fetched_at = self._clock()
        if not isinstance(fetched_at, UtcTimestamp):
            raise TypeError("clock must return UtcTimestamp")
        if not provider_rows:
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
        available = set(provider_rows[0])
        if any(column not in available for column in expected.values()):
            raise SourceAdapterError(SourceErrorCode.SCHEMA_DRIFT)
        parsed: list[dict[str, object]] = []
        for row in provider_rows:
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
        # Canonical pre_close is ALWAYS the prior unadjusted close across the
        # complete upstream series; vendor reference pre_close stays in Raw.
        canonical: list[dict[str, object]] = []
        target_row_count = 0
        previous_close: float | None = None
        for row in parsed:
            in_target = (
                request.start_date.isoformat() <= str(row["date"]) <= request.end_date.isoformat()
            )
            if in_target:
                target_row_count += 1
                if previous_close is not None:
                    canonical.append({**row, "pre_close": previous_close})
            close_value = row["close"]
            if isinstance(close_value, bool) or not isinstance(close_value, (int, float)):
                raise SourceAdapterError(SourceErrorCode.PARSE)
            previous_close = float(close_value)
        complete = len(canonical) == target_row_count
        partial_reason = None if complete else SourceErrorCode.PARTIAL_RESPONSE.value
        raw_payload = json.dumps(
            list(provider_rows),
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
            provider_payload=tuple(provider_rows),
            raw_payload=raw_payload,
            row_count=envelope.upstream_row_count,
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
                contract_version=AKSHARE_ADAPTER_VERSION,
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
    "AKSHARE_ADAPTER_VERSION",
    "AKSHARE_SESSION_TIMEOUT_SECONDS",
    "AKSHARE_UNIT_POLICY_VERSION",
    "AkshareDailyBarAdapter",
    "AkshareEndpointResponse",
    "AkshareMarketDataGateway",
    "AkshareSessionEnvelope",
    "AkshareSessionRunner",
    "ReturnedUnitPolicy",
]
