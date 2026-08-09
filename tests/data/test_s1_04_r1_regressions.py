"""S1-04-R1 regression tests for content and quality rules."""

from __future__ import annotations

from datetime import date

import pyarrow as pa

from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.models import DatasetKind


def _table(dataset: str, rows: list[dict[str, object]]) -> pa.Table:
    source = pa.Table.from_pylist(rows)
    schema = canonical_schema(dataset)
    return pa.Table.from_arrays(
        [source.column(field.name).cast(field.type, safe=False) for field in schema],
        schema=schema,
    )


def _daily_rows() -> list[dict[str, object]]:
    return [
        {
            "code": "000001.SZ",
            "trade_date": date(2026, 7, 14),
            "open": 10.0,
            "high": 11.0,
            "low": 9.5,
            "close": 10.5,
            "pre_close": 10.0,
            "volume": 1_000,
            "amount": 10_500.0,
        },
        {
            "code": "000001.SZ",
            "trade_date": date(2026, 7, 15),
            "open": 10.5,
            "high": 11.5,
            "low": 10.0,
            "close": 11.0,
            "pre_close": 10.5,
            "volume": 2_000,
            "amount": 22_000.0,
        },
    ]


def test_content_sha_is_independent_of_chunking_and_table_order() -> None:
    from quantking.data.content import canonical_content_sha256

    rows = _daily_rows()
    whole = _table("daily_bar_unadjusted", rows)
    first = _table("daily_bar_unadjusted", [rows[0]])
    second = _table("daily_bar_unadjusted", [rows[1]])

    expected = canonical_content_sha256(DatasetKind.DAILY_BAR_UNADJUSTED, (whole,))
    assert canonical_content_sha256(DatasetKind.DAILY_BAR_UNADJUSTED, (second, first)) == expected


def test_quality_engine_uses_actual_table_sha() -> None:
    from quantking.data.content import canonical_content_sha256
    from quantking.data.quality.engine import QualityEngine
    from quantking.data.quality.models import QualityRequest, ReferenceDataBinding
    from quantking.data.quality.rules import default_rule_set

    table = _table("daily_bar_unadjusted", [_daily_rows()[0]])
    calendar = _table(
        "exchange_calendar",
        [{"exchange": "SZ", "trade_date": date(2026, 7, 14), "is_open": True}],
    )
    request = QualityRequest(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        version_id=new_entity_id("data_version"),
        rule_version="quality-rules.v1",
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-04-r1",
        content_sha256="0" * 64,
        input_batch_ids=(),
        expected_securities=("000001.SZ",),
        expected_exchanges=("SZ",),
        coverage_start=TradingDate(date(2026, 7, 14)),
        coverage_end=TradingDate(date(2026, 7, 14)),
        reference_data={
            "exchange_calendar": ReferenceDataBinding(
                dataset=DatasetKind.EXCHANGE_CALENDAR,
                version_id=new_entity_id("data_version"),
                content_sha256="1" * 64,
            )
        },
        empty_result=None,
    )

    outcome = QualityEngine(rule_set=default_rule_set()).execute(
        request=request,
        table=table,
        reference_tables={"exchange_calendar": calendar},
    )

    assert outcome.content_sha256 == canonical_content_sha256(
        DatasetKind.DAILY_BAR_UNADJUSTED, (table,)
    )
    assert outcome.content_sha256 != request.content_sha256


def test_valid_adjustment_factor_passes() -> None:
    from quantking.data.quality.engine import QualityEngine
    from quantking.data.quality.models import QualityRequest, ReferenceDataBinding
    from quantking.data.quality.rules import default_rule_set

    table = _table(
        "adjustment_factor",
        [
            {
                "code": "000001.SZ",
                "trade_date": date(2026, 7, 15),
                "price_multiplier": 1.0,
                "volume_multiplier": 1.0,
                "normalization_date": date(2026, 7, 15),
                "algorithm_version": "factor.v1",
            }
        ],
    )
    calendar = _table(
        "exchange_calendar",
        [{"exchange": "SZ", "trade_date": date(2026, 7, 15), "is_open": True}],
    )
    request = QualityRequest(
        dataset=DatasetKind.ADJUSTMENT_FACTOR,
        version_id=new_entity_id("data_version"),
        rule_version="quality-rules.v1",
        schema_version="adjustment_factor.v1",
        code_version="s1-04-r1",
        content_sha256="0" * 64,
        input_batch_ids=(),
        expected_securities=("000001.SZ",),
        expected_exchanges=("SZ",),
        coverage_start=TradingDate(date(2026, 7, 15)),
        coverage_end=TradingDate(date(2026, 7, 15)),
        reference_data={
            "exchange_calendar": ReferenceDataBinding(
                dataset=DatasetKind.EXCHANGE_CALENDAR,
                version_id=new_entity_id("data_version"),
                content_sha256="1" * 64,
            )
        },
        empty_result=None,
    )

    outcome = QualityEngine(rule_set=default_rule_set()).execute(
        request=request,
        table=table,
        reference_tables={"exchange_calendar": calendar},
    )
    assert outcome.status.value == "PASSED"


def test_security_master_rejects_overlapping_effective_ranges() -> None:
    from quantking.data.quality.engine import QualityEngine
    from quantking.data.quality.models import QualityRequest
    from quantking.data.quality.rules import default_rule_set

    table = _table(
        "security_master",
        [
            {
                "code": "000001.SZ",
                "exchange": "SZ",
                "name": "Ping An",
                "board": "MAIN",
                "listing_date": date(1991, 4, 3),
                "delisting_date": None,
                "effective_from": date(2026, 1, 1),
                "effective_to": date(2026, 7, 31),
                "listing_status": "LISTED",
            },
            {
                "code": "000001.SZ",
                "exchange": "SZ",
                "name": "Ping An Bank",
                "board": "MAIN",
                "listing_date": date(1991, 4, 3),
                "delisting_date": None,
                "effective_from": date(2026, 7, 1),
                "effective_to": None,
                "listing_status": "LISTED",
            },
        ],
    )
    request = QualityRequest(
        dataset=DatasetKind.SECURITY_MASTER,
        version_id=new_entity_id("data_version"),
        rule_version="quality-rules.v1",
        schema_version="security_master.v1",
        code_version="s1-04-r1",
        content_sha256="0" * 64,
        input_batch_ids=(),
        expected_securities=("000001.SZ",),
        expected_exchanges=("SZ",),
        coverage_start=TradingDate(date(2026, 1, 1)),
        coverage_end=TradingDate(date(2026, 7, 31)),
        reference_data={},
        empty_result=None,
    )

    outcome = QualityEngine(rule_set=default_rule_set()).execute(
        request=request, table=table, reference_tables={}
    )
    rule = next(
        result for result in outcome.results if result.rule_code == "listing_effective_range"
    )
    assert rule.status.value == "FAILED"
