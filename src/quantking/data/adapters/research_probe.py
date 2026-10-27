"""One explicitly approved S2-01 discovery attempt, never a publisher.

The parent owns evidence, budgets and hard deadlines. A disposable child owns
the single SDK session. No retries, pagination, cleanup logout or alternate host.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import json
import math
import multiprocessing
import os
import queue
import socket
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 500
CALL_TIMEOUT = 30.0
TOTAL_TIMEOUT = 600.0
HOST = "public-api.baostock.com"
PORT = 10030
OUTPUT_RELATIVE = "docs/stage2/evidence/s2-01-source-probe-a1"
FIELDS = "date,code,open,high,low,close,preclose,volume,amount,tradestatus,isST"
TARGETS = (
    ("sh.600000", "2023-01-01", "2023-12-31"),
    ("sz.000001", "2023-01-01", "2023-12-31"),
    ("sz.002309", "2024-01-01", "2024-06-30"),
    ("sh.688469", "2023-06-01", "2023-07-31"),
)


@dataclass(frozen=True)
class ProbeCall:
    endpoint: str
    params: tuple[tuple[str, str], ...] = ()


def frozen_calls() -> tuple[ProbeCall, ...]:
    calls = [ProbeCall("login")]
    for code, start, end in TARGETS:
        dates = (("code", code), ("start_date", start), ("end_date", end))
        calls.extend(
            (
                ProbeCall("query_stock_basic", (("code", code), ("code_name", ""))),
                ProbeCall(
                    "query_history_k_data_plus",
                    (*dates, ("fields", FIELDS), ("frequency", "d"), ("adjustflag", "3")),
                ),
                ProbeCall("query_adjust_factor", dates),
                ProbeCall(
                    "query_dividend_data",
                    (("code", code), ("year", start[:4]), ("yearType", "operate")),
                ),
            )
        )
    return (*calls, ProbeCall("logout"))


def _encoded(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _utc() -> str:
    return datetime.now(UTC).isoformat()


def _exchange(channel: Connection, payload: dict[str, Any]) -> bool:
    channel.send_bytes(_encoded(payload))
    return channel.recv_bytes(16) == b"continue"


def _bounded_decompress(
    data: bytes, wbits: int = zlib.MAX_WBITS, bufsize: int = zlib.DEF_BUF_SIZE
) -> bytes:
    decoder = zlib.decompressobj(wbits)
    result = decoder.decompress(data, MAX_BYTES + 1)
    if len(result) > MAX_BYTES or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
        raise ValueError("DECOMPRESSED_SIZE_OR_FRAMING_INVALID")
    return result


def sdk_worker(channel: Connection) -> None:
    """Only this child imports the vendor; no filesystem evidence writes here."""
    current = 0
    try:
        sdk = importlib.import_module("baostock")
        constants = importlib.import_module("baostock.common.contants")
        transport = importlib.import_module("baostock.util.socketutil")
        context: Any = importlib.import_module("baostock.common.context")
        if (
            version("baostock"),
            sdk.__version__,
            constants.BAOSTOCK_SERVER_IP,
            constants.BAOSTOCK_SERVER_PORT,
            constants.BAOSTOCK_PER_PAGE_COUNT,
        ) != ("0.9.3", "00.9.30", HOST, PORT, 2000):
            raise ValueError("SDK_CONFIGURATION_DRIFT")
        connections = 0
        requests = 0
        per_call_requests = 0
        received_bytes = 0

        class GuardedSocket:
            def __init__(self, raw: Any) -> None:
                self.raw = raw

            def send(self, payload: bytes) -> int:
                nonlocal requests, per_call_requests
                if requests >= 18 or per_call_requests >= 1:
                    raise ValueError("PROTOCOL_BUDGET_EXCEEDED")
                if not _exchange(channel, {"event": "protocol", "call": current}):
                    raise ValueError("PARENT_STOPPED")
                requests += 1
                per_call_requests += 1
                sent = int(self.raw.send(payload))
                if sent != len(payload):
                    raise OSError("PARTIAL_SEND_NO_RETRY")
                return sent

            def recv(self, size: int) -> bytes:
                nonlocal received_bytes
                data = bytes(self.raw.recv(size))
                received_bytes += len(data)
                if not data or received_bytes > MAX_BYTES:
                    raise OSError("EOF_OR_WIRE_SIZE_LIMIT")
                return data

            def close(self) -> None:
                self.raw.close()

        def connect(instance: Any) -> None:
            nonlocal connections
            if connections or current != 1:
                raise ValueError("RECONNECT_FORBIDDEN")
            if not _exchange(channel, {"event": "connect", "call": current}):
                raise ValueError("PARENT_STOPPED")
            connections += 1
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                raw.connect((HOST, PORT))
            except Exception:
                raw.close()
                raise
            context.default_socket = GuardedSocket(raw)

        transport.SocketUtil.connect = connect
        transport.zlib.decompress = _bounded_decompress
        # SDK stdout can contain login details or arbitrary vendor exceptions.
        with (
            open(os.devnull, "w") as quiet,
            contextlib.redirect_stdout(quiet),
            contextlib.redirect_stderr(quiet),
        ):
            for current, spec in enumerate(frozen_calls(), 1):
                per_call_requests = 0
                if not _exchange(channel, {"event": "start", "call": current}):
                    return
                result = getattr(sdk, spec.endpoint)(**dict(spec.params))
                if result is None:
                    raise ValueError("SDK_RESULT_MISSING")
                # Read already materialized first-page facts; never call next/get_data.
                payload = {
                    "error_code": str(result.error_code),
                    "error_msg": str(result.error_msg)[:500],
                    "fields": result.fields,
                    "rows": result.data,
                    "page": str(result.cur_page_num),
                    "page_capacity": str(result.per_page_count),
                    "received_wire_bytes": received_bytes,
                }
                encoded = _encoded(payload)
                if len(encoded) > MAX_BYTES:
                    payload = {
                        "error_code": "RESPONSE_SIZE_LIMIT",
                        "original_bytes": len(encoded),
                        "original_sha256": hashlib.sha256(encoded).hexdigest(),
                    }
                if not _exchange(
                    channel, {"event": "response", "call": current, "payload": payload}
                ):
                    return
    except Exception as error:
        # The SDK's stable vendor error is captured in response; internal failures
        # retain a safe class/code only, without arbitrary exception payloads.
        with contextlib.suppress(BrokenPipeError, EOFError, OSError):
            _exchange(
                channel,
                {"event": "worker_error", "call": current, "error_type": type(error).__name__},
            )
    finally:
        channel.close()


def validate_response(
    spec: ProbeCall, payload: dict[str, Any], headers: dict[str, list[str]]
) -> str | None:
    """Discover reference headers once; reject subsequent drift, not guessed names."""
    if payload.get("error_code") != "0":
        return "VENDOR_OR_RESPONSE_ERROR"
    fields, rows = payload.get("fields"), payload.get("rows")
    if not isinstance(fields, list) or not isinstance(rows, list):
        return "RESPONSE_SHAPE_INVALID"
    if len(rows) > MAX_ROWS or payload.get("page") != "1":
        return "PAGE_OR_ROW_LIMIT"
    if str(payload.get("page_capacity")) != "2000":
        return "PAGE_CAPACITY_DRIFT"
    if spec.endpoint in {"login", "logout"}:
        return None if not rows else "SESSION_ROWS_UNEXPECTED"
    if not fields or not all(isinstance(f, str) and f for f in fields):
        return "FIELDS_INVALID"
    if len(set(fields)) != len(fields):
        return "FIELDS_DUPLICATED"
    if any(
        not isinstance(row, list)
        or len(row) != len(fields)
        or not all(isinstance(cell, str) for cell in row)
        for row in rows
    ):
        return "ROW_SHAPE_INVALID"
    if spec.endpoint == "query_history_k_data_plus" and fields != FIELDS.split(","):
        return "DAILY_FIELDS_DRIFT"
    if fields != headers.setdefault(spec.endpoint, fields):
        return "REFERENCE_FIELDS_DRIFT"
    params = dict(spec.params)
    if "code" not in fields:
        return "CODE_FIELD_MISSING"
    if any(row[fields.index("code")] != params["code"] for row in rows):
        return "SECURITY_OUTSIDE_SCOPE"
    if spec.endpoint == "query_history_k_data_plus":
        dates = [row[fields.index("date")] for row in rows]
        if dates != sorted(set(dates)) or any(
            not params["start_date"] <= d <= params["end_date"] for d in dates
        ):
            return "DATES_OUTSIDE_SCOPE_OR_DUPLICATE"
    return None


def run_probe(
    output: Path,
    *,
    worker: Callable[[Connection], None] = sdk_worker,
    call_timeout: float = CALL_TIMEOUT,
    total_timeout: float = TOTAL_TIMEOUT,
) -> dict[str, Any]:
    """Execute once; test injection is Python-only, CLI cannot change scope/limits."""
    for value, maximum in ((call_timeout, CALL_TIMEOUT), (total_timeout, TOTAL_TIMEOUT)):
        if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= maximum:
            raise ValueError("TIMEOUT_INVALID")
    # mkdir exclusive is the attempt guard, before process creation/network.
    output.mkdir(parents=True, exist_ok=False)
    specs = frozen_calls()
    plan = {
        "schema_version": "s2-01-source-probe.a1",
        "calls": [asdict(s) for s in specs],
        "host": HOST,
        "port": PORT,
        "sdk_version": "0.9.3",
        "call_timeout": call_timeout,
        "total_timeout": total_timeout,
        "max_rows": MAX_ROWS,
        "max_response_bytes": MAX_BYTES,
        "retry_limit": 0,
        "protocol_request_limit": 18,
        "connect_limit": 1,
        "authorization": "User approved S2-01_PROBE_PLAN.md first attempt, 2026-09-09",
        "executor_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "dependency_lock_sha256": hashlib.sha256(
            (Path(__file__).resolve().parents[4] / "uv.lock").read_bytes()
        ).hexdigest(),
    }
    (output / "plan.json").write_bytes(_encoded(plan))
    context = multiprocessing.get_context("spawn")
    channel, child = context.Pipe(duplex=True)
    process = context.Process(target=worker, args=(child,), daemon=True)
    inbox: queue.Queue[bytes | Exception] = queue.Queue(maxsize=1)
    stopped = threading.Event()

    def receive() -> None:
        while not stopped.is_set():
            try:
                item: bytes | Exception = channel.recv_bytes(MAX_BYTES + 65536)
            except Exception as error:
                item = error
            while not stopped.is_set():
                try:
                    inbox.put(item, timeout=0.1)
                    break
                except queue.Full:
                    continue
            if isinstance(item, Exception):
                return

    reader = threading.Thread(target=receive, daemon=True)
    began = time.monotonic()
    total_deadline = began + total_timeout
    deadline = min(total_deadline, began + call_timeout)
    started = completed = protocol = connects = response_bytes = 0
    active: int | None = None
    active_protocol = 0
    call_finished = False
    headers: dict[str, list[str]] = {}
    status, reason = "FAILED", "WORKER_NOT_STARTED"
    call_started = began
    journal = (output / "calls.jsonl").open("x", encoding="utf-8", newline="\n")

    def record(event: str, **details: Any) -> None:
        journal.write(_encoded({"event": event, "at_utc": _utc(), **details}).decode() + "\n")
        journal.flush()
        os.fsync(journal.fileno())

    try:
        process.start()
        child.close()
        reader.start()
        while completed < len(specs):
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise queue.Empty
                incoming = inbox.get(timeout=remaining)
            except queue.Empty:
                reason = "TOTAL_TIMEOUT" if time.monotonic() >= total_deadline else "CALL_TIMEOUT"
                break
            if isinstance(incoming, Exception):
                raise incoming
            message = json.loads(incoming)
            event, index = message.get("event"), message.get("call")
            if event == "start" and active is None and index == started + 1 and index <= 18:
                active = index
                started += 1
                active_protocol = 0
                call_finished = False
                call_started = time.monotonic()
                deadline = min(total_deadline, call_started + call_timeout)
                record("call_started", call=index, **asdict(specs[index - 1]))
            elif event == "connect" and index == active == 1 and connects == 0:
                connects += 1
                record("connect_started", call=index, host=HOST, port=PORT)
            elif (
                event == "protocol"
                and index == active
                and active is not None
                and connects == 1
                and active_protocol == 0
                and protocol < 18
            ):
                active_protocol += 1
                protocol += 1
                record("protocol_request_started", call=index, ordinal=protocol)
            elif event == "response" and index == active and active is not None:
                payload = message["payload"]
                encoded = _encoded(payload)
                if response_bytes + len(encoded) > MAX_BYTES:
                    reason = "TOTAL_RESPONSE_SIZE_LIMIT"
                    record(
                        "response_rejected",
                        call=index,
                        bytes=len(encoded),
                        sha256=hashlib.sha256(encoded).hexdigest(),
                    )
                    break
                response_bytes += len(encoded)
                (output / f"response-{index:02}.json").write_bytes(encoded)
                error = validate_response(specs[index - 1], payload, headers)
                if active_protocol != 1:
                    error = error or "PROTOCOL_COUNT_INVALID"
                record(
                    "call_finished",
                    call=index,
                    error_code=payload.get("error_code"),
                    rows=len(payload["rows"]) if isinstance(payload.get("rows"), list) else None,
                    elapsed=time.monotonic() - call_started,
                    classification=error
                    or (
                        "EMPTY_UNQUALIFIED"
                        if not payload.get("rows")
                        else "OBSERVATION_UNQUALIFIED"
                    ),
                )
                call_finished = True
                if error:
                    reason = error
                    break
                completed += 1
                active = None
                # A worker must start the next call promptly; this does not extend
                # either the previous call's deadline or the total deadline.
                deadline = min(total_deadline, time.monotonic() + call_timeout)
            else:
                reason = "WORKER_ERROR" if event == "worker_error" else "WORKER_PROTOCOL_INVALID"
                record("worker_message_rejected", message=message)
                break
            if time.monotonic() >= deadline:
                reason = "CALL_OR_TOTAL_TIMEOUT"
                break
            channel.send_bytes(b"continue")
        else:
            status, reason = "OBSERVATIONS_COLLECTED", "QUALIFICATION_STILL_REQUIRED"
    except Exception as error:
        reason = f"LOCAL_{type(error).__name__}"
    finally:
        stopped.set()
        if process.pid is not None:
            if active is not None or completed != len(specs):
                process.terminate()
            process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
        channel.close()
        child.close()
        if reader.ident is not None:
            reader.join(timeout=0.5)
        if active is not None and not call_finished:
            record(
                "call_aborted", call=active, reason=reason, elapsed=time.monotonic() - call_started
            )
        record("attempt_finished", status=status, reason=reason, active_call=active)
        journal.close()
    result = {
        "status": status,
        "reason": reason,
        "started_sdk_calls": started,
        "completed_sdk_calls": completed,
        "protocol_requests_started": protocol,
        "connections_started": connects,
        "response_bytes": response_bytes,
        "elapsed_seconds": time.monotonic() - began,
        "qualified": False,
    }
    (output / "result.json").write_bytes(_encoded(result))
    manifest = [
        {
            "path": p.name,
            "bytes": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in sorted(output.iterdir())
        if p.is_file()
    ]
    (output / "sha256.json").write_bytes(_encoded(manifest))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-approved-a1", action="store_true", required=True)
    parser.parse_args()
    output = Path(__file__).resolve().parents[4] / OUTPUT_RELATIVE
    result = run_probe(output)
    print(_encoded(result).decode())
    return 0 if result["status"] == "OBSERVATIONS_COLLECTED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
