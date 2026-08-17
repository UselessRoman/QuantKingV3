"""Isolated AKShare endpoint worker; imported only by an explicit adapter call.

Each production vendor call runs in a fresh subprocess so the parent can
enforce one wall-clock hard timeout over the whole endpoint invocation (the
Sina endpoint exposes no HTTP timeout parameter and the Tencent endpoint
issues several internal HTTP requests). The worker owns exactly one endpoint
call, performs no retry and no fallback, and emits a single canonical JSON
envelope on stdout. ``run_request`` is the pure in-process core used by unit
tests with a fake module loader; ``main`` is the subprocess entry point.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import math
import sys
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Protocol, cast

from quantking.data.adapters.sources.contracts import SourceErrorCode, classify_source_error
from quantking.data.adapters.sources.parameters import DailyBarRequest, endpoint_parameters
from quantking.data.models import DataUpstream


class _EndpointFrame(Protocol):
    @property
    def columns(self) -> object: ...

    @property
    def empty(self) -> object: ...

    def to_dict(self, orient: str) -> object: ...


class _AkshareModule(Protocol):
    __name__: str
    __version__: str

    def stock_zh_a_hist_tx(self, **kwargs: object) -> _EndpointFrame: ...

    def stock_zh_a_daily(self, **kwargs: object) -> _EndpointFrame: ...


_ENDPOINT_NAMES = {
    DataUpstream.TENCENT: "stock_zh_a_hist_tx",
    DataUpstream.SINA: "stock_zh_a_daily",
}


def _load_akshare() -> _AkshareModule:
    return cast(_AkshareModule, importlib.import_module("akshare"))


def _failure(code: str) -> dict[str, object]:
    return {
        "ok": False,
        "error_code": code,
        "rows": [],
        "library_version": "",
        "row_count": 0,
    }


def _plain_scalar(value: object) -> object:
    """Render one provider scalar as a JSON-safe value or raise TypeError."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise TypeError("non-finite float")
        return value
    item = getattr(value, "item", None)
    if callable(item):
        return _plain_scalar(item())
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        rendered = isoformat()
        if isinstance(rendered, str):
            return rendered[:10]
    raise TypeError("unrenderable scalar")


def _rows_from_frame(frame: object) -> tuple[list[dict[str, object]], int]:
    if not hasattr(frame, "columns") or not hasattr(frame, "to_dict"):
        raise TypeError("frame schema drift")
    raw = frame.to_dict(orient="records")
    if not isinstance(raw, list) or any(not isinstance(row, Mapping) for row in raw):
        raise TypeError("frame schema drift")
    rows: list[dict[str, object]] = []
    for row in raw:
        rows.append({str(key): _plain_scalar(value) for key, value in row.items()})
    return rows, len(rows)


def run_request(
    request: Mapping[str, object],
    *,
    module_loader: Callable[[], _AkshareModule] = _load_akshare,
) -> dict[str, object]:
    """Own exactly one endpoint call in this process and return an envelope."""
    module: _AkshareModule | None = None
    try:
        upstream = DataUpstream(str(request["upstream"]))
        symbol = str(request["symbol"])
        start_date = date.fromisoformat(str(request["start_date"]))
        end_date = date.fromisoformat(str(request["end_date"]))
        daily_request = DailyBarRequest(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
        )
        module = module_loader()
        library_version = vars(module).get("__version__")
        if not isinstance(library_version, str) or not library_version.strip():
            return _failure(SourceErrorCode.INTERNAL_PROVIDER.value)
        endpoint_name = _ENDPOINT_NAMES[upstream]
        endpoint = getattr(module, endpoint_name)
        parameters = endpoint_parameters(upstream, daily_request)
        frame = endpoint(**parameters)
        rows, row_count = _rows_from_frame(frame)
    except Exception as error:
        mapped = classify_source_error(error)
        return _failure(mapped.code.value)
    finally:
        del module
    return {
        "ok": True,
        "error_code": "0",
        "rows": rows,
        "library_version": library_version,
        "row_count": row_count,
    }


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict) or set(request) != {
            "upstream",
            "symbol",
            "start_date",
            "end_date",
        }:
            result = _failure(SourceErrorCode.REQUEST_REJECTED.value)
        else:
            # The provider library prints progress bars/status text. Suppress
            # it so stdout carries only the single controlled JSON envelope.
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                result = run_request(request)
    except Exception:
        result = _failure(SourceErrorCode.INTERNAL_PROVIDER.value)
    sys.stdout.write(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["run_request"]
