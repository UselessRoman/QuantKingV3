"""Managed, non-publishing S1-07K3C DATA_ONLY copy and conversion adapter."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq

from quantking.common.errors import IdempotencyConflictError
from quantking.common.paths import is_reparse_point
from quantking.data.adapters.canonical_schema import canonical_schema, ensure_sorted, validate_table
from quantking.data.adapters.legacy_import import (
    LegacyImportError,
    _safe_root,
    _safe_source_file,
)
from quantking.data.adapters.warehouse.verifier import assert_path_under_root
from quantking.data.content import canonical_content_sha256
from quantking.data.legacy_import.contracts import (
    K3C_REAL_AUTHORIZATION_MARKER,
    LEGACY_IMPORT_ADAPTER_VERSION,
    LEGACY_IMPORT_MANIFEST_SCHEMA_VERSION,
    LegacyImportAuthorization,
    LegacyImportInventory,
    LegacyImportSemantics,
    LegacyResolutionEvidenceSummary,
    canonical_json_bytes,
    inventory_from_payload,
    semantics_from_payload,
    sha256_bytes,
    validate_idempotency_key,
    validate_relative_path,
)
from quantking.data.models import DataProvider, DatasetKind, DataUpstream

_BUFFER_SIZE = 1024 * 1024
_MANIFEST = "manifest.json"
_COMPLETE = "COMPLETE"
_QUARANTINE = "QUARANTINE.json"
_CANONICAL_SOURCE = "canonical.parquet"


class LegacyImportQuarantined(LegacyImportError):
    """Import did not create a complete managed candidate."""


@dataclass(frozen=True, slots=True)
class LegacyManagedImportOutcome:
    batch_id: str
    manifest_sha256: str
    directory: Path
    created: bool
    reused: bool
    raw_file_count: int
    canonical_row_count: int
    evidence: LegacyResolutionEvidenceSummary
    warehouse_published: bool = False

    def __post_init__(self) -> None:
        if self.created == self.reused:
            raise ValueError("exactly one of created/reused must be true")
        if self.warehouse_published:
            raise ValueError("K3C managed imports must never publish Warehouse")


def _stable_batch_id(identity_sha256: str) -> str:
    raw = bytearray(bytes.fromhex(identity_sha256)[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return f"data_batch:{uuid.UUID(bytes=bytes(raw))}"


def _path_safe_batch_id(batch_id: str) -> str:
    return batch_id.replace(":", "--")


def _canonical_manifest(unsigned: Mapping[str, object]) -> tuple[bytes, str]:
    digest = sha256_bytes(canonical_json_bytes(unsigned))
    return canonical_json_bytes({**unsigned, "manifest_sha256": digest}), digest


def _write_atomic(path: Path, payload: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with temporary.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _copy_atomic(source: Path, target: Path) -> str:
    temporary = target.with_name(target.name + ".tmp")
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with source.open("rb") as source_stream, temporary.open("xb") as target_stream:
        while chunk := source_stream.read(_BUFFER_SIZE):
            target_stream.write(chunk)
            digest.update(chunk)
        target_stream.flush()
        os.fsync(target_stream.fileno())
    os.replace(temporary, target)
    if os.path.samefile(source, target):
        raise LegacyImportError("managed copy must not be a hard link")
    return digest.hexdigest()


def _manifest_identity(
    inventory: LegacyImportInventory, semantics: LegacyImportSemantics
) -> dict[str, object]:
    return {
        "schema_version": "legacy-import-identity.k3c.v1",
        "provider": DataProvider.XTQUANT_LEGACY.value,
        "upstream": DataUpstream.XTQUANT.value,
        "adapter_version": LEGACY_IMPORT_ADAPTER_VERSION,
        "source_inventory": inventory.to_payload(),
        "semantics": semantics.to_payload(),
    }


def _source_table(path: Path) -> pa.Table:
    try:
        reader = cast(Callable[[Path], pa.Table], pq.read_table)
        return reader(path)
    except (OSError, pa.ArrowException) as error:
        raise LegacyImportQuarantined("legacy source Parquet could not be read") from error


def _canonical_candidate(table: pa.Table, semantics: LegacyImportSemantics) -> pa.Table:
    if not semantics.confirmed:
        raise LegacyImportQuarantined("legacy source semantics are not fully confirmed")
    source_fields = set(table.column_names)
    required_source_fields = set(semantics.column_mapping.values()) | {semantics.suspension_field}
    if not required_source_fields.issubset(source_fields):
        raise LegacyImportQuarantined("legacy source schema is missing declared fields")
    if any(table[field].null_count for field in semantics.column_mapping.values()):
        raise LegacyImportQuarantined("legacy source required fields contain nulls")
    schema = canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value)
    try:
        arrays = [
            table[semantics.column_mapping[field.name]].cast(field.type, safe=True)
            for field in schema
        ]
        candidate = ensure_sorted(
            DatasetKind.DAILY_BAR_UNADJUSTED.value,
            pa.Table.from_arrays(arrays, schema=schema),
        )
    except (pa.ArrowException, ValueError, TypeError) as error:
        raise LegacyImportQuarantined("legacy source fields cannot map to Canonical") from error
    validation = validate_table(DatasetKind.DAILY_BAR_UNADJUSTED.value, candidate)
    if not validation.ok:
        raise LegacyImportQuarantined("legacy Canonical candidate failed structural validation")
    _validate_pre_close(candidate)
    _validate_suspensions(table, candidate, semantics)
    return candidate


def _validate_pre_close(candidate: pa.Table) -> int:
    previous: dict[str, float] = {}
    first_row_count = 0
    for row in candidate.to_pylist():
        code = row["code"]
        close = row["close"]
        pre_close = row["pre_close"]
        if (
            not isinstance(code, str)
            or not isinstance(close, float)
            or not isinstance(pre_close, float)
        ):
            raise LegacyImportQuarantined("legacy pre_close evidence has invalid types")
        if code not in previous:
            first_row_count += 1
        elif pre_close != previous[code]:
            raise LegacyImportQuarantined("legacy pre_close does not equal prior unadjusted close")
        previous[code] = close
    return first_row_count


def _key(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _validate_suspensions(
    source: pa.Table, candidate: pa.Table, semantics: LegacyImportSemantics
) -> None:
    source_code = semantics.column_mapping["code"]
    source_date = semantics.column_mapping["trade_date"]
    statuses = {
        (str(code), _key(trade_date)): str(status)
        for code, trade_date, status in zip(
            source[source_code].to_pylist(),
            source[source_date].to_pylist(),
            source[semantics.suspension_field].to_pylist(),
            strict=True,
        )
    }
    for row in candidate.to_pylist():
        status = statuses.get((str(row["code"]), _key(row["trade_date"])))
        if status not in semantics.suspension_true_values:
            continue
        price = row["pre_close"]
        if (
            row["open"] != price
            or row["high"] != price
            or row["low"] != price
            or row["close"] != price
            or row["volume"] != 0
            or row["amount"] != 0
        ):
            raise LegacyImportQuarantined("legacy suspended row semantics are inconsistent")


def _parquet_bytes(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    writer = cast(Callable[[pa.Table, object], None], pq.write_table)
    writer(table, sink)
    return cast(bytes, sink.getvalue().to_pybytes())


def _load_canonical_json(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LegacyImportError("managed legacy JSON is invalid") from error
    if not isinstance(payload, dict) or canonical_json_bytes(payload) != raw:
        raise LegacyImportError("managed legacy JSON is not canonical")
    return payload


_MANIFEST_FIELDS = {
    "manifest_schema_version",
    "manifest_sha256",
    "identity_sha256",
    "batch_id",
    "provider",
    "upstream",
    "adapter_version",
    "dataset",
    "schema_version",
    "source_inventory",
    "source_fingerprint",
    "semantics",
    "raw_file_count",
    "raw_byte_count",
    "raw_row_count",
    "canonical_row_count",
    "canonical_content_sha256",
    "first_row_pre_close_unverified_count",
    "files",
    "resolution_evidence",
    "warehouse_published",
    "status",
}


def _safe_managed_file(staging_root: Path, path: Path) -> Path:
    resolved = assert_path_under_root(staging_root, path)
    if not resolved.is_file():
        raise LegacyImportError("managed legacy file is missing")
    return resolved


def verify_managed_legacy_import(
    directory: Path, *, staging_root: Path
) -> tuple[dict[str, object], str]:
    root = _safe_root(staging_root, must_exist=True)
    resolved_directory = assert_path_under_root(root, directory)
    if resolved_directory.parent != root / "legacy-imports" or not resolved_directory.is_dir():
        raise LegacyImportError("managed legacy directory has invalid placement")
    _safe_managed_file(root, resolved_directory / _MANIFEST)
    _safe_managed_file(root, resolved_directory / _COMPLETE)
    manifest = _load_canonical_json(resolved_directory / _MANIFEST)
    if set(manifest) != _MANIFEST_FIELDS:
        raise LegacyImportError("managed legacy manifest fields are invalid")
    declared_sha = manifest.get("manifest_sha256")
    if not isinstance(declared_sha, str):
        raise LegacyImportError("managed legacy manifest SHA is missing")
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if sha256_bytes(canonical_json_bytes(unsigned)) != declared_sha:
        raise LegacyImportError("managed legacy manifest SHA does not match")
    if (
        manifest["manifest_schema_version"] != LEGACY_IMPORT_MANIFEST_SCHEMA_VERSION
        or manifest["provider"] != DataProvider.XTQUANT_LEGACY.value
        or manifest["upstream"] != DataUpstream.XTQUANT.value
        or manifest["adapter_version"] != LEGACY_IMPORT_ADAPTER_VERSION
        or manifest["dataset"] != DatasetKind.DAILY_BAR_UNADJUSTED.value
        or manifest["schema_version"] != "daily_bar_unadjusted.v1"
        or manifest["status"] != "STAGED_X_ONLY_UNVERIFIED"
        or manifest["warehouse_published"] is not False
    ):
        raise LegacyImportError("managed legacy source/status contract is invalid")
    try:
        inventory = inventory_from_payload(manifest["source_inventory"])
        semantics = semantics_from_payload(manifest["semantics"])
    except ValueError as error:
        raise LegacyImportError("managed legacy frozen inputs are invalid") from error
    if not semantics.confirmed:
        raise LegacyImportError("managed legacy semantics are not confirmed")
    identity_sha = sha256_bytes(canonical_json_bytes(_manifest_identity(inventory, semantics)))
    batch_id = _stable_batch_id(identity_sha)
    if (
        manifest["identity_sha256"] != identity_sha
        or manifest["batch_id"] != batch_id
        or resolved_directory.name != _path_safe_batch_id(batch_id)
        or manifest["source_fingerprint"] != inventory.source_fingerprint
    ):
        raise LegacyImportError("managed legacy identity binding is invalid")
    complete = _load_canonical_json(resolved_directory / _COMPLETE)
    if complete != {"batch_id": manifest.get("batch_id"), "manifest_sha256": declared_sha}:
        raise LegacyImportError("managed legacy COMPLETE does not bind manifest")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise LegacyImportError("managed legacy manifest files are invalid")
    expected = {_MANIFEST, _COMPLETE}
    file_entries: dict[str, dict[str, object]] = {}
    for value in files:
        if not isinstance(value, dict) or set(value) != {
            "relative_path",
            "row_count",
            "sha256",
            "size",
        }:
            raise LegacyImportError("managed legacy file entry is invalid")
        relative = value["relative_path"]
        if not isinstance(relative, str):
            raise LegacyImportError("managed legacy file path is invalid")
        size = value["size"]
        row_count = value["row_count"]
        file_sha = value["sha256"]
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(row_count, int)
            or isinstance(row_count, bool)
            or row_count < 0
            or not isinstance(file_sha, str)
            or len(file_sha) != 64
        ):
            raise LegacyImportError("managed legacy file metadata is invalid")
        try:
            validate_relative_path(relative)
        except ValueError as error:
            raise LegacyImportError("managed legacy file path is invalid") from error
        if relative in file_entries:
            raise LegacyImportError("managed legacy file paths are duplicated")
        file_entries[relative] = value
        path = _safe_managed_file(root, resolved_directory / Path(*relative.split("/")))
        payload = path.read_bytes()
        if len(payload) != value["size"] or sha256_bytes(payload) != value["sha256"]:
            raise LegacyImportError("managed legacy file content does not match manifest")
        expected.add(relative)
    actual: set[str] = set()
    for walk_root, names, filenames in os.walk(resolved_directory, followlinks=False):
        walk_path = Path(walk_root)
        for name in names:
            candidate = walk_path / name
            if is_reparse_point(candidate):
                raise LegacyImportError("managed legacy tree contains a symlink or junction")
        for filename in filenames:
            candidate = walk_path / filename
            _safe_managed_file(root, candidate)
            actual.add(candidate.relative_to(resolved_directory).as_posix())
    if actual != expected:
        raise LegacyImportError("managed legacy file inventory does not match manifest")
    expected_raw_paths = {f"raw/{entry.relative_path}" for entry in inventory.entries}
    if set(file_entries) != expected_raw_paths | {f"candidate/{_CANONICAL_SOURCE}"}:
        raise LegacyImportError("managed legacy Raw/candidate file set is invalid")
    raw_tables: list[pa.Table] = []
    for entry in inventory.entries:
        file_entry = file_entries[f"raw/{entry.relative_path}"]
        if (
            file_entry["size"] != entry.size
            or file_entry["sha256"] != entry.sha256
            or file_entry["row_count"] != entry.row_count
        ):
            raise LegacyImportError("managed legacy Raw file is not bound to inventory")
        raw_tables.append(
            _source_table(resolved_directory / "raw" / Path(*entry.relative_path.split("/")))
        )
    rebuilt = _canonical_candidate(
        pa.concat_tables(raw_tables, promote_options="default"), semantics
    )
    canonical_path = resolved_directory / "candidate" / _CANONICAL_SOURCE
    candidate = _source_table(canonical_path)
    validation = validate_table(DatasetKind.DAILY_BAR_UNADJUSTED.value, candidate)
    if not validation.ok or candidate.schema != canonical_schema(
        DatasetKind.DAILY_BAR_UNADJUSTED.value
    ):
        raise LegacyImportError("managed legacy Canonical schema/quality is invalid")
    if not candidate.equals(rebuilt):
        raise LegacyImportError("managed legacy Canonical does not rebuild from Raw")
    canonical_content = canonical_content_sha256(DatasetKind.DAILY_BAR_UNADJUSTED, [candidate])
    canonical_entry = file_entries[f"candidate/{_CANONICAL_SOURCE}"]
    first_rows = _validate_pre_close(candidate)
    evidence = LegacyResolutionEvidenceSummary(candidate.num_rows, candidate.num_rows)
    if (
        manifest["raw_file_count"] != inventory.file_count
        or manifest["raw_byte_count"] != inventory.total_bytes
        or manifest["raw_row_count"] != inventory.readable_rows
        or manifest["canonical_row_count"] != candidate.num_rows
        or manifest["canonical_content_sha256"] != canonical_content
        or manifest["first_row_pre_close_unverified_count"] != first_rows
        or manifest["resolution_evidence"] != evidence.to_payload()
        or canonical_entry["row_count"] != candidate.num_rows
    ):
        raise LegacyImportError("managed legacy count/content/evidence bindings are invalid")
    return manifest, declared_sha


class LegacyManagedImporter:
    """Copy immutable Raw, build an unverified candidate, never publish Warehouse."""

    def __init__(
        self,
        authorization: LegacyImportAuthorization,
        *,
        after_copy_hook: Callable[[Path], None] | None = None,
    ) -> None:
        if not isinstance(authorization, LegacyImportAuthorization):
            raise TypeError("authorization must be LegacyImportAuthorization")
        self._authorization = authorization
        self._after_copy_hook = after_copy_hook

    def import_inventory(
        self,
        *,
        inventory: LegacyImportInventory,
        semantics: LegacyImportSemantics,
        idempotency_key: str,
    ) -> LegacyManagedImportOutcome:
        validate_idempotency_key(idempotency_key)
        source_root = _safe_root(self._authorization.source_root, must_exist=True)
        staging_root = _safe_root(self._authorization.staging_root, must_exist=False)
        if (
            source_root == staging_root
            or source_root in staging_root.parents
            or staging_root in source_root.parents
        ):
            raise LegacyImportError("source and managed staging roots must not overlap")
        staging_root.mkdir(parents=True, exist_ok=True)
        identity = _manifest_identity(inventory, semantics)
        identity_sha = sha256_bytes(canonical_json_bytes(identity))
        batch_id = _stable_batch_id(identity_sha)
        imports_root = staging_root / "legacy-imports"
        final = imports_root / _path_safe_batch_id(batch_id)
        key_path = staging_root / "legacy-import-idempotency" / f"{idempotency_key}.json"
        existing_key = self._read_idempotency(key_path)
        if existing_key is not None and existing_key.get("identity_sha256") != identity_sha:
            raise IdempotencyConflictError("legacy idempotency key is bound to different content")
        if final.exists():
            manifest, digest = verify_managed_legacy_import(final, staging_root=staging_root)
            if manifest.get("identity_sha256") != identity_sha:
                raise IdempotencyConflictError("legacy import batch has different content")
            self._write_idempotency(key_path, identity_sha, batch_id, "COMPLETE")
            rows = manifest.get("canonical_row_count")
            raw_count = manifest.get("raw_file_count")
            if not isinstance(rows, int) or not isinstance(raw_count, int):
                raise LegacyImportError("managed legacy manifest counts are invalid")
            return LegacyManagedImportOutcome(
                batch_id=batch_id,
                manifest_sha256=digest,
                directory=final,
                created=False,
                reused=True,
                raw_file_count=raw_count,
                canonical_row_count=rows,
                evidence=LegacyResolutionEvidenceSummary(rows, rows),
            )
        if existing_key is not None:
            raise IdempotencyConflictError("legacy idempotency key already has immutable failure")
        incomplete = imports_root / (_path_safe_batch_id(batch_id) + ".incomplete")
        assert_path_under_root(staging_root, incomplete)
        if incomplete.exists():
            raise LegacyImportError("legacy import already has an incomplete attempt")
        incomplete.mkdir(parents=True)
        try:
            outcome = self._build(
                source_root=source_root,
                staging_root=staging_root,
                incomplete=incomplete,
                final=final,
                inventory=inventory,
                semantics=semantics,
                identity_sha=identity_sha,
                batch_id=batch_id,
            )
        except Exception as error:
            self._preserve_quarantine(
                staging_root, incomplete, idempotency_key, identity_sha, batch_id, error
            )
            self._write_idempotency(key_path, identity_sha, batch_id, "QUARANTINED")
            if isinstance(error, LegacyImportError):
                raise
            raise LegacyImportQuarantined("legacy import was quarantined") from error
        self._write_idempotency(key_path, identity_sha, batch_id, "COMPLETE")
        return outcome

    def _build(
        self,
        *,
        source_root: Path,
        staging_root: Path,
        incomplete: Path,
        final: Path,
        inventory: LegacyImportInventory,
        semantics: LegacyImportSemantics,
        identity_sha: str,
        batch_id: str,
    ) -> LegacyManagedImportOutcome:
        if any(entry.read_error is not None or entry.sha256 is None for entry in inventory.entries):
            raise LegacyImportQuarantined("legacy inventory contains unreadable candidates")
        file_entries: list[dict[str, object]] = []
        tables: list[pa.Table] = []
        for entry in inventory.entries:
            source = _safe_source_file(source_root, entry.relative_path)
            before = source.stat()
            if before.st_size != entry.size or before.st_mtime_ns != entry.mtime_ns:
                raise LegacyImportQuarantined(
                    "legacy source metadata changed after frozen inventory"
                )
            raw_relative = f"raw/{entry.relative_path}"
            raw_target = incomplete / Path(*raw_relative.split("/"))
            copied_sha = _copy_atomic(source, raw_target)
            if copied_sha != entry.sha256:
                raise LegacyImportQuarantined("legacy source content changed before copy")
            after = source.stat()
            if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                raise LegacyImportQuarantined("legacy source changed during copy")
            if (
                self._after_copy_hook is not None
                and self._authorization.marker != K3C_REAL_AUTHORIZATION_MARKER
            ):
                self._after_copy_hook(raw_target)
            copied_payload = raw_target.read_bytes()
            if sha256_bytes(copied_payload) != entry.sha256:
                raise LegacyImportQuarantined("managed Raw copy was interrupted or changed")
            file_entries.append(
                {
                    "relative_path": raw_relative,
                    "size": len(copied_payload),
                    "sha256": entry.sha256,
                    "row_count": entry.row_count,
                }
            )
            tables.append(_source_table(raw_target))
        try:
            combined = pa.concat_tables(tables, promote_options="default")
        except pa.ArrowException as error:
            raise LegacyImportQuarantined(
                "legacy source schemas cannot form one candidate"
            ) from error
        candidate = _canonical_candidate(combined, semantics)
        canonical_payload = _parquet_bytes(candidate)
        canonical_relative = f"candidate/{_CANONICAL_SOURCE}"
        _write_atomic(incomplete / canonical_relative, canonical_payload)
        file_entries.append(
            {
                "relative_path": canonical_relative,
                "size": len(canonical_payload),
                "sha256": sha256_bytes(canonical_payload),
                "row_count": candidate.num_rows,
            }
        )
        file_entries.sort(key=lambda value: str(value["relative_path"]))
        evidence = LegacyResolutionEvidenceSummary(candidate.num_rows, candidate.num_rows)
        canonical_content = canonical_content_sha256(DatasetKind.DAILY_BAR_UNADJUSTED, [candidate])
        first_rows = _validate_pre_close(candidate)
        unsigned = {
            "manifest_schema_version": LEGACY_IMPORT_MANIFEST_SCHEMA_VERSION,
            "identity_sha256": identity_sha,
            "batch_id": batch_id,
            "provider": DataProvider.XTQUANT_LEGACY.value,
            "upstream": DataUpstream.XTQUANT.value,
            "adapter_version": LEGACY_IMPORT_ADAPTER_VERSION,
            "dataset": DatasetKind.DAILY_BAR_UNADJUSTED.value,
            "schema_version": "daily_bar_unadjusted.v1",
            "source_inventory": inventory.to_payload(),
            "source_fingerprint": inventory.source_fingerprint,
            "semantics": semantics.to_payload(),
            "raw_file_count": inventory.file_count,
            "raw_byte_count": inventory.total_bytes,
            "raw_row_count": inventory.readable_rows,
            "canonical_row_count": candidate.num_rows,
            "canonical_content_sha256": canonical_content,
            "first_row_pre_close_unverified_count": first_rows,
            "files": file_entries,
            "resolution_evidence": evidence.to_payload(),
            "warehouse_published": False,
            "status": "STAGED_X_ONLY_UNVERIFIED",
        }
        manifest_bytes, manifest_sha = _canonical_manifest(unsigned)
        _write_atomic(incomplete / _MANIFEST, manifest_bytes)
        _write_atomic(
            incomplete / _COMPLETE,
            canonical_json_bytes({"batch_id": batch_id, "manifest_sha256": manifest_sha}),
        )
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(incomplete, final)
        verify_managed_legacy_import(final, staging_root=staging_root)
        return LegacyManagedImportOutcome(
            batch_id=batch_id,
            manifest_sha256=manifest_sha,
            directory=final,
            created=True,
            reused=False,
            raw_file_count=inventory.file_count,
            canonical_row_count=candidate.num_rows,
            evidence=evidence,
        )

    @staticmethod
    def _read_idempotency(path: Path) -> dict[str, object] | None:
        if not path.exists():
            return None
        return _load_canonical_json(path)

    @staticmethod
    def _write_idempotency(path: Path, identity_sha: str, batch_id: str, status: str) -> None:
        payload = canonical_json_bytes(
            {
                "schema_version": "legacy-import-idempotency.k3c.v1",
                "identity_sha256": identity_sha,
                "batch_id": batch_id,
                "status": status,
            }
        )
        if path.exists():
            if path.read_bytes() != payload:
                raise IdempotencyConflictError("legacy idempotency record is immutable")
            return
        _write_atomic(path, payload)

    @staticmethod
    def _preserve_quarantine(
        staging_root: Path,
        incomplete: Path,
        idempotency_key: str,
        identity_sha: str,
        batch_id: str,
        error: Exception,
    ) -> None:
        code = (
            "SEMANTICS_OR_DATA_REJECTED"
            if isinstance(error, LegacyImportQuarantined)
            else "IMPORT_INTERRUPTED"
        )
        _write_atomic(
            incomplete / _QUARANTINE,
            canonical_json_bytes(
                {
                    "schema_version": "legacy-import-quarantine.k3c.v1",
                    "batch_id": batch_id,
                    "identity_sha256": identity_sha,
                    "idempotency_key": idempotency_key,
                    "error_code": code,
                    "warehouse_published": False,
                }
            ),
        )
        target = (
            staging_root
            / "legacy-import-quarantine"
            / (_path_safe_batch_id(batch_id) + "--" + idempotency_key)
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(incomplete, target)


__all__ = [
    "LegacyImportQuarantined",
    "LegacyManagedImportOutcome",
    "LegacyManagedImporter",
    "verify_managed_legacy_import",
]
