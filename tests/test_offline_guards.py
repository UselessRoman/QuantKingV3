from __future__ import annotations

import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from . import conftest as guard_policy

_FORMAL_PATHS = (
    Path(r"E:\QuantKingV3Data") / "__guard_probe__",
    Path(r"F:\QuantKingV3Backups") / "__guard_probe__",
    Path(r"E:\QuantKing") / "__guard_probe__",
)
_ORIGINAL_SINGLE_PATH_CALLS = {
    name: getattr(os, name) for name in ("chdir", "remove", "rmdir", "unlink")
}
_ORIGINAL_TWO_PATH_CALLS = {
    name: getattr(os, name) for name in ("link", "rename", "replace", "symlink")
}
_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_STAT = os.stat


@pytest.mark.parametrize("formal_path", _FORMAL_PATHS, ids=("data", "offsite", "legacy"))
@pytest.mark.parametrize("operation", sorted(_ORIGINAL_SINGLE_PATH_CALLS))
def test_single_path_system_calls_are_blocked_before_dispatch(
    formal_path: Path, operation: str
) -> None:
    guarded = getattr(os, operation)

    assert guarded is not _ORIGINAL_SINGLE_PATH_CALLS[operation]
    with pytest.raises(RuntimeError, match="default tests cannot access formal path"):
        guarded(formal_path)


@pytest.mark.parametrize("formal_path", _FORMAL_PATHS, ids=("data", "offsite", "legacy"))
@pytest.mark.parametrize("operation", sorted(_ORIGINAL_TWO_PATH_CALLS))
def test_two_path_system_calls_block_either_formal_argument(
    tmp_path: Path, formal_path: Path, operation: str
) -> None:
    guarded = getattr(os, operation)
    ordinary_path = tmp_path / "ordinary"

    assert guarded is not _ORIGINAL_TWO_PATH_CALLS[operation]
    with pytest.raises(RuntimeError, match="default tests cannot access formal path"):
        guarded(formal_path, ordinary_path)
    with pytest.raises(RuntimeError, match="default tests cannot access formal path"):
        guarded(ordinary_path, formal_path)


@pytest.mark.parametrize("formal_path", _FORMAL_PATHS, ids=("data", "offsite", "legacy"))
def test_path_unlink_is_blocked_through_os_unlink(formal_path: Path) -> None:
    assert os.unlink is not _ORIGINAL_SINGLE_PATH_CALLS["unlink"]
    with pytest.raises(RuntimeError, match="default tests cannot access formal path"):
        formal_path.unlink()


def test_single_path_wrapper_preserves_integer_file_descriptor_semantics() -> None:
    calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    def fake(path: object, *args: object, **kwargs: object) -> str:
        calls.append((path, args, kwargs))
        return "called"

    guarded: Callable[..., Any] = guard_policy._guard_path_call(fake)

    assert guarded(7, "argument", follow_symlinks=False) == "called"
    assert calls == [(7, ("argument",), {"follow_symlinks": False})]


def test_socket_connect_and_getaddrinfo_are_blocked_before_network_access() -> None:
    assert socket.socket.connect is not _ORIGINAL_CONNECT
    assert socket.getaddrinfo is not _ORIGINAL_GETADDRINFO

    with (
        socket.socket() as client,
        pytest.raises(RuntimeError, match="default tests cannot access the network"),
    ):
        client.connect(("127.0.0.1", 9))
    with pytest.raises(RuntimeError, match="default tests cannot access the network"):
        socket.getaddrinfo("localhost", 80)


@pytest.mark.real_backup
def test_real_backup_marker_only_releases_formal_paths_not_network() -> None:
    assert os.stat is _ORIGINAL_STAT
    assert socket.socket.connect is not _ORIGINAL_CONNECT
    assert socket.getaddrinfo is not _ORIGINAL_GETADDRINFO

    with (
        socket.socket() as client,
        pytest.raises(RuntimeError, match="default tests cannot access the network"),
    ):
        client.connect(("127.0.0.1", 9))
