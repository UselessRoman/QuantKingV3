"""Immutable, vendor-neutral compatibility probe contracts."""

from __future__ import annotations

import json
import os
import platform
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from time import perf_counter_ns
from typing import Any


class ProbeStatus(StrEnum):
    """Stable outcome values for a probe or aggregate report."""

    PASSED = "passed"
    FAILED = "failed"


class ProbeErrorCode(StrEnum):
    """Machine-stable failure categories that never expose vendor details."""

    DEPENDENCY_MISSING = "dependency_missing"
    VERSION_UNAVAILABLE = "version_unavailable"
    VERSION_MISMATCH = "version_mismatch"
    INTERFACE_MISMATCH = "interface_mismatch"
    INVALID_INPUT = "invalid_input"
    EXECUTION_FAILED = "execution_failed"
    SECURITY_BOUNDARY = "security_boundary"


class CompatibilityProbeError(Exception):
    """An expected probe failure with a safe public message."""

    def __init__(self, code: ProbeErrorCode, message: str) -> None:
        if not isinstance(code, ProbeErrorCode):
            raise TypeError("code must be a ProbeErrorCode")
        if not isinstance(message, str) or not message.strip():
            raise TypeError("message must be non-empty text")
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ProbePayload:
    """Successful evidence produced inside one adapter boundary."""

    version: str
    steps: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise CompatibilityProbeError(
                ProbeErrorCode.VERSION_UNAVAILABLE,
                "dependency version is unavailable",
            )
        if not self.steps or any(
            not isinstance(step, str) or not step.strip() for step in self.steps
        ):
            raise CompatibilityProbeError(
                ProbeErrorCode.INTERFACE_MISMATCH,
                "probe did not record valid execution steps",
            )


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """A single immutable, JSON-safe compatibility result."""

    name: str
    status: ProbeStatus
    version: str | None
    steps: tuple[str, ...]
    duration_ms: float
    process_id: int
    error_code: ProbeErrorCode | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("probe name must be non-empty text")
        if self.duration_ms < 0:
            raise ValueError("probe duration cannot be negative")
        if self.process_id <= 0:
            raise ValueError("probe process ID must be positive")
        if self.status is ProbeStatus.PASSED:
            if self.version is None or self.error_code is not None or self.message is not None:
                raise ValueError("passed probes require a version and no error")
        elif self.error_code is None or self.message is None:
            raise ValueError("failed probes require an error code and message")

    def to_dict(self) -> dict[str, Any]:
        """Return a fresh JSON-compatible representation."""
        return {
            "name": self.name,
            "status": self.status.value,
            "version": self.version,
            "steps": list(self.steps),
            "duration_ms": self.duration_ms,
            "process_id": self.process_id,
            "error_code": self.error_code.value if self.error_code is not None else None,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ProbeReport:
    """One-process aggregate report for every requested subprobe."""

    schema_version: str
    status: ProbeStatus
    python_version: str
    platform: str
    process_id: int
    results: tuple[ProbeResult, ...]

    @classmethod
    def from_results(cls, results: tuple[ProbeResult, ...]) -> ProbeReport:
        """Build a report and reject evidence mixed across processes."""
        if not results:
            raise ValueError("probe report requires at least one result")
        process_ids = {result.process_id for result in results}
        if len(process_ids) != 1:
            raise ValueError("all probe results must come from the same process")
        status = (
            ProbeStatus.PASSED
            if all(result.status is ProbeStatus.PASSED for result in results)
            else ProbeStatus.FAILED
        )
        return cls(
            schema_version="1",
            status=status,
            python_version=platform.python_version(),
            platform=f"{platform.system()}-{platform.machine()}",
            process_id=results[0].process_id,
            results=results,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a fresh JSON-compatible representation."""
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "python_version": self.python_version,
            "python_implementation": sys.implementation.name,
            "platform": self.platform,
            "process_id": self.process_id,
            "results": [result.to_dict() for result in self.results],
        }

    def to_json(self) -> str:
        """Serialize deterministically without tracebacks or filesystem paths."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


def capture_probe(name: str, operation: Callable[[], ProbePayload]) -> ProbeResult:
    """Execute one adapter operation and convert every failure to stable evidence."""
    started = perf_counter_ns()
    process_id = os.getpid()
    try:
        payload = operation()
    except CompatibilityProbeError as error:
        return ProbeResult(
            name=name,
            status=ProbeStatus.FAILED,
            version=None,
            steps=(),
            duration_ms=_elapsed_ms(started),
            process_id=process_id,
            error_code=error.code,
            message=error.message,
        )
    except (ImportError, ModuleNotFoundError):
        return ProbeResult(
            name=name,
            status=ProbeStatus.FAILED,
            version=None,
            steps=(),
            duration_ms=_elapsed_ms(started),
            process_id=process_id,
            error_code=ProbeErrorCode.DEPENDENCY_MISSING,
            message="locked dependency is unavailable",
        )
    except Exception:
        return ProbeResult(
            name=name,
            status=ProbeStatus.FAILED,
            version=None,
            steps=(),
            duration_ms=_elapsed_ms(started),
            process_id=process_id,
            error_code=ProbeErrorCode.EXECUTION_FAILED,
            message="dependency probe execution failed",
        )
    return ProbeResult(
        name=name,
        status=ProbeStatus.PASSED,
        version=payload.version,
        steps=payload.steps,
        duration_ms=_elapsed_ms(started),
        process_id=process_id,
    )


def _elapsed_ms(started: int) -> float:
    return round((perf_counter_ns() - started) / 1_000_000, 3)
