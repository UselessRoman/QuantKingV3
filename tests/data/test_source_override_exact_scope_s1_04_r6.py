"""S1-04-R6 exact SourceOverride scope contract regressions."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate, utc_now
from quantking.data.comparison.verifier import VerifiedDifferenceReport
from quantking.data.models import DatasetKind, DataUpstream, SourceOverride
from quantking.data.override.service import CreateOverrideRequest, SourceOverrideService
from quantking.data.quality.models import QualityRequest


def _request(**changes: object) -> CreateOverrideRequest:
    values: dict[str, object] = {
        "difference_report_id": new_entity_id("data_diff"),
        "primary_batch_id": new_entity_id("data_batch"),
        "fallback_batch_id": new_entity_id("data_batch"),
        "primary_source": "EASTMONEY",
        "fallback_source": "TENCENT",
        "scope_codes": ("000001.SZ",),
        "scope_dates": (TradingDate(date(2026, 7, 15)),),
        "fields": ("close",),
        "reason": "approved exact cell",
        "field_replacements": (
            {"code": "000001.SZ", "trade_date": "2026-07-15", "field": "close"},
        ),
    }
    values.update(changes)
    return CreateOverrideRequest(**values)  # type: ignore[arg-type]


def test_api_request_rejects_summary_that_disagrees_with_exact_units() -> None:
    with pytest.raises(ValueError, match="summary"):
        _request(scope_codes=("000002.SZ",))


def test_direct_domain_rejects_dataset_incorrect_exact_key_shape() -> None:
    with pytest.raises(ValueError, match="primary key"):
        SourceOverride.pending(
            override_id=new_entity_id("data_override"),
            dataset=DatasetKind.SECURITY_MASTER,
            difference_report_id=new_entity_id("data_diff"),
            primary_batch_id=new_entity_id("data_batch"),
            fallback_batch_id=new_entity_id("data_batch"),
            primary_source=DataUpstream.EASTMONEY,
            fallback_source=DataUpstream.TENCENT,
            scope_codes=("000001.SZ",),
            scope_dates=(TradingDate(date(2026, 7, 15)),),
            fields=("listing_status",),
            reason="wrong date key must not be accepted",
            created_at=utc_now(),
            field_replacements=(
                {"code": "000001.SZ", "trade_date": "2026-07-15", "field": "listing_status"},
            ),
        )


def test_dataset_local_non_primary_field_is_accepted() -> None:
    override = SourceOverride.pending(
        override_id=new_entity_id("data_override"),
        dataset=DatasetKind.SECURITY_MASTER,
        difference_report_id=new_entity_id("data_diff"),
        primary_batch_id=new_entity_id("data_batch"),
        fallback_batch_id=new_entity_id("data_batch"),
        primary_source=DataUpstream.EASTMONEY,
        fallback_source=DataUpstream.TENCENT,
        scope_codes=("000001.SZ",),
        scope_dates=(TradingDate(date(2026, 7, 15)),),
        fields=("exchange",),
        reason="exchange is not a security_master primary key",
        created_at=utc_now(),
        field_replacements=(
            {"code": "000001.SZ", "effective_from": "2026-07-15", "field": "exchange"},
        ),
    )
    assert override.fields == ("exchange",)


def test_application_fingerprint_changes_with_exact_scope() -> None:
    exact = SourceOverride.pending(
        override_id=new_entity_id("data_override"),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        difference_report_id=new_entity_id("data_diff"),
        primary_batch_id=new_entity_id("data_batch"),
        fallback_batch_id=new_entity_id("data_batch"),
        primary_source=DataUpstream.EASTMONEY,
        fallback_source=DataUpstream.TENCENT,
        scope_codes=("000001.SZ",),
        scope_dates=(TradingDate(date(2026, 7, 15)),),
        fields=("close",),
        reason="exact cell",
        created_at=utc_now(),
        field_replacements=({"code": "000001.SZ", "trade_date": "2026-07-15", "field": "close"},),
    )
    changed = replace(
        exact,
        fields=("open",),
        field_replacements=({"code": "000001.SZ", "trade_date": "2026-07-15", "field": "open"},),
    )
    report = object.__new__(VerifiedDifferenceReport)
    object.__setattr__(report, "report_sha256", "a" * 64)
    quality = QualityRequest(
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        version_id=new_entity_id("data_version"),
        rule_version="quality-rules.v1",
        schema_version="exchange_calendar.v1",
        code_version="s1-04-r6",
        content_sha256="b" * 64,
        input_batch_ids=(),
        expected_securities=(),
        expected_exchanges=("SZ",),
        coverage_start=TradingDate(date(2026, 7, 15)),
        coverage_end=TradingDate(date(2026, 7, 15)),
        reference_data={},
        empty_result=None,
    )
    first = SourceOverrideService._application_fingerprint(
        current=exact, report=report, quality_request=quality
    )
    second = SourceOverrideService._application_fingerprint(
        current=changed, report=report, quality_request=quality
    )
    assert first != second
    assert exact.exact_scope_sha256 != changed.exact_scope_sha256
