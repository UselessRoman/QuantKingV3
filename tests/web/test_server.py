from __future__ import annotations

from fastapi import FastAPI

from quantking.web.main import LOOPBACK_HOST, run_server


def test_server_binding_is_fixed_to_loopback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, object] = {}

    def fake_run(app: FastAPI, *, host: str, port: int) -> None:
        captured.update(app=app, host=host, port=port)

    monkeypatch.setattr("quantking.web.main.uvicorn.run", fake_run)
    app = FastAPI()
    run_server(app, port=9123)

    assert LOOPBACK_HOST == "127.0.0.1"
    assert captured == {"app": app, "host": "127.0.0.1", "port": 9123}
