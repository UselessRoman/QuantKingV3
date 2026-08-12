"""Global pytest policy for the default offline suite."""

from __future__ import annotations

import builtins
import io
import ntpath
import os
import socket
from collections.abc import Callable, Iterator
from typing import Any

import pytest

_FORMAL_ROOTS = (
    ntpath.normcase(ntpath.abspath(r"E:\QuantKingV3Data")),
    ntpath.normcase(ntpath.abspath(r"F:\QuantKingV3Backups")),
    ntpath.normcase(ntpath.abspath(r"E:\QuantKing")),
)


def pytest_configure(config: pytest.Config) -> None:
    """Register every test class that requires explicit external authorization."""
    config.addinivalue_line("markers", "real_external: accesses an authorized external target")
    config.addinivalue_line("markers", "real_akshare: accesses real AKShare upstream services")
    config.addinivalue_line(
        "markers", "real_k3_sources: accesses fixed BaoStock and AKShare probe targets"
    )
    config.addinivalue_line("markers", "real_legacy_data: reads the authorized legacy-data root")
    config.addinivalue_line("markers", "real_backup: writes a real data or backup root")


def _formal_path(path: object) -> bool:
    if isinstance(path, int):
        return False
    try:
        normalized = ntpath.normcase(ntpath.abspath(os.fsdecode(os.fspath(path))))
    except TypeError:
        return False
    return any(
        normalized == root or normalized.startswith(f"{root}{ntpath.sep}") for root in _FORMAL_ROOTS
    )


def _guard_path_call(function: Callable[..., Any]) -> Callable[..., Any]:
    def guarded(path: object = ".", *args: object, **kwargs: object) -> Any:
        if _formal_path(path):
            raise RuntimeError(f"default tests cannot access formal path: {path!s}")
        return function(path, *args, **kwargs)

    return guarded


def _guard_two_path_call(function: Callable[..., Any]) -> Callable[..., Any]:
    def guarded(src: object, dst: object, *args: object, **kwargs: object) -> Any:
        for path in (src, dst):
            if _formal_path(path):
                raise RuntimeError(f"default tests cannot access formal path: {path!s}")
        return function(src, dst, *args, **kwargs)

    return guarded


@pytest.fixture(autouse=True)
def block_external_access(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[None]:
    """Keep unmarked tests away from the network and formal runtime roots."""

    def deny_network(*args: object, **kwargs: object) -> None:
        raise RuntimeError("default tests cannot access the network")

    network_authorized = any(
        request.node.get_closest_marker(marker) is not None
        for marker in (
            "real_external",
            "real_akshare",
            "real_k3_sources",
        )
    )
    if not network_authorized:
        monkeypatch.setattr(socket, "create_connection", deny_network)
        monkeypatch.setattr(socket, "getaddrinfo", deny_network)
        monkeypatch.setattr(socket.socket, "connect", deny_network)
        monkeypatch.setattr(socket.socket, "connect_ex", deny_network)

    external_path_authorized = any(
        request.node.get_closest_marker(marker) is not None
        for marker in (
            "real_external",
            "real_akshare",
            "real_k3_sources",
            "real_legacy_data",
            "real_backup",
        )
    )
    if not external_path_authorized:
        monkeypatch.setattr(builtins, "open", _guard_path_call(builtins.open))
        monkeypatch.setattr(io, "open", _guard_path_call(io.open))
        for name in (
            "chdir",
            "listdir",
            "lstat",
            "mkdir",
            "open",
            "remove",
            "rmdir",
            "scandir",
            "stat",
            "unlink",
        ):
            monkeypatch.setattr(os, name, _guard_path_call(getattr(os, name)))
        for name in ("link", "rename", "replace", "symlink"):
            monkeypatch.setattr(os, name, _guard_two_path_call(getattr(os, name)))

    yield
