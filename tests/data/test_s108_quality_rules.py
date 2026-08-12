from __future__ import annotations

from datetime import date

import pyarrow as pa
import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.models import DatasetKind, QualityRunStatus
from quantking.data.quality.engine import QualityEngine
from quantking.data.quality.models import S108_DAILY_RULE_VERSION, QualityRequest
from quantking.data.quality.rules import default_rule_set, s108_daily_rule_set


def _table() -> pa.Table:
    return pa.Table.from_pylist(
        [
            {
                "code": "000001.SZ",
                "trade_date": date(2026, 7, 15),
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "pre_close": 10.0,
                "volume": 1000,
                "amount": 10_500.0,
            },
            {
                "code": "600000.SH",
                "trade_date": date(2026, 7, 15),
                "open": 11.0,
                "high": 12.0,
                "low": 10.5,
                "close": 11.5,
                "pre_close": 11.0,
                "volume": 2000,
                "amount": 23_000.0,
            },
        ],
        schema=canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value),
    )


def _request(rule_version: str) -> QualityRequest:
    return QualityRequest(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        version_id=new_entity_id("data_version"),
        rule_version=rule_version,
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-08.v1",
        content_sha256="0" * 64,
        input_batch_ids=(new_entity_id("data_batch"),),
        expected_securities=("000001.SZ", "600000.SH"),
        expected_exchanges=("SH", "SZ"),
        coverage_start=TradingDate(date(2026, 6, 11)),
        coverage_end=TradingDate(date(2026, 8, 9)),
        reference_data={},
        empty_result=None,
    )


def test_s108_daily_quality_passes_without_calendar_reference() -> None:
    rule_set = s108_daily_rule_set()
    outcome = QualityEngine(rule_set=rule_set).execute(
        request=_request(S108_DAILY_RULE_VERSION),
        table=_table(),
        reference_tables={},
    )

    assert outcome.status is QualityRunStatus.PASSED
    assert {item.rule_code for item in outcome.results} == {
        "schema_conformance",
        "primary_key_uniqueness",
        "expected_scope_presence",
        "ohlc_consistency",
        "positive_prices",
        "non_negative_volume_amount",
        "coverage_window",
    }
    assert "calendar_membership" not in {item.rule_code for item in outcome.results}
    assert "coverage_by_security_exchange" not in {item.rule_code for item in outcome.results}


def test_s108_daily_quality_blocks_a_missing_frozen_symbol() -> None:
    outcome = QualityEngine(rule_set=s108_daily_rule_set()).execute(
        request=_request(S108_DAILY_RULE_VERSION),
        table=_table().slice(0, 1),
        reference_tables={},
    )

    assert outcome.status is QualityRunStatus.FAILED
    failed = next(item for item in outcome.results if item.rule_code == "expected_scope_presence")
    assert failed.affected_count == 1


def test_legacy_v1_still_requires_calendar_and_engine_rejects_version_mismatch() -> None:
    with pytest.raises(ValueError, match="exchange_calendar"):
        _request("quality-rules.v1")

    with pytest.raises(ValueError, match="rule_version"):
        QualityEngine(rule_set=default_rule_set()).execute(
            request=_request(S108_DAILY_RULE_VERSION),
            table=_table(),
            reference_tables={},
        )
