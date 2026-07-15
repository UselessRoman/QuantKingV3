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
    INVALID_JOB_TRANSITION = "invalid_job_transition"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    JOB_NOT_FOUND = "job_not_found"
    PERSISTENCE_BUSY = "persistence_busy"
    UNKNOWN_JOB_TYPE = "unknown_job_type"
    UPSTREAM_JOB_NOT_READY = "upstream_job_not_ready"


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


class InvalidJobTransitionError(QuantKingError):
    """A durable job attempted a transition outside its state machine."""

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        super().__init__(ErrorCode.INVALID_JOB_TRANSITION, message, context=context)


class IdempotencyConflictError(QuantKingError):
    """An idempotency key was reused with materially different command input."""

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        super().__init__(ErrorCode.IDEMPOTENCY_CONFLICT, message, context=context)


class JobNotFoundError(QuantKingError):
    """A requested durable job does not exist."""

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        super().__init__(ErrorCode.JOB_NOT_FOUND, message, context=context)


class PersistenceBusyError(QuantKingError):
    """A replay-safe SQLite transaction exhausted its bounded lock retries."""

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        super().__init__(ErrorCode.PERSISTENCE_BUSY, message, context=context)


class UnknownJobTypeError(QuantKingError):
    """No explicitly registered Worker handler exists for a job type."""

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        super().__init__(ErrorCode.UNKNOWN_JOB_TYPE, message, context=context)


class UpstreamJobNotReadyError(QuantKingError):
    """A durable job cannot start because an upstream job has not succeeded."""

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        super().__init__(ErrorCode.UPSTREAM_JOB_NOT_READY, message, context=context)
