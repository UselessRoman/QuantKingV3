"""Pure domain contracts for an explicitly authorised legacy DATA_ONLY import."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from quantking.data.models import DataProvider, DatasetKind, DataUpstream

LEGACY_IMPORT_ADAPTER_VERSION = "xtquant-legacy-data-only.k3c.v1"
LEGACY_IMPORT_MANIFEST_SCHEMA_VERSION = "legacy-import-manifest.k3c.v1"
LEGACY_INVENTORY_SCHEMA_VERSION = "legacy-inventory.k3c.v1"
K3C_SYNTHETIC_AUTHORIZATION_MARKER = "S1-07K3C-SYNTHETIC-FIXTURE-V1"
K3C_REAL_AUTHORIZATION_MARKER = "S1-07K3C-REAL-MIGRATION-V1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_CANONICAL_FIELDS = (
    "code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
)


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("legacy source path must be non-empty POSIX relative text")
    path = PurePosixPath(value)
    if path.is_absolute() or path.drive or ".." in path.parts or "." in path.parts:
        raise ValueError("legacy source path must not be absolute or traverse parents")
    if value != path.as_posix() or any(not part for part in path.parts):
        raise ValueError("legacy source path must use canonical POSIX form")
    return value


class LegacyImportMode(StrEnum):
    INVENTORY = "INVENTORY"
    PILOT = "PILOT"
    FULL = "FULL"


class LegacyEvidenceStatus(StrEnum):
    X_ONLY_UNVERIFIED = "X_ONLY_UNVERIFIED"
    X_A_CONSENSUS = "X_A_CONSENSUS"
    SERVICE_CONSENSUS_REPLACED_X = "SERVICE_CONSENSUS_REPLACED_X"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class LegacyImportAuthorization:
    """Exact roots and phase marker supplied by an authorised entry point."""

    source_root: Path
    staging_root: Path
    marker: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_root, Path) or not self.source_root.is_absolute():
            raise ValueError("source_root must be an absolute Path")
        if not isinstance(self.staging_root, Path) or not self.staging_root.is_absolute():
            raise ValueError("staging_root must be an absolute Path")
        if self.marker not in {
            K3C_SYNTHETIC_AUTHORIZATION_MARKER,
            K3C_REAL_AUTHORIZATION_MARKER,
        }:
            raise ValueError("legacy import requires an exact explicit authorization marker")
        if self.source_root == self.staging_root:
            raise ValueError("source_root and staging_root must be distinct")


@dataclass(frozen=True, slots=True)
class LegacyImportSemantics:
    """Explicit no-guess mapping required before Canonical conversion."""

    dataset: DatasetKind
    schema_version: str
    column_mapping: Mapping[str, str]
    unadjusted: bool
    volume_unit: str
    amount_unit: str
    timezone: str
    suspension_field: str
    suspension_true_values: tuple[str, ...]
    suspension_semantics: str
    null_semantics: str

    def __post_init__(self) -> None:
        normalized = dict(sorted(self.column_mapping.items()))
        if set(normalized) != set(_CANONICAL_FIELDS):
            raise ValueError("legacy column mapping must bind the exact daily-bar fields")
        if any(not isinstance(value, str) or not value for value in normalized.values()):
            raise ValueError("legacy column names must be non-empty text")
        if len(set(normalized.values())) != len(normalized):
            raise ValueError("legacy column mapping must be one-to-one")
        object.__setattr__(self, "column_mapping", MappingProxyType(normalized))
        if not isinstance(self.suspension_field, str) or not self.suspension_field:
            raise ValueError("suspension_field must be non-empty text")
        suspension_values = tuple(sorted(set(self.suspension_true_values)))
        if (
            not suspension_values
            or any(not isinstance(value, str) or not value for value in suspension_values)
            or suspension_values != self.suspension_true_values
        ):
            raise ValueError("suspension_true_values must be sorted, unique, and non-empty")

    @property
    def confirmed(self) -> bool:
        return (
            self.dataset is DatasetKind.DAILY_BAR_UNADJUSTED
            and self.schema_version == "daily_bar_unadjusted.v1"
            and self.unadjusted is True
            and self.volume_unit == "share"
            and self.amount_unit == "cny_yuan"
            and self.timezone == "Asia/Shanghai"
            and self.suspension_semantics == "PRICE_EQUALS_PRE_CLOSE_VOLUME_AMOUNT_ZERO"
            and self.null_semantics == "REJECT_REQUIRED_FIELD_NULLS"
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "dataset": self.dataset.value,
            "schema_version": self.schema_version,
            "column_mapping": dict(self.column_mapping),
            "unadjusted": self.unadjusted,
            "volume_unit": self.volume_unit,
            "amount_unit": self.amount_unit,
            "timezone": self.timezone,
            "suspension_field": self.suspension_field,
            "suspension_true_values": list(self.suspension_true_values),
            "suspension_semantics": self.suspension_semantics,
            "null_semantics": self.null_semantics,
        }


@dataclass(frozen=True, slots=True)
class LegacyInventoryEntry:
    relative_path: str
    size: int
    mtime_ns: int
    sha256: str | None
    parquet_schema: str | None
    row_count: int | None
    code_min: str | None
    code_max: str | None
    date_min: str | None
    date_max: str | None
    null_counts: Mapping[str, int]
    duplicate_key_count: int | None
    read_error: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "relative_path", validate_relative_path(self.relative_path))
        for field in ("size", "mtime_ns"):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        if self.sha256 is not None and _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("sha256 must be lowercase SHA-256 or None")
        if self.row_count is not None and self.row_count < 0:
            raise ValueError("row_count must be non-negative or None")
        normalized_nulls = dict(sorted(self.null_counts.items()))
        if any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            for key, value in normalized_nulls.items()
        ):
            raise ValueError("null_counts must map fields to non-negative integers")
        object.__setattr__(self, "null_counts", MappingProxyType(normalized_nulls))
        if self.duplicate_key_count is not None and self.duplicate_key_count < 0:
            raise ValueError("duplicate_key_count must be non-negative or None")
        if self.read_error is None:
            if self.sha256 is None or self.parquet_schema is None or self.row_count is None:
                raise ValueError("readable inventory entry requires SHA, schema, and row count")
        elif self.read_error not in {
            "FILE_NOT_FOUND",
            "PARQUET_READ_ERROR",
            "SOURCE_MUTATED_DURING_READ",
        }:
            raise ValueError("read_error must use a stable inventory error code")

    def to_payload(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
            "parquet_schema": self.parquet_schema,
            "row_count": self.row_count,
            "code_min": self.code_min,
            "code_max": self.code_max,
            "date_min": self.date_min,
            "date_max": self.date_max,
            "null_counts": dict(self.null_counts),
            "duplicate_key_count": self.duplicate_key_count,
            "read_error": self.read_error,
        }


@dataclass(frozen=True, slots=True)
class LegacyImportInventory:
    entries: tuple[LegacyInventoryEntry, ...]
    source_fingerprint: str = ""

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        paths = tuple(entry.relative_path for entry in entries)
        if paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            raise ValueError("inventory entries must be sorted and unique")
        object.__setattr__(self, "entries", entries)
        payload = {
            "schema_version": LEGACY_INVENTORY_SCHEMA_VERSION,
            "provider": DataProvider.XTQUANT_LEGACY.value,
            "upstream": DataUpstream.XTQUANT.value,
            "entries": [entry.to_payload() for entry in entries],
        }
        fingerprint = sha256_bytes(canonical_json_bytes(payload))
        if self.source_fingerprint and self.source_fingerprint != fingerprint:
            raise ValueError("source_fingerprint does not match inventory entries")
        object.__setattr__(self, "source_fingerprint", fingerprint)

    @property
    def file_count(self) -> int:
        return len(self.entries)

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.entries)

    @property
    def readable_rows(self) -> int:
        return sum(entry.row_count or 0 for entry in self.entries)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": LEGACY_INVENTORY_SCHEMA_VERSION,
            "provider": DataProvider.XTQUANT_LEGACY.value,
            "upstream": DataUpstream.XTQUANT.value,
            "source_fingerprint": self.source_fingerprint,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "readable_rows": self.readable_rows,
            "entries": [entry.to_payload() for entry in self.entries],
        }


@dataclass(frozen=True, slots=True)
class LegacyResolutionEvidenceSummary:
    input_rows: int
    x_only_unverified_rows: int
    x_a_consensus_rows: int = 0
    service_consensus_replaced_x_rows: int = 0
    quarantined_rows: int = 0

    def __post_init__(self) -> None:
        values = (
            self.input_rows,
            self.x_only_unverified_rows,
            self.x_a_consensus_rows,
            self.service_consensus_replaced_x_rows,
            self.quarantined_rows,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values
        ):
            raise ValueError("legacy evidence counts must be non-negative integers")
        if sum(values[1:]) != self.input_rows:
            raise ValueError("legacy evidence counts must conserve input rows")

    def to_payload(self) -> dict[str, object]:
        return {
            "input_rows": self.input_rows,
            "statuses": {
                LegacyEvidenceStatus.X_ONLY_UNVERIFIED.value: self.x_only_unverified_rows,
                LegacyEvidenceStatus.X_A_CONSENSUS.value: self.x_a_consensus_rows,
                LegacyEvidenceStatus.SERVICE_CONSENSUS_REPLACED_X.value: (
                    self.service_consensus_replaced_x_rows
                ),
                LegacyEvidenceStatus.QUARANTINED.value: self.quarantined_rows,
            },
        }


@dataclass(frozen=True, slots=True)
class LegacyImportDryRun:
    mode: LegacyImportMode
    source_root_label: str
    staging_root_label: str
    relative_paths: tuple[str, ...]
    declared_file_count: int
    declared_bytes: int
    declared_rows: int
    service_network_invocation_upper_bound: int = 0

    def __post_init__(self) -> None:
        paths = tuple(validate_relative_path(value) for value in self.relative_paths)
        if paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            raise ValueError("dry-run relative paths must be sorted and unique")
        object.__setattr__(self, "relative_paths", paths)
        if self.declared_file_count != len(paths):
            raise ValueError("declared_file_count must equal the fixed path list")
        for field in (
            "declared_file_count",
            "declared_bytes",
            "declared_rows",
            "service_network_invocation_upper_bound",
        ):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        if self.service_network_invocation_upper_bound != 0:
            raise ValueError("K3C offline import plans must have zero service network calls")
        if not self.source_root_label or not self.staging_root_label:
            raise ValueError("dry-run root labels must be non-empty")

    def to_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "schema_version": "legacy-import-dry-run.k3c.v1",
                "mode": self.mode.value,
                "provider": DataProvider.XTQUANT_LEGACY.value,
                "upstream": DataUpstream.XTQUANT.value,
                "source_root": self.source_root_label,
                "managed_staging_root": self.staging_root_label,
                "relative_paths": list(self.relative_paths),
                "declared_file_count": self.declared_file_count,
                "declared_bytes": self.declared_bytes,
                "declared_rows": self.declared_rows,
                "service_network_invocation_upper_bound": 0,
                "application_retries": 0,
                "reads_source": False,
                "writes_staging": False,
                "publishes_warehouse": False,
                "requires_new_authorization_for_execution": True,
            }
        )


def validate_idempotency_key(value: str) -> str:
    if not isinstance(value, str) or _SAFE_KEY.fullmatch(value) is None:
        raise ValueError("idempotency_key must be a safe stable identifier")
    return value


def inventory_from_payload(value: object) -> LegacyImportInventory:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "provider",
        "upstream",
        "source_fingerprint",
        "file_count",
        "total_bytes",
        "readable_rows",
        "entries",
    }:
        raise ValueError("legacy inventory payload has invalid fields")
    if (
        value["schema_version"] != LEGACY_INVENTORY_SCHEMA_VERSION
        or value["provider"] != DataProvider.XTQUANT_LEGACY.value
        or value["upstream"] != DataUpstream.XTQUANT.value
    ):
        raise ValueError("legacy inventory source contract is invalid")
    raw_entries = value["entries"]
    if not isinstance(raw_entries, list):
        raise ValueError("legacy inventory entries must be a list")
    entries: list[LegacyInventoryEntry] = []
    fields = {
        "relative_path",
        "size",
        "mtime_ns",
        "sha256",
        "parquet_schema",
        "row_count",
        "code_min",
        "code_max",
        "date_min",
        "date_max",
        "null_counts",
        "duplicate_key_count",
        "read_error",
    }
    for item in raw_entries:
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("legacy inventory entry payload has invalid fields")
        null_counts = item["null_counts"]
        if not isinstance(null_counts, dict):
            raise ValueError("legacy inventory null counts are invalid")
        entries.append(
            LegacyInventoryEntry(
                relative_path=item["relative_path"],
                size=item["size"],
                mtime_ns=item["mtime_ns"],
                sha256=item["sha256"],
                parquet_schema=item["parquet_schema"],
                row_count=item["row_count"],
                code_min=item["code_min"],
                code_max=item["code_max"],
                date_min=item["date_min"],
                date_max=item["date_max"],
                null_counts=null_counts,
                duplicate_key_count=item["duplicate_key_count"],
                read_error=item["read_error"],
            )
        )
    fingerprint = value["source_fingerprint"]
    if not isinstance(fingerprint, str):
        raise ValueError("legacy inventory fingerprint is invalid")
    inventory = LegacyImportInventory(entries=tuple(entries), source_fingerprint=fingerprint)
    if inventory.to_payload() != value:
        raise ValueError("legacy inventory summary does not match entries")
    return inventory


def semantics_from_payload(value: object) -> LegacyImportSemantics:
    if not isinstance(value, dict) or set(value) != {
        "dataset",
        "schema_version",
        "column_mapping",
        "unadjusted",
        "volume_unit",
        "amount_unit",
        "timezone",
        "suspension_field",
        "suspension_true_values",
        "suspension_semantics",
        "null_semantics",
    }:
        raise ValueError("legacy import semantics payload has invalid fields")
    mapping = value["column_mapping"]
    suspension_values = value["suspension_true_values"]
    if not isinstance(mapping, dict) or not isinstance(suspension_values, list):
        raise ValueError("legacy import semantics mapping is invalid")
    try:
        semantics = LegacyImportSemantics(
            dataset=DatasetKind(value["dataset"]),
            schema_version=value["schema_version"],
            column_mapping=mapping,
            unadjusted=value["unadjusted"],
            volume_unit=value["volume_unit"],
            amount_unit=value["amount_unit"],
            timezone=value["timezone"],
            suspension_field=value["suspension_field"],
            suspension_true_values=tuple(suspension_values),
            suspension_semantics=value["suspension_semantics"],
            null_semantics=value["null_semantics"],
        )
    except (TypeError, ValueError) as error:
        raise ValueError("legacy import semantics payload is invalid") from error
    if semantics.to_payload() != value:
        raise ValueError("legacy import semantics payload is not canonical")
    return semantics


__all__ = [
    "K3C_REAL_AUTHORIZATION_MARKER",
    "K3C_SYNTHETIC_AUTHORIZATION_MARKER",
    "LEGACY_IMPORT_ADAPTER_VERSION",
    "LEGACY_IMPORT_MANIFEST_SCHEMA_VERSION",
    "LegacyEvidenceStatus",
    "LegacyImportAuthorization",
    "LegacyImportDryRun",
    "LegacyImportInventory",
    "LegacyImportMode",
    "LegacyImportSemantics",
    "LegacyInventoryEntry",
    "LegacyResolutionEvidenceSummary",
    "canonical_json_bytes",
    "inventory_from_payload",
    "semantics_from_payload",
    "sha256_bytes",
    "validate_idempotency_key",
    "validate_relative_path",
]
