"""Calendar-mode BaoStock session tests: worker protocol + parent runner."""

from __future__ import annotations

import json
import sys
from datetime import date
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from quantking.data.adapters.sources.baostock import BaostockCalendarRunner
from quantking.data.adapters.sources.baostock_worker import (
    BAOSTOCK_CALENDAR_FIELDS,
    run_session,
)
from quantking.data.adapters.sources.contracts import SourceAdapterError, SourceErrorCode


class _CalendarResult:
    """Paged-result double shaped like a BaoStock ``query_trade_dates`` result."""

    def __init__(
        self,
        rows: list[list[str]],
        *,
        error_code: str = "0",
        fields: tuple[str, ...] = BAOSTOCK_CALENDAR_FIELDS,
    ) -> None:
        self.fields = fields
        self.error_code = error_code
        self.cur_page_num = "1"
        self.cur_row_num = 0
        self.data = rows

    def next(self) -> bool:
        return self.cur_row_num < len(self.data)

    def get_row_data(self) -> list[str]:
        row = self.data[self.cur_row_num]
        self.cur_row_num += 1
        return row


def _calendar_module(
    result: _CalendarResult | None,
    monkeypatch: pytest.MonkeyPatch,
    *,
    login_code: str = "0",
    logout_code: str = "0",
) -> tuple[ModuleType, dict[str, Any]]:
    module = ModuleType("fake_baostock_calendar")
    module.__version__ = "00.9.30"
    captured: dict[str, Any] = {}
    module.login = lambda: SimpleNamespace(error_code=login_code)  # type: ignore[attr-defined]

    def _query(*_args: object, **kwargs: object) -> _CalendarResult | None:
        captured.update(kwargs)
        assert result is not None
        return result

    module.query_trade_dates = _query  # type: ignore[attr-defined]
    module.logout = lambda: SimpleNamespace(error_code=logout_code)  # type: ignore[attr-defined]
    context = ModuleType("fake_baostock_calendar.common.context")
    context.default_socket = None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_baostock_calendar.common.context", context)
    return module, captured


def test_worker_calendar_session_returns_rows_with_protocol_accounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [["2026-05-27", "1"], ["2026-05-28", "0"]]
    module, captured = _calendar_module(_CalendarResult(rows), monkeypatch)
    result = run_session(
        {"mode": "calendar", "start_date": "2026-05-27", "end_date": "2026-05-28"},
        module_loader=lambda: module,
    )
    assert result["ok"] is True
    assert result["rows"] == rows
    assert result["fields"] == list(BAOSTOCK_CALENDAR_FIELDS)
    assert result["login_messages"] == 1
    assert result["query_messages"] == 1
    assert result["logout_messages"] == 1
    assert captured == {"start_date": "2026-05-27", "end_date": "2026-05-28"}


def test_worker_calendar_session_rejects_field_drift_before_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _captured = _calendar_module(
        _CalendarResult([], fields=("calendar_date", "wrong")), monkeypatch
    )
    result = run_session(
        {"mode": "calendar", "start_date": "2026-05-27", "end_date": "2026-05-28"},
        module_loader=lambda: module,
    )
    assert result["ok"] is False
    assert result["error_code"] == "BAOSTOCK_SCHEMA_DRIFT"
    assert result["login_messages"] == 1
    assert result["query_messages"] == 1


def test_worker_calendar_session_maps_service_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _captured = _calendar_module(_CalendarResult([], error_code="10002005"), monkeypatch)
    result = run_session(
        {"mode": "calendar", "start_date": "2026-05-27", "end_date": "2026-05-28"},
        module_loader=lambda: module,
    )
    assert result["ok"] is False
    assert result["error_code"] == "BAOSTOCK_DISCONNECTED"


def test_worker_rejects_mixed_request_shapes_before_any_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _captured = _calendar_module(_CalendarResult([]), monkeypatch)
    calls: list[object] = []
    module.login = lambda: calls.append(1) or SimpleNamespace(error_code="0")  # type: ignore[attr-defined]
    result = run_session(
        {"mode": "calendar", "code": "sh.600000", "start_date": "a", "end_date": "b"},
        module_loader=lambda: module,
    )
    assert result["error_code"] == "BAOSTOCK_SCHEMA_DRIFT"
    assert calls == []


def _envelope_command(envelope: dict[str, object]) -> tuple[str, ...]:
    script = f"import sys; sys.stdin.read(); print({json.dumps(envelope)!r})"
    return (sys.executable, "-c", script)


def _window(days: list[tuple[str, str]]) -> dict[str, object]:
    return {
        "ok": True,
        "error_code": "0",
        "rows": [[day, flag] for day, flag in days],
        "fields": list(BAOSTOCK_CALENDAR_FIELDS),
        "distribution_version": "0.9.3",
        "runtime_version": "00.9.30",
        "login_messages": 1,
        "query_messages": 1,
        "logout_messages": 1,
    }


def test_parent_runner_parses_a_complete_window() -> None:
    runner = BaostockCalendarRunner(
        command=_envelope_command(
            _window(
                [
                    ("2026-05-28", "0"),
                    ("2026-05-27", "1"),
                    ("2026-05-29", "1"),
                ]
            )
        )
    )
    result = runner.run(start_date=date(2026, 5, 27), end_date=date(2026, 5, 29))
    assert result.days == (("2026-05-27", "1"), ("2026-05-28", "0"), ("2026-05-29", "1"))
    assert result.protocol_messages == 3


def test_parent_runner_rejects_missing_day_as_schema_drift() -> None:
    runner = BaostockCalendarRunner(
        command=_envelope_command(_window([("2026-05-27", "1"), ("2026-05-29", "1")]))
    )
    with pytest.raises(SourceAdapterError) as caught:
        runner.run(start_date=date(2026, 5, 27), end_date=date(2026, 5, 29))
    assert caught.value.code is SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT


def test_parent_runner_rejects_duplicate_day_as_schema_drift() -> None:
    runner = BaostockCalendarRunner(
        command=_envelope_command(
            _window(
                [
                    ("2026-05-27", "1"),
                    ("2026-05-27", "0"),
                    ("2026-05-28", "1"),
                ]
            )
        )
    )
    with pytest.raises(SourceAdapterError) as caught:
        runner.run(start_date=date(2026, 5, 27), end_date=date(2026, 5, 28))
    assert caught.value.code is SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT


def test_parent_runner_rejects_unknown_trading_flag() -> None:
    runner = BaostockCalendarRunner(command=_envelope_command(_window([("2026-05-27", "2")])))
    with pytest.raises(SourceAdapterError) as caught:
        runner.run(start_date=date(2026, 5, 27), end_date=date(2026, 5, 27))
    assert caught.value.code is SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT


def test_parent_runner_rejects_bad_protocol_accounting() -> None:
    envelope = _window([("2026-05-27", "1")])
    envelope["login_messages"] = 0
    runner = BaostockCalendarRunner(command=_envelope_command(envelope))
    with pytest.raises(SourceAdapterError) as caught:
        runner.run(start_date=date(2026, 5, 27), end_date=date(2026, 5, 27))
    assert caught.value.code is SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER


def test_parent_runner_enforces_hard_timeout() -> None:
    runner = BaostockCalendarRunner(
        timeout_seconds=0.05,
        command=(sys.executable, "-c", "import time; time.sleep(2)"),
    )
    with pytest.raises(SourceAdapterError) as caught:
        runner.run(start_date=date(2026, 5, 27), end_date=date(2026, 5, 27))
    assert caught.value.code is SourceErrorCode.BAOSTOCK_TIMEOUT
