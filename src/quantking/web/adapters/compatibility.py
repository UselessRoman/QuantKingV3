"""Probe-only FastAPI adapter; this is not the production Web application."""

from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import patch

from quantking.common.compatibility import (
    CompatibilityProbeError,
    ProbeErrorCode,
    ProbePayload,
    ProbeResult,
    capture_probe,
)

_SOCKET_CONNECT = socket.socket.connect


def probe_fastapi(root: Path) -> ProbeResult:
    """Exercise a temporary health endpoint through the in-process TestClient."""
    return capture_probe("fastapi", lambda: _run_fastapi(root))


def _run_fastapi(_root: Path) -> ProbePayload:
    import asyncio

    import fastapi
    from fastapi.testclient import TestClient

    app = fastapi.FastAPI()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    previous_policy = asyncio.get_event_loop_policy()
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        # TestClient's Windows event-loop wakeup pipe uses a private loopback socket.
        # The ASGI request remains in-process and the fixed health route has no I/O.
        with patch.object(socket.socket, "connect", _SOCKET_CONNECT), TestClient(app) as client:
            response = client.get("/health")
    finally:
        asyncio.set_event_loop_policy(previous_policy)
    if response.status_code != 200 or response.json() != {"status": "ok"}:
        raise CompatibilityProbeError(
            ProbeErrorCode.EXECUTION_FAILED,
            "FastAPI in-process health check returned unexpected evidence",
        )
    return ProbePayload(
        str(fastapi.__version__),
        ("temporary_app_created", "testclient_health_requested", "health_response_verified"),
    )
