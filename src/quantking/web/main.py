"""Uvicorn entry helpers with a non-configurable loopback host."""

from __future__ import annotations

from typing import Final

import uvicorn
from fastapi import FastAPI

LOOPBACK_HOST: Final = "127.0.0.1"


def run_server(app: FastAPI, *, port: int) -> None:
    """Run the injected app on loopback only; callers may select a local port."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    uvicorn.run(app, host=LOOPBACK_HOST, port=port)
