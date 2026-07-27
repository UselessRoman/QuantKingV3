"""Uvicorn entry helpers with a non-configurable loopback host."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Final, cast

import uvicorn
from fastapi import FastAPI

LOOPBACK_HOST: Final = "127.0.0.1"


def run_server(app: FastAPI, *, port: int) -> None:
    """Run the injected app on loopback only; callers may select a local port."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    uvicorn.run(app, host=LOOPBACK_HOST, port=port)


def create_runtime_app() -> FastAPI:
    """Uvicorn factory for the fully injected local application."""
    from quantking.runtime import build_eod_runtime, load_runtime_settings

    return cast(FastAPI, build_eod_runtime(load_runtime_settings()).web_app)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quantking-web")
    parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    from quantking.runtime import build_eod_runtime, load_runtime_settings

    runtime = build_eod_runtime(load_runtime_settings())
    run_server(cast(FastAPI, runtime.web_app), port=runtime.settings.web_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
