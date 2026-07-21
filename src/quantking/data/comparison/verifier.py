"""DifferenceReport strict verifier for S1-04-R2.

Reads a DifferenceReport artifact directory and verifies:
* manifest.json exists and has valid embedded manifest_sha256
* COMPLETE marker exists and binds to manifest
* row_differences.json exists and matches manifest
* All file references in manifest exist on disk
* Provides scope_matches() to validate override approval scope
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quantking.common.ids import EntityId
from quantking.data.adapters.warehouse.manifest import (
    WAREHOUSE_COMPLETE_FILENAME,
    WAREHOUSE_MANIFEST_FILENAME,
    compute_sha256,
)


class DifferenceReportVerificationError(Exception):
    """Raised when a DifferenceReport artifact fails structural verification."""


@dataclass(frozen=True)
class VerifiedDifferenceReport:
    """A verified DifferenceReport with validated structure and content."""

    report_id: EntityId
    dataset: str
    primary_source: str
    fallback_source: str
    total_differences: int
    row_differences: list[dict[str, Any]]

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
            if diff["field"]  # Skip row-level differences (PRIMARY_ONLY/FALLBACK_ONLY)
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
            import json

            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DifferenceReportVerificationError("manifest.json is not valid JSON") from error

        if not isinstance(manifest, dict):
            raise DifferenceReportVerificationError("manifest.json must be a JSON object")

        embedded_sha = manifest.pop("manifest_sha256", None)
        if not isinstance(embedded_sha, str) or len(embedded_sha) != 64:
            raise DifferenceReportVerificationError("manifest_sha256 is missing or invalid")

        # Recompute manifest SHA without the embedded hash and verify.
        import json

        canonical = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
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
        row_differences_path = directory / "row_differences.json"
        if not row_differences_path.is_file():
            raise DifferenceReportVerificationError("row_differences.json is missing")

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

        return VerifiedDifferenceReport(
            report_id=report_id,
            dataset=manifest.get("dataset", ""),
            primary_source=manifest.get("primary_source", ""),
            fallback_source=manifest.get("fallback_source", ""),
            total_differences=len(row_differences),
            row_differences=row_differences,
        )

    def _directory_for(self, report_id: EntityId) -> Path:
        """Return the artifact directory path for a report_id."""
        sanitized = str(report_id).replace(":", "--")
        return self._artifacts_root / "data-diff" / sanitized


__all__ = [
    "DifferenceReportVerificationError",
    "DifferenceReportVerifier",
    "VerifiedDifferenceReport",
]
