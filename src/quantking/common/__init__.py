"""Stable shared contracts that do not depend on external adapters."""

from quantking.common.compatibility import (
    CompatibilityProbeError,
    ProbeErrorCode,
    ProbeReport,
    ProbeResult,
    ProbeStatus,
)
from quantking.common.config import AppSettings, load_settings
from quantking.common.errors import (
    ConfigurationError,
    ErrorCode,
    InvalidIdError,
    InvalidPathError,
    InvalidTimeError,
    QuantKingError,
)
from quantking.common.ids import EntityId, new_entity_id, parse_entity_id
from quantking.common.paths import RuntimePaths, resolve_under
from quantking.common.time import TradingDate, UtcTimestamp, utc_now

__all__ = [
    "AppSettings",
    "CompatibilityProbeError",
    "ConfigurationError",
    "EntityId",
    "ErrorCode",
    "InvalidIdError",
    "InvalidPathError",
    "InvalidTimeError",
    "ProbeErrorCode",
    "ProbeReport",
    "ProbeResult",
    "ProbeStatus",
    "QuantKingError",
    "RuntimePaths",
    "TradingDate",
    "UtcTimestamp",
    "load_settings",
    "new_entity_id",
    "parse_entity_id",
    "resolve_under",
    "utc_now",
]
