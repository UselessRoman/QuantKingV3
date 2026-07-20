"""Application-level Canonical publish coordinator (Stage 1 S1-02-R1, S1-04A).

The coordinator is the *single* application-level entry point for
publishing a Canonical :class:`DatasetVersion` end-to-end. It owns the
full publish flow mandated by the S1-02-R1 repair brief and the S1-04A
quality gate:

1. Validate the BUILDING :class:`DatasetVersion` and the request
   credentials.
2. **Quality gate**: load the ``QualityRun`` referenced by
   ``quality_run_id`` and verify it is PASSED, bound to the same
   ``version_id``, the same ``rule_version``, the same
   ``schema_version`` and the same candidate ``content_sha256`` the
   coordinator computes from ``request.tables``. A FAILED, SKIPPED,
   mismatched or missing run transitions the version to FAILED with
   a QUALITY failure and aborts the coordination before any file is
   written.
3. Outside any transaction, build, validate and atomically publish
   the on-disk Parquet files through :meth:`CanonicalWarehouse.publish`.
   The warehouse publish is idempotent: a re-coordination against an
   existing directory returns the same path without rewriting any
   file.
4. Read the actual ``manifest_sha256``, coverage dates,
   ``security_count``, ``row_count`` and ``coverage_summary`` from
   the on-disk manifest. The coordinator never accepts caller-supplied
   values for these fields; the request dataclass does not even
   declare them.
5. Short transaction: save the BUILDING version (if it is not yet in
   SQLite) and transition it to VALIDATED with the real manifest SHA
   and the actual coverage metadata. If the version is already
   VALIDATED, the coordinator verifies the stored credentials match
   the on-disk manifest and proceeds.
6. Re-verify the final published directory through the unified strict
   verifier (:func:`verify_canonical_directory`).
7. Short transaction: transition VALIDATED -> PUBLISHED. The SQLite
   trigger created by migration ``20260716_0004_quality_gate`` blocks
   this transition if no PASSED ``QualityRun`` row exists for the same
   ``version_id``; this is the database-level backstop that prevents
   a direct SQL UPDATE or a repository call from bypassing the gate.
8. Return the final PUBLISHED :class:`DatasetVersion` and its on-disk
   directory.

If the SQLite commit fails after the file publish (between
BUILDING->VALIDATED or VALIDATED->PUBLISHED), a re-coordination with
the exact same request must succeed without rewriting the published
files: the warehouse publish is idempotent, and the metadata
transitions are short, re-entrant and credential-bound.

The coordinator is the only component that may transition a
:class:`DatasetVersion` to PUBLISHED. Direct calls to the SQLite
repository's ``save_version`` cannot bypass the credential binding
because the query adapter re-validates the on-disk manifest against
the SQLite row on every read, and the SQLite trigger blocks any
PUBLISHED transition that lacks a matching PASSED quality run.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from quantking.common.errors import DatasetVersionNotFoundError
from quantking.common.ids import EntityId
from quantking.common.time import TradingDate, utc_now
from quantking.data.adapters.persistence.sqlite import DataSqliteUnitOfWorkFactory
from quantking.data.adapters.warehouse.canonical_warehouse import (
    CanonicalPublishRequest,
    CanonicalWarehouse,
    CanonicalWarehouseError,
)
from quantking.data.adapters.warehouse.manifest import WAREHOUSE_MANIFEST_FILENAME
from quantking.data.adapters.warehouse.verifier import verify_canonical_directory
from quantking.data.models import (
    BatchFailureInfo,
    BatchFailureKind,
    DatasetVersion,
    DatasetVersionStatus,
    EmptyResultInfo,
    QualityRun,
    QualityRunStatus,
    transition_dataset_version,
)
from quantking.data.schemas import descriptor_for


def compute_content_sha256(tables: tuple[pa.Table, ...]) -> str:
    """Compute a deterministic SHA-256 over the parquet bytes of ``tables``.

    The same algorithm is used by the quality rule engine's
    :class:`QualityRequest.content_sha256` and by the coordinator's
    quality-gate check so a PASSED run can be proven to bind to the
    exact candidate content the coordinator is publishing. The hash is
    order-sensitive: ``tables`` is a tuple, not a set.
    """
    if not isinstance(tables, tuple):
        raise TypeError("tables must be a tuple")
    hasher = hashlib.sha256()
    for table in tables:
        if not isinstance(table, pa.Table):
            raise TypeError("tables must contain pa.Table values")
        buffer = io.BytesIO()
        pq.write_table(table, buffer, compression="snappy")  # type: ignore[no-untyped-call]
        hasher.update(buffer.getvalue())
    return hasher.hexdigest()


@dataclass(frozen=True, slots=True)
class CanonicalPublishCoordinatorRequest:
    """A coordinator publish request.

    The request deliberately does NOT accept ``manifest_sha256``,
    ``row_count``, ``security_count`` or ``coverage_*`` fields: those
    credentials are sourced from the actual on-disk manifest by the
    coordinator. Callers cannot forge them. Passing any of these
    fields as a keyword argument raises :class:`TypeError` because the
    dataclass does not declare them.

    ``quality_run_id`` is mandatory: no PUBLISHED transition may occur
    without a matching PASSED ``QualityRun``. The coordinator verifies
    the run's ``version_id``, ``rule_version``, ``schema_version`` and
    ``content_sha256`` all match the candidate before publishing.
    """

    version: DatasetVersion
    tables: tuple[pa.Table, ...]
    code_version: str
    rule_version: str
    quality_run_id: EntityId
    empty_result: EmptyResultInfo | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.version, DatasetVersion):
            raise TypeError("version must be a DatasetVersion")
        if not isinstance(self.tables, tuple):
            object.__setattr__(self, "tables", tuple(self.tables))
        if not self.tables and self.empty_result is None:
            raise ValueError("tables or an explicit empty_result is required")
        for table in self.tables:
            if not isinstance(table, pa.Table):
                raise TypeError("tables must contain pa.Table values")
        if not isinstance(self.code_version, str) or not self.code_version.strip():
            raise ValueError("code_version must be non-empty text")
        if not isinstance(self.rule_version, str) or not self.rule_version.strip():
            raise ValueError("rule_version must be non-empty text")
        if not isinstance(self.quality_run_id, EntityId):
            raise TypeError("quality_run_id must be an EntityId")
        if self.quality_run_id.namespace != "data_quality":
            raise ValueError("quality_run_id must use the data_quality namespace")
        if self.empty_result is not None and not isinstance(self.empty_result, EmptyResultInfo):
            raise TypeError("empty_result must be an EmptyResultInfo or None")


@dataclass(frozen=True, slots=True)
class CanonicalPublishResult:
    """The PUBLISHED version and its on-disk directory."""

    version: DatasetVersion
    directory: Path


class CanonicalPublishCoordinator:
    """The unique application-level Canonical publish entry point."""

    def __init__(
        self,
        *,
        warehouse: CanonicalWarehouse,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        if not isinstance(warehouse, CanonicalWarehouse):
            raise TypeError("warehouse must be a CanonicalWarehouse")
        if uow_factory is None:
            raise ValueError("uow_factory is required")
        self._warehouse = warehouse
        self._uow_factory = uow_factory

    # -- public API ---------------------------------------------------------

    def coordinate(self, request: CanonicalPublishCoordinatorRequest) -> CanonicalPublishResult:
        """Execute the quality-gated publish coordination flow."""
        if not isinstance(request, CanonicalPublishCoordinatorRequest):
            raise TypeError("request must be a CanonicalPublishCoordinatorRequest")
        # Step 1: validate the BUILDING version. The caller may pass a
        # BUILDING version even if SQLite already has a VALIDATED or
        # PUBLISHED row (re-coordination after a commit failure); we
        # trust the SQLite row as the source of truth below.
        version = request.version
        if version.status is not DatasetVersionStatus.BUILDING:
            raise CanonicalWarehouseError(
                "coordinator request must carry a BUILDING dataset version",
                context={
                    "version_id": str(version.version_id),
                    "status": version.status.value,
                },
            )
        expected_schema = descriptor_for(version.dataset.value).schema_version
        if version.schema_version != expected_schema:
            raise CanonicalWarehouseError(
                "dataset version schema_version does not match the canonical descriptor",
                context={"version_id": str(version.version_id)},
            )
        if request.code_version != version.code_version:
            raise CanonicalWarehouseError(
                "request code_version does not match the dataset version",
                context={"version_id": str(version.version_id)},
            )
        if request.rule_version != version.rule_version:
            raise CanonicalWarehouseError(
                "request rule_version does not match the dataset version",
                context={"version_id": str(version.version_id)},
            )
        # Step 2: quality gate. Compute the candidate content SHA from
        # the request tables, load the referenced QualityRun and verify
        # it is PASSED and bound to the same version_id, rule_version,
        # schema_version and content_sha256. A FAILED/SKIPPED/mismatched
        # run transitions the version to FAILED with a QUALITY failure
        # and aborts before any file is written.
        candidate_content_sha = compute_content_sha256(request.tables)
        self._enforce_quality_gate(
            version=version,
            quality_run_id=request.quality_run_id,
            candidate_content_sha=candidate_content_sha,
        )
        # Step 3: build, validate and atomically publish the on-disk
        # files. ``CanonicalWarehouse.publish`` is idempotent: a
        # re-coordination against an existing directory re-validates
        # the manifest and returns the same path without rewriting
        # any file.
        warehouse_request = CanonicalPublishRequest(
            version=request.version,
            tables=request.tables,
            code_version=request.code_version,
            rule_version=request.rule_version,
            empty_result=request.empty_result,
        )
        directory = self._warehouse.publish(warehouse_request)
        # Step 4: read the actual manifest credentials from the on-disk
        # manifest. The coordinator never accepts caller-supplied
        # values for these fields.
        credentials = self._read_manifest_credentials(directory)
        # Step 5: short transaction. Save the BUILDING version (if it
        # is not yet in SQLite) and transition it to VALIDATED with
        # the real manifest SHA and the actual coverage metadata. If
        # the version is already VALIDATED, verify the stored
        # credentials match the on-disk manifest.
        self._transition_to_validated(version, credentials)
        # Step 6: re-verify the final published directory through the
        # unified strict verifier. The warehouse already verified the
        # directory at publish time, but we re-verify here so a
        # post-publish tamper is caught before the PUBLISHED
        # transition is committed.
        verify_canonical_directory(directory, data_root=self._warehouse.data_root)
        # Step 7: short transaction. Transition VALIDATED -> PUBLISHED.
        # The SQLite trigger ``dataset_versions_quality_gate_before_publish``
        # backstops this transition: even a direct SQL UPDATE cannot
        # mark a version PUBLISHED without a PASSED quality run.
        published = self._transition_to_published(version)
        # Step 8: return the final PUBLISHED version and directory.
        return CanonicalPublishResult(version=published, directory=directory)

    # -- quality gate ------------------------------------------------------

    def _enforce_quality_gate(
        self,
        *,
        version: DatasetVersion,
        quality_run_id: EntityId,
        candidate_content_sha: str,
    ) -> QualityRun:
        """Load the quality run and verify it binds to the candidate content.

        On any failure (missing run, FAILED/SKIPPED status, or a
        version_id/rule_version/schema_version/content_sha256 mismatch)
        the version is transitioned to FAILED with a QUALITY failure
        and the coordination is aborted.
        """
        quality_failure = BatchFailureInfo(
            error_type=BatchFailureKind.QUALITY,
            message="quality gate rejected the candidate version",
            retryable=False,
        )
        with self._uow_factory() as uow:
            try:
                stored = uow.data.get_version(str(version.version_id))
            except DatasetVersionNotFoundError:
                stored = None
            try:
                run = uow.data.get_quality_run(str(quality_run_id))
            except Exception:
                run = None
            failure_reason: str | None = None
            if run is None:
                failure_reason = "quality run does not exist"
            elif run.version_id != version.version_id:
                failure_reason = "quality run is bound to a different version_id"
            elif run.rule_version != version.rule_version:
                failure_reason = "quality run rule_version does not match the version"
            elif run.schema_version != version.schema_version:
                failure_reason = "quality run schema_version does not match the version"
            elif run.content_sha256 != candidate_content_sha:
                failure_reason = "quality run content_sha256 does not match the candidate"
            elif run.status is QualityRunStatus.FAILED:
                failure_reason = "quality run is FAILED"
            elif run.status is QualityRunStatus.SKIPPED:
                failure_reason = "quality run is SKIPPED"
            elif run.status is QualityRunStatus.RUNNING:
                failure_reason = "quality run is still RUNNING"
            elif run.status is not QualityRunStatus.PASSED:
                failure_reason = f"quality run status {run.status.value} is not PASSED"
            if failure_reason is not None:
                if stored is not None and stored.status is DatasetVersionStatus.BUILDING:
                    failed = transition_dataset_version(
                        stored,
                        DatasetVersionStatus.FAILED,
                        at=utc_now(),
                        failure=quality_failure,
                    )
                    uow.data.save_version(failed)
                    uow.commit()
                else:
                    uow.rollback()
                raise CanonicalWarehouseError(
                    "quality gate rejected the candidate version",
                    context={
                        "version_id": str(version.version_id),
                        "quality_run_id": str(quality_run_id),
                        "reason": failure_reason,
                    },
                )
            uow.rollback()
            assert run is not None  # failure_reason is None only when run is PASSED
            return run

    # -- internals ----------------------------------------------------------

    @dataclass(frozen=True, slots=True)
    class _ManifestCredentials:
        """Credentials sourced from the on-disk manifest."""

        manifest_sha256: str
        security_count: int
        row_count: int
        coverage_start: TradingDate | None
        coverage_end: TradingDate | None
        coverage_summary: dict[str, str]

    def _read_manifest_credentials(self, directory: Path) -> _ManifestCredentials:
        """Read the on-disk manifest and extract the coordinator credentials."""
        manifest_path = directory / WAREHOUSE_MANIFEST_FILENAME
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CanonicalWarehouseError(
                "failed to read published manifest for coordinator credentials",
                context={"directory": str(directory), "detail": str(error)},
            ) from error
        if not isinstance(manifest, dict):
            raise CanonicalWarehouseError(
                "published manifest is not a JSON object",
                context={"directory": str(directory)},
            )
        manifest_sha = manifest.get("manifest_sha256")
        if not isinstance(manifest_sha, str) or not manifest_sha.strip():
            raise CanonicalWarehouseError(
                "published manifest is missing a valid manifest_sha256",
                context={"directory": str(directory)},
            )
        coverage_summary_raw = manifest.get("coverage_summary", {})
        if not isinstance(coverage_summary_raw, dict):
            raise CanonicalWarehouseError(
                "published manifest coverage_summary is not a JSON object",
                context={"directory": str(directory)},
            )
        coverage_summary: dict[str, str] = {
            str(key): str(value) for key, value in coverage_summary_raw.items()
        }
        security_count = int(coverage_summary.get("security_count", "0"))
        row_count = int(coverage_summary.get("row_count", "0"))
        coverage_start_value = manifest.get("coverage_start")
        coverage_end_value = manifest.get("coverage_end")
        coverage_start: TradingDate | None = None
        coverage_end: TradingDate | None = None
        if isinstance(coverage_start_value, str) and coverage_start_value:
            try:
                coverage_start = TradingDate(date.fromisoformat(coverage_start_value))
            except ValueError as error:
                raise CanonicalWarehouseError(
                    "published manifest coverage_start is not a valid ISO date",
                    context={"directory": str(directory), "value": coverage_start_value},
                ) from error
        if isinstance(coverage_end_value, str) and coverage_end_value:
            try:
                coverage_end = TradingDate(date.fromisoformat(coverage_end_value))
            except ValueError as error:
                raise CanonicalWarehouseError(
                    "published manifest coverage_end is not a valid ISO date",
                    context={"directory": str(directory), "value": coverage_end_value},
                ) from error
        return self._ManifestCredentials(
            manifest_sha256=manifest_sha,
            security_count=security_count,
            row_count=row_count,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            coverage_summary=coverage_summary,
        )

    def _transition_to_validated(
        self,
        version: DatasetVersion,
        credentials: _ManifestCredentials,
    ) -> DatasetVersion:
        """Save the BUILDING version (if needed) and transition to VALIDATED."""
        with self._uow_factory() as uow:
            try:
                stored = uow.data.get_version(str(version.version_id))
            except DatasetVersionNotFoundError:
                # First coordination: the BUILDING version is not yet
                # in SQLite. Save it from the request, then reload so
                # the SQLite-side state is the source of truth.
                uow.data.save_version(version)
                uow.commit()
                stored = uow.data.get_version(str(version.version_id))
            if stored.status is DatasetVersionStatus.BUILDING:
                validated = transition_dataset_version(
                    stored,
                    DatasetVersionStatus.VALIDATED,
                    at=utc_now(),
                    manifest_sha256=credentials.manifest_sha256,
                    security_count=credentials.security_count,
                    row_count=credentials.row_count,
                    coverage_start=credentials.coverage_start,
                    coverage_end=credentials.coverage_end,
                    coverage_summary=credentials.coverage_summary,
                )
                uow.data.save_version(validated)
                uow.commit()
                return validated
            if stored.status is DatasetVersionStatus.VALIDATED:
                # Re-coordination after a VALIDATED commit that
                # succeeded but a subsequent PUBLISHED commit failed.
                # Verify the stored credentials match the on-disk
                # manifest, then return the stored VALIDATED version
                # so the caller can proceed to the PUBLISHED transition.
                self._verify_validated_credentials(stored, credentials)
                uow.rollback()
                return stored
            if stored.status is DatasetVersionStatus.PUBLISHED:
                # Another coordinator won the race; the version is
                # already PUBLISHED. Verify the credentials match and
                # return the stored PUBLISHED version. The caller's
                # step 6 will be a no-op.
                self._verify_validated_credentials(stored, credentials)
                uow.rollback()
                return stored
            raise CanonicalWarehouseError(
                "dataset version is in an incompatible state for coordination",
                context={
                    "version_id": str(version.version_id),
                    "status": stored.status.value,
                },
            )

    def _transition_to_published(self, version: DatasetVersion) -> DatasetVersion:
        """Transition the version from VALIDATED to PUBLISHED."""
        with self._uow_factory() as uow:
            stored = uow.data.get_version(str(version.version_id))
            if stored.status is DatasetVersionStatus.VALIDATED:
                published = transition_dataset_version(
                    stored,
                    DatasetVersionStatus.PUBLISHED,
                    at=utc_now(),
                )
                uow.data.save_version(published)
                uow.commit()
                return published
            if stored.status is DatasetVersionStatus.PUBLISHED:
                # Idempotent re-coordination: another coordinator
                # already transitioned to PUBLISHED.
                uow.rollback()
                return stored
            raise CanonicalWarehouseError(
                "dataset version is not in VALIDATED state for PUBLISHED transition",
                context={
                    "version_id": str(version.version_id),
                    "status": stored.status.value,
                },
            )

    def _verify_validated_credentials(
        self,
        stored: DatasetVersion,
        credentials: _ManifestCredentials,
    ) -> None:
        """Verify the SQLite row's credentials match the on-disk manifest."""
        if stored.manifest_sha256 != credentials.manifest_sha256:
            raise CanonicalWarehouseError(
                "stored manifest_sha256 does not match the on-disk manifest",
                context={"version_id": str(stored.version_id)},
            )
        if stored.security_count != credentials.security_count:
            raise CanonicalWarehouseError(
                "stored security_count does not match the on-disk manifest",
                context={"version_id": str(stored.version_id)},
            )
        if stored.row_count != credentials.row_count:
            raise CanonicalWarehouseError(
                "stored row_count does not match the on-disk manifest",
                context={"version_id": str(stored.version_id)},
            )
        if stored.coverage_start != credentials.coverage_start:
            raise CanonicalWarehouseError(
                "stored coverage_start does not match the on-disk manifest",
                context={"version_id": str(stored.version_id)},
            )
        if stored.coverage_end != credentials.coverage_end:
            raise CanonicalWarehouseError(
                "stored coverage_end does not match the on-disk manifest",
                context={"version_id": str(stored.version_id)},
            )
        if dict(stored.coverage_summary) != credentials.coverage_summary:
            raise CanonicalWarehouseError(
                "stored coverage_summary does not exactly match the on-disk manifest",
                context={"version_id": str(stored.version_id)},
            )


__all__ = [
    "CanonicalPublishCoordinator",
    "CanonicalPublishCoordinatorRequest",
    "CanonicalPublishResult",
]
