from __future__ import annotations

import socket

import pytest

from quantking.data.adapters.sources.ipv4_resolution import (
    EASTMONEY_RESOLUTION_POLICY_VERSION,
    force_ipv4_resolution,
)


def _mixed_results() -> list[tuple[object, ...]]:
    return [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ]


def _fake_getaddrinfo(
    host: object,
    port: object,
    family: object = socket.AF_UNSPEC,
    type: object = socket.SOCK_STREAM,
    proto: object = 0,
    flags: object = 0,
) -> list[tuple[object, ...]]:
    del host, port, family, type, proto, flags
    return _mixed_results()


def test_force_ipv4_resolution_filters_to_af_inet_and_restores() -> None:
    original = socket.getaddrinfo
    try:
        socket.getaddrinfo = _fake_getaddrinfo  # type: ignore[assignment]
        with force_ipv4_resolution():
            results = socket.getaddrinfo("push2his.eastmoney.com", 443)
            assert [item[0] for item in results] == [socket.AF_INET]
        assert socket.getaddrinfo is _fake_getaddrinfo
        assert [item[0] for item in socket.getaddrinfo("host", 443)] == [
            socket.AF_INET6,
            socket.AF_INET,
        ]
    finally:
        socket.getaddrinfo = original


def test_force_ipv4_resolution_restores_after_exception() -> None:
    original = socket.getaddrinfo
    try:
        socket.getaddrinfo = _fake_getaddrinfo  # type: ignore[assignment]
        with pytest.raises(RuntimeError), force_ipv4_resolution():
            raise RuntimeError("bounded")
        assert socket.getaddrinfo is _fake_getaddrinfo
    finally:
        socket.getaddrinfo = original


def test_policy_version_is_frozen() -> None:
    assert EASTMONEY_RESOLUTION_POLICY_VERSION == "eastmoney-ipv4-only.v1"
