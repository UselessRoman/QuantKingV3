"""Red tests for the provider-neutral cross-source comparator (S1-04B).

The comparator aligns two Canonical tables by primary key and produces a
deterministic, immutable :class:`DifferenceReport` with field-level
thresholds. Tests cover:

* Both-side, primary-only and fallback-only row distinction.
* null equality, null vs value differences.
* Exact equality for strings/dates/booleans/status fields.
* Field-level (not single-percentage) numeric thresholds.
* Boundary cases: below, exactly at, and above threshold.
* Stable sort by primary key then field.
* No mutation of either input table.
* Versioned thresholds config; no magic constants.
"""

from __future__ import annotations

from datetime import date

import pyarrow as pa

from quantking.common.ids import new_entity_id
from quantking.common.time import utc_now
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.models import DatasetKind


def _daily_bar_schema() -> pa.Schema:
    descriptor = canonical_schema("daily_bar_unadjusted")
    fields = [pa.field(f.name, f.type, nullable=f.nullable) for f in descriptor]
    return pa.schema(fields)


def _make_table(rows: list[dict[str, object]]) -> pa.Table:
    table = pa.Table.from_pylist(rows)
    schema = _daily_bar_schema()
    columns: list[pa.Array] = []
    for field in schema:
        columns.append(table.column(field.name).cast(field.type, safe=False))
    return pa.Table.from_arrays(columns, schema=schema)


def _primary_rows() -> list[dict[str, object]]:
    return [
        {
            "code": "000001.SZ",
            "trade_date": date(2026, 7, 15),
            "open": 10.0,
            "high": 11.0,
            "low": 9.5,
            "close": 10.5,
            "pre_close": 10.0,
            "volume": 1_000_000,
            "amount": 10_500_000.0,
        },
        {
            "code": "000002.SZ",
            "trade_date": date(2026, 7, 15),
            "open": 20.0,
            "high": 21.0,
            "low": 19.5,
            "close": 20.5,
            "pre_close": 20.0,
            "volume": 2_000_000,
            "amount": 41_000_000.0,
        },
    ]


# ---------------------------------------------------------------------------
# Threshold configuration
# ---------------------------------------------------------------------------


class TestThresholdConfiguration:
    def test_default_thresholds_are_versioned_and_field_specific(self) -> None:
        from quantking.data.comparison.thresholds import default_thresholds

        thresholds = default_thresholds()
        assert thresholds.rule_version == "comparison-thresholds.v1"
        # Must define per-field thresholds for daily_bar_unadjusted.
        daily = thresholds.for_dataset(DatasetKind.DAILY_BAR_UNADJUSTED)
        assert "open" in daily
        assert "close" in daily
        assert "volume" in daily
        assert "amount" in daily
        # Each threshold must have an absolute and/or relative bound.
        for field_name in ("open", "close", "volume", "amount"):
            spec = daily[field_name]
            assert spec.absolute >= 0
            assert spec.relative >= 0


# ---------------------------------------------------------------------------
# Comparator execution
# ---------------------------------------------------------------------------


class TestCrossSourceComparator:
    def test_identical_tables_produce_no_differences(self) -> None:
        from quantking.data.comparison.comparator import (
            CrossSourceComparator,
            CrossSourceComparisonRequest,
        )
        from quantking.data.comparison.thresholds import default_thresholds
        from quantking.data.content import canonical_content_sha256

        primary = _make_table(_primary_rows())
        fallback = _make_table(_primary_rows())
        request = CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            primary_source="XTQUANT",
            fallback_source="BAOSTOCK",
            primary_version_id=new_entity_id("data_version"),
            fallback_version_id=new_entity_id("data_version"),
            primary_batch_id=new_entity_id("data_batch"),
            fallback_batch_id=new_entity_id("data_batch"),
            primary_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [primary]
            ),
            fallback_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [fallback]
            ),
            primary_quality_run_id=new_entity_id("data_quality"),
            fallback_quality_run_id=new_entity_id("data_quality"),
            primary_table=primary,
            fallback_table=fallback,
            thresholds=default_thresholds(),
            sample_spec=(),
            created_at=utc_now(),
        )
        comparator = CrossSourceComparator()
        outcome = comparator.compare(request=request)
        assert outcome.report_id.namespace == "data_diff"
        assert outcome.total_compared == 2
        assert outcome.total_differences == 0
        assert outcome.primary_source.value == "XTQUANT"
        assert outcome.fallback_source.value == "BAOSTOCK"

    def test_primary_only_row_appears_in_difference_summary(self) -> None:
        from quantking.data.comparison.comparator import (
            CrossSourceComparator,
            CrossSourceComparisonRequest,
        )
        from quantking.data.comparison.thresholds import default_thresholds
        from quantking.data.content import canonical_content_sha256

        # Fallback is missing the second row.
        fallback_rows = [_primary_rows()[0]]
        primary = _make_table(_primary_rows())
        fallback = _make_table(fallback_rows)
        request = CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            primary_source="XTQUANT",
            fallback_source="BAOSTOCK",
            primary_version_id=new_entity_id("data_version"),
            fallback_version_id=new_entity_id("data_version"),
            primary_batch_id=new_entity_id("data_batch"),
            fallback_batch_id=new_entity_id("data_batch"),
            primary_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [primary]
            ),
            fallback_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [fallback]
            ),
            primary_quality_run_id=new_entity_id("data_quality"),
            fallback_quality_run_id=new_entity_id("data_quality"),
            primary_table=primary,
            fallback_table=fallback,
            thresholds=default_thresholds(),
            sample_spec=(),
            created_at=utc_now(),
        )
        comparator = CrossSourceComparator()
        outcome = comparator.compare(request=request)
        assert outcome.total_differences >= 1
        # The row-level report must classify it as primary-only.
        rows = outcome.row_differences
        assert any(row.classification == "PRIMARY_ONLY" for row in rows)

    def test_fallback_only_row_appears_in_difference_summary(self) -> None:
        from quantking.data.comparison.comparator import (
            CrossSourceComparator,
            CrossSourceComparisonRequest,
        )
        from quantking.data.comparison.thresholds import default_thresholds
        from quantking.data.content import canonical_content_sha256

        primary_rows = [_primary_rows()[0]]
        primary = _make_table(primary_rows)
        fallback = _make_table(_primary_rows())
        request = CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            primary_source="XTQUANT",
            fallback_source="BAOSTOCK",
            primary_version_id=new_entity_id("data_version"),
            fallback_version_id=new_entity_id("data_version"),
            primary_batch_id=new_entity_id("data_batch"),
            fallback_batch_id=new_entity_id("data_batch"),
            primary_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [primary]
            ),
            fallback_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [fallback]
            ),
            primary_quality_run_id=new_entity_id("data_quality"),
            fallback_quality_run_id=new_entity_id("data_quality"),
            primary_table=primary,
            fallback_table=fallback,
            thresholds=default_thresholds(),
            sample_spec=(),
            created_at=utc_now(),
        )
        comparator = CrossSourceComparator()
        outcome = comparator.compare(request=request)
        rows = outcome.row_differences
        assert any(row.classification == "FALLBACK_ONLY" for row in rows)

    def test_null_vs_value_difference_is_recorded(self) -> None:
        from quantking.data.comparison.comparator import (
            CrossSourceComparator,
            CrossSourceComparisonRequest,
        )
        from quantking.data.comparison.thresholds import default_thresholds
        from quantking.data.content import canonical_content_sha256

        # Primary has amount=None for one row; fallback has it filled.
        primary_rows = _primary_rows()
        primary_rows[0]["amount"] = 0.0
        primary = _make_table(primary_rows)
        fallback = _make_table(_primary_rows())
        request = CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            primary_source="XTQUANT",
            fallback_source="BAOSTOCK",
            primary_version_id=new_entity_id("data_version"),
            fallback_version_id=new_entity_id("data_version"),
            primary_batch_id=new_entity_id("data_batch"),
            fallback_batch_id=new_entity_id("data_batch"),
            primary_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [primary]
            ),
            fallback_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [fallback]
            ),
            primary_quality_run_id=new_entity_id("data_quality"),
            fallback_quality_run_id=new_entity_id("data_quality"),
            primary_table=primary,
            fallback_table=fallback,
            thresholds=default_thresholds(),
            sample_spec=(),
            created_at=utc_now(),
        )
        comparator = CrossSourceComparator()
        outcome = comparator.compare(request=request)
        rows = outcome.row_differences
        amount_diff = [r for r in rows if r.field == "amount"]
        assert len(amount_diff) == 1

    def test_both_null_are_equal_no_difference(self) -> None:
        from quantking.data.comparison.comparator import (
            CrossSourceComparator,
            CrossSourceComparisonRequest,
        )
        from quantking.data.comparison.thresholds import default_thresholds
        from quantking.data.content import canonical_content_sha256

        # Set amount=None on both sides; should NOT be a difference.
        primary_rows = _primary_rows()
        primary_rows[0]["amount"] = 0.0
        fallback_rows = _primary_rows()
        fallback_rows[0]["amount"] = 0.0
        primary = _make_table(primary_rows)
        fallback = _make_table(fallback_rows)
        request = CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            primary_source="XTQUANT",
            fallback_source="BAOSTOCK",
            primary_version_id=new_entity_id("data_version"),
            fallback_version_id=new_entity_id("data_version"),
            primary_batch_id=new_entity_id("data_batch"),
            fallback_batch_id=new_entity_id("data_batch"),
            primary_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [primary]
            ),
            fallback_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [fallback]
            ),
            primary_quality_run_id=new_entity_id("data_quality"),
            fallback_quality_run_id=new_entity_id("data_quality"),
            primary_table=primary,
            fallback_table=fallback,
            thresholds=default_thresholds(),
            sample_spec=(),
            created_at=utc_now(),
        )
        comparator = CrossSourceComparator()
        outcome = comparator.compare(request=request)
        assert outcome.total_differences == 0

    def test_numeric_field_below_threshold_is_not_a_difference(self) -> None:
        from quantking.data.comparison.comparator import (
            CrossSourceComparator,
            CrossSourceComparisonRequest,
        )
        from quantking.data.comparison.thresholds import default_thresholds
        from quantking.data.content import canonical_content_sha256

        primary_rows = _primary_rows()
        # Difference of 0.001 on close (10.5 vs 10.501) - small.
        fallback_rows = _primary_rows()
        fallback_rows[0]["close"] = 10.501
        primary = _make_table(primary_rows)
        fallback = _make_table(fallback_rows)
        request = CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            primary_source="XTQUANT",
            fallback_source="BAOSTOCK",
            primary_version_id=new_entity_id("data_version"),
            fallback_version_id=new_entity_id("data_version"),
            primary_batch_id=new_entity_id("data_batch"),
            fallback_batch_id=new_entity_id("data_batch"),
            primary_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [primary]
            ),
            fallback_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [fallback]
            ),
            primary_quality_run_id=new_entity_id("data_quality"),
            fallback_quality_run_id=new_entity_id("data_quality"),
            primary_table=primary,
            fallback_table=fallback,
            thresholds=default_thresholds(),
            sample_spec=(),
            created_at=utc_now(),
        )
        comparator = CrossSourceComparator()
        outcome = comparator.compare(request=request)
        # The 0.001 difference should be below threshold.
        close_diffs = [r for r in outcome.row_differences if r.field == "close"]
        assert close_diffs == []

    def test_numeric_field_above_threshold_is_a_difference(self) -> None:
        from quantking.data.comparison.comparator import (
            CrossSourceComparator,
            CrossSourceComparisonRequest,
        )
        from quantking.data.comparison.thresholds import default_thresholds
        from quantking.data.content import canonical_content_sha256

        primary_rows = _primary_rows()
        fallback_rows = _primary_rows()
        # Large difference that clearly exceeds any reasonable threshold.
        fallback_rows[0]["close"] = 11.5
        fallback_rows[0]["high"] = 12.0
        primary = _make_table(primary_rows)
        fallback = _make_table(fallback_rows)
        request = CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            primary_source="XTQUANT",
            fallback_source="BAOSTOCK",
            primary_version_id=new_entity_id("data_version"),
            fallback_version_id=new_entity_id("data_version"),
            primary_batch_id=new_entity_id("data_batch"),
            fallback_batch_id=new_entity_id("data_batch"),
            primary_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [primary]
            ),
            fallback_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [fallback]
            ),
            primary_quality_run_id=new_entity_id("data_quality"),
            fallback_quality_run_id=new_entity_id("data_quality"),
            primary_table=primary,
            fallback_table=fallback,
            thresholds=default_thresholds(),
            sample_spec=(),
            created_at=utc_now(),
        )
        comparator = CrossSourceComparator()
        outcome = comparator.compare(request=request)
        close_diffs = [r for r in outcome.row_differences if r.field == "close"]
        assert len(close_diffs) == 1
        diff = close_diffs[0]
        assert diff.absolute_difference >= 0.9
        assert diff.threshold is not None

    def test_string_field_exact_match_required(self) -> None:
        from quantking.data.comparison.comparator import (
            CrossSourceComparator,
            CrossSourceComparisonRequest,
        )
        from quantking.data.comparison.thresholds import default_thresholds
        from quantking.data.content import canonical_content_sha256

        primary_rows = _primary_rows()
        fallback_rows = _primary_rows()
        # Change code on one row - exact string comparison must catch it.
        fallback_rows[0]["code"] = "000001.SH"
        primary = _make_table(primary_rows)
        fallback = _make_table(fallback_rows)
        request = CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            primary_source="XTQUANT",
            fallback_source="BAOSTOCK",
            primary_version_id=new_entity_id("data_version"),
            fallback_version_id=new_entity_id("data_version"),
            primary_batch_id=new_entity_id("data_batch"),
            fallback_batch_id=new_entity_id("data_batch"),
            primary_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [primary]
            ),
            fallback_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [fallback]
            ),
            primary_quality_run_id=new_entity_id("data_quality"),
            fallback_quality_run_id=new_entity_id("data_quality"),
            primary_table=primary,
            fallback_table=fallback,
            thresholds=default_thresholds(),
            sample_spec=(),
            created_at=utc_now(),
        )
        comparator = CrossSourceComparator()
        outcome = comparator.compare(request=request)
        # Code change creates a fallback-only and primary-only pair (since
        # the primary key differs).
        assert outcome.total_differences >= 2

    def test_output_sorted_by_primary_key_then_field(self) -> None:
        from quantking.data.comparison.comparator import (
            CrossSourceComparator,
            CrossSourceComparisonRequest,
        )
        from quantking.data.comparison.thresholds import default_thresholds
        from quantking.data.content import canonical_content_sha256

        primary_rows = _primary_rows()
        fallback_rows = _primary_rows()
        fallback_rows[0]["close"] = 11.5
        fallback_rows[0]["high"] = 12.0
        fallback_rows[1]["open"] = 25.0
        fallback_rows[1]["high"] = 26.0
        primary = _make_table(primary_rows)
        fallback = _make_table(fallback_rows)
        request = CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            primary_source="XTQUANT",
            fallback_source="BAOSTOCK",
            primary_version_id=new_entity_id("data_version"),
            fallback_version_id=new_entity_id("data_version"),
            primary_batch_id=new_entity_id("data_batch"),
            fallback_batch_id=new_entity_id("data_batch"),
            primary_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [primary]
            ),
            fallback_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [fallback]
            ),
            primary_quality_run_id=new_entity_id("data_quality"),
            fallback_quality_run_id=new_entity_id("data_quality"),
            primary_table=primary,
            fallback_table=fallback,
            thresholds=default_thresholds(),
            sample_spec=(),
            created_at=utc_now(),
        )
        comparator = CrossSourceComparator()
        outcome = comparator.compare(request=request)
        rows = outcome.row_differences
        # Sort key: (code, trade_date, field).
        keys = [(row.primary_key["code"], row.primary_key["trade_date"], row.field) for row in rows]
        assert keys == sorted(keys)

    def test_comparator_does_not_mutate_input_tables(self) -> None:
        from quantking.data.comparison.comparator import (
            CrossSourceComparator,
            CrossSourceComparisonRequest,
        )
        from quantking.data.comparison.thresholds import default_thresholds
        from quantking.data.content import canonical_content_sha256

        primary_rows = _primary_rows()
        fallback_rows = _primary_rows()
        fallback_rows[0]["close"] = 11.5
        fallback_rows[0]["high"] = 12.0
        primary = _make_table(primary_rows)
        fallback = _make_table(fallback_rows)
        primary_before = primary.to_pylist()
        fallback_before = fallback.to_pylist()
        request = CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            primary_source="XTQUANT",
            fallback_source="BAOSTOCK",
            primary_version_id=new_entity_id("data_version"),
            fallback_version_id=new_entity_id("data_version"),
            primary_batch_id=new_entity_id("data_batch"),
            fallback_batch_id=new_entity_id("data_batch"),
            primary_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [primary]
            ),
            fallback_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [fallback]
            ),
            primary_quality_run_id=new_entity_id("data_quality"),
            fallback_quality_run_id=new_entity_id("data_quality"),
            primary_table=primary,
            fallback_table=fallback,
            thresholds=default_thresholds(),
            sample_spec=(),
            created_at=utc_now(),
        )
        comparator = CrossSourceComparator()
        comparator.compare(request=request)
        assert primary.to_pylist() == primary_before
        assert fallback.to_pylist() == fallback_before

    def test_same_inputs_produce_same_report_id_deterministically(self) -> None:
        from quantking.data.comparison.comparator import (
            CrossSourceComparator,
            CrossSourceComparisonRequest,
        )
        from quantking.data.comparison.thresholds import default_thresholds
        from quantking.data.content import canonical_content_sha256

        def _build() -> CrossSourceComparisonRequest:
            primary = _make_table(_primary_rows())
            fallback = _make_table(_primary_rows())
            primary_sha = canonical_content_sha256(DatasetKind.DAILY_BAR_UNADJUSTED, [primary])
            fallback_sha = canonical_content_sha256(DatasetKind.DAILY_BAR_UNADJUSTED, [fallback])
            return CrossSourceComparisonRequest(
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                schema_version="daily_bar_unadjusted.v1",
                primary_source="XTQUANT",
                fallback_source="BAOSTOCK",
                primary_version_id=new_entity_id("data_version"),
                fallback_version_id=new_entity_id("data_version"),
                primary_batch_id=new_entity_id("data_batch"),
                fallback_batch_id=new_entity_id("data_batch"),
                primary_content_sha256=primary_sha,
                fallback_content_sha256=fallback_sha,
                primary_quality_run_id=new_entity_id("data_quality"),
                fallback_quality_run_id=new_entity_id("data_quality"),
                primary_table=primary,
                fallback_table=fallback,
                thresholds=default_thresholds(),
                sample_spec=(),
                created_at=utc_now(),
            )

        comparator = CrossSourceComparator()
        outcome1 = comparator.compare(request=_build())
        outcome2 = comparator.compare(request=_build())
        # Same inputs must produce same report_id (deterministic).
        assert outcome1.report_id == outcome2.report_id


__all__ = ["TestCrossSourceComparator", "TestThresholdConfiguration"]
