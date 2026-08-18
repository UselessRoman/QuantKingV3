"""Isolated BaoStock session worker; imported only by an explicit adapter call."""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import sys
from collections.abc import Callable, Mapping
from typing import Protocol, cast

BAOSTOCK_FIELDS = (
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "adjustflag",
    "tradestatus",
    "isST",
)
BAOSTOCK_CALENDAR_FIELDS = ("calendar_date", "is_trading_day")
BAOSTOCK_DISTRIBUTION_VERSION = "0.9.3"
BAOSTOCK_RUNTIME_VERSION = "00.9.30"
BAOSTOCK_PAGE_SIZE = 2000
_DAILY_REQUEST_KEYS = frozenset({"code", "start_date", "end_date"})
_CALENDAR_REQUEST_KEYS = frozenset({"mode", "start_date", "end_date"})


class _StatusResult(Protocol):
    error_code: object


class _QueryResult(Protocol):
    fields: object
    error_code: object
    cur_page_num: object
    cur_row_num: object
    data: object

    def next(self) -> bool: ...

    def get_row_data(self) -> object: ...


class _BaostockModule(Protocol):
    __name__: str
    __version__: str

    def login(self) -> _StatusResult | None: ...

    def query_history_k_data_plus(self, *args: object, **kwargs: object) -> _QueryResult | None: ...

    def query_trade_dates(self, *args: object, **kwargs: object) -> _QueryResult | None: ...

    def logout(self) -> _StatusResult | None: ...


class _Socket(Protocol):
    def close(self) -> None: ...


class _SocketContext(Protocol):
    default_socket: _Socket | None


def _load_baostock() -> _BaostockModule:
    return cast(_BaostockModule, importlib.import_module("baostock"))


def _failure(
    code: str,
    *,
    rows: list[list[str]] | None = None,
    login_messages: int = 0,
    query_messages: int = 0,
    logout_messages: int = 0,
) -> dict[str, object]:
    return {
        "ok": False,
        "error_code": code,
        "rows": rows or [],
        "login_messages": login_messages,
        "query_messages": query_messages,
        "logout_messages": logout_messages,
    }


def _service_failure_code(stage: str, code: object) -> str:
    if str(code) in {
        "10002001",
        "10002002",
        "10002003",
        "10002004",
        "10002005",
        "10002006",
        "10002007",
        "10002008",
    }:
        return "BAOSTOCK_DISCONNECTED"
    if stage == "login":
        return "BAOSTOCK_LOGIN_FAILED"
    return "BAOSTOCK_SERVICE_REJECTED"


def _force_close(module: _BaostockModule) -> None:
    try:
        context = cast(
            _SocketContext,
            importlib.import_module(f"{module.__name__}.common.context"),
        )
        socket_object = getattr(context, "default_socket", None)
        if socket_object is not None:
            socket_object.close()
            context.default_socket = None
    except Exception:
        # Process exit is the final containment boundary; cleanup must never
        # replace the primary login/query/paging failure.
        return


def _collect_rows(result: _QueryResult) -> tuple[list[list[str]], int, str | None]:
    rows: list[list[str]] = []
    page_messages = 1
    while True:
        before_page = str(getattr(result, "cur_page_num", ""))
        before_data = getattr(result, "data", None)
        before_length = len(before_data) if isinstance(before_data, list) else -1
        before_row = getattr(result, "cur_row_num", None)
        try:
            has_next = result.next()
        except Exception:
            error_code = "BAOSTOCK_PARTIAL_RESPONSE" if rows else "BAOSTOCK_QUERY_FAILED"
            return rows, page_messages, error_code
        if has_next:
            if str(getattr(result, "cur_page_num", "")) != before_page:
                page_messages += 1
            row = result.get_row_data()
            if not isinstance(row, list) or any(not isinstance(item, str) for item in row):
                return rows, page_messages, "BAOSTOCK_SCHEMA_DRIFT"
            rows.append(row)
            continue
        error_code = str(getattr(result, "error_code", ""))
        if error_code != "0":
            return (
                rows,
                page_messages,
                (
                    "BAOSTOCK_PARTIAL_RESPONSE"
                    if rows
                    else _service_failure_code("query", error_code)
                ),
            )
        current_page = str(getattr(result, "cur_page_num", ""))
        current_data = getattr(result, "data", None)
        # ResultData.next() swallows a failed continuation request and returns
        # False without advancing the page or clearing the full 2000-row page.
        if (
            before_length == BAOSTOCK_PAGE_SIZE
            and before_row == before_length
            and current_page == before_page
            and isinstance(current_data, list)
            and len(current_data) == before_length
        ):
            return rows, page_messages + 1, "BAOSTOCK_PARTIAL_RESPONSE"
        if before_length == BAOSTOCK_PAGE_SIZE and before_row == before_length:
            page_messages += 1
        return rows, page_messages, None


def run_session(
    request: Mapping[str, object],
    *,
    module_loader: Callable[[], _BaostockModule] = _load_baostock,
) -> dict[str, object]:
    """Own exactly one login/query/iterate/logout session in this process.

    Two controlled request shapes exist: the daily-bar request
    (``code``/``start_date``/``end_date``) and the calendar request
    (``mode="calendar"``/``start_date``/``end_date``). Anything else is a
    schema failure before any vendor call.
    """
    keys = set(request)
    calendar_mode = keys == _CALENDAR_REQUEST_KEYS and request.get("mode") == "calendar"
    if not calendar_mode and keys != _DAILY_REQUEST_KEYS:
        return _failure("BAOSTOCK_SCHEMA_DRIFT")
    primary: dict[str, object] | None = None
    module: _BaostockModule | None = None
    logged_in = False
    query_started = False
    try:
        module = module_loader()
        if getattr(module, "__version__", None) != BAOSTOCK_RUNTIME_VERSION:
            return _failure("BAOSTOCK_INTERNAL_PROVIDER")
        login = module.login()
        if login is None or str(getattr(login, "error_code", "")) != "0":
            return _failure(
                _service_failure_code("login", getattr(login, "error_code", "")),
                login_messages=1,
            )
        logged_in = True
        query_started = True
        expected_fields = BAOSTOCK_CALENDAR_FIELDS if calendar_mode else BAOSTOCK_FIELDS
        if calendar_mode:
            result = module.query_trade_dates(
                start_date=str(request["start_date"]),
                end_date=str(request["end_date"]),
            )
        else:
            result = module.query_history_k_data_plus(
                str(request["code"]),
                ",".join(BAOSTOCK_FIELDS),
                start_date=str(request["start_date"]),
                end_date=str(request["end_date"]),
                frequency="d",
                adjustflag="3",
            )
        if result is None:
            primary = _failure("BAOSTOCK_QUERY_FAILED", login_messages=1, query_messages=1)
        elif str(getattr(result, "error_code", "")) != "0":
            primary = _failure(
                _service_failure_code("query", getattr(result, "error_code", "")),
                login_messages=1,
                query_messages=1,
            )
        elif tuple(getattr(result, "fields", ())) != expected_fields:
            primary = _failure("BAOSTOCK_SCHEMA_DRIFT", login_messages=1, query_messages=1)
        else:
            rows, page_messages, error_code = _collect_rows(result)
            if error_code is not None:
                primary = _failure(
                    error_code,
                    rows=rows,
                    login_messages=1,
                    query_messages=page_messages,
                )
            else:
                primary = {
                    "ok": True,
                    "error_code": "0",
                    "rows": rows,
                    "fields": list(expected_fields),
                    "distribution_version": BAOSTOCK_DISTRIBUTION_VERSION,
                    "runtime_version": BAOSTOCK_RUNTIME_VERSION,
                    "login_messages": 1,
                    "query_messages": page_messages,
                    "logout_messages": 0,
                }
    except (KeyError, TypeError, ValueError):
        primary = _failure(
            "BAOSTOCK_SCHEMA_DRIFT",
            login_messages=int(logged_in),
            query_messages=int(query_started),
        )
    except Exception:
        primary = _failure(
            "BAOSTOCK_INTERNAL_PROVIDER",
            login_messages=int(logged_in),
            query_messages=int(query_started),
        )
    finally:
        logout_failed = False
        if module is not None and logged_in:
            if primary is not None:
                primary["logout_messages"] = 1
            try:
                logout = module.logout()
                logout_failed = logout is None or str(getattr(logout, "error_code", "")) != "0"
            except Exception:
                logout_failed = True
        if module is not None:
            _force_close(module)
        if logout_failed and primary is not None and bool(primary.get("ok")):
            login_messages = primary.get("login_messages", 1)
            query_messages = primary.get("query_messages", 0)
            primary = _failure(
                "BAOSTOCK_LOGOUT_FAILED",
                login_messages=login_messages if isinstance(login_messages, int) else 1,
                query_messages=query_messages if isinstance(query_messages, int) else 0,
                logout_messages=1,
            )
    return primary or _failure("BAOSTOCK_INTERNAL_PROVIDER")


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        keys = set(request) if isinstance(request, dict) else set()
        valid_daily = keys == {"code", "start_date", "end_date"}
        valid_calendar = (
            keys == {"mode", "start_date", "end_date"} and request.get("mode") == "calendar"
        )
        if not isinstance(request, dict) or not (valid_daily or valid_calendar):
            result = _failure("BAOSTOCK_SCHEMA_DRIFT")
        else:
            # The package prints login/logout status. Suppress it so stdout is
            # reserved for the single controlled JSON envelope.
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                result = run_session(request)
    except Exception:
        result = _failure("BAOSTOCK_INTERNAL_PROVIDER")
    sys.stdout.write(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BAOSTOCK_CALENDAR_FIELDS",
    "BAOSTOCK_DISTRIBUTION_VERSION",
    "BAOSTOCK_FIELDS",
    "BAOSTOCK_PAGE_SIZE",
    "BAOSTOCK_RUNTIME_VERSION",
    "run_session",
]
