from __future__ import annotations

import json
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.sources.baostock import (
    BaostockDailyBarAdapter,
    BaostockMarketDataGateway,
    BaostockSessionResult,
    BaostockSessionRunner,
)
from quantking.data.adapters.sources.baostock_worker import BAOSTOCK_FIELDS, run_session
from quantking.data.adapters.sources.contracts import (
    SourceAdapterError,
    SourceErrorCode,
    SupplierResponse,
)
from quantking.data.adapters.sources.parameters import DailyBarRequest
from quantking.data.incremental import FetchChunk
from quantking.data.models import DataProvider, DatasetKind, DataUpstream


def _row(day: str, close: str, *, official_preclose: str) -> tuple[str, ...]:
    values = {
        "date": day,
        "code": "sh.600000",
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "preclose": official_preclose,
        "volume": "100",
        "amount": "1000.50",
        "adjustflag": "3",
        "tradestatus": "1",
        "isST": "0",
    }
    return tuple(values[field] for field in BAOSTOCK_FIELDS)


class _StubRunner:
    def __init__(self, result: BaostockSessionResult) -> None:
        self.result = result
        self.calls: list[DailyBarRequest] = []

    def run(self, request: DailyBarRequest) -> BaostockSessionResult:
        self.calls.append(request)
        return self.result


def test_importing_baostock_adapter_does_not_import_vendor_package() -> None:
    assert "baostock" not in sys.modules
    runner = BaostockSessionRunner(command=(sys.executable, "-c", "raise SystemExit(0)"))
    assert runner.timeout_seconds == 20.0
    assert "baostock" not in sys.modules


def test_parent_enforces_hard_process_timeout_without_leaking_child_text() -> None:
    runner = BaostockSessionRunner(
        timeout_seconds=0.05,
        command=(sys.executable, "-c", "import time; print('secret'); time.sleep(2)"),
    )
    request = DailyBarRequest("600000.SH", date(2026, 7, 1), date(2026, 7, 2))
    with pytest.raises(SourceAdapterError) as caught:
        runner.run(request)
    assert caught.value.code is SourceErrorCode.BAOSTOCK_TIMEOUT
    assert str(caught.value) == "BaoStock session exceeded its hard timeout"
    assert "secret" not in str(caught.value)


def test_runner_accepts_only_controlled_child_envelope_and_maps_stable_error() -> None:
    envelope = json.dumps({"ok": False, "error_code": "BAOSTOCK_SERVICE_REJECTED", "rows": []})
    runner = BaostockSessionRunner(
        command=(
            sys.executable,
            "-c",
            f"import sys; sys.stdin.read(); sys.stderr.write('secret path'); print({envelope!r})",
        )
    )
    request = DailyBarRequest("600000.SH", date(2026, 7, 1), date(2026, 7, 2))
    with pytest.raises(SourceAdapterError) as caught:
        runner.run(request)
    assert caught.value.code is SourceErrorCode.BAOSTOCK_SERVICE_REJECTED
    assert str(caught.value) == "BaoStock service rejected the request"
    assert "secret" not in str(caught.value)


def test_adapter_keeps_official_preclose_raw_but_derives_canonical_previous_close() -> None:
    result = BaostockSessionResult(
        rows=(
            _row("2026-06-30", "10.00", official_preclose="9.90"),
            _row("2026-07-01", "10.50", official_preclose="99.99"),
        ),
        distribution_version="0.9.3",
        runtime_version="00.9.30",
        login_messages=1,
        query_messages=1,
        logout_messages=1,
        complete=True,
        partial_reason=None,
    )
    runner = _StubRunner(result)
    adapter = BaostockDailyBarAdapter(
        runner=runner,  # type: ignore[arg-type]
        clock=lambda: UtcTimestamp(datetime(2026, 8, 12, tzinfo=UTC)),
    )
    chunk = FetchChunk(
        sequence=7,
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        scope=("600000.SH",),
        start=TradingDate(date(2026, 7, 1)),
        end=TradingDate(date(2026, 7, 1)),
        request_sha256="a" * 64,
    )
    response = BaostockMarketDataGateway(adapter).fetch(chunk)

    assert len(runner.calls) == 1
    assert response.records == (
        {
            "stock_code": "600000.SH",
            "date": "2026-07-01",
            "open": 10.5,
            "high": 10.5,
            "low": 10.5,
            "close": 10.5,
            "pre_close": 10.0,
            "volume": 100,
            "amount": 1000.5,
        },
    )
    assert response.contract.lineage.provider is DataProvider.BAOSTOCK
    assert response.contract.lineage.upstream is DataUpstream.BAOSTOCK
    raw = json.loads(response.raw_payload)
    assert raw["provider_payload"]["rows"][1][_FIELD_INDEX["preclose"]] == "99.99"
    assert raw["provider_payload"]["response_metadata"] == {
        "distribution_version": "0.9.3",
        "runtime_version": "00.9.30",
        "login_messages": 1,
        "query_messages": 1,
        "logout_messages": 1,
        "error_code": "0",
        "unit_policy_version": "baostock-0.9.3-returned-units.v1",
    }


_FIELD_INDEX = {field: index for index, field in enumerate(BAOSTOCK_FIELDS)}


def _suspended_row(day: str, close: str) -> tuple[str, ...]:
    """BaoStock suspended-day placeholder (K4C-R3-R1 diagnostic shape)."""
    values = {
        "date": day,
        "code": "sh.600000",
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "preclose": close,
        "volume": "",
        "amount": "",
        "adjustflag": "3",
        "tradestatus": "0",
        "isST": "0",
    }
    return tuple(values[field] for field in BAOSTOCK_FIELDS)


def _adapter_response(
    rows: tuple[tuple[str, ...], ...],
    *,
    end: date,
) -> SupplierResponse:
    result = BaostockSessionResult(
        rows=rows,
        distribution_version="0.9.3",
        runtime_version="00.9.30",
        login_messages=1,
        query_messages=1,
        logout_messages=1,
        complete=True,
        partial_reason=None,
    )
    runner = _StubRunner(result)
    adapter = BaostockDailyBarAdapter(
        runner=runner,  # type: ignore[arg-type]
        clock=lambda: UtcTimestamp(datetime(2026, 8, 12, tzinfo=UTC)),
    )
    chunk = FetchChunk(
        sequence=7,
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        scope=("600000.SH",),
        start=TradingDate(end),
        end=TradingDate(end),
        request_sha256="a" * 64,
    )
    return BaostockMarketDataGateway(adapter).fetch(chunk)


def test_suspended_placeholder_row_is_dropped_without_schema_drift() -> None:
    response = _adapter_response(
        (
            _row("2026-07-15", "53.50", official_preclose="53.00"),
            _suspended_row("2026-07-16", "53.50"),
        ),
        end=date(2026, 7, 16),
    )
    assert response.complete is True
    assert response.records == ()  # the suspended day carries no bar
    assert response.upstream_row_count == 2  # raw rows stay verbatim


def test_suspended_placeholder_does_not_feed_next_pre_close() -> None:
    response = _adapter_response(
        (
            _row("2026-07-15", "53.50", official_preclose="53.00"),
            _suspended_row("2026-07-16", "60.00"),  # filled close must be ignored
            _row("2026-07-17", "54.00", official_preclose="99.99"),
        ),
        end=date(2026, 7, 17),
    )
    assert [record["date"] for record in response.records] == ["2026-07-17"]
    assert response.records[0]["pre_close"] == 53.5


def test_suspended_row_with_volume_is_schema_drift() -> None:
    row = list(_suspended_row("2026-07-16", "53.50"))
    row[BAOSTOCK_FIELDS.index("volume")] = "100"
    with pytest.raises(SourceAdapterError) as caught:
        _adapter_response(
            (_row("2026-07-15", "53.50", official_preclose="53.00"), tuple(row)),
            end=date(2026, 7, 16),
        )
    assert caught.value.code is SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT


def test_trading_row_with_empty_volume_is_schema_drift() -> None:
    row = list(_row("2026-07-16", "53.50", official_preclose="53.00"))
    row[BAOSTOCK_FIELDS.index("volume")] = ""
    with pytest.raises(SourceAdapterError) as caught:
        _adapter_response((tuple(row),), end=date(2026, 7, 16))
    assert caught.value.code is SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT


class _PagedResult:
    def __init__(self, pages: list[list[list[str]]], *, fail_continuation: bool = False) -> None:
        self.fields = list(BAOSTOCK_FIELDS)
        self.error_code = "0"
        self.error_msg = ""
        self.cur_page_num = "1"
        self.cur_row_num = 0
        self.data = pages[0]
        self._pages = pages
        self._fail_continuation = fail_continuation

    def next(self) -> bool:
        if self.cur_row_num < len(self.data):
            return True
        if len(self.data) < 2000:
            return False
        if self._fail_continuation:
            return False
        next_page = int(self.cur_page_num) + 1
        self.cur_page_num = str(next_page)
        self.data = self._pages[next_page - 1]
        self.cur_row_num = 0
        return bool(self.data)

    def get_row_data(self) -> list[str]:
        row = self.data[self.cur_row_num]
        self.cur_row_num += 1
        return row


def _fake_module(
    result: _PagedResult,
    monkeypatch: pytest.MonkeyPatch,
    *,
    login_code: str = "0",
    logout_code: str = "0",
) -> tuple[ModuleType, SimpleNamespace]:
    module = ModuleType("fake_baostock")
    module.__version__ = "00.9.30"
    module.login = lambda: SimpleNamespace(error_code=login_code, error_msg="secret")  # type: ignore[attr-defined]
    module.query_history_k_data_plus = lambda *_args, **_kwargs: result  # type: ignore[attr-defined]
    module.logout = lambda: SimpleNamespace(error_code=logout_code, error_msg="secret")  # type: ignore[attr-defined]
    socket = SimpleNamespace(closed=False)

    def close() -> None:
        socket.closed = True

    socket.close = close
    context = ModuleType("fake_baostock.common.context")
    context.default_socket = socket  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_baostock.common.context", context)
    return module, socket


@pytest.mark.parametrize(
    ("row_count", "expected_messages"),
    [(2000, 2), (2001, 2), (4001, 3)],
)
def test_worker_counts_every_successful_page_protocol_query_and_closes_socket(
    monkeypatch: pytest.MonkeyPatch,
    row_count: int,
    expected_messages: int,
) -> None:
    rows = [[str(index)] * len(BAOSTOCK_FIELDS) for index in range(row_count)]
    pages = [rows[index : index + 2000] for index in range(0, row_count, 2000)]
    if row_count % 2000 == 0:
        pages.append([])
    module, socket = _fake_module(_PagedResult(pages), monkeypatch)
    result = run_session(
        {"code": "sh.600000", "start_date": "2026-01-01", "end_date": "2026-08-01"},
        module_loader=lambda: module,
    )
    assert result["ok"] is True
    assert result["query_messages"] == expected_messages
    assert socket.closed is True


def test_worker_detects_swallowed_continuation_as_partial_and_preserves_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [[str(index)] * len(BAOSTOCK_FIELDS) for index in range(2000)]
    module, socket = _fake_module(
        _PagedResult([rows], fail_continuation=True),
        monkeypatch,
        logout_code="10002007",
    )
    result = run_session(
        {"code": "sh.600000", "start_date": "2026-01-01", "end_date": "2026-08-01"},
        module_loader=lambda: module,
    )
    assert result == {
        "ok": False,
        "error_code": "BAOSTOCK_PARTIAL_RESPONSE",
        "rows": rows,
        "login_messages": 1,
        "query_messages": 2,
        "logout_messages": 1,
    }
    assert socket.closed is True


@pytest.mark.parametrize(
    ("login_code", "expected"),
    [
        ("10002007", "BAOSTOCK_DISCONNECTED"),
        ("10001002", "BAOSTOCK_LOGIN_FAILED"),
    ],
)
def test_worker_login_failure_mapping_is_fixed_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    login_code: str,
    expected: str,
) -> None:
    module, socket = _fake_module(_PagedResult([[]]), monkeypatch, login_code=login_code)
    result = run_session(
        {"code": "sh.600000", "start_date": "2026-01-01", "end_date": "2026-08-01"},
        module_loader=lambda: module,
    )
    assert result == {
        "ok": False,
        "error_code": expected,
        "rows": [],
        "login_messages": 1,
        "query_messages": 0,
        "logout_messages": 0,
    }
    assert "secret" not in json.dumps(result)
    assert socket.closed is True


def test_default_isolated_module_entrypoint_rejects_before_vendor_session() -> None:
    completed = __import__("subprocess").run(
        [
            sys.executable,
            "-I",
            "-m",
            "quantking.data.adapters.sources.baostock_worker",
        ],
        input="{}",
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {
        "ok": False,
        "error_code": "BAOSTOCK_SCHEMA_DRIFT",
        "rows": [],
        "login_messages": 0,
        "query_messages": 0,
        "logout_messages": 0,
    }
    assert completed.stderr == ""
    assert "baostock" not in sys.modules


def test_timeout_terminates_child_before_it_can_continue(tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist.txt"
    child = (
        "import pathlib,time; "
        "time.sleep(0.3); "
        f"pathlib.Path({str(marker)!r}).write_text('leaked', encoding='utf-8')"
    )
    runner = BaostockSessionRunner(
        timeout_seconds=0.05,
        command=(sys.executable, "-c", child),
    )
    with pytest.raises(SourceAdapterError) as caught:
        runner.run(DailyBarRequest("600000.SH", date(2026, 7, 1), date(2026, 7, 2)))
    assert caught.value.code is SourceErrorCode.BAOSTOCK_TIMEOUT
    time.sleep(0.4)
    assert not marker.exists()


def test_missing_context_close_marks_first_target_row_partial() -> None:
    result = BaostockSessionResult(
        rows=(_row("2026-07-01", "10.50", official_preclose="99.99"),),
        distribution_version="0.9.3",
        runtime_version="00.9.30",
        login_messages=1,
        query_messages=1,
        logout_messages=1,
        complete=True,
        partial_reason=None,
    )
    adapter = BaostockDailyBarAdapter(
        runner=_StubRunner(result),  # type: ignore[arg-type]
        clock=lambda: UtcTimestamp(datetime(2026, 8, 12, tzinfo=UTC)),
    )
    endpoint = adapter.fetch(DailyBarRequest("600000.SH", date(2026, 7, 1), date(2026, 7, 1)))
    assert endpoint.records == ()
    assert endpoint.complete is False
    assert endpoint.partial_reason == "BAOSTOCK_PARTIAL_RESPONSE"
    assert endpoint.provider_payload["rows"][0][_FIELD_INDEX["preclose"]] == "99.99"


def test_every_stable_error_code_has_complete_redacted_contract_mapping() -> None:
    expected: dict[SourceErrorCode, tuple[str, str, bool]] = {
        SourceErrorCode.BAOSTOCK_LOGIN_FAILED: ("BaoStock login failed", "NETWORK_FAILURE", True),
        SourceErrorCode.BAOSTOCK_QUERY_FAILED: (
            "BaoStock query failed",
            "NETWORK_FAILURE",
            True,
        ),
        SourceErrorCode.BAOSTOCK_SERVICE_REJECTED: (
            "BaoStock service rejected the request",
            "PERMISSION_FAILURE",
            False,
        ),
        SourceErrorCode.BAOSTOCK_TIMEOUT: (
            "BaoStock session exceeded its hard timeout",
            "NETWORK_FAILURE",
            True,
        ),
        SourceErrorCode.BAOSTOCK_DISCONNECTED: (
            "BaoStock service connection was interrupted",
            "NETWORK_FAILURE",
            True,
        ),
        # Reserved classification: no adapter path raises it yet, because an
        # empty BaoStock window can be legitimate. The mapping stays pinned.
        SourceErrorCode.BAOSTOCK_UNEXPECTED_EMPTY: (
            "BaoStock returned an unexpected empty response",
            "UNEXPECTED_EMPTY",
            False,
        ),
        SourceErrorCode.BAOSTOCK_PARTIAL_RESPONSE: (
            "BaoStock returned a partial response",
            "PARTIAL_SOURCE_FAILURE",
            True,
        ),
        SourceErrorCode.BAOSTOCK_SCHEMA_DRIFT: (
            "BaoStock response schema changed",
            "PARSE_FAILURE",
            False,
        ),
        SourceErrorCode.BAOSTOCK_LOGOUT_FAILED: (
            "BaoStock logout failed",
            "NETWORK_FAILURE",
            True,
        ),
        SourceErrorCode.BAOSTOCK_INTERNAL_PROVIDER: (
            "BaoStock provider failed internally",
            "INTERNAL_FAILURE",
            False,
        ),
    }
    assert {code for code in SourceErrorCode if code.value.startswith("BAOSTOCK_")} == set(expected)
    for code, (message, failure_kind, retryable) in expected.items():
        error = SourceAdapterError(code)
        assert error.code is code
        assert str(error) == message
        assert error.failure_kind.value == failure_kind
        assert error.retryable is retryable
