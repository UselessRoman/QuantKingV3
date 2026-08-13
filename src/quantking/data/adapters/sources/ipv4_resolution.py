"""Bounded IPv4-only address resolution for the Eastmoney adapter (S1-07K3B).

Eastmoney's IPv6 CDN (push2hisipv6.trafficmanager.cn) accepts TCP but cuts the
HTTP data path, which urllib3 surfaces as ``RemoteDisconnected``. Evidence:
``docs/stage1/evidence/k3b_eastmoney_ipv6_root_cause_2026-08-13.json``.

Until the network or CDN path is repaired, the Eastmoney adapter resolves
IPv4 only. This is an explicit per-upstream address-family policy — not IP
evasion, UA spoofing, or silent fallback. The patch is process-global for the
duration of one vendor call; EOD workers and this CLI are single-threaded.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager

EASTMONEY_RESOLUTION_POLICY_VERSION = "eastmoney-ipv4-only.v1"


@contextmanager
def force_ipv4_resolution() -> Iterator[None]:
    original = socket.getaddrinfo

    def ipv4_only(
        host: bytes | str | None,
        port: bytes | str | int | None,
        family: int = socket.AF_UNSPEC,
        type: int = socket.SOCK_STREAM,
        proto: int = 0,
        flags: int = 0,
    ) -> object:
        results = original(host, port, family, type, proto, flags)
        return [item for item in results if item[0] == socket.AF_INET]

    socket.getaddrinfo = ipv4_only  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.getaddrinfo = original


__all__ = [
    "EASTMONEY_RESOLUTION_POLICY_VERSION",
    "force_ipv4_resolution",
]
