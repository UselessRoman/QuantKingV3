"""Standard-library logging handler for one-JSON-object-per-line job logs."""

from __future__ import annotations

import json
import logging
import re
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from quantking.common.errors import InvalidPathError
from quantking.common.paths import resolve_under

_REDACTED: Final = "[REDACTED]"
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "account",
        "accountid",
        "apikey",
        "authorization",
        "cookie",
        "password",
        "secret",
        "token",
    }
)
_MESSAGE_SECRET = re.compile(
    r"(?i)\b(password|token|secret|authorization|api[\s_-]*key|cookie|account(?:[\s_-]*id)?)"
    r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_STANDARD_RECORD_KEYS = frozenset(logging.makeLogRecord({}).__dict__)


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.casefold())


def _redact_text(value: str) -> str:
    return _MESSAGE_SECRET.sub(lambda match: f"{match.group(1)}={_REDACTED}", value)


def _sensitive_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _sanitize(value: object, *, key: str | None = None) -> object:
    if key is not None and _sensitive_key(key):
        return _REDACTED
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): _sanitize(item, key=str(item_key)) for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


def _exception_payload(error: BaseException) -> dict[str, object]:
    chain: list[dict[str, str]] = []
    current = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        chain.append(
            {
                "type": type(current).__name__,
                "message": _redact_text(str(current)),
            }
        )
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return {
        "type": type(error).__name__,
        "message": _redact_text(str(error)),
        "stack": _redact_text("".join(traceback.format_exception(error))),
        "chain": chain,
    }


class JsonLinesFormatter(logging.Formatter):
    """Convert a standard LogRecord into the stable redacted JSON schema."""

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", None)
        if not isinstance(event, str) or not event.strip():
            raise ValueError("structured log records require a non-empty event")
        payload: dict[str, Any] = {
            "event": event,
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat()
            .removesuffix("+00:00")
            + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": _redact_text(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_KEYS or key in {"event", "message"}:
                continue
            payload[key] = _sanitize(value, key=key)
        if record.exc_info and record.exc_info[1] is not None:
            payload["exception"] = _exception_payload(record.exc_info[1])
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class JsonLinesHandler(logging.Handler):
    """Write structured, recursively redacted records to an explicit managed path."""

    def __init__(self, managed_root: Path, log_id: str | Path) -> None:
        super().__init__()
        if not isinstance(managed_root, Path) or not managed_root.is_absolute():
            raise InvalidPathError("managed log root must be absolute")
        if not managed_root.is_dir():
            raise InvalidPathError(
                "managed log root must already exist",
                context={"root": str(managed_root)},
            )
        path = resolve_under(managed_root, log_id)
        if path == managed_root.resolve(strict=False):
            raise InvalidPathError("log_id must identify a file below the managed log root")
        if path.suffix.casefold() != ".jsonl":
            raise InvalidPathError("managed job logs must use the .jsonl suffix")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("a", encoding="utf-8", newline="\n")
        self.setFormatter(JsonLinesFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._stream.write(self.format(record))
            self._stream.write("\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if hasattr(self, "_stream") and not self._stream.closed:
            self._stream.close()
        super().close()
