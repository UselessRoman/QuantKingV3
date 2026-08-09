from __future__ import annotations

import socket
from http.client import RemoteDisconnected

import pytest
from requests.exceptions import ProxyError, SSLError, Timeout
from urllib3.exceptions import ProtocolError

from quantking.data.adapters.sources.contracts import (
    NetworkReason,
    SourceErrorCode,
    classify_source_error,
)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ProxyError("secret proxy URL and credentials"), NetworkReason.PROXY),
        (socket.gaierror(-2, "secret hostname"), NetworkReason.DNS),
        (SSLError("secret certificate or URL"), NetworkReason.TLS),
        (Timeout("secret request URL"), NetworkReason.TIMEOUT),
        (ConnectionError("secret endpoint"), NetworkReason.CONNECTION),
        (OSError("secret provider detail"), NetworkReason.UNKNOWN),
    ],
)
def test_network_errors_are_stably_classified_without_message_leakage(
    error: BaseException,
    reason: NetworkReason,
) -> None:
    mapped = classify_source_error(error)

    assert mapped.code is SourceErrorCode.NETWORK
    assert mapped.network_reason is reason
    assert "secret" not in str(mapped).lower()
    assert "url" not in str(mapped).lower()


def test_protocol_error_chain_prefers_remote_disconnected_reason() -> None:
    remote = RemoteDisconnected("secret remote response")
    protocol = ProtocolError("secret pool", "secret protocol detail")
    protocol.__cause__ = remote

    mapped = classify_source_error(protocol)

    assert mapped.code is SourceErrorCode.NETWORK
    assert mapped.network_reason is NetworkReason.REMOTE_DISCONNECTED
    assert "secret" not in str(mapped).lower()


def test_non_network_error_has_no_network_reason() -> None:
    mapped = classify_source_error(ValueError("secret parse detail"))

    assert mapped.code is SourceErrorCode.PARSE
    assert mapped.network_reason is None
    assert "secret" not in str(mapped).lower()


def test_nested_429_remains_rate_limited() -> None:
    class Response:
        status_code = 429

    nested = RuntimeError("secret third-party detail")
    nested.response = Response()  # type: ignore[attr-defined]
    outer = ProtocolError("secret pool", "secret protocol detail")
    outer.__context__ = nested

    mapped = classify_source_error(outer)

    assert mapped.code is SourceErrorCode.RATE_LIMITED
    assert mapped.network_reason is None
    assert "secret" not in str(mapped).lower()
