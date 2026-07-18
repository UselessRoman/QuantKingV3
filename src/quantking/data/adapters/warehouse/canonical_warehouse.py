"""Canonical Warehouse publish and atomic promotion (Stage 1 S1-02).

The canonical warehouse is the only component that physically writes
published Parquet data. Its invariants mirror the raw store's but
operate on already-validated PyArrow tables:

* Every input chunk must pass :func:`quantking.data.adapters.canonical_schema.validate_table`.
* Multiple input chunks are concatenated before global primary-key
  uniqueness and global sort order are checked; chunk-local
  validation is not sufficient.
* A legal empty version (carrying an :class:`EmptyResultInfo`) is
  published with zero Parquet files but a fully populated manifest.
* The deterministic manifest records partition scheme, parent
  version, input batch ids, code/rule versions, coverage window and
  every Parquet file with managed relative path, partition key,
  size, row count and SHA-256.
* The publish is atomic: a ``.incomplete`` directory is promoted
  to its final name via :func:`os.replace`; only then may the
  :class:`DatasetVersion` be marked ``PUBLISHED``.
* Idempotent: a successful publish with the same inputs and
  manifest returns the same directory. A repeat with the same
  ``version_id`` but different contents, manifest or inputs raises
  :class:`IdempotencyConflictError`.
* The Parquet BLOB is not stored in SQLite. The on-disk file is the
  only authoritative copy of the data and is verified by SHA-256
  before and after the atomic rename.
* Every reconcile (idempotent or first-publish) routes through the
  unified strict published-directory verifier in
  :mod:`quantking.data.adapters.warehouse.verifier`. The verifier
  never deletes, overwrites or auto-repairs the directory.

The module only imports :mod:`pyarrow` because the
``check_architecture_boundaries`` contract allows PyArrow inside
``data/adapters``; :mod:`duckdb` is intentionally absent and lives
in :mod:`quantking.data.adapters.warehouse.query`.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
from pyarrow import parquet as pa_parquet

from quantking.common.errors import IdempotencyConflictError, InvalidPathError
from quantking.data.adapters.canonical_schema import (
    CanonicalValidationIssue,
    ensure_sorted,
    validate_table,
)
from quantking.data.adapters.warehouse.errors import (
    CanonicalWarehouseError,
    IncompatibleInputError,
    ManifestIntegrityError,
)
from quantking.data.adapters.warehouse.manifest import (
    WAREHOUSE_COMPLETE_FILENAME,
    WAREHOUSE_FAILURE_FILENAME,
    WAREHOUSE_INPUT_LISTING_FILENAME,
    WAREHOUSE_MANIFEST_FILENAME,
    CanonicalManifest,
    CanonicalManifestFile,
    compute_sha256,
    path_safe_id,
)
from quantking.data.adapters.warehouse.partitioning import (
    partition_scheme_for,
)
from quantking.data.adapters.warehouse.verifier import (
    assert_path_under_root,
    verify_canonical_directory,
    verify_data_root,
)
from quantking.data.models import (
    DatasetVersion,
    EmptyResultInfo,
)
from quantking.data.schemas import descriptor_for


@dataclass(frozen=True, slots=True)
class CanonicalPublishRequest:
    """The request to publish one canonical dataset version.

    The request deliberately does NOT accept ``manifest_sha256``,
    ``row_count`` or ``security_count``: those credentials are sourced
    from the actual on-disk manifest by the publish coordinator. If
    ``coverage_start``/``coverage_end``/``coverage_summary`` are
    supplied they are treated as expected credentials and must match
    the values computed from the actual data, otherwise the publish
    fails.
    """

    version: DatasetVersion
    tables: tuple[pa.Table, ...]
    code_version: str
    rule_version: str
    empty_result: EmptyResultInfo | None = None
    coverage_start: object | None = None
    coverage_end: object | None = None
    coverage_summary: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.version, DatasetVersion):
            raise TypeError("version must be a DatasetVersion")
        if not isinstance(self.tables, tuple):
            object.__setattr__(self, "tables", tuple(self.tables))
        if not isinstance(self.code_version, str) or not self.code_version.strip():
            raise ValueError("code_version must be non-empty text")
        if not isinstance(self.rule_version, str) or not self.rule_version.strip():
            raise ValueError("rule_version must be non-empty text")
        if self.empty_result is not None and not isinstance(self.empty_result, EmptyResultInfo):
            raise TypeError("empty_result must be an EmptyResultInfo or None")
        if self.coverage_summary and not isinstance(self.coverage_summary, dict):
            object.__setattr__(self, "coverage_summary", dict(self.coverage_summary))


def _empty_dataset_marker(dataset: str) -> bytes:
    """Return a deterministic empty-dataset marker for a given dataset.

    Used as the content payload when publishing a legal empty version
    so the manifest's ``content_sha256`` still has a stable value to
    compare against during idempotent reconciliation.
    """
    return f"quantking.warehouse:{dataset}:empty".encode()


def _coerce_partition_to_int(partition: Mapping[str, object]) -> dict[str, int]:
    """Return the partition dict with numeric values cast to int.

    Parquet partition keys are stored as strings on disk but the
    canonical warehouse manifest keeps the integer year/month for
    typed comparison during reconciliation. This helper bridges the
    two representations so plan and on-disk signatures compare equal.
    """
    coerced: dict[str, int] = {}
    for key, value in partition.items():
        if isinstance(value, bool):
            raise TypeError("partition values must not be bool")
        if isinstance(value, int):
            coerced[key] = value
        elif isinstance(value, str):
            try:
                coerced[key] = int(value)
            except ValueError as error:
                raise ValueError(f"partition value {key!r}={value!r} is not numeric") from error
        else:
            raise TypeError(f"partition value {key!r} is not int or str")
    return coerced


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat().removesuffix("+00:00") + "Z"


def _string_context(mapping: Mapping[str, object] | None) -> dict[str, str]:
    """Coerce an arbitrary context mapping to ``dict[str, str]``.

    The :class:`quantking.common.errors.QuantKingError` superclass
    requires every context value to be a string; warehouse code passes
    through things like issue dictionaries, so we stringify everything
    before passing it on.
    """
    return {str(key): str(value) for key, value in (mapping or {}).items()}


class CanonicalWarehouse:
    """Land canonical Parquet data and publish :class:`DatasetVersion` rows.

    The publisher is constructed with a single absolute data root and
    refuses to operate outside it. :meth:`publish` returns the final
    published directory. Every publish and every idempotent reconcile
    routes through the unified strict published-directory verifier
    (:func:`verify_canonical_directory`) so the on-disk state is
    guaranteed consistent before the caller treats the directory as
    published.
    """

    def __init__(self, *, data_root: Path) -> None:
        self._data_root = verify_data_root(data_root)

    @property
    def data_root(self) -> Path:
        """The resolved managed data root (read-only)."""
        return self._data_root

    # -- public API ---------------------------------------------------------

    def publish(self, request: CanonicalPublishRequest) -> Path:
        """Validate, partition, write and atomically publish a canonical version."""
        final = self._final_path(request)
        if final.exists():
            return self._reconcile_existing(final, request)
        incomplete = self._incomplete_path(request)
        # Create the ``.incomplete`` directory first so any failure
        # (including pre-write validation rejection) leaves the
        # forensic evidence in place. We do this before validation so
        # the caller can inspect ``incomplete/failure.json`` to see
        # why a request was rejected.
        try:
            incomplete.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            raise CanonicalWarehouseError(
                "failed to create incomplete directory",
                context={"version_id": str(request.version.version_id), "detail": str(error)},
            ) from error
        try:
            self._write_input_listing(incomplete, request)
            self._validate_inputs(request)
        except CanonicalWarehouseError as error:
            # Pre-write validation failures must still preserve a
            # ``failure.json`` so operators can see exactly which
            # inputs were rejected.
            self._preserve_failure(incomplete, request, error)
            raise
        return self._publish_fresh(request, final)

    # -- internals ----------------------------------------------------------

    def _validate_inputs(self, request: CanonicalPublishRequest) -> None:
        if not request.tables and request.empty_result is None:
            raise CanonicalWarehouseError(
                "publish request must include at least one table or an explicit empty result",
                context={"version_id": str(request.version.version_id)},
            )
        dataset = request.version.dataset.value
        for ordinal, table in enumerate(request.tables):
            result = validate_table(dataset, table, empty_result=request.empty_result)
            if not result.ok:
                raise CanonicalWarehouseError(
                    "canonical validation failed for input chunk",
                    context=_string_context(
                        {
                            "dataset": dataset,
                            "chunk": ordinal,
                            "issues": [_issue_to_dict(issue) for issue in result.issues],
                        }
                    ),
                )
        # Global primary key uniqueness and sort order across all chunks.
        if request.tables:
            concatenated = pa.concat_tables(list(request.tables))
            concatenated = ensure_sorted(dataset, concatenated)
            result = validate_table(dataset, concatenated, empty_result=request.empty_result)
            if not result.ok:
                raise CanonicalWarehouseError(
                    "global canonical validation failed across chunks",
                    context=_string_context(
                        {
                            "dataset": dataset,
                            "issues": [_issue_to_dict(issue) for issue in result.issues],
                        }
                    ),
                )

    def _final_path(self, request: CanonicalPublishRequest) -> Path:
        try:
            directory_name = path_safe_id(str(request.version.version_id))
        except InvalidPathError as error:
            raise IncompatibleInputError(
                "dataset version id is not a safe managed relative identifier",
                context={"version_id": str(request.version.version_id), "detail": str(error)},
            ) from error
        return self._data_root / "warehouse" / request.version.dataset.value / directory_name

    def _incomplete_path(self, request: CanonicalPublishRequest) -> Path:
        final = self._final_path(request)
        return final.with_name(final.name + ".incomplete")

    def _publish_fresh(self, request: CanonicalPublishRequest, final: Path) -> Path:
        # The ``.incomplete`` directory was created by :meth:`publish`
        # so pre-write validation failures could also leave a
        # ``failure.json`` behind. We re-resolve its path here and
        # assert it still exists; if a previous test or external
        # process has removed it we re-create it.
        incomplete = self._incomplete_path(request)
        if not incomplete.exists():
            try:
                incomplete.mkdir(parents=True, exist_ok=False)
            except OSError as error:
                raise CanonicalWarehouseError(
                    "failed to re-create incomplete directory",
                    context={"version_id": str(request.version.version_id), "detail": str(error)},
                ) from error
        try:
            files, coverage = self._write_partitions(incomplete, request)
            manifest = self._build_manifest(request, files, coverage)
            self._write_manifest_and_complete(incomplete, manifest)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(incomplete, final)
        except CanonicalWarehouseError as error:
            self._preserve_failure(incomplete, request, error)
            raise
        except Exception as error:
            self._preserve_failure(incomplete, request, error)
            raise CanonicalWarehouseError(
                "canonical warehouse publish failed",
                context={"version_id": str(request.version.version_id)},
            ) from error
        self._validate_final(final)
        return final

    def _reconcile_existing(self, final: Path, request: CanonicalPublishRequest) -> Path:
        # Run the unified strict verifier first. Any on-disk integrity
        # failure (tampered manifest, mutated file, missing COMPLETE,
        # duplicate paths, extra files, wrong row counts) surfaces as
        # IdempotencyConflictError here because the caller is trying
        # to re-publish the same request against a directory that no
        # longer matches its declared state.
        try:
            verified = verify_canonical_directory(final, data_root=self._data_root)
        except ManifestIntegrityError as error:
            raise IdempotencyConflictError(
                "existing canonical directory failed integrity verification",
                context={"version_id": str(request.version.version_id)},
            ) from error
        previous = verified.manifest
        if previous.get("manifest_schema_version") != "warehouse-manifest.v1":
            raise IdempotencyConflictError(
                "canonical manifest schema version is unsupported",
                context={"version_id": str(request.version.version_id)},
            )
        # Compare every credential field that the request controls.
        # The manifest's manifest_sha256 has already been verified by
        # the verifier; the remaining fields must match the request's
        # declared inputs.
        if previous.get("version_id") != str(request.version.version_id):
            raise IdempotencyConflictError(
                "version_id mismatch against existing manifest",
                context={"version_id": str(request.version.version_id)},
            )
        if previous.get("dataset") != request.version.dataset.value:
            raise IdempotencyConflictError(
                "dataset mismatch against existing manifest",
                context={"version_id": str(request.version.version_id)},
            )
        # ``input_batch_ids`` is the historical list of raw batch ids that
        # contributed to this version; for a repeat publish the request
        # must carry the same list in the same order.
        request_batch_ids = [str(batch_id) for batch_id in request.version.input_batch_ids]
        previous_batch_ids_raw = previous.get("input_batch_ids", [])
        previous_batch_ids = (
            [str(b) for b in previous_batch_ids_raw]
            if isinstance(previous_batch_ids_raw, list)
            else []
        )
        if previous_batch_ids != request_batch_ids:
            raise IdempotencyConflictError(
                "input_batch_ids mismatch against existing manifest",
                context={"version_id": str(request.version.version_id)},
            )
        if previous.get("rule_version") != request.rule_version:
            raise IdempotencyConflictError(
                "rule_version mismatch against existing manifest",
                context={"version_id": str(request.version.version_id)},
            )
        if previous.get("code_version") != request.code_version:
            raise IdempotencyConflictError(
                "code_version mismatch against existing manifest",
                context={"version_id": str(request.version.version_id)},
            )
        expected_schema_version = descriptor_for(request.version.dataset.value).schema_version
        if previous.get("schema_version") != expected_schema_version:
            raise IdempotencyConflictError(
                "schema_version mismatch against existing manifest",
                context={"version_id": str(request.version.version_id)},
            )
        # Compare the content_sha256 to detect any byte-level drift in
        # the publishable data, even if the file layout is unchanged.
        previous_content_sha = (
            self._compute_content_sha(request.version.dataset.value, request.tables)
            if request.tables
            else compute_sha256(_empty_dataset_marker(request.version.dataset.value))
        )
        if previous.get("content_sha256") != previous_content_sha:
            raise IdempotencyConflictError(
                "content mismatch against existing manifest",
                context={"version_id": str(request.version.version_id)},
            )
        return final

    # -- writing partitions -------------------------------------------------

    def _write_partitions(
        self, directory: Path, request: CanonicalPublishRequest
    ) -> tuple[list[CanonicalManifestFile], dict[str, str]]:
        dataset = request.version.dataset.value
        if not request.tables:
            return [], {
                "security_count": "0",
                "row_count": "0",
                "date_count": "0",
            }
        combined = pa.concat_tables(list(request.tables))
        combined = ensure_sorted(dataset, combined)
        scheme = partition_scheme_for(dataset)
        date_column = scheme.date_field
        groups: dict[tuple[int, int], list[int]] = {}
        for index in range(combined.num_rows):
            date_value = combined.column(date_column)[index].as_py()
            year_month = (date_value.year, date_value.month)
            groups.setdefault(year_month, []).append(index)
        files: list[CanonicalManifestFile] = []
        for ordinal, ((year, month), row_indices) in enumerate(sorted(groups.items())):
            relative = f"year={year:04d}/month={month:02d}/part-{ordinal:05d}.parquet"
            target = directory / relative
            self._assert_managed_target(directory, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            table_slice = combined.take(pa.array(row_indices, type=pa.int64()))
            pa_parquet.write_table(table_slice, target, compression="snappy")  # type: ignore[no-untyped-call]
            payload = target.read_bytes()
            files.append(
                CanonicalManifestFile(
                    relative_path=relative,
                    partition={
                        "year": f"{year:04d}",
                        "month": f"{month:02d}",
                    },
                    size=len(payload),
                    row_count=table_slice.num_rows,
                    sha256=compute_sha256(payload),
                )
            )
        coverage = self._build_coverage(combined, request)
        return files, coverage

    def _build_coverage(
        self, combined: pa.Table, request: CanonicalPublishRequest
    ) -> dict[str, str]:
        dataset = request.version.dataset.value
        scheme = partition_scheme_for(dataset)
        date_column = scheme.date_field
        if date_column not in combined.column_names:
            return {"security_count": "0", "row_count": str(combined.num_rows)}
        dates = sorted(
            {combined.column(date_column)[index].as_py() for index in range(combined.num_rows)}
        )
        security_count = (
            len(set(combined.column("code").to_pylist())) if "code" in combined.column_names else 0
        )
        coverage: dict[str, str] = {
            "security_count": str(security_count),
            "row_count": str(combined.num_rows),
            "date_count": str(len(dates)),
        }
        # Only include first_date/last_date when there is at least one
        # row. For legal empty versions the manifest's coverage_start
        # and coverage_end are None and the coverage_summary must not
        # carry empty-string values (the DatasetVersion contract
        # requires non-empty text for every coverage_summary value).
        if dates:
            coverage["first_date"] = dates[0].isoformat()
            coverage["last_date"] = dates[-1].isoformat()
        return coverage

    def _build_manifest(
        self,
        request: CanonicalPublishRequest,
        files: Sequence[CanonicalManifestFile],
        coverage: dict[str, str],
    ) -> CanonicalManifest:
        dataset = request.version.dataset.value
        scheme = partition_scheme_for(dataset)
        # The manifest schema_version MUST come from the dataset
        # descriptor so it matches the DatasetVersion's schema_version
        # exactly. The caller cannot override this.
        schema_version = descriptor_for(dataset).schema_version
        # Coverage credentials: the manifest always stores the computed
        # coverage_summary (so the publish coordinator and query adapter
        # see the full set of keys). Caller-supplied values are treated
        # as expected credentials and must equal the computed values.
        coverage_summary = coverage
        if request.coverage_summary:
            for key, expected in request.coverage_summary.items():
                actual = coverage.get(key)
                if actual is not None and expected != actual:
                    raise CanonicalWarehouseError(
                        "caller-supplied coverage credential does not match actual data",
                        context=_string_context(
                            {
                                "version_id": str(request.version.version_id),
                                "field": key,
                                "expected": expected,
                                "actual": actual,
                            }
                        ),
                    )
        content_sha = (
            self._compute_content_sha(dataset, request.tables)
            if request.tables
            else compute_sha256(_empty_dataset_marker(dataset))
        )
        # coverage_start/coverage_end are always sourced from the actual
        # data (via the ``first_date``/``last_date`` computed coverage
        # entries) so the manifest, the publish coordinator and the
        # DatasetVersion all see the same value. Caller-supplied values
        # are treated as expected credentials and must match exactly.
        coverage_start_value: str | None = coverage.get("first_date") or None
        if request.coverage_start is not None:
            expected_start = str(request.coverage_start)
            if coverage_start_value != expected_start:
                raise CanonicalWarehouseError(
                    "caller-supplied coverage_start does not match actual data",
                    context=_string_context(
                        {
                            "version_id": str(request.version.version_id),
                            "expected": coverage_start_value or "",
                            "actual": expected_start,
                        }
                    ),
                )
        coverage_end_value: str | None = coverage.get("last_date") or None
        if request.coverage_end is not None:
            expected_end = str(request.coverage_end)
            if coverage_end_value != expected_end:
                raise CanonicalWarehouseError(
                    "caller-supplied coverage_end does not match actual data",
                    context=_string_context(
                        {
                            "version_id": str(request.version.version_id),
                            "expected": coverage_end_value or "",
                            "actual": expected_end,
                        }
                    ),
                )
        return CanonicalManifest(
            dataset=dataset,
            version_id=str(request.version.version_id),
            schema_version=schema_version,
            code_version=request.code_version,
            rule_version=request.rule_version,
            parent_version_id=(
                str(request.version.parent_version_id)
                if request.version.parent_version_id is not None
                else None
            ),
            input_batch_ids=tuple(str(batch_id) for batch_id in request.version.input_batch_ids),
            coverage_start=coverage_start_value,
            coverage_end=coverage_end_value,
            coverage_summary=coverage_summary,
            files=tuple(files),
            created_at=_now_iso(),
            content_sha256=content_sha,
            partition_scheme={
                "scheme_id": scheme.scheme_id,
                "scheme_version": scheme.scheme_version,
                "partition_keys": list(scheme.partition_keys),
                "date_field": scheme.date_field,
            },
        )

    @staticmethod
    def _compute_content_sha(dataset: str, tables: Sequence[pa.Table]) -> str:
        """Return a deterministic SHA-256 of the publishable data.

        The combined table is sorted by the canonical sort key and
        written to an in-memory Parquet buffer. Sorting the data first
        keeps the byte stream identical for the same logical contents
        regardless of input chunk ordering.
        """
        if not tables:
            return compute_sha256(_empty_dataset_marker(dataset))
        combined = pa.concat_tables(list(tables))
        combined = ensure_sorted(dataset, combined)
        import io

        buffer = io.BytesIO()
        pa_parquet.write_table(combined, buffer, compression="snappy")  # type: ignore[no-untyped-call]
        return compute_sha256(buffer.getvalue())

    def _write_manifest(self, directory: Path, manifest: CanonicalManifest) -> None:
        target = directory / WAREHOUSE_MANIFEST_FILENAME
        # Write the signed manifest so the on-disk file embeds its own
        # SHA-256; the COMPLETE marker also binds the same hash.
        target.write_text(manifest.to_signed_json(), encoding="utf-8")

    def _write_manifest_and_complete(self, directory: Path, manifest: CanonicalManifest) -> None:
        """Write the manifest, verify the inventory, then write COMPLETE.

        Centralised in its own method so tests can inject failures here.
        """
        self._write_manifest(directory, manifest)
        self._verify_inventory(directory, manifest)
        self._write_complete_marker(directory, manifest)

    def _verify_inventory(self, directory: Path, manifest: CanonicalManifest) -> None:
        listed = {entry.relative_path: entry for entry in manifest.files}
        managed_set = {
            WAREHOUSE_MANIFEST_FILENAME,
            WAREHOUSE_COMPLETE_FILENAME,
            WAREHOUSE_FAILURE_FILENAME,
            WAREHOUSE_INPUT_LISTING_FILENAME,
        }
        for absolute in directory.rglob("*"):
            if absolute.is_dir():
                continue
            try:
                managed = absolute.relative_to(directory).as_posix()
            except ValueError as error:
                raise ManifestIntegrityError(
                    "warehouse file escapes its managed directory",
                    context={"path": str(absolute)},
                ) from error
            if managed in managed_set:
                continue
            if managed not in listed:
                raise ManifestIntegrityError(
                    "warehouse directory has unexpected file",
                    context={"relative_path": managed},
                )
        for relative, entry in listed.items():
            absolute = directory / relative
            if not absolute.is_file():
                raise ManifestIntegrityError(
                    "manifest file is missing on disk",
                    context={"relative_path": relative},
                )
            payload = absolute.read_bytes()
            if len(payload) != entry.size:
                raise ManifestIntegrityError(
                    "manifest file size mismatch",
                    context={"relative_path": relative},
                )
            if compute_sha256(payload) != entry.sha256:
                raise ManifestIntegrityError(
                    "manifest file SHA-256 mismatch",
                    context={"relative_path": relative},
                )

    def _write_complete_marker(self, directory: Path, manifest: CanonicalManifest) -> None:
        target = directory / WAREHOUSE_COMPLETE_FILENAME
        payload = {
            "version_id": manifest.version_id,
            "manifest_sha256": manifest.manifest_sha256(),
            "completed_at": _now_iso(),
        }
        target.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

    def _validate_final(self, final: Path) -> None:
        # Re-verify the final directory through the unified verifier so
        # the on-disk state is guaranteed consistent before the caller
        # treats the directory as published. The verifier checks
        # manifest canonical bytes, self-SHA, COMPLETE binding, file
        # set equality, per-file size/SHA/row_count and Parquet schema
        # consistency.
        try:
            verify_canonical_directory(final, data_root=self._data_root)
        except ManifestIntegrityError as error:
            raise CanonicalWarehouseError(
                "canonical published directory failed integrity verification",
                context={"version_id": str(final.name)},
            ) from error

    def _assert_managed_target(self, root: Path, target: Path) -> None:
        # Delegate to the shared verifier helper so the symlink/junction
        # and containment checks are identical across Raw, Canonical and
        # Query.
        assert_path_under_root(self._data_root, target)

    def _write_input_listing(self, directory: Path, request: CanonicalPublishRequest) -> None:
        """Write a forensic input listing for the publish request.

        The listing records the version identity, dataset, code/rule
        versions, schema version, input batch ids and the row count of
        each input table. It is written *before* any payload write so
        it survives any subsequent failure.
        """
        listing = {
            "version_id": str(request.version.version_id),
            "dataset": request.version.dataset.value,
            "schema_version": descriptor_for(request.version.dataset.value).schema_version,
            "code_version": request.code_version,
            "rule_version": request.rule_version,
            "parent_version_id": (
                str(request.version.parent_version_id)
                if request.version.parent_version_id is not None
                else None
            ),
            "input_batch_ids": [str(b) for b in request.version.input_batch_ids],
            "table_count": len(request.tables),
            "table_row_counts": [int(table.num_rows) for table in request.tables],
            "empty_result": (
                None
                if request.empty_result is None
                else {
                    "classification": request.empty_result.classification.value,
                    "reason": request.empty_result.reason,
                }
            ),
            "captured_at": _now_iso(),
        }
        target = directory / WAREHOUSE_INPUT_LISTING_FILENAME
        target.write_text(
            json.dumps(listing, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

    def _preserve_failure(
        self,
        incomplete: Path,
        request: CanonicalPublishRequest,
        error: BaseException | None,
    ) -> None:
        record: dict[str, object] = {
            "version_id": str(request.version.version_id),
            "dataset": request.version.dataset.value,
            "schema_version": descriptor_for(request.version.dataset.value).schema_version,
            "code_version": request.code_version,
            "rule_version": request.rule_version,
            "parent_version_id": (
                str(request.version.parent_version_id)
                if request.version.parent_version_id is not None
                else None
            ),
            "input_batch_ids": [str(b) for b in request.version.input_batch_ids],
            "failed_at": _now_iso(),
        }
        failure = _sanitize_failure_record(record, error=error)
        if incomplete.exists():
            with contextlib.suppress(OSError):
                (incomplete / WAREHOUSE_FAILURE_FILENAME).write_text(
                    json.dumps(
                        failure,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
            # The input listing is already on disk (written before any
            # payload), but we re-write a minimal listing if the failure
            # happened before _write_input_listing had a chance to run.
            input_listing_path = incomplete / WAREHOUSE_INPUT_LISTING_FILENAME
            if not input_listing_path.is_file():
                with contextlib.suppress(OSError):
                    self._write_input_listing(incomplete, request)


# ---------------------------------------------------------------------------
# Failure sanitisation helpers
# ---------------------------------------------------------------------------


_ABSOLUTE_PATH_RE = re.compile(r"[A-Za-z]:[\\/]|^/")


def _sanitize_failure_record(
    record: dict[str, object], *, error: BaseException | None
) -> dict[str, object]:
    """Return a failure record with all sensitive content removed.

    The raw exception text and any absolute paths are stripped so the
    ``failure.json`` is safe to inspect without leaking filesystem
    layout, credentials or request keys. A stable ``error_category``
    derived from the exception class replaces the raw message.
    """
    sanitized: dict[str, object] = {}
    for key, value in record.items():
        if isinstance(value, str):
            sanitized[key] = _sanitize_text(value)
        elif isinstance(value, list):
            sanitized[key] = [
                _sanitize_text(item) if isinstance(item, str) else item for item in value
            ]
        else:
            sanitized[key] = value
    if error is not None:
        sanitized["error_type"] = type(error).__name__
        sanitized["error_category"] = _classify_error(error)
    else:
        sanitized["error_type"] = "UnknownError"
        sanitized["error_category"] = "INTERNAL_FAILURE"
    return sanitized


def _sanitize_text(value: str) -> str:
    """Strip absolute paths and control characters from a string."""
    if not isinstance(value, str):
        return ""
    # Replace any absolute path pattern with a redaction marker.
    redacted = _ABSOLUTE_PATH_RE.sub("[REDACTED]", value)
    # Remove control characters except for the common whitespace chars.
    cleaned = "".join(ch if ch >= " " or ch in " \t" else "" for ch in redacted)
    return cleaned


def _classify_error(error: BaseException) -> str:
    """Return a stable, sanitized error category."""
    name = type(error).__name__
    if isinstance(error, OSError):
        return "IO_FAILURE"
    if isinstance(error, (KeyError, ValueError, TypeError)):
        return "CONTRACT_FAILURE"
    if isinstance(
        error,
        (CanonicalWarehouseError, ManifestIntegrityError, IncompatibleInputError),
    ):
        return "WAREHOUSE_FAILURE"
    return f"INTERNAL_FAILURE:{name}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _issue_to_dict(issue: CanonicalValidationIssue) -> dict[str, object]:
    """Convert a :class:`CanonicalValidationIssue` to a plain dict.

    The issue is a ``slots=True`` frozen dataclass which means it has
    no ``__dict__`` attribute; we use :func:`dataclasses.asdict` so the
    serialisable context payload survives the structured error path.
    """
    from dataclasses import asdict

    return asdict(issue)


def _date_for(year: str, month: str) -> object:
    import datetime as _dt

    return _dt.date(int(year), int(month), 1)


def _normalise_files(entries: object) -> list[dict[str, object]]:
    if not isinstance(entries, list):
        return []
    normalised: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return []
        relative_path = entry.get("relative_path")
        partition = entry.get("partition", {})
        size = entry.get("size")
        row_count = entry.get("row_count")
        sha = entry.get("sha256")
        if not all(
            isinstance(value, (str, int)) for value in (relative_path, size, row_count, sha)
        ):
            return []
        if size is None or row_count is None:
            return []
        normalised.append(
            {
                "relative_path": str(relative_path),
                "partition": dict(partition) if isinstance(partition, dict) else {},
                "size": int(size),
                "row_count": int(row_count),
                "sha256": str(sha),
            }
        )
    normalised.sort(key=lambda item: str(item["relative_path"]))
    return normalised


def _build_request_signature(
    request: CanonicalPublishRequest, files: list[CanonicalManifestFile]
) -> dict[str, object]:
    return {
        "version_id": str(request.version.version_id),
        "dataset": request.version.dataset.value,
        "code_version": request.code_version,
        "rule_version": request.rule_version,
        "input_batch_ids": [str(batch_id) for batch_id in request.version.input_batch_ids],
        "files": _normalise_files([entry.to_dict() for entry in files]),
    }


__all__ = [
    "CanonicalPublishRequest",
    "CanonicalWarehouse",
    "CanonicalWarehouseError",
    "IncompatibleInputError",
]
