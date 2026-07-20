"""Red tests for the framework-free single-source quality rule engine (S1-04A).

These tests target the S1-04A spec: a framework-free quality rule contract
and executor that gates Canonical publish. They MUST fail until the
``quantking.data.quality`` package and a quality-gated publish coordinator
land.

The tests only use ``tmp_path`` and synthetic PyArrow tables.
"""

from __future__ import annotations

from datetime import date

import pyarrow as pa
import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.models import (
    DatasetKind,
    EmptyResultInfo,
    LegalEmptyClassification,
)

# ---------------------------------------------------------------------------
# Synthetic table builders
# ---------------------------------------------------------------------------


def _daily_bar_table(rows: list[dict[str, object]] | None = None) -> pa.Table:
    rows = (
        rows
        if rows is not None
        else [
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
            }
        ]
    )
    table = pa.Table.from_pylist(rows)
    descriptor = canonical_schema("daily_bar_unadjusted")
    columns: list[pa.Array] = []
    fields: list[pa.Field] = []
    for field in descriptor:
        columns.append(table.column(field.name).cast(field.type, safe=False))
        fields.append(pa.field(field.name, field.type, nullable=field.nullable))
    return pa.Table.from_arrays(columns, schema=pa.schema(fields))


def _calendar_table(rows: list[dict[str, object]] | None = None) -> pa.Table:
    rows = (
        rows
        if rows is not None
        else [
            {"exchange": "SZ", "trade_date": date(2026, 7, 15), "is_open": True},
        ]
    )
    table = pa.Table.from_pylist(rows)
    descriptor = canonical_schema("exchange_calendar")
    columns: list[pa.Array] = []
    fields: list[pa.Field] = []
    for field in descriptor:
        columns.append(table.column(field.name).cast(field.type, safe=False))
        fields.append(pa.field(field.name, field.type, nullable=field.nullable))
    return pa.Table.from_arrays(columns, schema=pa.schema(fields))


# ---------------------------------------------------------------------------
# Quality request contract
# ---------------------------------------------------------------------------


class TestQualityRequestContract:
    def test_quality_request_requires_explicit_credentials(self) -> None:
        from quantking.data.quality.models import QualityRequest, ReferenceDataBinding

        version_id = new_entity_id("data_version")
        calendar_version = new_entity_id("data_version")
        with pytest.raises((TypeError, ValueError)):
            QualityRequest(
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                version_id=version_id,
                rule_version="quality-rules.v1",
                schema_version="daily_bar_unadjusted.v1",
                content_sha256="a" * 64,
                input_batch_ids=(new_entity_id("data_batch"),),
                expected_securities=("000001.SZ",),
                expected_exchanges=("SZ",),
                coverage_start=TradingDate(date(2026, 7, 15)),
                coverage_end=TradingDate(date(2026, 7, 15)),
                reference_data={
                    "exchange_calendar": ReferenceDataBinding(
                        dataset=DatasetKind.EXCHANGE_CALENDAR,
                        version_id=calendar_version,
                        content_sha256="b" * 64,
                    )
                },
                empty_result=None,
                # Missing required field should raise.
            )  # type: ignore[call-arg]

    def test_quality_request_rejects_missing_reference_data_for_applicable_rule(
        self,
    ) -> None:
        from quantking.data.quality.models import QualityRequest

        version_id = new_entity_id("data_version")
        # daily_bar_unadjusted requires exchange_calendar reference data;
        # omitting it must fail validation.
        with pytest.raises((TypeError, ValueError)):
            QualityRequest(
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                version_id=version_id,
                rule_version="quality-rules.v1",
                schema_version="daily_bar_unadjusted.v1",
                code_version="s1-04",
                content_sha256="a" * 64,
                input_batch_ids=(new_entity_id("data_batch"),),
                expected_securities=("000001.SZ",),
                expected_exchanges=("SZ",),
                coverage_start=TradingDate(date(2026, 7, 15)),
                coverage_end=TradingDate(date(2026, 7, 15)),
                reference_data={},
                empty_result=None,
            )

    def test_quality_request_rejects_invalid_content_sha(self) -> None:
        from quantking.data.quality.models import QualityRequest, ReferenceDataBinding

        version_id = new_entity_id("data_version")
        calendar_version = new_entity_id("data_version")
        with pytest.raises((TypeError, ValueError)):
            QualityRequest(
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                version_id=version_id,
                rule_version="quality-rules.v1",
                schema_version="daily_bar_unadjusted.v1",
                code_version="s1-04",
                content_sha256="not-a-sha",
                input_batch_ids=(new_entity_id("data_batch"),),
                expected_securities=("000001.SZ",),
                expected_exchanges=("SZ",),
                coverage_start=TradingDate(date(2026, 7, 15)),
                coverage_end=TradingDate(date(2026, 7, 15)),
                reference_data={
                    "exchange_calendar": ReferenceDataBinding(
                        dataset=DatasetKind.EXCHANGE_CALENDAR,
                        version_id=calendar_version,
                        content_sha256="b" * 64,
                    )
                },
                empty_result=None,
            )


# ---------------------------------------------------------------------------
# Rule set versioning and applicability
# ---------------------------------------------------------------------------


class TestQualityRuleSet:
    def test_rule_set_exposes_versioned_rules_for_each_dataset(self) -> None:
        from quantking.data.quality.rules import default_rule_set

        rule_set = default_rule_set()
        assert rule_set.rule_version == "quality-rules.v1"
        daily_rules = rule_set.for_dataset(DatasetKind.DAILY_BAR_UNADJUSTED)
        rule_codes = {rule.code for rule in daily_rules}
        # Must cover schema, primary key, OHLC, positive prices,
        # non-negative volume/amount, calendar, coverage.
        assert "schema_conformance" in rule_codes
        assert "primary_key_uniqueness" in rule_codes
        assert "ohlc_consistency" in rule_codes
        assert "positive_prices" in rule_codes
        assert "non_negative_volume_amount" in rule_codes
        assert "calendar_membership" in rule_codes
        assert "coverage_window" in rule_codes

    def test_rule_set_for_security_master_includes_listing_status_rule(self) -> None:
        from quantking.data.quality.rules import default_rule_set

        rule_set = default_rule_set()
        sm_rules = rule_set.for_dataset(DatasetKind.SECURITY_MASTER)
        codes = {rule.code for rule in sm_rules}
        assert "listing_effective_range" in codes

    def test_rule_set_for_adjustment_factor_includes_positive_multiplier(self) -> None:
        from quantking.data.quality.rules import default_rule_set

        rule_set = default_rule_set()
        af_rules = rule_set.for_dataset(DatasetKind.ADJUSTMENT_FACTOR)
        codes = {rule.code for rule in af_rules}
        assert "positive_adjustment_factor" in codes

    def test_rule_set_for_benchmark_index_daily_honors_nullable_volume(self) -> None:
        from quantking.data.quality.rules import default_rule_set

        rule_set = default_rule_set()
        bid_rules = rule_set.for_dataset(DatasetKind.BENCHMARK_INDEX_DAILY)
        codes = {rule.code for rule in bid_rules}
        # benchmark_index_daily has nullable volume/amount; the rule must exist
        # but accept nulls.
        assert "non_negative_volume_amount" in codes


# ---------------------------------------------------------------------------
# Engine execution and outcomes
# ---------------------------------------------------------------------------


class TestQualityEngineExecution:
    def test_engine_passes_clean_daily_bar(self) -> None:
        from quantking.data.quality.engine import QualityEngine
        from quantking.data.quality.models import QualityRequest, ReferenceDataBinding
        from quantking.data.quality.rules import default_rule_set

        version_id = new_entity_id("data_version")
        calendar_version = new_entity_id("data_version")
        request = QualityRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            version_id=version_id,
            rule_version="quality-rules.v1",
            schema_version="daily_bar_unadjusted.v1",
            code_version="s1-04",
            content_sha256="a" * 64,
            input_batch_ids=(new_entity_id("data_batch"),),
            expected_securities=("000001.SZ",),
            expected_exchanges=("SZ",),
            coverage_start=TradingDate(date(2026, 7, 15)),
            coverage_end=TradingDate(date(2026, 7, 15)),
            reference_data={
                "exchange_calendar": ReferenceDataBinding(
                    dataset=DatasetKind.EXCHANGE_CALENDAR,
                    version_id=calendar_version,
                    content_sha256="b" * 64,
                )
            },
            empty_result=None,
        )
        engine = QualityEngine(rule_set=default_rule_set())
        outcome = engine.execute(
            request=request,
            table=_daily_bar_table(),
            reference_tables={"exchange_calendar": _calendar_table()},
        )
        assert outcome.status.value == "PASSED"
        assert outcome.run_id.namespace == "data_quality"
        assert outcome.version_id == version_id
        assert outcome.rule_version == "quality-rules.v1"
        assert outcome.ended_at is not None
        # PASSED must include all applicable blocking rules.
        applicable_codes = {
            rule.code for rule in default_rule_set().for_dataset(DatasetKind.DAILY_BAR_UNADJUSTED)
        }
        outcome_codes = {result.rule_code for result in outcome.results}
        assert applicable_codes <= outcome_codes
        assert all(result.status.value == "PASSED" for result in outcome.results)

    def test_engine_fails_on_ohlc_violation(self) -> None:
        from quantking.data.quality.engine import QualityEngine
        from quantking.data.quality.models import QualityRequest, ReferenceDataBinding
        from quantking.data.quality.rules import default_rule_set

        version_id = new_entity_id("data_version")
        calendar_version = new_entity_id("data_version")
        request = QualityRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            version_id=version_id,
            rule_version="quality-rules.v1",
            schema_version="daily_bar_unadjusted.v1",
            code_version="s1-04",
            content_sha256="a" * 64,
            input_batch_ids=(new_entity_id("data_batch"),),
            expected_securities=("000001.SZ",),
            expected_exchanges=("SZ",),
            coverage_start=TradingDate(date(2026, 7, 15)),
            coverage_end=TradingDate(date(2026, 7, 15)),
            reference_data={
                "exchange_calendar": ReferenceDataBinding(
                    dataset=DatasetKind.EXCHANGE_CALENDAR,
                    version_id=calendar_version,
                    content_sha256="b" * 64,
                )
            },
            empty_result=None,
        )
        bad_rows = [
            {
                "code": "000001.SZ",
                "trade_date": date(2026, 7, 15),
                "open": 10.0,
                "high": 9.0,  # high < open -> OHLC violation
                "low": 9.5,
                "close": 10.5,
                "pre_close": 10.0,
                "volume": 1_000_000,
                "amount": 10_500_000.0,
            }
        ]
        engine = QualityEngine(rule_set=default_rule_set())
        outcome = engine.execute(
            request=request, table=_daily_bar_table(bad_rows), reference_tables={}
        )
        assert outcome.status.value == "FAILED"
        ohlc = next(r for r in outcome.results if r.rule_code == "ohlc_consistency")
        assert ohlc.status.value == "FAILED"
        assert ohlc.affected_count >= 1

    def test_engine_fails_on_duplicate_primary_key(self) -> None:
        from quantking.data.quality.engine import QualityEngine
        from quantking.data.quality.models import QualityRequest, ReferenceDataBinding
        from quantking.data.quality.rules import default_rule_set

        version_id = new_entity_id("data_version")
        calendar_version = new_entity_id("data_version")
        request = QualityRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            version_id=version_id,
            rule_version="quality-rules.v1",
            schema_version="daily_bar_unadjusted.v1",
            code_version="s1-04",
            content_sha256="a" * 64,
            input_batch_ids=(new_entity_id("data_batch"),),
            expected_securities=("000001.SZ",),
            expected_exchanges=("SZ",),
            coverage_start=TradingDate(date(2026, 7, 15)),
            coverage_end=TradingDate(date(2026, 7, 15)),
            reference_data={
                "exchange_calendar": ReferenceDataBinding(
                    dataset=DatasetKind.EXCHANGE_CALENDAR,
                    version_id=calendar_version,
                    content_sha256="b" * 64,
                )
            },
            empty_result=None,
        )
        dup_rows = [
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
        ]
        engine = QualityEngine(rule_set=default_rule_set())
        outcome = engine.execute(
            request=request, table=_daily_bar_table(dup_rows), reference_tables={}
        )
        assert outcome.status.value == "FAILED"
        pk = next(r for r in outcome.results if r.rule_code == "primary_key_uniqueness")
        assert pk.status.value == "FAILED"

    def test_engine_fails_when_calendar_reference_missing(self) -> None:
        from quantking.data.quality.engine import QualityEngine
        from quantking.data.quality.models import QualityRequest, ReferenceDataBinding

        version_id = new_entity_id("data_version")
        calendar_version = new_entity_id("data_version")
        # Declare the calendar reference binding but supply no actual table.
        request = QualityRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            version_id=version_id,
            rule_version="quality-rules.v1",
            schema_version="daily_bar_unadjusted.v1",
            code_version="s1-04",
            content_sha256="a" * 64,
            input_batch_ids=(new_entity_id("data_batch"),),
            expected_securities=("000001.SZ",),
            expected_exchanges=("SZ",),
            coverage_start=TradingDate(date(2026, 7, 15)),
            coverage_end=TradingDate(date(2026, 7, 15)),
            reference_data={
                "exchange_calendar": ReferenceDataBinding(
                    dataset=DatasetKind.EXCHANGE_CALENDAR,
                    version_id=calendar_version,
                    content_sha256="b" * 64,
                )
            },
            empty_result=None,
        )
        from quantking.data.quality.rules import default_rule_set

        engine = QualityEngine(rule_set=default_rule_set())
        outcome = engine.execute(request=request, table=_daily_bar_table(), reference_tables={})
        # Calendar rule must FAIL because the reference table is missing.
        assert outcome.status.value == "FAILED"
        cal = next(r for r in outcome.results if r.rule_code == "calendar_membership")
        assert cal.status.value == "FAILED"

    def test_engine_passes_when_calendar_reference_supplied(self) -> None:
        from quantking.data.quality.engine import QualityEngine
        from quantking.data.quality.models import QualityRequest, ReferenceDataBinding
        from quantking.data.quality.rules import default_rule_set

        version_id = new_entity_id("data_version")
        calendar_version = new_entity_id("data_version")
        request = QualityRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            version_id=version_id,
            rule_version="quality-rules.v1",
            schema_version="daily_bar_unadjusted.v1",
            code_version="s1-04",
            content_sha256="a" * 64,
            input_batch_ids=(new_entity_id("data_batch"),),
            expected_securities=("000001.SZ",),
            expected_exchanges=("SZ",),
            coverage_start=TradingDate(date(2026, 7, 15)),
            coverage_end=TradingDate(date(2026, 7, 15)),
            reference_data={
                "exchange_calendar": ReferenceDataBinding(
                    dataset=DatasetKind.EXCHANGE_CALENDAR,
                    version_id=calendar_version,
                    content_sha256="b" * 64,
                )
            },
            empty_result=None,
        )
        engine = QualityEngine(rule_set=default_rule_set())
        outcome = engine.execute(
            request=request,
            table=_daily_bar_table(),
            reference_tables={"exchange_calendar": _calendar_table()},
        )
        assert outcome.status.value == "PASSED"

    def test_engine_internal_exception_yields_internal_failure_not_running(self) -> None:
        from quantking.data.quality.engine import QualityEngine
        from quantking.data.quality.models import QualityRequest, ReferenceDataBinding
        from quantking.data.quality.rules import default_rule_set

        version_id = new_entity_id("data_version")
        calendar_version = new_entity_id("data_version")
        request = QualityRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            version_id=version_id,
            rule_version="quality-rules.v1",
            schema_version="daily_bar_unadjusted.v1",
            code_version="s1-04",
            content_sha256="a" * 64,
            input_batch_ids=(new_entity_id("data_batch"),),
            expected_securities=("000001.SZ",),
            expected_exchanges=("SZ",),
            coverage_start=TradingDate(date(2026, 7, 15)),
            coverage_end=TradingDate(date(2026, 7, 15)),
            reference_data={
                "exchange_calendar": ReferenceDataBinding(
                    dataset=DatasetKind.EXCHANGE_CALENDAR,
                    version_id=calendar_version,
                    content_sha256="b" * 64,
                )
            },
            empty_result=None,
        )
        engine = QualityEngine(rule_set=default_rule_set())
        # Pass a table that is not a pa.Table to force an internal error.
        with pytest.raises(TypeError):
            engine.execute(
                request=request,
                table="not-a-table",  # type: ignore[arg-type]
                reference_tables={},
            )

    def test_engine_skips_inapplicable_rules(self) -> None:
        from quantking.data.quality.engine import QualityEngine
        from quantking.data.quality.models import QualityRequest, ReferenceDataBinding
        from quantking.data.quality.rules import default_rule_set

        version_id = new_entity_id("data_version")
        calendar_version = new_entity_id("data_version")
        # Exchange calendar dataset: rules like OHLC don't apply; they
        # must be SKIPPED, not PASSED.
        request = QualityRequest(
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            version_id=version_id,
            rule_version="quality-rules.v1",
            schema_version="exchange_calendar.v1",
            code_version="s1-04",
            content_sha256="a" * 64,
            input_batch_ids=(new_entity_id("data_batch"),),
            expected_securities=(),
            expected_exchanges=("SZ",),
            coverage_start=TradingDate(date(2026, 7, 15)),
            coverage_end=TradingDate(date(2026, 7, 15)),
            reference_data={
                "exchange_calendar": ReferenceDataBinding(
                    dataset=DatasetKind.EXCHANGE_CALENDAR,
                    version_id=calendar_version,
                    content_sha256="b" * 64,
                )
            },
            empty_result=None,
        )
        engine = QualityEngine(rule_set=default_rule_set())
        outcome = engine.execute(request=request, table=_calendar_table(), reference_tables={})
        assert outcome.status.value == "PASSED"
        # OHLC rule must not appear (or must be SKIPPED) for calendar dataset.
        ohlc_results = [r for r in outcome.results if r.rule_code == "ohlc_consistency"]
        for result in ohlc_results:
            assert result.status.value == "SKIPPED"

    def test_engine_legal_empty_result_passes(self) -> None:
        from quantking.data.quality.engine import QualityEngine
        from quantking.data.quality.models import QualityRequest, ReferenceDataBinding
        from quantking.data.quality.rules import default_rule_set

        version_id = new_entity_id("data_version")
        calendar_version = new_entity_id("data_version")
        schema = canonical_schema("daily_bar_unadjusted")
        empty = pa.Table.from_arrays(
            [pa.array([], type=field.type) for field in schema],
            schema=schema,
        )
        request = QualityRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            version_id=version_id,
            rule_version="quality-rules.v1",
            schema_version="daily_bar_unadjusted.v1",
            code_version="s1-04",
            content_sha256="a" * 64,
            input_batch_ids=(new_entity_id("data_batch"),),
            expected_securities=(),
            expected_exchanges=("SZ",),
            coverage_start=TradingDate(date(2026, 7, 15)),
            coverage_end=TradingDate(date(2026, 7, 15)),
            reference_data={
                "exchange_calendar": ReferenceDataBinding(
                    dataset=DatasetKind.EXCHANGE_CALENDAR,
                    version_id=calendar_version,
                    content_sha256="b" * 64,
                )
            },
            empty_result=EmptyResultInfo(
                classification=LegalEmptyClassification.NO_TRADING_SESSION,
                reason="calendar is closed for the requested window",
            ),
        )
        engine = QualityEngine(rule_set=default_rule_set())
        outcome = engine.execute(
            request=request,
            table=empty,
            reference_tables={"exchange_calendar": _calendar_table()},
        )
        assert outcome.status.value == "PASSED"

    def test_engine_unexpected_empty_without_legal_classification_fails(self) -> None:
        from quantking.data.quality.engine import QualityEngine
        from quantking.data.quality.models import QualityRequest, ReferenceDataBinding
        from quantking.data.quality.rules import default_rule_set

        version_id = new_entity_id("data_version")
        calendar_version = new_entity_id("data_version")
        schema = canonical_schema("daily_bar_unadjusted")
        empty = pa.Table.from_arrays(
            [pa.array([], type=field.type) for field in schema],
            schema=schema,
        )
        request = QualityRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            version_id=version_id,
            rule_version="quality-rules.v1",
            schema_version="daily_bar_unadjusted.v1",
            code_version="s1-04",
            content_sha256="a" * 64,
            input_batch_ids=(new_entity_id("data_batch"),),
            expected_securities=("000001.SZ",),
            expected_exchanges=("SZ",),
            coverage_start=TradingDate(date(2026, 7, 15)),
            coverage_end=TradingDate(date(2026, 7, 15)),
            reference_data={
                "exchange_calendar": ReferenceDataBinding(
                    dataset=DatasetKind.EXCHANGE_CALENDAR,
                    version_id=calendar_version,
                    content_sha256="b" * 64,
                )
            },
            empty_result=None,
        )
        engine = QualityEngine(rule_set=default_rule_set())
        outcome = engine.execute(request=request, table=empty, reference_tables={})
        assert outcome.status.value == "FAILED"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestQualityEngineDeterminism:
    def test_same_input_yields_same_run_id_and_outcomes(self) -> None:
        from quantking.data.quality.engine import QualityEngine
        from quantking.data.quality.models import QualityRequest, ReferenceDataBinding
        from quantking.data.quality.rules import default_rule_set

        version_id = new_entity_id("data_version")
        calendar_version = new_entity_id("data_version")
        request = QualityRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            version_id=version_id,
            rule_version="quality-rules.v1",
            schema_version="daily_bar_unadjusted.v1",
            code_version="s1-04",
            content_sha256="a" * 64,
            input_batch_ids=(new_entity_id("data_batch"),),
            expected_securities=("000001.SZ",),
            expected_exchanges=("SZ",),
            coverage_start=TradingDate(date(2026, 7, 15)),
            coverage_end=TradingDate(date(2026, 7, 15)),
            reference_data={
                "exchange_calendar": ReferenceDataBinding(
                    dataset=DatasetKind.EXCHANGE_CALENDAR,
                    version_id=calendar_version,
                    content_sha256="b" * 64,
                )
            },
            empty_result=None,
        )
        engine = QualityEngine(rule_set=default_rule_set())
        outcome1 = engine.execute(request=request, table=_daily_bar_table(), reference_tables={})
        outcome2 = engine.execute(request=request, table=_daily_bar_table(), reference_tables={})
        # Same request + table must produce same run_id (deterministic).
        assert outcome1.run_id == outcome2.run_id
        assert outcome1.status == outcome2.status
        assert [r.status for r in outcome1.results] == [r.status for r in outcome2.results]


__all__ = [
    "TestQualityEngineDeterminism",
    "TestQualityEngineExecution",
    "TestQualityRequestContract",
    "TestQualityRuleSet",
]
