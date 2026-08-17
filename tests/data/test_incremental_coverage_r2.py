"""S1-08R2 A5: per-security real coverage segments with internal gaps.

The incremental planner and the R2 release manifest must express each
security's true contiguous coverage runs; a global ``(min, max)`` pair may
never masquerade as continuous coverage. Internal gaps must be refetched,
and legal exemptions (suspension, pre-listing) must be explicit.
"""

from __future__ import annotations

from datetime import date

import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate
from quantking.data.eod.release_manifest import (
    ReleaseManifestError,
    SecurityCoveragePlan,
    build_release_manifest,
)
from quantking.data.incremental import (
    CoverageWindow,
    IncrementalPlanRequest,
    build_incremental_plan,
)
from quantking.data.models import DatasetKind


def _request(
    scope: tuple[str, ...],
    coverage: tuple[CoverageWindow, ...],
    *,
    request_start: date = date(2026, 6, 11),
    request_end: date = date(2026, 7, 15),
) -> IncrementalPlanRequest:
    return IncrementalPlanRequest(
        batch_id=new_entity_id("data_batch"),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        scope=scope,
        request_start=TradingDate(request_start),
        request_end=TradingDate(request_end),
        start_inclusive=True,
        end_inclusive=True,
        known_coverage=coverage,
        lookback_days=0,
        max_days_per_chunk=31,
        max_items_per_chunk=1,
        adapter_version="test-adapter.v1",
    )


def test_planner_refetches_internal_gaps_only() -> None:
    plan = build_incremental_plan(
        _request(
            ("000001.SZ",),
            (
                CoverageWindow(
                    "000001.SZ", TradingDate(date(2026, 6, 11)), TradingDate(date(2026, 6, 30))
                ),
                CoverageWindow(
                    "000001.SZ", TradingDate(date(2026, 7, 10)), TradingDate(date(2026, 7, 15))
                ),
            ),
        )
    )

    assert [(str(chunk.start), str(chunk.end)) for chunk in plan.chunks] == [
        ("2026-07-01", "2026-07-09")
    ]


def test_planner_keeps_per_security_windows_independent() -> None:
    plan = build_incremental_plan(
        _request(
            ("000001.SZ", "600000.SH"),
            (
                CoverageWindow(
                    "000001.SZ", TradingDate(date(2026, 6, 11)), TradingDate(date(2026, 7, 15))
                ),
                CoverageWindow(
                    "600000.SH", TradingDate(date(2026, 6, 11)), TradingDate(date(2026, 6, 20))
                ),
            ),
        )
    )

    fetched = {(chunk.scope[0], str(chunk.start), str(chunk.end)) for chunk in plan.chunks}
    assert fetched == {("600000.SH", "2026-06-21", "2026-07-15")}


def test_planner_lookback_reopens_recent_coverage_per_security() -> None:
    request = IncrementalPlanRequest(
        batch_id=new_entity_id("data_batch"),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        scope=("000001.SZ",),
        request_start=TradingDate(date(2026, 7, 1)),
        request_end=TradingDate(date(2026, 7, 15)),
        start_inclusive=True,
        end_inclusive=True,
        known_coverage=(
            CoverageWindow(
                "000001.SZ", TradingDate(date(2026, 7, 1)), TradingDate(date(2026, 7, 15))
            ),
        ),
        lookback_days=5,
        max_days_per_chunk=31,
        max_items_per_chunk=1,
        adapter_version="test-adapter.v1",
    )
    plan = build_incremental_plan(request)

    assert [(str(chunk.start), str(chunk.end)) for chunk in plan.chunks] == [
        ("2026-07-11", "2026-07-15")
    ]


def _manifest(
    *,
    known_coverage: tuple[tuple[str, date, date], ...] = (),
    exemptions: tuple[tuple[str, date, date, str], ...] = (),
):
    return build_release_manifest(
        symbols=("000001.SZ", "600000.SH"),
        target_start=date(2026, 6, 11),
        target_end=date(2026, 7, 15),
        lookback_days=15,
        calendar_version_id="exchange-calendar-baostock.v1",
        calendar_sha256="a" * 64,
        known_coverage=known_coverage,
        exemptions=exemptions,
        source_route_version="BAO-TX-SINA.k4.v1",
        adapter_version="baostock-akshare-daily-bar.k4.v1",
        code_version="s1-08r2.v1",
        quality_rule_version="quality-rules.daily-bar-unadjusted.k4.v1",
        threshold_rule_version="comparison-thresholds.daily-bar-unadjusted.k4.v1",
        resolution_rule_version="resolution.daily-bar-unadjusted.k4.v1",
        unit_policy_versions={"baostock": "b"},
    )


def test_manifest_expresses_segments_internal_gaps_and_exemptions() -> None:
    manifest = _manifest(
        known_coverage=(
            ("000001.SZ", date(2026, 6, 1), date(2026, 6, 10)),
            ("000001.SZ", date(2026, 6, 20), date(2026, 6, 25)),
        ),
        exemptions=(
            ("600000.SH", date(2026, 7, 1), date(2026, 7, 3), "SUSPENSION"),
            ("600000.SH", date(2026, 6, 11), date(2026, 6, 11), "PRE_LISTING"),
        ),
    )

    by_symbol = {plan.symbol: plan for plan in manifest.security_coverage}
    ping_an = by_symbol["000001.SZ"]
    assert ping_an.segments == (
        (date(2026, 6, 1), date(2026, 6, 10)),
        (date(2026, 6, 20), date(2026, 6, 25)),
    )
    assert ping_an.internal_gaps == ((date(2026, 6, 11), date(2026, 6, 19)),)
    SPdbank = by_symbol["600000.SH"]
    assert SPdbank.segments == ()
    assert SPdbank.internal_gaps == ()
    assert SPdbank.exemptions == (
        (date(2026, 6, 11), date(2026, 6, 11), "PRE_LISTING"),
        (date(2026, 7, 1), date(2026, 7, 3), "SUSPENSION"),
    )


def test_manifest_rejects_gaps_overlapping_segments_and_unknown_exemption_reasons() -> None:
    with pytest.raises(ReleaseManifestError):
        SecurityCoveragePlan(
            symbol="000001.SZ",
            segments=((date(2026, 6, 1), date(2026, 6, 30)),),
            internal_gaps=((date(2026, 6, 5), date(2026, 6, 8)),),
        )
    with pytest.raises(ReleaseManifestError):
        SecurityCoveragePlan(
            symbol="000001.SZ",
            segments=(),
            exemptions=((date(2026, 6, 1), date(2026, 6, 2), "VENDOR_HOLIDAY"),),
        )
    with pytest.raises(ReleaseManifestError):
        SecurityCoveragePlan(
            symbol="600000.SH",
            segments=((date(2026, 6, 1), date(2026, 6, 30)),),
            internal_gaps=((date(2026, 5, 1), date(2026, 5, 3)),),
        )


def test_manifest_rejects_exemptions_outside_the_declared_scope() -> None:
    with pytest.raises(ReleaseManifestError):
        _manifest(exemptions=(("688981.SH", date(2026, 7, 1), date(2026, 7, 2), "SUSPENSION"),))


def test_manifest_roundtrip_preserves_coverage_semantics() -> None:
    from quantking.data.eod.release_manifest import ReleaseManifest

    manifest = _manifest(
        known_coverage=(
            ("000001.SZ", date(2026, 6, 1), date(2026, 6, 10)),
            ("000001.SZ", date(2026, 6, 20), date(2026, 6, 25)),
        ),
        exemptions=(("600000.SH", date(2026, 7, 1), date(2026, 7, 3), "SUSPENSION"),),
    )
    parsed = ReleaseManifest.from_dict(manifest.to_dict())
    assert parsed.security_coverage == manifest.security_coverage
    assert parsed.manifest_sha256() == manifest.manifest_sha256()
