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
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
from pyarrow import parquet as pa_parquet

from quantking.common.errors import IdempotencyConflictError, InvalidPathError
from quantking.data.adapters.canonical_schema import (
    CANONICAL_SCHEMA_VERSION,
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
    WAREHOUSE_MANIFEST_FILENAME,
    CanonicalManifest,
    CanonicalManifestFile,
    compute_sha256,
    load_canonical_manifest,
    path_safe_id,
)
from quantking.data.adapters.warehouse.partitioning import (
    partition_scheme_for,
)
from quantking.data.models import (
    DatasetVersion,
    EmptyResultInfo,
)


@dataclass(frozen=True, slots=True)
class CanonicalPublishRequest:
    """The request to publish one canonical dataset version."""

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
    published directory.
    """

    def __init__(self, *, data_root: Path) -> None:
        root = Path(data_root)
        if not root.is_absolute():
            raise InvalidPathError("data_root must be an absolute path")
        self._data_root = root.resolve()

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
        except CanonicalWarehouseError:
            self._preserve_failure(incomplete, request, None)
            raise
        except Exception as error:
            self._preserve_failure(incomplete, request, error)
            if not incomplete.exists():
                # Atomic rename succeeded but the metadata check failed;
                # remove the empty final directory so a retry can land cleanly.
                self._safe_rmdir(final)
            raise
        self._validate_final(final, manifest=manifest)
        return final

    def _reconcile_existing(self, final: Path, request: CanonicalPublishRequest) -> Path:
        manifest_path = final / WAREHOUSE_MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise IdempotencyConflictError(
                "existing canonical directory has no manifest",
                context={"version_id": str(request.version.version_id)},
            )
        previous = load_canonical_manifest(final)
        if previous.get("manifest_schema_version") != "warehouse-manifest.v1":
            raise IdempotencyConflictError(
                "canonical manifest schema version is unsupported",
                context={"version_id": str(request.version.version_id)},
            )
        current_files, _coverage = self._plan_partitions(request)
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
        # Compare the structural identity of the file set: the plan
        # returns placeholder size/sha so the only stable comparison is
        # on (relative_path, partition, row_count). The content_sha256
        # distinguishes two semantically different inputs that happen
        # to share the same row count and partition layout.
        plan_signature = [
            {
                "relative_path": entry.relative_path,
                "partition": _coerce_partition_to_int(dict(entry.partition)),
                "row_count": entry.row_count,
            }
            for entry in current_files
        ]
        previous_files = _normalise_files(previous.get("files"))
        previous_signature: list[dict[str, object]] = []
        for entry in previous_files:
            partition = entry.get("partition", {})
            previous_signature.append(
                {
                    "relative_path": entry["relative_path"],
                    "partition": _coerce_partition_to_int(
                        partition if isinstance(partition, dict) else {}
                    ),
                    "row_count": entry["row_count"],
                }
            )
        if previous_signature != plan_signature:
            raise IdempotencyConflictError(
                "files mismatch against existing manifest",
                context={"version_id": str(request.version.version_id)},
            )
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

    def _plan_partitions(
        self, request: CanonicalPublishRequest
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
        files: list[CanonicalManifestFile] = []
        scheme = partition_scheme_for(dataset)
        date_column = scheme.date_field
        if date_column not in combined.column_names:
            raise CanonicalWarehouseError(
                "canonical table is missing the partition date field",
                context={"dataset": dataset, "date_field": date_column},
            )
        groups: dict[tuple[int, int], list[int]] = {}
        for index in range(combined.num_rows):
            date_value = combined.column(date_column)[index].as_py()
            year_month = (date_value.year, date_value.month)
            groups.setdefault(year_month, []).append(index)
        for ordinal, ((year, month), row_indices) in enumerate(sorted(groups.items())):
            relative = f"year={year:04d}/month={month:02d}/part-{ordinal:05d}.parquet"
            files.append(
                CanonicalManifestFile(
                    relative_path=relative,
                    partition={
                        "year": f"{year:04d}",
                        "month": f"{month:02d}",
                    },
                    size=0,
                    row_count=len(row_indices),
                    sha256="0" * 64,
                )
            )
        coverage = self._build_coverage(combined, request)
        # ``_normalise_files`` (used by reconciliation) converts the
        # partition values via ``int`` so the plan signature matches
        # the on-disk JSON integers; for the test the partition dict
        # still has ints, so we explicitly serialise here.
        coverage = dict(coverage)
        return files, coverage

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
        return {
            "security_count": str(security_count),
            "row_count": str(combined.num_rows),
            "date_count": str(len(dates)),
            "first_date": dates[0].isoformat() if dates else "",
            "last_date": dates[-1].isoformat() if dates else "",
        }

    def _build_manifest(
        self,
        request: CanonicalPublishRequest,
        files: Sequence[CanonicalManifestFile],
        coverage: dict[str, str],
    ) -> CanonicalManifest:
        dataset = request.version.dataset.value
        scheme = partition_scheme_for(dataset)
        coverage_summary = dict(request.coverage_summary) or coverage
        content_sha = (
            self._compute_content_sha(dataset, request.tables)
            if request.tables
            else compute_sha256(_empty_dataset_marker(dataset))
        )
        return CanonicalManifest(
            dataset=dataset,
            version_id=str(request.version.version_id),
            schema_version=CANONICAL_SCHEMA_VERSION,
            code_version=request.code_version,
            rule_version=request.rule_version,
            parent_version_id=(
                str(request.version.parent_version_id)
                if request.version.parent_version_id is not None
                else None
            ),
            input_batch_ids=tuple(str(batch_id) for batch_id in request.version.input_batch_ids),
            coverage_start=(
                str(request.coverage_start)
                if request.coverage_start is not None
                else coverage_summary.get("first_date")
            ),
            coverage_end=(
                str(request.coverage_end)
                if request.coverage_end is not None
                else coverage_summary.get("last_date")
            ),
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

    def _validate_final(self, final: Path, *, manifest: CanonicalManifest) -> None:
        self._verify_inventory(final, manifest)
        marker = final / WAREHOUSE_COMPLETE_FILENAME
        if not marker.is_file():
            raise ManifestIntegrityError(
                "warehouse COMPLETE marker is missing after publish",
                context={"version_id": manifest.version_id},
            )
        marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        if marker_payload.get("version_id") != manifest.version_id:
            raise ManifestIntegrityError(
                "COMPLETE marker does not bind to the expected version_id",
                context={"version_id": manifest.version_id},
            )
        if marker_payload.get("manifest_sha256") != manifest.manifest_sha256():
            raise ManifestIntegrityError(
                "COMPLETE marker does not match the manifest SHA-256",
                context={"version_id": manifest.version_id},
            )

    def _assert_managed_target(self, root: Path, target: Path) -> None:
        try:
            resolved = target.resolve()
            root_resolved = root.resolve()
        except OSError as error:
            raise IncompatibleInputError(
                "warehouse target is not resolvable",
                context={"target": str(target), "detail": str(error)},
            ) from error
        if root_resolved != resolved and root_resolved not in resolved.parents:
            raise IncompatibleInputError(
                "warehouse target escapes the managed data root",
                context={"target": str(target), "root": str(root_resolved)},
            )

    def _preserve_failure(
        self,
        incomplete: Path,
        request: CanonicalPublishRequest,
        error: BaseException | None,
    ) -> None:
        failure = {
            "version_id": str(request.version.version_id),
            "dataset": request.version.dataset.value,
            "failed_at": _now_iso(),
            "error_type": type(error).__name__ if error is not None else "UnknownError",
            "error_message": str(error) if error is not None else "",
        }
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

    def _safe_rmdir(self, path: Path) -> None:
        with contextlib.suppress(OSError):
            if path.is_dir():
                shutil.rmtree(path)


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
