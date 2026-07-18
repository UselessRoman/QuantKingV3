"""DuckDB-based read-only query adapter for Stage 1 S1-02.

The query adapter is the *only* component that imports :mod:`duckdb`,
per the architecture boundary contract. It accepts a concrete
``version_id`` and rejects every other input form: ``"latest"``,
filesystem paths, ad-hoc SQL strings, unknown field names. Every
query is dispatched through DuckDB with a parameterised filter and
returns a deterministic ``pyarrow.Table`` ordered by the canonical
sort key.

Before the query is dispatched, the adapter routes through the unified
strict published-directory verifier
(:func:`quantking.data.adapters.warehouse.verifier.verify_canonical_directory`)
so the on-disk manifest, COMPLETE marker, file inventory, per-file
SHA-256/row count and Parquet schema are all revalidated. It then
compares the disk manifest credentials against the SQLite
:class:`DatasetVersion` row (manifest_sha256, dataset, version_id,
schema_version, code_version, rule_version, parent_version_id,
input_batch_ids, coverage_start/end/summary, row_count,
security_count) and refuses to query if any credential differs.

Legal empty versions (zero Parquet files) are served as a zero-row
:class:`pyarrow.Table` with the dataset's canonical schema, without
ever entering the DuckDB UNION ALL path.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow as pa

from quantking.common.errors import InvalidPathError
from quantking.common.time import TradingDate
from quantking.data.adapters.canonical_schema import canonical_schema, sort_key
from quantking.data.adapters.persistence.sqlite import DataSqliteUnitOfWorkFactory
from quantking.data.adapters.warehouse.errors import (
    DatasetVersionNotQueryableError,
    IncompatibleInputError,
    ManifestIntegrityError,
    QueryContractError,
)
from quantking.data.adapters.warehouse.manifest import path_safe_id
from quantking.data.adapters.warehouse.verifier import (
    VerifiedDirectory,
    verify_canonical_directory,
    verify_data_root,
)
from quantking.data.models import DatasetKind, DatasetVersion, DatasetVersionStatus
from quantking.data.schemas import descriptor_for

_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*:[0-9a-fA-F-]{36}\Z")
_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]|^[\\/]")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class CanonicalQueryRequest:
    """A read-only, schema-allowlist query against one published version.

    ``fields`` is ``None`` (the default) to select every canonical
    field, or a non-empty tuple of canonical field names. An empty
    tuple is a contract error: callers must either omit ``fields`` or
    list at least one column.
    """

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
        if self.fields is not None:
            if not isinstance(self.fields, tuple):
                object.__setattr__(self, "fields", tuple(self.fields))
            if len(self.fields) == 0:
                raise QueryContractError(
                    "fields must be None or a non-empty tuple of canonical field names"
                )
            for name in self.fields:
                if not isinstance(name, str) or not name.strip():
                    raise QueryContractError("fields must contain non-empty strings only")
        if self.codes is not None:
            if not isinstance(self.codes, tuple):
                object.__setattr__(self, "codes", tuple(self.codes))
            if len(self.codes) == 0:
                raise QueryContractError(
                    "codes must be None or a non-empty tuple of canonical security codes"
                )
            for code in self.codes:
                if not isinstance(code, str) or not code.strip():
                    raise QueryContractError("codes must contain non-empty strings only")
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
        # ``verify_data_root`` rejects symlinks and Windows junctions
        # so an attacker cannot redirect the managed tree.
        self._data_root = verify_data_root(data_root)
        if uow_factory is None:
            raise QueryContractError("uow_factory is required")
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
        """Convenience wrapper that builds a :class:`CanonicalQueryRequest`."""
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
            verified = verify_canonical_directory(directory, data_root=self._data_root)
            self._verify_credentials(verified, version)
            return self._run_query(verified, request, version)
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

    # -- credential binding -------------------------------------------------

    def _verify_credentials(self, verified: VerifiedDirectory, version: DatasetVersion) -> None:
        """Compare disk manifest credentials against the SQLite DatasetVersion.

        Every credential field listed in the S1-02-R1 contract must
        match exactly; any mismatch raises
        :class:`DatasetVersionNotQueryableError` so callers see a
        stable, sanitised error rather than silently reading a
        directory whose on-disk state drifted from the metadata.
        """
        manifest = verified.manifest
        version_id = str(version.version_id)
        # ``manifest_sha256``: the SQLite row's manifest_sha256 is the
        # authoritative credential set at VALIDATED time. It must be a
        # 64-char lowercase hex string and equal the on-disk manifest
        # SHA (already verified by the unified verifier).
        sqlite_sha = version.manifest_sha256
        if not isinstance(sqlite_sha, str) or not _HEX64_RE.match(sqlite_sha):
            raise DatasetVersionNotQueryableError(
                "dataset version is missing a valid manifest_sha256",
                context={"version_id": version_id},
            )
        if sqlite_sha != verified.manifest_sha:
            raise DatasetVersionNotQueryableError(
                "SQLite manifest_sha256 does not match the disk manifest",
                context={"version_id": version_id},
            )
        # ``dataset`` and ``version_id``: already cross-checked by
        # ``_load_version`` and the verifier, but we re-check here so
        # a tampered manifest cannot bypass the binding.
        if manifest.get("dataset") != version.dataset.value:
            raise DatasetVersionNotQueryableError(
                "manifest dataset does not match the dataset version",
                context={"version_id": version_id},
            )
        if manifest.get("version_id") != version_id:
            raise DatasetVersionNotQueryableError(
                "manifest version_id does not match the dataset version",
                context={"version_id": version_id},
            )
        # ``schema_version``: the manifest must use the dataset
        # descriptor's schema_version so it lines up with the
        # DatasetVersion's schema_version exactly.
        expected_schema_version = descriptor_for(version.dataset.value).schema_version
        if manifest.get("schema_version") != expected_schema_version:
            raise DatasetVersionNotQueryableError(
                "manifest schema_version does not match the dataset contract",
                context={"version_id": version_id},
            )
        if manifest.get("schema_version") != version.schema_version:
            raise DatasetVersionNotQueryableError(
                "manifest schema_version does not match the SQLite row",
                context={"version_id": version_id},
            )
        # ``code_version`` and ``rule_version``.
        if manifest.get("code_version") != version.code_version:
            raise DatasetVersionNotQueryableError(
                "manifest code_version does not match the SQLite row",
                context={"version_id": version_id},
            )
        if manifest.get("rule_version") != version.rule_version:
            raise DatasetVersionNotQueryableError(
                "manifest rule_version does not match the SQLite row",
                context={"version_id": version_id},
            )
        # ``parent_version_id``.
        manifest_parent = manifest.get("parent_version_id")
        sqlite_parent = (
            str(version.parent_version_id) if version.parent_version_id is not None else None
        )
        if manifest_parent != sqlite_parent:
            raise DatasetVersionNotQueryableError(
                "manifest parent_version_id does not match the SQLite row",
                context={"version_id": version_id},
            )
        # ``input_batch_ids``.
        manifest_batches_raw = manifest.get("input_batch_ids", [])
        manifest_batches = (
            [str(b) for b in manifest_batches_raw] if isinstance(manifest_batches_raw, list) else []
        )
        sqlite_batches = [str(b) for b in version.input_batch_ids]
        if sorted(manifest_batches) != sorted(sqlite_batches):
            raise DatasetVersionNotQueryableError(
                "manifest input_batch_ids do not match the SQLite row",
                context={"version_id": version_id},
            )
        # ``coverage_start`` and ``coverage_end``: stored as ISO
        # strings in the manifest; the SQLite row carries
        # :class:`TradingDate` values.
        manifest_start = manifest.get("coverage_start")
        sqlite_start = (
            version.coverage_start.value.isoformat() if version.coverage_start is not None else None
        )
        if manifest_start != sqlite_start:
            raise DatasetVersionNotQueryableError(
                "manifest coverage_start does not match the SQLite row",
                context={"version_id": version_id},
            )
        manifest_end = manifest.get("coverage_end")
        sqlite_end = (
            version.coverage_end.value.isoformat() if version.coverage_end is not None else None
        )
        if manifest_end != sqlite_end:
            raise DatasetVersionNotQueryableError(
                "manifest coverage_end does not match the SQLite row",
                context={"version_id": version_id},
            )
        # ``coverage_summary``: the manifest's summary must be a
        # superset of (or equal to) the SQLite row's summary. The
        # publish coordinator always writes the full computed summary,
        # so the SQLite row should also carry the full set.
        manifest_summary = manifest.get("coverage_summary")
        if not isinstance(manifest_summary, dict):
            raise DatasetVersionNotQueryableError(
                "manifest coverage_summary is not a JSON object",
                context={"version_id": version_id},
            )
        for key, expected in version.coverage_summary.items():
            actual = manifest_summary.get(key)
            if actual != expected:
                raise DatasetVersionNotQueryableError(
                    "manifest coverage_summary does not match the SQLite row",
                    context={"version_id": version_id, "field": key},
                )
        # ``row_count`` and ``security_count``: stored as strings in
        # the manifest's ``coverage_summary`` and as ints in the
        # SQLite row. The manifest's per-file row counts have already
        # been summed by the verifier; here we compare the summary
        # values against the SQLite row's authoritative counts.
        if version.row_count is not None:
            manifest_row_count = manifest_summary.get("row_count")
            if manifest_row_count is None or int(manifest_row_count) != version.row_count:
                raise DatasetVersionNotQueryableError(
                    "manifest row_count does not match the SQLite row",
                    context={"version_id": version_id},
                )
        if version.security_count is not None:
            manifest_security_count = manifest_summary.get("security_count")
            if (
                manifest_security_count is None
                or int(manifest_security_count) != version.security_count
            ):
                raise DatasetVersionNotQueryableError(
                    "manifest security_count does not match the SQLite row",
                    context={"version_id": version_id},
                )

    # -- DuckDB execution ---------------------------------------------------

    def _run_query(
        self,
        verified: VerifiedDirectory,
        request: CanonicalQueryRequest,
        version: DatasetVersion,
    ) -> pa.Table:
        dataset = version.dataset.value
        descriptor_fields = tuple(canonical_schema(dataset).names)
        selected_fields = self._resolve_fields(request, descriptor_fields)
        # Legal empty version: zero Parquet files. Return a zero-row
        # table with the requested fields' schema directly, without
        # entering the DuckDB UNION ALL path (which would fail on an
        # empty file list).
        if not verified.files:
            return self._empty_result(dataset, selected_fields)
        code_field: str | None = "code" if "code" in descriptor_fields else None
        from quantking.data.adapters.warehouse.partitioning import partition_scheme_for

        date_field: str | None = partition_scheme_for(dataset).date_field
        if date_field not in descriptor_fields:
            date_field = None
        connection = duckdb.connect(":memory:")
        try:
            try:
                # Register each manifest file as a view so the SQL
                # below can reference a single virtual table
                # ``_warehouse`` that unions them.
                # ``read_parquet(?)`` does not accept a parameter
                # inside a prepared statement, but the path itself
                # comes from the verifier (which has already
                # validated the file is inside the managed
                # directory) so inlining the escaped string is safe.
                for index, entry in enumerate(verified.files):
                    view = f"_parquet_{index:03d}"
                    escaped = str(entry.absolute_path).replace("'", "''")
                    connection.execute(
                        f"CREATE VIEW {view} AS "
                        f"SELECT * FROM read_parquet('{escaped}', hive_partitioning=false)"
                    )
                union_sql = " UNION ALL ".join(
                    f"SELECT {', '.join(descriptor_fields)} FROM _parquet_{index:03d}"
                    for index in range(len(verified.files))
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
                # The SQL ORDER BY uses the canonical sort key. If
                # the caller did not request the sort columns we
                # still ORDER BY them in SQL (DuckDB allows ordering
                # by columns that are not in the SELECT list when
                # they exist on the underlying view), so the result
                # is already canonically ordered. We deliberately do
                # NOT re-sort the Arrow result afterwards: that would
                # require the sort columns to be present in the
                # projection, which is exactly what the
                # field-projection-without-sort-keys test forbids.
                sort_columns = list(sort_key(dataset))
                # If the sort columns are a subset of the selected
                # fields, ORDER BY them directly. Otherwise we need
                # to select them in an outer query so ORDER BY can
                # reference them, then project them away.
                missing_sort_columns = [c for c in sort_columns if c not in selected_fields]
                if not missing_sort_columns:
                    order_sql = f" ORDER BY {', '.join(sort_columns)}"
                    sql = f"SELECT {fields_csv} FROM _warehouse{where_sql}{order_sql}"
                    if parameters:
                        result = connection.execute(sql, parameters).to_arrow_table()
                    else:
                        result = connection.execute(sql).to_arrow_table()
                else:
                    # Wrap the projection so ORDER BY can reference
                    # the hidden sort columns, then project them away
                    # in the outer SELECT.
                    inner_fields_csv = ", ".join(sorted(set(selected_fields) | set(sort_columns)))
                    order_sql = f" ORDER BY {', '.join(sort_columns)}"
                    sql = (
                        f"SELECT {fields_csv} FROM ("
                        f"SELECT {inner_fields_csv} FROM _warehouse{where_sql}{order_sql}"
                        f")"
                    )
                    if parameters:
                        result = connection.execute(sql, parameters).to_arrow_table()
                    else:
                        result = connection.execute(sql).to_arrow_table()
            except duckdb.Error as error:
                raise ManifestIntegrityError(
                    "DuckDB failed to execute the canonical query",
                    context={"version_id": str(version.version_id), "detail": str(error)},
                ) from error
            except (pa.ArrowException, OSError) as error:
                raise ManifestIntegrityError(
                    "Arrow/IO failure while reading the canonical Parquet files",
                    context={"version_id": str(version.version_id), "detail": str(error)},
                ) from error
        finally:
            connection.close()
        return result

    def _empty_result(self, dataset: str, selected_fields: tuple[str, ...]) -> pa.Table:
        """Return a zero-row table with the requested field schema."""
        full_schema = canonical_schema(dataset)
        if tuple(full_schema.names) == selected_fields:
            return pa.Table.from_arrays(
                [pa.array([], type=field.type) for field in full_schema],
                schema=full_schema,
            )
        selected_fields_set = set(selected_fields)
        fields = [field for field in full_schema if field.name in selected_fields_set]
        schema = pa.schema(fields)
        return pa.Table.from_arrays(
            [pa.array([], type=field.type) for field in fields],
            schema=schema,
        )

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
