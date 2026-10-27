"""Offline process/failure tests; no real provider import or data-root access."""

import hashlib
import json
import time
from multiprocessing.connection import Connection
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import quantking.data.adapters.research_probe as probe
from quantking.data.adapters.research_probe import (
    FIELDS,
    MAX_BYTES,
    ProbeCall,
    _bounded_decompress,
    _exchange,
    frozen_calls,
    run_probe,
    validate_response,
)


def _payload(spec: ProbeCall) -> dict[str, Any]:
    fields: list[str] = []
    rows: list[list[str]] = []
    params = dict(spec.params)
    if spec.endpoint.startswith("query_"):
        fields = ["code"]
        rows = [[params["code"]]]
    if spec.endpoint == "query_history_k_data_plus":
        fields = FIELDS.split(",")
        rows = [[params["start_date"], params["code"], *(["1"] * 9)]]
    return {
        "error_code": "0",
        "error_msg": "",
        "fields": fields,
        "rows": rows,
        "page": "1",
        "page_capacity": 2000,
    }


def _fake_worker(channel: Connection, mode: str) -> None:
    for index, spec in enumerate(frozen_calls(), 1):
        if not _exchange(channel, {"event": "start", "call": index}):
            return
        if mode == "hang":
            time.sleep(60)
        if index == 1 and not _exchange(channel, {"event": "connect", "call": index}):
            return
        if not _exchange(channel, {"event": "protocol", "call": index}):
            return
        if mode == "extra_request":
            _exchange(channel, {"event": "protocol", "call": index})
            return
        payload = _payload(spec)
        if index == 2 and mode == "failure":
            payload["error_code"] = "10001001"
        if index == 2 and mode == "oversized":
            payload["rows"] = [["sh.600000"]] * 501
        if index == 2 and mode == "bytes":
            payload["padding"] = "x" * MAX_BYTES
        if not _exchange(channel, {"event": "response", "call": index, "payload": payload}):
            return
    channel.close()


def happy_worker(channel: Connection) -> None:
    _fake_worker(channel, "happy")


def failure_worker(channel: Connection) -> None:
    _fake_worker(channel, "failure")


def oversize_worker(channel: Connection) -> None:
    _fake_worker(channel, "oversized")


def bytes_worker(channel: Connection) -> None:
    _fake_worker(channel, "bytes")


def extra_request_worker(channel: Connection) -> None:
    _fake_worker(channel, "extra_request")


def hang_worker(channel: Connection) -> None:
    _fake_worker(channel, "hang")


def test_plan_exactly_matches_authorized_scope() -> None:
    specs = frozen_calls()
    assert len(specs) == 18
    assert [s.endpoint for s in specs[1:5]] == [
        "query_stock_basic",
        "query_history_k_data_plus",
        "query_adjust_factor",
        "query_dividend_data",
    ]
    assert [dict(specs[i].params)["code"] for i in (1, 5, 9, 13)] == [
        "sh.600000",
        "sz.000001",
        "sz.002309",
        "sh.688469",
    ]
    assert [
        (dict(specs[i].params)["start_date"], dict(specs[i].params)["end_date"])
        for i in (2, 6, 10, 14)
    ] == [
        ("2023-01-01", "2023-12-31"),
        ("2023-01-01", "2023-12-31"),
        ("2024-01-01", "2024-06-30"),
        ("2023-06-01", "2023-07-31"),
    ]
    assert all(dict(specs[i].params)["yearType"] == "operate" for i in (4, 8, 12, 16))


def test_all_observations_and_sha_are_saved_without_claiming_qualification(tmp_path: Path) -> None:
    output = tmp_path / "a1"
    result = run_probe(output, worker=happy_worker)
    assert result["status"] == "OBSERVATIONS_COLLECTED"
    assert result["started_sdk_calls"] == result["completed_sdk_calls"] == 18
    assert result["protocol_requests_started"] == 18 and result["connections_started"] == 1
    assert result["qualified"] is False
    manifest = json.loads((output / "sha256.json").read_bytes())
    assert len(manifest) == 21
    for entry in manifest:
        content = (output / entry["path"]).read_bytes()
        assert len(content) == entry["bytes"]
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]
    journal = [json.loads(line) for line in (output / "calls.jsonl").read_text().splitlines()]
    assert journal[-1]["status"] == "OBSERVATIONS_COLLECTED"
    with pytest.raises(FileExistsError):
        run_probe(output, worker=happy_worker)
    assert json.loads((output / "sha256.json").read_bytes()) == manifest


@pytest.mark.parametrize(
    "worker, reason, started",
    [
        (failure_worker, "VENDOR_OR_RESPONSE_ERROR", 2),
        (oversize_worker, "PAGE_OR_ROW_LIMIT", 2),
        (bytes_worker, "TOTAL_RESPONSE_SIZE_LIMIT", 2),
        (extra_request_worker, "WORKER_PROTOCOL_INVALID", 1),
    ],
)
def test_first_error_stops_without_cleanup_logout(
    tmp_path: Path, worker: Any, reason: str, started: int
) -> None:
    result = run_probe(tmp_path / "a1", worker=worker)
    assert result["status"] == "FAILED" and result["reason"] == reason
    assert result["started_sdk_calls"] == started
    assert not (tmp_path / "a1/response-18.json").exists()
    assert (tmp_path / "a1/result.json").is_file()


def test_parent_hard_timeout_terminates_blocked_worker(tmp_path: Path) -> None:
    began = time.monotonic()
    result = run_probe(tmp_path / "a1", worker=hang_worker, call_timeout=3, total_timeout=10)
    assert result["reason"] == "CALL_TIMEOUT" and result["status"] == "FAILED"
    assert time.monotonic() - began < 9
    assert result["protocol_requests_started"] == 0
    events = [json.loads(line) for line in (tmp_path / "a1/calls.jsonl").read_text().splitlines()]
    if result["started_sdk_calls"]:
        assert events[-2]["event"] == "call_aborted" and events[-2]["elapsed"] >= 3


def test_total_deadline_and_invalid_timeouts(tmp_path: Path) -> None:
    result = run_probe(tmp_path / "total", worker=hang_worker, call_timeout=5, total_timeout=1)
    assert result["reason"] == "TOTAL_TIMEOUT"
    for timeout in (0, -1, float("nan"), 31):
        with pytest.raises(ValueError, match="TIMEOUT_INVALID"):
            run_probe(tmp_path / "invalid", worker=happy_worker, call_timeout=timeout)
    assert not (tmp_path / "invalid").exists()


def test_reference_header_discovery_then_drift_and_daily_fields() -> None:
    spec = frozen_calls()[1]
    payload = _payload(spec)
    headers: dict[str, list[str]] = {}
    assert validate_response(spec, payload, headers) is None
    assert (
        validate_response(spec, {**payload, "fields": ["code", "extra"], "rows": []}, headers)
        == "REFERENCE_FIELDS_DRIFT"
    )
    assert validate_response(frozen_calls()[2], payload, {}) == "DAILY_FIELDS_DRIFT"
    assert (
        validate_response(spec, {**payload, "rows": [["sz.000001"]]}, {})
        == "SECURITY_OUTSIDE_SCOPE"
    )
    assert validate_response(spec, {**payload, "rows": []}, {}) is None


def test_bounded_decompression_rejects_bomb_and_truncation() -> None:
    import zlib

    assert _bounded_decompress(zlib.compress(b"small")) == b"small"
    with pytest.raises(ValueError):
        _bounded_decompress(zlib.compress(b"x" * (MAX_BYTES + 1)))
    with pytest.raises(ValueError):
        _bounded_decompress(zlib.compress(b"small")[:-2])
    with pytest.raises(ValueError):
        _bounded_decompress(zlib.compress(b"small") + b"trailing")


@pytest.mark.parametrize(
    "connection_error, partial_send, capacity",
    [
        (False, False, "2000"),
        (False, False, "100"),
        (True, False, "2000"),
        (False, True, "2000"),
    ],
)
def test_real_worker_uses_native_result_and_guarded_transport(
    monkeypatch: pytest.MonkeyPatch, connection_error: bool, partial_send: bool, capacity: str
) -> None:
    # Native SDK result construction is offline; socket and endpoints are fakes.
    from baostock.data.resultset import ResultData

    messages: list[dict[str, Any]] = []
    sdk_calls: list[str] = []
    connection = SimpleNamespace()

    class Channel:
        def send_bytes(self, raw: bytes) -> None:
            messages.append(json.loads(raw))

        def recv_bytes(self, maximum: int) -> bytes:
            message = messages[-1]
            if message["event"] == "response" and message["call"] == 2:
                return b"stop"
            return b"continue"

        def close(self) -> None:
            pass

    class RawSocket:
        closed = False

        def connect(self, address: tuple[str, int]) -> None:
            assert address == ("public-api.baostock.com", 10030)
            if connection_error:
                raise ConnectionRefusedError("synthetic connection failure")

        def send(self, payload: bytes) -> int:
            return len(payload) - 1 if partial_send else len(payload)

        def close(self) -> None:
            self.closed = True

    raw_socket = RawSocket()

    class SocketUtil:
        pass

    transport = SimpleNamespace(SocketUtil=SocketUtil, zlib=SimpleNamespace())

    class FakeSdk:
        __version__ = "00.9.30"

        def __getattr__(self, endpoint: str) -> Any:
            def invoke(**params: str) -> Any:
                sdk_calls.append(endpoint)
                if endpoint == "login":
                    SocketUtil().connect()  # type: ignore[attr-defined]
                connection.default_socket.send(b"synthetic-request")
                result = ResultData()
                result.error_code = "0"
                if endpoint == "query_stock_basic":
                    result.fields = ["code"]
                    result.data = [[params["code"]]]
                    result.per_page_count = capacity
                return result

            return invoke

    modules = {
        "baostock": FakeSdk(),
        "baostock.common.contants": SimpleNamespace(
            BAOSTOCK_SERVER_IP="public-api.baostock.com",
            BAOSTOCK_SERVER_PORT=10030,
            BAOSTOCK_PER_PAGE_COUNT=2000,
        ),
        "baostock.util.socketutil": transport,
        "baostock.common.context": connection,
    }
    monkeypatch.setattr(probe, "importlib", SimpleNamespace(import_module=modules.__getitem__))
    monkeypatch.setattr(probe, "version", lambda name: "0.9.3")
    monkeypatch.setattr(
        probe, "socket", SimpleNamespace(AF_INET=2, SOCK_STREAM=1, socket=lambda *args: raw_socket)
    )
    probe.sdk_worker(cast(Connection, Channel()))
    if connection_error:
        assert sdk_calls == ["login"] and raw_socket.closed
        assert not any(m["event"] == "protocol" for m in messages)
        assert messages[-1]["event"] == "worker_error"
    elif partial_send:
        assert sdk_calls == ["login"]
        assert sum(m["event"] == "protocol" for m in messages) == 1
        assert messages[-1]["event"] == "worker_error"
    else:
        assert sdk_calls == ["login", "query_stock_basic"]
        response = messages[-1]["payload"]
        assert response["page_capacity"] == capacity
        expected = "PAGE_CAPACITY_DRIFT" if capacity == "100" else None
        assert validate_response(frozen_calls()[1], response, {}) == expected
