"""Strict default-TOML plus process-environment configuration loading."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quantking.common.errors import ConfigurationError, InvalidPathError
from quantking.common.paths import RuntimePaths

_EXPECTED_KEYS: Mapping[str, frozenset[str]] = {
    "paths": frozenset({"code_root", "data_root", "offsite_backup_root"}),
    "web": frozenset({"host", "port"}),
    "sqlite": frozenset(
        {
            "busy_timeout_ms",
            "max_attempts",
            "retry_base_delay_ms",
            "retry_cap_delay_ms",
            "retry_jitter_ratio",
            "heartbeat_min_interval_ms",
            "progress_min_interval_ms",
        }
    ),
}
_ENV_FIELDS: Mapping[str, tuple[str, str]] = {
    "QUANTKING_CODE_ROOT": ("paths", "code_root"),
    "QUANTKING_DATA_ROOT": ("paths", "data_root"),
    "QUANTKING_OFFSITE_BACKUP_ROOT": ("paths", "offsite_backup_root"),
    "QUANTKING_WEB_HOST": ("web", "host"),
    "QUANTKING_WEB_PORT": ("web", "port"),
    "QUANTKING_SQLITE_BUSY_TIMEOUT_MS": ("sqlite", "busy_timeout_ms"),
    "QUANTKING_SQLITE_MAX_ATTEMPTS": ("sqlite", "max_attempts"),
    "QUANTKING_SQLITE_RETRY_BASE_DELAY_MS": ("sqlite", "retry_base_delay_ms"),
    "QUANTKING_SQLITE_RETRY_CAP_DELAY_MS": ("sqlite", "retry_cap_delay_ms"),
    "QUANTKING_SQLITE_RETRY_JITTER_RATIO": ("sqlite", "retry_jitter_ratio"),
    "QUANTKING_JOB_HEARTBEAT_MIN_INTERVAL_MS": ("sqlite", "heartbeat_min_interval_ms"),
    "QUANTKING_JOB_PROGRESS_MIN_INTERVAL_MS": ("sqlite", "progress_min_interval_ms"),
}


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Validated Stage 0 settings with explicit fields and ranges."""

    paths: RuntimePaths
    web_host: str
    web_port: int
    sqlite_busy_timeout_ms: int
    sqlite_max_attempts: int
    sqlite_retry_base_delay_ms: int
    sqlite_retry_cap_delay_ms: int
    sqlite_retry_jitter_ratio: float
    job_heartbeat_min_interval_ms: int
    job_progress_min_interval_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.paths, RuntimePaths):
            raise ConfigurationError(
                "paths must be validated RuntimePaths", context={"field": "paths"}
            )
        if not isinstance(self.web_host, str) or self.web_host != "127.0.0.1":
            raise ConfigurationError(
                "web_host must be the IPv4 loopback address",
                context={"field": "web_host"},
            )
        if (
            isinstance(self.web_port, bool)
            or not isinstance(self.web_port, int)
            or not 1 <= self.web_port <= 65535
        ):
            raise ConfigurationError(
                "web_port must be between 1 and 65535",
                context={"field": "web_port"},
            )
        if (
            isinstance(self.sqlite_busy_timeout_ms, bool)
            or not isinstance(self.sqlite_busy_timeout_ms, int)
            or not 1 <= self.sqlite_busy_timeout_ms <= 60_000
        ):
            raise ConfigurationError(
                "sqlite_busy_timeout_ms must be between 1 and 60000",
                context={"field": "sqlite_busy_timeout_ms"},
            )
        if not 1 <= self.sqlite_max_attempts <= 20:
            raise ConfigurationError(
                "sqlite_max_attempts must be between 1 and 20",
                context={"field": "sqlite_max_attempts"},
            )
        if (
            not 0 <= self.sqlite_retry_base_delay_ms <= 5000
            or not self.sqlite_retry_base_delay_ms <= self.sqlite_retry_cap_delay_ms <= 5000
        ):
            raise ConfigurationError(
                "SQLite retry delay bounds are invalid",
                context={"field": "sqlite_retry_cap_delay_ms"},
            )
        if not 0.0 <= self.sqlite_retry_jitter_ratio <= 1.0:
            raise ConfigurationError(
                "sqlite_retry_jitter_ratio must be between 0 and 1",
                context={"field": "sqlite_retry_jitter_ratio"},
            )
        if not 1 <= self.job_heartbeat_min_interval_ms <= 60_000:
            raise ConfigurationError(
                "job_heartbeat_min_interval_ms must be between 1 and 60000",
                context={"field": "job_heartbeat_min_interval_ms"},
            )
        if not 1 <= self.job_progress_min_interval_ms <= 60_000:
            raise ConfigurationError(
                "job_progress_min_interval_ms must be between 1 and 60000",
                context={"field": "job_progress_min_interval_ms"},
            )


def _validate_document(document: dict[str, Any]) -> None:
    unknown_sections = set(document) - set(_EXPECTED_KEYS)
    if unknown_sections:
        raise ConfigurationError(
            "configuration contains unknown sections",
            context={"keys": ",".join(sorted(unknown_sections))},
        )

    for section, expected_fields in _EXPECTED_KEYS.items():
        raw_section = document.get(section)
        if not isinstance(raw_section, dict):
            raise ConfigurationError(
                "configuration section is missing or invalid", context={"section": section}
            )
        unknown_fields = set(raw_section) - expected_fields
        if unknown_fields:
            raise ConfigurationError(
                "configuration contains unknown keys",
                context={"keys": ",".join(f"{section}.{key}" for key in sorted(unknown_fields))},
            )
        missing_fields = expected_fields - set(raw_section)
        if missing_fields:
            raise ConfigurationError(
                "configuration is missing required keys",
                context={"keys": ",".join(f"{section}.{key}" for key in sorted(missing_fields))},
            )


def _apply_environment(document: dict[str, Any], environ: Mapping[str, str]) -> None:
    prefixed_keys = {key for key in environ if key.startswith("QUANTKING_")}
    unknown = prefixed_keys - set(_ENV_FIELDS)
    if unknown:
        raise ConfigurationError(
            "environment contains unknown QUANTKING_ keys",
            context={"keys": ",".join(sorted(unknown))},
        )

    for key in prefixed_keys:
        value = environ[key]
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationError("environment override must not be empty", context={"key": key})
        section, field = _ENV_FIELDS[key]
        document[section][field] = value


def _integer(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, base=10)
    raise ValueError(f"{field} must be an integer")


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value


def _number(value: object, *, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number")
    if isinstance(value, (int, float, str)):
        return float(value)
    raise ValueError(f"{field} must be a number")


def load_settings(default_config: Path, *, environ: Mapping[str, str] | None = None) -> AppSettings:
    """Load explicit non-secret defaults, then apply known process environment values."""
    try:
        with default_config.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(
            "default configuration could not be loaded",
            context={"path": str(default_config)},
        ) from error

    _validate_document(document)
    _apply_environment(document, os.environ if environ is None else environ)

    try:
        paths = RuntimePaths(
            code_root=Path(_text(document["paths"]["code_root"], field="code_root")),
            data_root=Path(_text(document["paths"]["data_root"], field="data_root")),
            offsite_backup_root=Path(
                _text(document["paths"]["offsite_backup_root"], field="offsite_backup_root")
            ),
        )
        return AppSettings(
            paths=paths,
            web_host=_text(document["web"]["host"], field="web_host"),
            web_port=_integer(document["web"]["port"], field="web_port"),
            sqlite_busy_timeout_ms=_integer(
                document["sqlite"]["busy_timeout_ms"], field="sqlite_busy_timeout_ms"
            ),
            sqlite_max_attempts=_integer(
                document["sqlite"]["max_attempts"], field="sqlite_max_attempts"
            ),
            sqlite_retry_base_delay_ms=_integer(
                document["sqlite"]["retry_base_delay_ms"],
                field="sqlite_retry_base_delay_ms",
            ),
            sqlite_retry_cap_delay_ms=_integer(
                document["sqlite"]["retry_cap_delay_ms"],
                field="sqlite_retry_cap_delay_ms",
            ),
            sqlite_retry_jitter_ratio=_number(
                document["sqlite"]["retry_jitter_ratio"],
                field="sqlite_retry_jitter_ratio",
            ),
            job_heartbeat_min_interval_ms=_integer(
                document["sqlite"]["heartbeat_min_interval_ms"],
                field="job_heartbeat_min_interval_ms",
            ),
            job_progress_min_interval_ms=_integer(
                document["sqlite"]["progress_min_interval_ms"],
                field="job_progress_min_interval_ms",
            ),
        )
    except InvalidPathError as error:
        raise ConfigurationError("runtime path configuration is invalid") from error
    except ValueError as error:
        raise ConfigurationError("configuration value is invalid") from error
