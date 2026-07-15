"""Stable public error contracts."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class ErrorCode(StrEnum):
    """Machine-stable error codes exposed across QuantKing boundaries."""

    CONFIGURATION = "configuration_error"
    INVALID_ID = "invalid_id"
    INVALID_TIME = "invalid_time"
    INVALID_PATH = "invalid_path"


class QuantKingError(Exception):
    """Base class for stable failures with immutable string context."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        context: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(code, ErrorCode):
            raise TypeError("code must be an ErrorCode")
        if not isinstance(message, str) or not message.strip():
            raise TypeError("message must be a non-empty string")

        normalized_context: dict[str, str] = {}
        for key, value in (context or {}).items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("error context keys and values must be strings")
            normalized_context[key] = value

        self.code = code
        self.message = message
        self.context: Mapping[str, str] = MappingProxyType(dict(sorted(normalized_context.items())))
        super().__init__(message)

    def __str__(self) -> str:
        rendered = f"[{self.code.value}] {self.message}"
        if self.context:
            details = ", ".join(f"{key}={value}" for key, value in self.context.items())
            rendered = f"{rendered} ({details})"
        return rendered


class ConfigurationError(QuantKingError):
    """Configuration loading or validation failed."""

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        super().__init__(ErrorCode.CONFIGURATION, message, context=context)


class InvalidIdError(QuantKingError):
    """An entity identifier violated its stable format."""

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        super().__init__(ErrorCode.INVALID_ID, message, context=context)


class InvalidTimeError(QuantKingError):
    """A timestamp or trading date violated its type contract."""

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        super().__init__(ErrorCode.INVALID_TIME, message, context=context)


class InvalidPathError(QuantKingError):
    """A managed path violated a root or containment boundary."""

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        super().__init__(ErrorCode.INVALID_PATH, message, context=context)
