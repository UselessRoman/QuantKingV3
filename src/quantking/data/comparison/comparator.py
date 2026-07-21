"""Provider-neutral cross-source comparator (S1-04B).

The comparator aligns two Canonical tables by primary key and produces
a deterministic, immutable :class:`CrossSourceComparisonOutcome` with
field-level thresholds. The outcome is later persisted by the
:class:`DifferenceReportArtifactWriter` and the SQLite repository.

Rules:

* Both tables must share the same Canonical ``DatasetKind`` and
  ``schema_version``.
* Rows are aligned by the canonical primary key.
* Both-side rows: per-field exact comparison for strings/dates/booleans;
  per-field threshold for numeric fields.
* Primary-only and fallback-only rows are recorded as such.
* Both-null is equal; one-null is a difference.
* Output is sorted by (primary_key, field).
* Input tables are never mutated.
* ``report_id`` is derived deterministically from the request inputs.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any

import pyarrow as pa

from quantking.common.ids import EntityId
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.canonical_schema import canonical_schema, validate_table
from quantking.data.comparison.thresholds import ThresholdSet, ThresholdSpec
from quantking.data.models import DatasetKind, DataSource
from quantking.data.schemas import descriptor_for

# ---------------------------------------------------------------------------
# Row-level difference classification
# ---------------------------------------------------------------------------


class RowClassification:
    """Stable classification strings for row-level differences."""

    BOTH = "BOTH"
    PRIMARY_ONLY = "PRIMARY_ONLY"
    FALLBACK_ONLY = "FALLBACK_ONLY"


@dataclass(frozen=True, slots=True)
class RowDifference:
    """One row-level difference record.

    The ``primary_key`` is a mapping of primary-key field name to value,
    suitable for JSON serialization. ``classification`` is one of
    ``BOTH``, ``PRIMARY_ONLY`` or ``FALLBACK_ONLY``. For
    ``PRIMARY_ONLY`` / ``FALLBACK_ONLY`` rows, ``field`` is the empty
    string and the row signals a missing record on the other side.
    """

    primary_key: Mapping[str, Any]
    field: str
    classification: str
    primary_value: Any
    fallback_value: Any
    absolute_difference: float | None
    relative_difference: float | None
    threshold: ThresholdSpec | None

    def __post_init__(self) -> None:
        if not isinstance(self.primary_key, Mapping):
            raise TypeError("primary_key must be a mapping")
        if not isinstance(self.field, str):
            raise TypeError("field must be a string")
        if self.classification not in (
            RowClassification.BOTH,
            RowClassification.PRIMARY_ONLY,
            RowClassification.FALLBACK_ONLY,
        ):
            raise ValueError(f"invalid classification: {self.classification!r}")
        object.__setattr__(self, "primary_key", MappingProxyType(dict(self.primary_key)))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dict for the artifact manifest."""
        return {
            "primary_key": {k: _json_value(v) for k, v in self.primary_key.items()},
            "field": self.field,
            "classification": self.classification,
            "primary_value": _json_value(self.primary_value),
            "fallback_value": _json_value(self.fallback_value),
            "absolute_difference": self.absolute_difference,
            "relative_difference": self.relative_difference,
            "threshold": (
                {
                    "absolute": self.threshold.absolute,
                    "relative": self.threshold.relative,
                }
                if self.threshold is not None
                else None
            ),
        }


def _json_value(value: Any) -> Any:
    """Convert a Python value to a JSON-friendly representation."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _coerce_data_source(value: Any, *, field: str) -> DataSource:
    """Accept a DataSource enum or its string value."""
    if isinstance(value, DataSource):
        return value
    if isinstance(value, str):
        try:
            return DataSource(value)
        except ValueError as error:
            raise ValueError(f"{field} is not a valid DataSource value: {value!r}") from error
    raise TypeError(f"{field} must be a DataSource or its string value")


# ---------------------------------------------------------------------------
# Request and outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrossSourceComparisonRequest:
    """One cross-source comparison request.

    Both sides must have been canonicalized to the same ``DatasetKind``
    and ``schema_version``, and both must have passed their respective
    single-source quality rules. The request carries the explicit
    batch IDs, version IDs, content SHAs and quality-run IDs so the
    resulting :class:`CrossSourceComparisonOutcome` can be bound to a
    :class:`DifferenceReport` with full lineage.
    """

    dataset: DatasetKind
    schema_version: str
    primary_source: DataSource
    fallback_source: DataSource
    primary_version_id: EntityId
    fallback_version_id: EntityId
    primary_batch_id: EntityId
    fallback_batch_id: EntityId
    primary_content_sha256: str
    fallback_content_sha256: str
    primary_quality_run_id: EntityId
    fallback_quality_run_id: EntityId
    primary_table: pa.Table
    fallback_table: pa.Table
    thresholds: ThresholdSet
    sample_spec: tuple[str, ...]
    created_at: UtcTimestamp

    def __post_init__(self) -> None:
        if not isinstance(self.dataset, DatasetKind):
            raise TypeError("dataset must be a DatasetKind")
        if not isinstance(self.schema_version, str) or not self.schema_version.strip():
            raise ValueError("schema_version must be non-empty text")
        # Accept DataSource enum or its string value (e.g. "XTQUANT").
        primary_source = _coerce_data_source(self.primary_source, field="primary_source")
        fallback_source = _coerce_data_source(self.fallback_source, field="fallback_source")
        object.__setattr__(self, "primary_source", primary_source)
        object.__setattr__(self, "fallback_source", fallback_source)
        if primary_source == fallback_source:
            raise ValueError("primary and fallback sources must differ")
        for name, value in (
            ("primary_version_id", self.primary_version_id),
            ("fallback_version_id", self.fallback_version_id),
        ):
            if not isinstance(value, EntityId) or value.namespace != "data_version":
                raise ValueError(f"{name} must use the data_version namespace")
        for name, value in (
            ("primary_batch_id", self.primary_batch_id),
            ("fallback_batch_id", self.fallback_batch_id),
        ):
            if not isinstance(value, EntityId) or value.namespace != "data_batch":
                raise ValueError(f"{name} must use the data_batch namespace")
        for name, value in (
            ("primary_quality_run_id", self.primary_quality_run_id),
            ("fallback_quality_run_id", self.fallback_quality_run_id),
        ):
            if not isinstance(value, EntityId) or value.namespace != "data_quality":
                raise ValueError(f"{name} must use the data_quality namespace")
        if not isinstance(self.primary_table, pa.Table):
            raise TypeError("primary_table must be a pa.Table")
        if not isinstance(self.fallback_table, pa.Table):
            raise TypeError("fallback_table must be a pa.Table")
        if not isinstance(self.thresholds, ThresholdSet):
            raise TypeError("thresholds must be a ThresholdSet")
        if not isinstance(self.sample_spec, tuple):
            object.__setattr__(self, "sample_spec", tuple(self.sample_spec))
        if not isinstance(self.created_at, UtcTimestamp):
            raise TypeError("created_at must be a UtcTimestamp")

    def to_deterministic_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict that uniquely identifies the request.

        The dict is sorted by key and used to derive the deterministic
        ``report_id``. The actual table content is represented by its
        content SHA-256, not by the table bytes, so the dict is small
        and stable. Random metadata IDs (version_id, batch_id,
        quality_run_id) are deliberately excluded so two comparisons
        with identical content produce the same ``report_id``;
        ``created_at`` is also excluded for the same reason. The
        random IDs are still persisted on the resulting
        :class:`DifferenceReport` for full lineage binding.
        """
        return {
            "dataset": self.dataset.value,
            "schema_version": self.schema_version,
            "primary_source": self.primary_source.value,
            "fallback_source": self.fallback_source.value,
            "primary_content_sha256": self.primary_content_sha256,
            "fallback_content_sha256": self.fallback_content_sha256,
            "threshold_rule_version": self.thresholds.rule_version,
            "sample_spec": list(self.sample_spec),
        }


@dataclass(frozen=True, slots=True)
class CrossSourceComparisonOutcome:
    """The full outcome of one cross-source comparison.

    The ``report_id`` is deterministically derived from the request.
    ``row_differences`` is sorted by (primary_key, field) for stable
    output. The outcome is later persisted by the artifact writer and
    the SQLite repository as a :class:`DifferenceReport`.
    """

    report_id: EntityId
    dataset: DatasetKind
    schema_version: str
    primary_source: DataSource
    fallback_source: DataSource
    primary_version_id: EntityId
    fallback_version_id: EntityId
    primary_batch_id: EntityId
    fallback_batch_id: EntityId
    primary_content_sha256: str
    fallback_content_sha256: str
    primary_quality_run_id: EntityId
    fallback_quality_run_id: EntityId
    sample_spec: tuple[str, ...]
    threshold_rule_version: str
    created_at: UtcTimestamp
    total_compared: int
    total_differences: int
    field_differences: Mapping[str, str]
    row_differences: tuple[RowDifference, ...]
    report_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.report_id, EntityId) or self.report_id.namespace != "data_diff":
            raise ValueError("report_id must use the data_diff namespace")
        if not isinstance(self.dataset, DatasetKind):
            raise TypeError("dataset must be a DatasetKind")
        if not isinstance(self.primary_source, DataSource):
            raise TypeError("primary_source must be a DataSource")
        if not isinstance(self.fallback_source, DataSource):
            raise TypeError("fallback_source must be a DataSource")
        if (
            not isinstance(self.threshold_rule_version, str)
            or not self.threshold_rule_version.strip()
        ):
            raise ValueError("threshold_rule_version must be non-empty text")
        if (
            not isinstance(self.total_compared, int)
            or isinstance(self.total_compared, bool)
            or self.total_compared < 0
        ):
            raise ValueError("total_compared must be a non-negative integer")
        if (
            not isinstance(self.total_differences, int)
            or isinstance(self.total_differences, bool)
            or self.total_differences < 0
        ):
            raise ValueError("total_differences must be a non-negative integer")
        if not isinstance(self.field_differences, Mapping):
            raise TypeError("field_differences must be a mapping")
        if not isinstance(self.row_differences, tuple):
            object.__setattr__(self, "row_differences", tuple(self.row_differences))
        if not isinstance(self.report_sha256, str) or len(self.report_sha256) != 64:
            raise ValueError("report_sha256 must be a 64-character hex string")
        sorted_field_differences = MappingProxyType(dict(sorted(self.field_differences.items())))
        object.__setattr__(self, "field_differences", sorted_field_differences)
        object.__setattr__(self, "sample_spec", tuple(self.sample_spec))


# ---------------------------------------------------------------------------
# Comparator
# ---------------------------------------------------------------------------


class CrossSourceComparator:
    """Align two Canonical tables by primary key and produce a DifferenceReport."""

    def compare(self, *, request: CrossSourceComparisonRequest) -> CrossSourceComparisonOutcome:
        """Execute the comparison and return a deterministic outcome.

        Recomputes content SHA-256 for both tables and verifies they match
        the claimed request.primary_content_sha256 and request.fallback_content_sha256.
        Raises ValueError if declared content SHAs do not match actual content.
        """
        if not isinstance(request, CrossSourceComparisonRequest):
            raise TypeError("request must be a CrossSourceComparisonRequest")
        descriptor = descriptor_for(request.dataset.value)
        if descriptor.schema_version != request.schema_version:
            raise ValueError("request schema_version does not match the canonical descriptor")
        # Validate that both tables conform to the canonical schema.
        canonical = canonical_schema(request.dataset.value)
        canonical_tables = (
            ("primary_table", request.primary_table),
            ("fallback_table", request.fallback_table),
        )
        for table_name, table in canonical_tables:
            validation = validate_table(request.dataset.value, table)
            if not validation.ok:
                issue = validation.issues[0]
                raise ValueError(f"{table_name} is not canonical: {issue.rule}: {issue.message}")

        # Recompute content SHA-256 and verify against request claims.
        from quantking.data.content import canonical_content_sha256

        actual_primary_sha = canonical_content_sha256(request.dataset, [request.primary_table])
        if actual_primary_sha != request.primary_content_sha256:
            raise ValueError(
                f"primary_content_sha256 mismatch: "
                f"claimed {request.primary_content_sha256}, actual {actual_primary_sha}"
            )

        actual_fallback_sha = canonical_content_sha256(request.dataset, [request.fallback_table])
        if actual_fallback_sha != request.fallback_content_sha256:
            raise ValueError(
                f"fallback_content_sha256 mismatch: "
                f"claimed {request.fallback_content_sha256}, actual {actual_fallback_sha}"
            )

        primary_rows = request.primary_table.to_pylist()
        fallback_rows = request.fallback_table.to_pylist()
        primary_by_key = _index_rows(primary_rows, descriptor.primary_key, "primary")
        fallback_by_key = _index_rows(fallback_rows, descriptor.primary_key, "fallback")

        all_keys = sorted(primary_by_key.keys() | fallback_by_key.keys())
        thresholds = request.thresholds.for_dataset(request.dataset)
        # Build a set of non-primary-key field names for BOTH-side comparison.
        non_pk_fields = [f.name for f in canonical if f.name not in descriptor.primary_key]

        row_differences: list[RowDifference] = []
        field_diff_counts: dict[str, int] = {}
        total_compared = 0

        for key in all_keys:
            primary_row = primary_by_key.get(key)
            fallback_row = fallback_by_key.get(key)
            key_dict = _key_dict(key, descriptor.primary_key)
            if primary_row is not None and fallback_row is not None:
                total_compared += 1
                for field_name in non_pk_fields:
                    diff = _compare_field(
                        field_name=field_name,
                        primary_row=primary_row,
                        fallback_row=fallback_row,
                        thresholds=thresholds,
                        key_dict=key_dict,
                    )
                    if diff is not None:
                        row_differences.append(diff)
                        field_diff_counts[field_name] = field_diff_counts.get(field_name, 0) + 1
            elif primary_row is not None:
                # Primary-only row: record as PRIMARY_ONLY with empty field.
                row_differences.append(
                    RowDifference(
                        primary_key=key_dict,
                        field="",
                        classification=RowClassification.PRIMARY_ONLY,
                        primary_value=None,
                        fallback_value=None,
                        absolute_difference=None,
                        relative_difference=None,
                        threshold=None,
                    )
                )
            else:
                # Fallback-only row.
                row_differences.append(
                    RowDifference(
                        primary_key=key_dict,
                        field="",
                        classification=RowClassification.FALLBACK_ONLY,
                        primary_value=None,
                        fallback_value=None,
                        absolute_difference=None,
                        relative_difference=None,
                        threshold=None,
                    )
                )

        # Stable sort by (primary_key tuple, field).
        row_differences.sort(
            key=lambda d: (_sort_key(d.primary_key, descriptor.primary_key), d.field)
        )
        field_differences_str = {k: str(v) for k, v in field_diff_counts.items()}
        report_id = _derive_report_id(request)
        report_sha = _compute_report_sha256(
            request=request,
            total_compared=total_compared,
            total_differences=len(row_differences),
            field_differences=field_differences_str,
            row_differences=row_differences,
        )
        return CrossSourceComparisonOutcome(
            report_id=report_id,
            dataset=request.dataset,
            schema_version=request.schema_version,
            primary_source=request.primary_source,
            fallback_source=request.fallback_source,
            primary_version_id=request.primary_version_id,
            fallback_version_id=request.fallback_version_id,
            primary_batch_id=request.primary_batch_id,
            fallback_batch_id=request.fallback_batch_id,
            primary_content_sha256=request.primary_content_sha256,
            fallback_content_sha256=request.fallback_content_sha256,
            primary_quality_run_id=request.primary_quality_run_id,
            fallback_quality_run_id=request.fallback_quality_run_id,
            sample_spec=request.sample_spec,
            threshold_rule_version=request.thresholds.rule_version,
            created_at=request.created_at,
            total_compared=total_compared,
            total_differences=len(row_differences),
            field_differences=field_differences_str,
            row_differences=tuple(row_differences),
            report_sha256=report_sha,
        )


def _row_key(row: dict[str, Any], primary_key: tuple[str, ...]) -> tuple[Any, ...]:
    values = tuple(row.get(name) for name in primary_key)
    if any(value is None for value in values):
        raise ValueError("primary key values must not be null")
    return values


def _index_rows(
    rows: list[dict[str, Any]], primary_key: tuple[str, ...], side: str
) -> dict[tuple[Any, ...], dict[str, Any]]:
    indexed: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = _row_key(row, primary_key)
        if key in indexed:
            raise ValueError(f"{side} table contains duplicate primary key {key!r}")
        indexed[key] = row
    return indexed


def _key_dict(key: tuple[Any, ...], primary_key: tuple[str, ...]) -> dict[str, Any]:
    return {name: value for name, value in zip(primary_key, key, strict=True)}


def _sort_key(
    primary_key: Mapping[str, Any], primary_key_fields: tuple[str, ...]
) -> tuple[Any, ...]:
    return tuple(_sortable(primary_key.get(name)) for name in primary_key_fields)


def _sortable(value: Any) -> Any:
    """Convert a value to a sortable representation for sorting."""
    if value is None:
        return (0, "")
    if isinstance(value, date):
        return (1, value.isoformat())
    if isinstance(value, (int, float)):
        return (2, float(value))
    return (3, str(value))


def _compare_field(
    *,
    field_name: str,
    primary_row: dict[str, Any],
    fallback_row: dict[str, Any],
    thresholds: Mapping[str, ThresholdSpec],
    key_dict: Mapping[str, Any],
) -> RowDifference | None:
    """Return a RowDifference if the two values differ, else None."""
    primary_value = primary_row.get(field_name)
    fallback_value = fallback_row.get(field_name)
    if primary_value is None and fallback_value is None:
        return None
    if primary_value is None or fallback_value is None:
        return RowDifference(
            primary_key=key_dict,
            field=field_name,
            classification=RowClassification.BOTH,
            primary_value=primary_value,
            fallback_value=fallback_value,
            absolute_difference=None,
            relative_difference=None,
            threshold=None,
        )
    # Both non-None.
    if isinstance(primary_value, bool) or isinstance(fallback_value, bool):
        if primary_value != fallback_value:
            return RowDifference(
                primary_key=key_dict,
                field=field_name,
                classification=RowClassification.BOTH,
                primary_value=primary_value,
                fallback_value=fallback_value,
                absolute_difference=None,
                relative_difference=None,
                threshold=None,
            )
        return None
    if isinstance(primary_value, (int, float)) and isinstance(fallback_value, (int, float)):
        spec = thresholds.get(field_name)
        if spec is not None:
            if not spec.exceeds(float(primary_value), float(fallback_value)):
                return None
            diff = abs(float(primary_value) - float(fallback_value))
            base = max(abs(float(primary_value)), abs(float(fallback_value)), 1e-12)
            return RowDifference(
                primary_key=key_dict,
                field=field_name,
                classification=RowClassification.BOTH,
                primary_value=primary_value,
                fallback_value=fallback_value,
                absolute_difference=diff,
                relative_difference=diff / base,
                threshold=spec,
            )
        # No threshold: exact equality.
        if float(primary_value) != float(fallback_value):
            return RowDifference(
                primary_key=key_dict,
                field=field_name,
                classification=RowClassification.BOTH,
                primary_value=primary_value,
                fallback_value=fallback_value,
                absolute_difference=abs(float(primary_value) - float(fallback_value)),
                relative_difference=None,
                threshold=None,
            )
        return None
    # Strings, dates, etc.: exact equality.
    if primary_value != fallback_value:
        return RowDifference(
            primary_key=key_dict,
            field=field_name,
            classification=RowClassification.BOTH,
            primary_value=primary_value,
            fallback_value=fallback_value,
            absolute_difference=None,
            relative_difference=None,
            threshold=None,
        )
    return None


def _derive_report_id(request: CrossSourceComparisonRequest) -> EntityId:
    """Derive a deterministic data_diff EntityId from the request."""
    payload = request.to_deterministic_dict()
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(serialized.encode("utf-8")).digest()
    # Convert to a UUID4 (deterministic) by setting version/variant bits.
    bytes16 = bytearray(digest[:16])
    bytes16[6] = (bytes16[6] & 0x0F) | 0x40  # version 4
    bytes16[8] = (bytes16[8] & 0x3F) | 0x80  # variant 1
    uuid4 = uuid.UUID(bytes=bytes(bytes16))
    return EntityId(namespace="data_diff", value=uuid4)


def _compute_report_sha256(
    *,
    request: CrossSourceComparisonRequest,
    total_compared: int,
    total_differences: int,
    field_differences: Mapping[str, str],
    row_differences: list[RowDifference],
) -> str:
    """Compute a deterministic SHA-256 of the row-level report content."""
    payload = {
        "request": request.to_deterministic_dict(),
        "total_compared": total_compared,
        "total_differences": total_differences,
        "field_differences": dict(field_differences),
        "row_differences": [d.to_dict() for d in row_differences],
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


__all__ = [
    "CrossSourceComparator",
    "CrossSourceComparisonOutcome",
    "CrossSourceComparisonRequest",
    "RowClassification",
    "RowDifference",
]
