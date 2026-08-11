"""DifferenceReport strict verifier for S1-04-R3.

Reads a DifferenceReport artifact directory and verifies:
* manifest.json exists and has valid embedded manifest_sha256
* COMPLETE marker exists and binds to manifest
* row_differences.json exists and matches manifest
* All file references in manifest exist on disk
* Provides scope_matches() to validate override approval scope
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

from quantking.common.ids import EntityId, parse_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.warehouse.manifest import (
    WAREHOUSE_COMPLETE_FILENAME,
    WAREHOUSE_MANIFEST_FILENAME,
    compute_sha256,
)
from quantking.data.comparison.comparator import RowClassification
from quantking.data.comparison.thresholds import THRESHOLD_RULE_VERSION
from quantking.data.models import EmptyResultInfo, LegalEmptyClassification
from quantking.data.schemas import canonical_code_pattern, descriptor_for

_HEX64 = re.compile(r"^[0-9a-f]{64}\Z")
_ROW_DIFFERENCES_FILENAME = "row_differences.json"
_MANIFEST_SCHEMA_VERSION = "difference-report-manifest.v1"
_K2_MANIFEST_SCHEMA_VERSION = "difference-report-manifest.k2.v2"
_K2_BINDING_MODE = "SHARED_RESOLUTION_CANDIDATE"


class DifferenceReportVerificationError(Exception):
    """Raised when a DifferenceReport artifact fails structural verification."""


@dataclass(frozen=True)
class VerifiedDifferenceUnit:
    primary_key: dict[str, str]
    field: str
    classification: str


@dataclass(frozen=True)
class VerifiedDifferenceReport:
    """A verified DifferenceReport with validated structure and content."""

    report_id: EntityId
    created_at: UtcTimestamp
    dataset: str
    primary_source: str
    fallback_source: str
    report_relative_path: str
    manifest_sha256: str
    schema_version: str
    primary_version_id: EntityId
    fallback_version_id: EntityId
    primary_batch_id: EntityId
    fallback_batch_id: EntityId
    primary_content_sha256: str
    fallback_content_sha256: str
    primary_quality_run_id: EntityId
    fallback_quality_run_id: EntityId
    sample_spec_sha256: str
    threshold_rule_version: str
    report_sha256: str
    total_compared: int
    total_differences: int
    field_differences: dict[str, str]
    row_differences: list[dict[str, Any]]
    units: tuple[VerifiedDifferenceUnit, ...]
    fallback_only_rows: frozenset[tuple[tuple[str, str], ...]]
    manifest_schema_version: str = _MANIFEST_SCHEMA_VERSION
    binding_mode: str | None = None
    comparison_primary_keys: tuple[tuple[str, ...], ...] | None = None
    primary_empty_result: dict[str, str] | None = None
    fallback_empty_result: dict[str, str] | None = None

    def scope_matches(
        self, *, codes: frozenset[str], dates: frozenset[str], fields: frozenset[str]
    ) -> bool:
        """Return True if all (code, date, field) tuples exist in row_differences."""
        actual = {
            (
                str(diff["primary_key"]["code"]),
                str(
                    diff["primary_key"].get("trade_date", diff["primary_key"].get("effective_from"))
                ),
                diff["field"],
            )
            for diff in self.row_differences
            if diff["classification"] == RowClassification.BOTH and diff["field"]
        }
        requested = {
            (code, date_str, field) for code in codes for date_str in dates for field in fields
        }
        return requested.issubset(actual)


class DifferenceReportVerifier:
    """Read and verify DifferenceReport artifact directories."""

    def __init__(self, *, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def verify(self, *, report_id: EntityId) -> VerifiedDifferenceReport:
        """Read and verify a DifferenceReport artifact directory.

        Raises DifferenceReportVerificationError if any structural
        constraint fails.
        """
        if not isinstance(report_id, EntityId) or report_id.namespace != "data_diff":
            raise ValueError("report_id must use the data_diff namespace")

        directory = self._directory_for(report_id)
        if not directory.is_dir():
            raise DifferenceReportVerificationError(
                f"DifferenceReport directory does not exist: {directory}"
            )

        # Verify manifest.json exists and has embedded manifest_sha256.
        manifest_path = directory / WAREHOUSE_MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise DifferenceReportVerificationError("manifest.json is missing")

        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DifferenceReportVerificationError("manifest.json is not valid JSON") from error

        if not isinstance(manifest, dict):
            raise DifferenceReportVerificationError("manifest.json must be a JSON object")

        embedded_sha = manifest.get("manifest_sha256")
        if not isinstance(embedded_sha, str) or len(embedded_sha) != 64:
            raise DifferenceReportVerificationError("manifest_sha256 is missing or invalid")

        # Recompute manifest SHA without the embedded hash and verify.
        manifest_without_sha = dict(manifest)
        manifest_without_sha.pop("manifest_sha256", None)
        canonical = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        canonical = json.dumps(
            manifest_without_sha, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        computed_sha = compute_sha256(canonical.encode("utf-8"))
        if computed_sha != embedded_sha:
            raise DifferenceReportVerificationError(
                f"manifest_sha256 mismatch: embedded {embedded_sha}, computed {computed_sha}"
            )

        # Verify COMPLETE marker exists and binds to manifest.
        complete_path = directory / WAREHOUSE_COMPLETE_FILENAME
        if not complete_path.is_file():
            raise DifferenceReportVerificationError("COMPLETE marker is missing")

        try:
            complete_bytes = complete_path.read_bytes()
            complete = json.loads(complete_bytes.decode("utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DifferenceReportVerificationError("COMPLETE marker is not valid JSON") from error

        if not isinstance(complete, dict):
            raise DifferenceReportVerificationError("COMPLETE marker must be a JSON object")

        complete_manifest_sha = complete.get("manifest_sha256")
        if complete_manifest_sha != embedded_sha:
            raise DifferenceReportVerificationError(
                f"COMPLETE marker does not bind to manifest: "
                f"complete={complete_manifest_sha}, manifest={embedded_sha}"
            )

        # Verify row_differences.json exists.
        row_differences_path = directory / _ROW_DIFFERENCES_FILENAME
        if not row_differences_path.is_file():
            raise DifferenceReportVerificationError("row_differences.json is missing")

        canonical_bytes = json.dumps(
            manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        if canonical_bytes != manifest_bytes:
            raise DifferenceReportVerificationError(
                "manifest bytes are not in canonical deterministic form"
            )

        actual_files = {
            item.relative_to(directory).as_posix()
            for item in directory.rglob("*")
            if item.is_file()
        }
        expected_files = {
            WAREHOUSE_MANIFEST_FILENAME,
            WAREHOUSE_COMPLETE_FILENAME,
            _ROW_DIFFERENCES_FILENAME,
        }
        if actual_files != expected_files:
            raise DifferenceReportVerificationError("artifact file set does not match manifest")

        try:
            row_differences_bytes = row_differences_path.read_bytes()
            row_differences_payload = json.loads(row_differences_bytes.decode("utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DifferenceReportVerificationError(
                "row_differences.json is not valid JSON"
            ) from error

        if not isinstance(row_differences_payload, dict):
            raise DifferenceReportVerificationError("row_differences.json must be a JSON object")

        row_differences = row_differences_payload.get("rows", [])
        if not isinstance(row_differences, list):
            raise DifferenceReportVerificationError(
                "row_differences.json 'rows' field must be a JSON array"
            )

        self._verify_manifest(
            manifest,
            report_id=report_id,
            rows=row_differences,
            rows_bytes=row_differences_bytes,
        )
        units = self._verify_rows(manifest, row_differences)
        fallback_only = frozenset(
            tuple(sorted((key, str(value)) for key, value in unit.primary_key.items()))
            for unit in units
            if unit.classification == RowClassification.FALLBACK_ONLY
        )

        return VerifiedDifferenceReport(
            report_id=report_id,
            created_at=UtcTimestamp.from_iso(str(manifest["created_at"])),
            dataset=str(manifest["dataset"]),
            primary_source=str(manifest["primary_source"]),
            fallback_source=str(manifest["fallback_source"]),
            report_relative_path=directory.relative_to(self._artifacts_root).as_posix(),
            manifest_sha256=embedded_sha,
            schema_version=str(manifest["schema_version"]),
            primary_version_id=parse_entity_id(str(manifest["primary_version_id"])),
            fallback_version_id=parse_entity_id(str(manifest["fallback_version_id"])),
            primary_batch_id=parse_entity_id(str(manifest["primary_batch_id"])),
            fallback_batch_id=parse_entity_id(str(manifest["fallback_batch_id"])),
            primary_content_sha256=str(manifest["primary_content_sha256"]),
            fallback_content_sha256=str(manifest["fallback_content_sha256"]),
            primary_quality_run_id=parse_entity_id(str(manifest["primary_quality_run_id"])),
            fallback_quality_run_id=parse_entity_id(str(manifest["fallback_quality_run_id"])),
            sample_spec_sha256=str(manifest["sample_spec_sha256"]),
            threshold_rule_version=str(manifest["threshold_rule_version"]),
            report_sha256=str(manifest["report_sha256"]),
            total_compared=int(manifest["total_compared"]),
            total_differences=int(manifest["total_differences"]),
            field_differences={str(k): str(v) for k, v in manifest["field_differences"].items()},
            row_differences=row_differences,
            units=tuple(units),
            fallback_only_rows=fallback_only,
            manifest_schema_version=str(manifest["manifest_schema_version"]),
            binding_mode=(
                None if manifest.get("binding_mode") is None else str(manifest["binding_mode"])
            ),
            comparison_primary_keys=(
                None
                if manifest.get("comparison_primary_keys") is None
                else tuple(
                    tuple(str(value) for value in key)
                    for key in manifest["comparison_primary_keys"]
                )
            ),
            primary_empty_result=_empty_result_payload(manifest.get("primary_empty_result")),
            fallback_empty_result=_empty_result_payload(manifest.get("fallback_empty_result")),
        )

    def _verify_manifest(
        self,
        manifest: dict[str, object],
        *,
        report_id: EntityId,
        rows: list[object],
        rows_bytes: bytes,
    ) -> None:
        required = {
            "created_at",
            "dataset",
            "fallback_batch_id",
            "fallback_content_sha256",
            "fallback_quality_run_id",
            "fallback_source",
            "fallback_version_id",
            "field_differences",
            "manifest_schema_version",
            "manifest_sha256",
            "primary_batch_id",
            "primary_content_sha256",
            "primary_quality_run_id",
            "primary_source",
            "primary_version_id",
            "report_id",
            "report_sha256",
            "row_differences_file",
            "row_differences_file_sha256",
            "row_differences_file_size",
            "sample_spec",
            "sample_spec_sha256",
            "schema_version",
            "threshold_rule_version",
            "total_compared",
            "total_differences",
        }
        manifest_schema_version = manifest.get("manifest_schema_version")
        if manifest_schema_version == _K2_MANIFEST_SCHEMA_VERSION:
            required.update(
                {
                    "binding_mode",
                    "comparison_primary_keys",
                    "primary_empty_result",
                    "fallback_empty_result",
                }
            )
        if set(manifest) != required:
            raise DifferenceReportVerificationError("manifest fields do not match schema")
        if manifest_schema_version not in {
            _MANIFEST_SCHEMA_VERSION,
            _K2_MANIFEST_SCHEMA_VERSION,
        }:
            raise DifferenceReportVerificationError("manifest schema version is unsupported")
        if manifest_schema_version == _K2_MANIFEST_SCHEMA_VERSION and (
            manifest["binding_mode"] != _K2_BINDING_MODE
            or manifest["primary_version_id"] != manifest["fallback_version_id"]
            or manifest["dataset"] != "daily_bar_unadjusted"
            or manifest["threshold_rule_version"] != THRESHOLD_RULE_VERSION
        ):
            raise DifferenceReportVerificationError("K2 shared candidate binding is invalid")
        comparison_keys = manifest.get("comparison_primary_keys")
        if manifest_schema_version == _K2_MANIFEST_SCHEMA_VERSION and (
            not isinstance(comparison_keys, list)
            or any(
                not isinstance(key, list)
                or len(key) != 2
                or any(not isinstance(value, str) for value in key)
                for key in comparison_keys
            )
        ):
            raise DifferenceReportVerificationError("K2 comparison scope is invalid")
        if manifest_schema_version == _K2_MANIFEST_SCHEMA_VERSION:
            comparison_keys = cast(list[list[str]], comparison_keys)
            normalized_scope: list[tuple[str, str]] = []
            for key in comparison_keys:
                symbol, trade_date_value = key
                try:
                    parsed_date = date.fromisoformat(trade_date_value)
                except ValueError as error:
                    raise DifferenceReportVerificationError(
                        "K2 comparison scope is invalid"
                    ) from error
                if (
                    canonical_code_pattern().fullmatch(symbol) is None
                    or parsed_date.isoformat() != trade_date_value
                ):
                    raise DifferenceReportVerificationError("K2 comparison scope is invalid")
                normalized_scope.append((symbol, trade_date_value))
            if normalized_scope != sorted(set(normalized_scope)):
                raise DifferenceReportVerificationError(
                    "K2 comparison scope must be sorted and unique"
                )
            _empty_result_payload(manifest["primary_empty_result"])
            _empty_result_payload(manifest["fallback_empty_result"])
        if (
            manifest_schema_version == _MANIFEST_SCHEMA_VERSION
            and manifest["primary_version_id"] == manifest["fallback_version_id"]
        ):
            raise DifferenceReportVerificationError("legacy DifferenceReport versions must differ")
        if manifest["report_id"] != str(report_id):
            raise DifferenceReportVerificationError("manifest report_id does not match request")
        if manifest["row_differences_file"] != _ROW_DIFFERENCES_FILENAME:
            raise DifferenceReportVerificationError("row evidence file name is invalid")
        if manifest["row_differences_file_sha256"] != compute_sha256(rows_bytes):
            raise DifferenceReportVerificationError("row evidence SHA-256 mismatch")
        if manifest["row_differences_file_size"] != len(rows_bytes):
            raise DifferenceReportVerificationError("row evidence size mismatch")
        if manifest["total_differences"] != len(rows):
            raise DifferenceReportVerificationError("total_differences does not match row evidence")
        for field in (
            "primary_content_sha256",
            "fallback_content_sha256",
            "sample_spec_sha256",
            "report_sha256",
        ):
            value = manifest[field]
            if not isinstance(value, str) or not _HEX64.fullmatch(value):
                raise DifferenceReportVerificationError(f"{field} is not a SHA-256")
        sample_spec = manifest["sample_spec"]
        if not isinstance(sample_spec, list) or any(
            not isinstance(item, str) for item in sample_spec
        ):
            raise DifferenceReportVerificationError("sample_spec is invalid")
        sample_spec_sha = compute_sha256(
            json.dumps(
                sample_spec, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        )
        if sample_spec_sha != manifest["sample_spec_sha256"]:
            raise DifferenceReportVerificationError("sample_spec_sha256 mismatch")
        field_differences = manifest["field_differences"]
        if not isinstance(field_differences, dict):
            raise DifferenceReportVerificationError("field_differences is invalid")
        recomputed_field_counts: dict[str, str] = {}
        for row in rows:
            if isinstance(row, dict) and row.get("classification") == RowClassification.BOTH:
                row_field = row.get("field")
                if isinstance(row_field, str) and row_field:
                    current = int(recomputed_field_counts.get(row_field, "0")) + 1
                    recomputed_field_counts[row_field] = str(current)
        declared_field_counts = {str(k): str(v) for k, v in field_differences.items()}
        if declared_field_counts != dict(sorted(recomputed_field_counts.items())):
            raise DifferenceReportVerificationError("field_differences summary mismatch")
        report_sha = _compute_report_sha_from_manifest(manifest, rows)
        if report_sha != manifest["report_sha256"]:
            raise DifferenceReportVerificationError("report_sha256 mismatch")

    def _verify_rows(
        self, manifest: dict[str, object], rows: list[object]
    ) -> list[VerifiedDifferenceUnit]:
        descriptor = descriptor_for(str(manifest["dataset"]))
        allowed_fields = set(descriptor.field_names) - set(descriptor.primary_key)
        required = {
            "absolute_difference",
            "classification",
            "fallback_value",
            "field",
            "primary_key",
            "primary_value",
            "relative_difference",
            "threshold",
        }
        previous_sort: tuple[tuple[str, ...], str] | None = None
        units: list[VerifiedDifferenceUnit] = []
        comparison_scope = None
        if manifest.get("manifest_schema_version") == _K2_MANIFEST_SCHEMA_VERSION:
            comparison_keys = cast(list[list[str]], manifest["comparison_primary_keys"])
            comparison_scope = {(item[0], item[1]) for item in comparison_keys}
        for row in rows:
            if not isinstance(row, dict) or set(row) != required:
                raise DifferenceReportVerificationError("row evidence structure is invalid")
            classification = row["classification"]
            if classification not in {
                RowClassification.BOTH,
                RowClassification.PRIMARY_ONLY,
                RowClassification.FALLBACK_ONLY,
            }:
                raise DifferenceReportVerificationError("row evidence classification is invalid")
            primary_key = row["primary_key"]
            if not isinstance(primary_key, dict) or set(primary_key) != set(descriptor.primary_key):
                raise DifferenceReportVerificationError("row evidence primary key is invalid")
            pk = {key: str(primary_key[key]) for key in descriptor.primary_key}
            if (
                comparison_scope is not None
                and (
                    pk["code"],
                    pk["trade_date"],
                )
                not in comparison_scope
            ):
                raise DifferenceReportVerificationError(
                    "row evidence is outside the K2 comparison scope"
                )
            field = row["field"]
            if not isinstance(field, str):
                raise DifferenceReportVerificationError("row evidence field is invalid")
            if classification == RowClassification.BOTH:
                if not field or field not in allowed_fields:
                    raise DifferenceReportVerificationError("row evidence field is invalid")
            elif field:
                raise DifferenceReportVerificationError("row-level difference must not carry field")
            sort_key = (tuple(pk[name] for name in descriptor.primary_key), field)
            if previous_sort is not None and sort_key < previous_sort:
                raise DifferenceReportVerificationError("row evidence is not in stable order")
            previous_sort = sort_key
            units.append(
                VerifiedDifferenceUnit(
                    primary_key=pk,
                    field=field,
                    classification=str(classification),
                )
            )
        return units

    def _directory_for(self, report_id: EntityId) -> Path:
        """Return the artifact directory path for a report_id."""
        sanitized = str(report_id).replace(":", "--")
        return self._artifacts_root / "data-diff" / sanitized


__all__ = [
    "DifferenceReportVerificationError",
    "DifferenceReportVerifier",
    "VerifiedDifferenceReport",
    "VerifiedDifferenceUnit",
]


def _compute_report_sha_from_manifest(manifest: dict[str, object], rows: list[object]) -> str:
    request = {
        "dataset": manifest["dataset"],
        "schema_version": manifest["schema_version"],
        "primary_source": manifest["primary_source"],
        "fallback_source": manifest["fallback_source"],
        "primary_content_sha256": manifest["primary_content_sha256"],
        "fallback_content_sha256": manifest["fallback_content_sha256"],
        "threshold_rule_version": manifest["threshold_rule_version"],
        "sample_spec": manifest["sample_spec"],
    }
    if manifest.get("manifest_schema_version") == _K2_MANIFEST_SCHEMA_VERSION:
        request["comparison_primary_keys"] = manifest["comparison_primary_keys"]
        request["primary_empty_result"] = manifest["primary_empty_result"]
        request["fallback_empty_result"] = manifest["fallback_empty_result"]
    payload = {
        "request": request,
        "total_compared": manifest["total_compared"],
        "total_differences": manifest["total_differences"],
        "field_differences": manifest["field_differences"],
        "row_differences": rows,
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _empty_result_payload(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"classification", "reason"}:
        raise DifferenceReportVerificationError("K2 empty result binding is invalid")
    classification = value["classification"]
    reason = value["reason"]
    if not isinstance(classification, str) or not isinstance(reason, str):
        raise DifferenceReportVerificationError("K2 empty result binding is invalid")
    try:
        empty = EmptyResultInfo(
            classification=LegalEmptyClassification(classification),
            reason=reason,
        )
    except (TypeError, ValueError) as error:
        raise DifferenceReportVerificationError("K2 empty result binding is invalid") from error
    if empty.classification is not LegalEmptyClassification.NO_TRADING_SESSION:
        raise DifferenceReportVerificationError("K2 empty result must be NO_TRADING_SESSION")
    return {"classification": classification, "reason": reason}
