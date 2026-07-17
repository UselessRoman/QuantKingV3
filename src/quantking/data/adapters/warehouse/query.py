"""DuckDB-based read-only query adapter for Stage 1 S1-02.

The query adapter is the *only* component that imports :mod:`duckdb`,
per the architecture boundary contract. It accepts a concrete
``version_id`` and rejects every other input form: ``"latest"``,
filesystem paths, ad-hoc SQL strings, unknown field names. Every
query is dispatched through DuckDB with a parameterised filter and
returns a deterministic ``pyarrow.Table`` ordered by the canonical
sort key.

Before the query is dispatched, the adapter re-validates the on-disk
file set against the deterministic manifest and refuses to query any
version whose file inventory is missing, has extra files, has a size
or SHA-256 mismatch, or whose ``COMPLETE`` marker is tampered with.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.compute as pc

from quantking.common.errors import InvalidPathError
from quantking.common.time import TradingDate
from quantking.data.adapters.canonical_schema import sort_key
from quantking.data.adapters.persistence.sqlite import DataSqliteUnitOfWorkFactory
from quantking.data.adapters.warehouse.errors import (
    DatasetVersionNotQueryableError,
    IncompatibleInputError,
    ManifestIntegrityError,
    QueryContractError,
)
from quantking.data.adapters.warehouse.manifest import (
    WAREHOUSE_COMPLETE_FILENAME,
    WAREHOUSE_MANIFEST_FILENAME,
    path_safe_id,
    validate_managed_relative,
)
from quantking.data.adapters.warehouse.partitioning import partition_scheme_for
from quantking.data.models import DatasetKind, DatasetVersion, DatasetVersionStatus

_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*:[0-9a-fA-F-]{36}\Z")
_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]|^[\\/]")


@dataclass(frozen=True, slots=True)
class CanonicalQueryRequest:
    """A read-only, schema-allowlist query against one published version."""

    version_id: str
    dataset: str
    fields: tuple[str, ...] | None = None
    codes: tuple[str, ...] | None = None
    start_date: TradingDate | None = None
    end_date: TradingDate | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.version_id, str) or not self.version_id.strip():
            raise QueryContractError("version_id must be a non-empty string")
        if not isinstance(self.dataset, str) or not self.dataset.strip():
            raise QueryContractError("dataset must be a non-empty string")
        try:
            DatasetKind(self.dataset)
        except ValueError as error:
            raise QueryContractError(
                "dataset is not a canonical dataset kind",
                context={"dataset": self.dataset},
            ) from error
        if self.fields is not None and not isinstance(self.fields, tuple):
            object.__setattr__(self, "fields", tuple(self.fields))
        if self.codes is not None and not isinstance(self.codes, tuple):
            object.__setattr__(self, "codes", tuple(self.codes))
        if self.start_date is not None and not isinstance(self.start_date, TradingDate):
            raise QueryContractError("start_date must be a TradingDate or None")
        if self.end_date is not None and not isinstance(self.end_date, TradingDate):
            raise QueryContractError("end_date must be a TradingDate or None")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date.value < self.start_date.value
        ):
            raise QueryContractError("end_date must not precede start_date")


def _looks_like_path(value: str) -> bool:
    if not value:
        return False
    if _PATH_RE.match(value):
        return True
    if "\\" in value or value.startswith("./") or value.startswith("../"):
        return True
    # Heuristic: anything with a slash other than an entity id.
    return "/" in value and not value.startswith("latest")


class CanonicalQueryAdapter:
    """Read-only DuckDB query adapter for one managed data root.

    The adapter is constructed with a single absolute data root and a
    :class:`DataSqliteUnitOfWorkFactory` so the metadata status can be
    verified before any Parquet file is touched. Each :meth:`query`
    call opens a fresh in-memory DuckDB connection, runs the query
    with the manifest-listed Parquet files only, and returns the
    result as a :class:`pyarrow.Table` ordered by the canonical sort
    key. The connection is closed before the method returns.
    """

    def __init__(self, *, data_root: Path, uow_factory: DataSqliteUnitOfWorkFactory) -> None:
        root = Path(data_root)
        if not root.is_absolute():
            raise InvalidPathError("data_root must be an absolute path")
        if uow_factory is None:
            raise QueryContractError("uow_factory is required")
        self._data_root = root.resolve()
        self._uow_factory = uow_factory

    # -- public API ---------------------------------------------------------

    def query(self, request: CanonicalQueryRequest) -> pa.Table:
        """Execute a parameterised query against a single PUBLISHED version."""
        return self._query(request)

    def query_by_id(
        self,
        *,
        version_id: str,
        dataset: str,
        fields: Sequence[str] | None = None,
        codes: Sequence[str] | None = None,
        start_date: TradingDate | None = None,
        end_date: TradingDate | None = None,
    ) -> pa.Table:
        """Convenience wrapper that builds a :class:`CanonicalQueryRequest`.

        Most call sites only know the version id, dataset and a small
        set of filters; accepting them as keyword arguments makes the
        common case ergonomic while still funnelling every query
        through the same allowlist and parameterised DuckDB path.
        """
        request = CanonicalQueryRequest(
            version_id=version_id,
            dataset=dataset,
            fields=tuple(fields) if fields is not None else None,
            codes=tuple(codes) if codes is not None else None,
            start_date=start_date,
            end_date=end_date,
        )
        return self._query(request)

    def _query(self, request: CanonicalQueryRequest) -> pa.Table:
        # Request-contract violations (bad version_id form, unknown field,
        # ad-hoc SQL, unknown dataset) surface as :class:`QueryContractError`
        # directly: they describe a malformed caller request, not a version
        # that cannot be served. Only metadata-state and on-disk integrity
        # failures are re-raised as :class:`DatasetVersionNotQueryableError`
        # so callers can distinguish "the request was invalid" from "the
        # version exists but cannot be served right now".
        self._validate_request(request)
        try:
            version = self._load_version(request)
            directory = self._directory_for(version)
            manifest = self._verify_manifest(directory, version)
            self._verify_complete(directory, manifest)
            self._verify_inventory(directory, manifest)
            return self._run_query(directory, manifest, request, version)
        except (ManifestIntegrityError, IncompatibleInputError) as error:
            raise DatasetVersionNotQueryableError(
                "dataset version is not queryable",
                context={"version_id": request.version_id, "detail": str(error)},
            ) from error

    def execute_raw_sql(self, *, version_id: str, dataset: str, sql: str) -> pa.Table:
        """Reject ad-hoc SQL with a stable error.

        S1-02 deliberately does not allow ad-hoc SQL. The method
        exists only to give callers a clear, stable error code rather
        than silently dropping the request.
        """
        raise QueryContractError(
            "ad-hoc SQL is not supported by the S1-02 query adapter",
            context={"dataset": dataset, "version_id": version_id, "sql_kind": "raw_sql"},
        )

    # -- request handling ---------------------------------------------------

    def _validate_request(self, request: CanonicalQueryRequest) -> None:
        if request.version_id.lower() == "latest":
            raise QueryContractError("'latest' is not an acceptable version_id")
        if _looks_like_path(request.version_id):
            raise QueryContractError(
                "version_id must be an entity id, not a filesystem path",
                context={"version_id": request.version_id},
            )
        if not _ID_RE.match(request.version_id):
            raise QueryContractError(
                "version_id must be a namespaced UUID4",
                context={"version_id": request.version_id},
            )
        if not _is_known_dataset(request.dataset):
            raise QueryContractError(
                "dataset is not a canonical dataset",
                context={"dataset": request.dataset},
            )

    def _load_version(self, request: CanonicalQueryRequest) -> DatasetVersion:
        with self._uow_factory() as uow:
            version = uow.data.get_version(request.version_id)
        if version.dataset.value != request.dataset:
            raise QueryContractError(
                "version_id does not belong to the requested dataset",
                context={
                    "version_id": request.version_id,
                    "expected_dataset": request.dataset,
                    "actual_dataset": version.dataset.value,
                },
            )
        if version.status is not DatasetVersionStatus.PUBLISHED:
            raise DatasetVersionNotQueryableError(
                "only PUBLISHED dataset versions are queryable",
                context={
                    "version_id": request.version_id,
                    "status": version.status.value,
                },
            )
        return version

    def _directory_for(self, version: DatasetVersion) -> Path:
        try:
            directory_name = path_safe_id(str(version.version_id))
        except InvalidPathError as error:
            raise IncompatibleInputError(
                "version_id is not a safe managed relative identifier",
                context={"version_id": str(version.version_id), "detail": str(error)},
            ) from error
        return self._data_root / "warehouse" / version.dataset.value / directory_name

    def _verify_manifest(self, directory: Path, version: DatasetVersion) -> dict[str, object]:
        target = directory / WAREHOUSE_MANIFEST_FILENAME
        if not target.is_file():
            raise ManifestIntegrityError(
                "canonical manifest is missing",
                context={"version_id": str(version.version_id)},
            )
        raw = target.read_bytes()
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ManifestIntegrityError(
                "canonical manifest is not valid JSON",
                context={"version_id": str(version.version_id), "detail": str(error)},
            ) from error
        if not isinstance(manifest, dict):
            raise ManifestIntegrityError("canonical manifest must be a JSON object")
        # The manifest is written deterministically (sorted keys, fixed
        # separators, no extraneous whitespace). Re-serialising the parsed
        # payload with the same rules must reproduce the on-disk bytes
        # exactly; any divergence (trailing whitespace, reformatted JSON,
        # injected keys) means the file was tampered with after publishing.
        canonical_bytes = json.dumps(
            manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if raw != canonical_bytes:
            raise ManifestIntegrityError(
                "manifest bytes are not in canonical deterministic form",
                context={"version_id": str(version.version_id)},
            )
        if manifest.get("manifest_schema_version") != "warehouse-manifest.v1":
            raise ManifestIntegrityError(
                "manifest schema version is unsupported",
                context={"version_id": str(version.version_id)},
            )
        if manifest.get("version_id") != str(version.version_id):
            raise ManifestIntegrityError(
                "manifest version_id does not match the dataset version",
                context={"version_id": str(version.version_id)},
            )
        if manifest.get("dataset") != version.dataset.value:
            raise ManifestIntegrityError(
                "manifest dataset does not match the dataset version",
                context={"version_id": str(version.version_id)},
            )
        declared = manifest.get("manifest_sha256")
        if not isinstance(declared, str):
            raise ManifestIntegrityError(
                "manifest is missing its own SHA-256",
                context={"version_id": str(version.version_id)},
            )
        # The on-disk manifest embeds its own SHA-256. The field
        # itself is excluded from the hash, so to verify we strip the
        # field, re-serialise, hash and compare. Combined with the
        # canonical-bytes check above this catches both structural and
        # whitespace-level tampering.
        from quantking.data.adapters.warehouse.manifest import compute_sha256

        without_self_hash = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
        expected = compute_sha256(
            json.dumps(
                without_self_hash,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        if expected != declared:
            raise ManifestIntegrityError(
                "manifest SHA-256 does not match the on-disk file",
                context={"version_id": str(version.version_id)},
            )
        files = manifest.get("files")
        if not isinstance(files, list) or not all(isinstance(entry, dict) for entry in files):
            raise ManifestIntegrityError(
                "manifest files entry is not a list of dicts",
                context={"version_id": str(version.version_id)},
            )
        return manifest

    def _verify_complete(self, directory: Path, manifest: dict[str, object]) -> dict[str, object]:
        target = directory / WAREHOUSE_COMPLETE_FILENAME
        if not target.is_file():
            raise ManifestIntegrityError(
                "warehouse COMPLETE marker is missing",
                context={"version_id": str(manifest.get("version_id", ""))},
            )
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ManifestIntegrityError(
                "COMPLETE marker is not valid JSON",
                context={"detail": str(error)},
            ) from error
        if not isinstance(payload, dict):
            raise ManifestIntegrityError("COMPLETE marker must be a JSON object")
        if payload.get("version_id") != manifest.get("version_id"):
            raise ManifestIntegrityError(
                "COMPLETE marker does not bind to the manifest version_id",
                context={"version_id": str(manifest.get("version_id", ""))},
            )
        complete_sha = payload.get("manifest_sha256")
        manifest_sha = manifest.get("manifest_sha256")
        if (
            isinstance(complete_sha, str)
            and isinstance(manifest_sha, str)
            and complete_sha != manifest_sha
        ):
            raise ManifestIntegrityError(
                "COMPLETE manifest_sha256 does not match the manifest",
                context={"version_id": str(manifest.get("version_id", ""))},
            )
        return payload

    def _verify_inventory(
        self, directory: Path, manifest: dict[str, object]
    ) -> list[dict[str, object]]:
        listed = manifest.get("files")
        if not isinstance(listed, list):
            raise ManifestIntegrityError("manifest files is not a list")
        declared: list[dict[str, object]] = []
        for entry in listed:
            if not isinstance(entry, dict):
                raise ManifestIntegrityError("manifest file entry is not a dict")
            relative = entry.get("relative_path")
            if not isinstance(relative, str):
                raise ManifestIntegrityError("manifest file relative_path is not a string")
            try:
                validate_managed_relative(relative, field="relative_path")
            except InvalidPathError as error:
                raise ManifestIntegrityError(
                    "manifest contains an unsafe relative path",
                    context={"relative_path": relative, "detail": str(error)},
                ) from error
            absolute = (directory / relative).resolve()
            try:
                absolute.relative_to(directory.resolve())
            except ValueError as error:
                raise ManifestIntegrityError(
                    "manifest file escapes the managed directory",
                    context={"relative_path": relative, "detail": str(error)},
                ) from error
            if not absolute.is_file():
                raise ManifestIntegrityError(
                    "manifest file is missing on disk",
                    context={"relative_path": relative},
                )
            size = entry.get("size")
            if not isinstance(size, int) or size < 0:
                raise ManifestIntegrityError(
                    "manifest file size is not a non-negative integer",
                    context={"relative_path": relative},
                )
            payload = absolute.read_bytes()
            if len(payload) != size:
                raise ManifestIntegrityError(
                    "manifest file size does not match on-disk size",
                    context={"relative_path": relative},
                )
            from quantking.data.adapters.warehouse.manifest import compute_sha256

            digest = compute_sha256(payload)
            if digest != entry.get("sha256"):
                raise ManifestIntegrityError(
                    "manifest file SHA-256 does not match on-disk digest",
                    context={"relative_path": relative},
                )
            declared.append(
                {
                    "relative_path": relative,
                    "absolute": str(absolute),
                    "row_count": int(entry.get("row_count", 0)),
                    "size": size,
                    "partition": dict(entry.get("partition", {})),
                }
            )
        managed_set = {
            WAREHOUSE_MANIFEST_FILENAME,
            WAREHOUSE_COMPLETE_FILENAME,
            "failure.json",
        }
        for absolute in directory.rglob("*"):
            if absolute.is_dir():
                continue
            managed = absolute.relative_to(directory).as_posix()
            if managed in managed_set:
                continue
            if not any(item["relative_path"] == managed for item in declared):
                raise ManifestIntegrityError(
                    "warehouse directory has unexpected file",
                    context={"relative_path": managed},
                )
        return declared

    # -- DuckDB execution ---------------------------------------------------

    def _run_query(
        self,
        directory: Path,
        manifest: dict[str, object],
        request: CanonicalQueryRequest,
        version: DatasetVersion,
    ) -> pa.Table:
        declared = self._verify_inventory(directory, manifest)
        dataset = version.dataset.value
        descriptor_fields = self._allowed_fields(dataset)
        selected_fields = self._resolve_fields(request, descriptor_fields)
        code_field: str | None = "code" if "code" in descriptor_fields else None
        date_field: str | None = partition_scheme_for(dataset).date_field
        if date_field not in descriptor_fields:
            date_field = None
        connection = duckdb.connect(":memory:")
        try:
            # Register each manifest file as a view so the SQL below
            # can reference a single virtual table ``_warehouse`` that
            # unions them. ``read_parquet(?)`` does not accept a
            # parameter inside a prepared statement, but the path
            # itself comes from the manifest (which has already been
            # validated against the managed directory) so inlining the
            # escaped string is safe.
            for index, entry in enumerate(declared):
                view = f"_parquet_{index:03d}"
                escaped = str(entry["absolute"]).replace("'", "''")
                connection.execute(
                    f"CREATE VIEW {view} AS "
                    f"SELECT * FROM read_parquet('{escaped}', hive_partitioning=false)"
                )
            union_sql = " UNION ALL ".join(
                f"SELECT {', '.join(descriptor_fields)} FROM _parquet_{index:03d}"
                for index in range(len(declared))
            )
            connection.execute(f"CREATE TEMPORARY VIEW _warehouse AS {union_sql}")
            where_clauses: list[str] = []
            parameters: list[object] = []
            if code_field is not None and request.codes:
                placeholders = ",".join(["?"] * len(request.codes))
                where_clauses.append(f"{code_field} IN ({placeholders})")
                parameters.extend(request.codes)
            if date_field is not None and request.start_date is not None:
                where_clauses.append(f"{date_field} >= ?")
                parameters.append(request.start_date.value)
            if date_field is not None and request.end_date is not None:
                where_clauses.append(f"{date_field} <= ?")
                parameters.append(request.end_date.value)
            fields_csv = ", ".join(selected_fields)
            where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
            sql = (
                f"SELECT {fields_csv} FROM _warehouse"
                f"{where_sql}"
                f" ORDER BY {', '.join(sort_key(dataset))}"
            )
            if parameters:
                result = connection.execute(sql, parameters).to_arrow_table()
            else:
                result = connection.execute(sql).to_arrow_table()
        finally:
            connection.close()
        # DuckDB already applied the order by; the deterministic order
        # is therefore an implicit guarantee. Defensively re-sort to
        # match the canonical sort key so callers can rely on it.
        sort_columns = list(sort_key(dataset))
        order = pc.sort_indices(result, sort_keys=[(col, "ascending") for col in sort_columns])  # type: ignore[attr-defined]
        return result.take(order)

    def _allowed_fields(self, dataset: str) -> tuple[str, ...]:
        from quantking.data.adapters.canonical_schema import canonical_schema

        return tuple(canonical_schema(dataset).names)

    def _resolve_fields(
        self, request: CanonicalQueryRequest, allowed: tuple[str, ...]
    ) -> tuple[str, ...]:
        if request.fields is None:
            return allowed
        for name in request.fields:
            if name not in allowed:
                raise QueryContractError(
                    "field is not in the canonical schema allowlist",
                    context={"field": name, "dataset": request.dataset},
                )
        return tuple(request.fields)


def _is_known_dataset(dataset: str) -> bool:
    try:
        DatasetKind(dataset)
    except ValueError:
        return False
    return True


__all__ = [
    "CanonicalQueryAdapter",
    "CanonicalQueryRequest",
    "DatasetVersionNotQueryableError",
    "QueryContractError",
]
