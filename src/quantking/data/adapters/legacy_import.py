"""Read-only inventory adapter for S1-07K3C legacy DATA_ONLY candidates."""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq

from quantking.common.paths import is_reparse_point
from quantking.data.legacy_import.contracts import (
    K3C_REAL_AUTHORIZATION_MARKER,
    LegacyImportAuthorization,
    LegacyImportInventory,
    LegacyInventoryEntry,
    validate_relative_path,
)

_READ_CHUNK_SIZE = 1024 * 1024
_CODE_CANDIDATES = ("code", "stock_code", "symbol")
_DATE_CANDIDATES = ("trade_date", "date", "datetime", "time")


class LegacyImportError(RuntimeError):
    """Stable DATA_ONLY boundary error without leaking absolute paths."""


class LegacyRuntimeFetchRejected(LegacyImportError):
    """Raised by the deliberately absent XTQuant runtime adapter."""


class _LegacySourceMissing(LegacyImportError):
    pass


def reject_xtquant_runtime_fetch() -> None:
    """Fail before importing or invoking any retired XTQuant runtime SDK."""
    raise LegacyRuntimeFetchRejected("XTQUANT_LEGACY is DATA_ONLY and has no runtime fetch")


def _safe_root(root: Path, *, must_exist: bool) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        raise LegacyImportError("legacy import root must be absolute")
    current = Path(root.anchor)
    try:
        for part in root.parts[1:]:
            current /= part
            if current.exists() and is_reparse_point(current):
                raise LegacyImportError("legacy import root contains a symlink or junction")
        if must_exist and not root.is_dir():
            raise LegacyImportError("legacy source root does not exist")
        return root.resolve(strict=must_exist)
    except LegacyImportError:
        raise
    except (OSError, RuntimeError) as error:
        raise LegacyImportError("legacy import root could not be resolved") from error


def _safe_source_file(source_root: Path, relative_path: str) -> Path:
    relative = Path(*validate_relative_path(relative_path).split("/"))
    current = source_root
    try:
        for part in relative.parts:
            current /= part
            if current.exists() and is_reparse_point(current):
                raise LegacyImportError("legacy source path contains a symlink or junction")
        if not current.exists():
            raise _LegacySourceMissing("legacy source file is missing")
        resolved = current.resolve(strict=True)
        resolved.relative_to(source_root)
    except LegacyImportError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise LegacyImportError("legacy source path is missing or escapes its root") from error
    if not resolved.is_file():
        raise LegacyImportError("legacy source path is not a file")
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_READ_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _value_iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _column_name(schema: pa.Schema, candidates: tuple[str, ...]) -> str | None:
    return next((value for value in candidates if value in schema.names), None)


def _read_parquet(path: Path) -> pa.Table:
    reader = cast(Callable[[Path], pa.Table], pq.read_table)
    return reader(path)


def _parquet_facts(
    path: Path,
) -> tuple[
    str,
    int,
    str | None,
    str | None,
    str | None,
    str | None,
    dict[str, int],
    int | None,
]:
    table = _read_parquet(path)
    schema_text = table.schema.to_string(show_field_metadata=True)
    null_counts = {name: table[name].null_count for name in table.column_names}
    code_field = _column_name(table.schema, _CODE_CANDIDATES)
    date_field = _column_name(table.schema, _DATE_CANDIDATES)
    code_values: list[str] = []
    date_values: list[str] = []
    duplicate_count: int | None = None
    if code_field is not None:
        code_values = [str(value) for value in table[code_field].to_pylist() if value is not None]
    if date_field is not None:
        date_values = [
            _value_iso(value) for value in table[date_field].to_pylist() if value is not None
        ]
    if code_field is not None and date_field is not None:
        keys = [
            (str(code), _value_iso(trade_date))
            for code, trade_date in zip(
                table[code_field].to_pylist(), table[date_field].to_pylist(), strict=True
            )
            if code is not None and trade_date is not None
        ]
        duplicate_count = sum(count - 1 for count in Counter(keys).values() if count > 1)
    return (
        schema_text,
        table.num_rows,
        min(code_values) if code_values else None,
        max(code_values) if code_values else None,
        min(date_values) if date_values else None,
        max(date_values) if date_values else None,
        null_counts,
        duplicate_count,
    )


class LegacyInventoryPlanner:
    """Inventory only explicitly listed files without discovering hidden scope."""

    def __init__(
        self,
        authorization: LegacyImportAuthorization,
        *,
        after_hash_hook: Callable[[Path], None] | None = None,
    ) -> None:
        if not isinstance(authorization, LegacyImportAuthorization):
            raise TypeError("authorization must be LegacyImportAuthorization")
        self._authorization = authorization
        self._after_hash_hook = after_hash_hook

    def discover(self) -> tuple[str, ...]:
        """Discover every Parquet candidate once; later phases consume this frozen list."""
        source_root = _safe_root(self._authorization.source_root, must_exist=True)
        discovered: list[str] = []
        try:
            for directory, names, filenames in os.walk(source_root, followlinks=False):
                directory_path = Path(directory)
                safe_names: list[str] = []
                for name in names:
                    candidate = directory_path / name
                    if is_reparse_point(candidate):
                        raise LegacyImportError(
                            "legacy discovery encountered a symlink or junction"
                        )
                    safe_names.append(name)
                names[:] = sorted(safe_names)
                for filename in sorted(filenames):
                    if not filename.lower().endswith(".parquet"):
                        continue
                    candidate = directory_path / filename
                    if is_reparse_point(candidate):
                        raise LegacyImportError(
                            "legacy discovery encountered a symlink or junction"
                        )
                    relative = candidate.relative_to(source_root).as_posix()
                    discovered.append(validate_relative_path(relative))
        except LegacyImportError:
            raise
        except OSError as error:
            raise LegacyImportError("legacy source discovery failed") from error
        result = tuple(sorted(discovered))
        if len(set(result)) != len(result):
            raise LegacyImportError("legacy discovery produced duplicate paths")
        return result

    def inventory(self, relative_paths: tuple[str, ...]) -> LegacyImportInventory:
        source_root = _safe_root(self._authorization.source_root, must_exist=True)
        _safe_root(self._authorization.staging_root, must_exist=False)
        paths = tuple(validate_relative_path(value) for value in relative_paths)
        if paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            raise LegacyImportError("legacy inventory paths must be sorted and unique")
        entries = tuple(self._inventory_one(source_root, value) for value in paths)
        return LegacyImportInventory(entries=entries)

    def _inventory_one(self, source_root: Path, relative_path: str) -> LegacyInventoryEntry:
        try:
            path = _safe_source_file(source_root, relative_path)
        except _LegacySourceMissing:
            return LegacyInventoryEntry(
                relative_path=relative_path,
                size=0,
                mtime_ns=0,
                sha256=None,
                parquet_schema=None,
                row_count=None,
                code_min=None,
                code_max=None,
                date_min=None,
                date_max=None,
                null_counts={},
                duplicate_key_count=None,
                read_error="FILE_NOT_FOUND",
            )
        before = path.stat()
        try:
            digest = _sha256_file(path)
            if (
                self._after_hash_hook is not None
                and self._authorization.marker != K3C_REAL_AUTHORIZATION_MARKER
            ):
                self._after_hash_hook(path)
            changed = path.stat()
            if before.st_size != changed.st_size or before.st_mtime_ns != changed.st_mtime_ns:
                return self._mutated_entry(relative_path, changed)
            facts = _parquet_facts(path)
            final_digest = _sha256_file(path)
            after = path.stat()
        except (OSError, pa.ArrowException, ValueError):
            return LegacyInventoryEntry(
                relative_path=relative_path,
                size=before.st_size,
                mtime_ns=before.st_mtime_ns,
                sha256=None,
                parquet_schema=None,
                row_count=None,
                code_min=None,
                code_max=None,
                date_min=None,
                date_max=None,
                null_counts={},
                duplicate_key_count=None,
                read_error="PARQUET_READ_ERROR",
            )
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or digest != final_digest
        ):
            return self._mutated_entry(relative_path, after)
        schema, row_count, code_min, code_max, date_min, date_max, nulls, duplicates = facts
        return LegacyInventoryEntry(
            relative_path=relative_path,
            size=after.st_size,
            mtime_ns=after.st_mtime_ns,
            sha256=digest,
            parquet_schema=schema,
            row_count=row_count,
            code_min=code_min,
            code_max=code_max,
            date_min=date_min,
            date_max=date_max,
            null_counts=nulls,
            duplicate_key_count=duplicates,
            read_error=None,
        )

    @staticmethod
    def _mutated_entry(relative_path: str, stat: object) -> LegacyInventoryEntry:
        size = getattr(stat, "st_size", None)
        mtime_ns = getattr(stat, "st_mtime_ns", None)
        if not isinstance(size, int) or not isinstance(mtime_ns, int):
            raise LegacyImportError("legacy source mutation could not be described")
        return LegacyInventoryEntry(
            relative_path=relative_path,
            size=size,
            mtime_ns=mtime_ns,
            sha256=None,
            parquet_schema=None,
            row_count=None,
            code_min=None,
            code_max=None,
            date_min=None,
            date_max=None,
            null_counts={},
            duplicate_key_count=None,
            read_error="SOURCE_MUTATED_DURING_READ",
        )


def assert_unchanged_inventory(before: LegacyImportInventory, after: LegacyImportInventory) -> None:
    if not isinstance(before, LegacyImportInventory) or not isinstance(
        after, LegacyImportInventory
    ):
        raise TypeError("before and after must be LegacyImportInventory")
    if before.source_fingerprint != after.source_fingerprint:
        raise LegacyImportError("legacy source fingerprint changed during import")


__all__ = [
    "LegacyImportError",
    "LegacyInventoryPlanner",
    "LegacyRuntimeFetchRejected",
    "assert_unchanged_inventory",
    "reject_xtquant_runtime_fetch",
]
