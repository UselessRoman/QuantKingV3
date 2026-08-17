"""S1-08R2 B1/B2/B4/B5: frozen release manifest contract tests."""

from __future__ import annotations

import json
from datetime import date

import pytest

from quantking.data.eod.release_manifest import (
    K4D_INVENTORY_EVIDENCE_SHA256,
    K4D_PILOT_ATTEMPT2_DIAGNOSTIC_SHA256,
    RELEASE_SAFETY_CAPS,
    ReleaseManifest,
    ReleaseManifestError,
    build_release_manifest,
    build_zero_network_dry_run,
    symbols_sha256,
)

_SYMBOLS = ("000001.SZ", "000858.SZ", "300750.SZ", "600000.SH", "600519.SH", "688981.SH")


def _manifest(
    *,
    symbols: tuple[str, ...] = _SYMBOLS,
    target_start: date = date(2026, 6, 11),
    target_end: date = date(2026, 8, 9),
) -> ReleaseManifest:
    return build_release_manifest(
        symbols=symbols,
        target_start=target_start,
        target_end=target_end,
        lookback_days=15,
        calendar_version_id="exchange-calendar-baostock.v1",
        calendar_sha256="a" * 64,
        source_route_version="BAO-TX-SINA.k4.v1",
        adapter_version="baostock-akshare-daily-bar.k4.v1",
        code_version="s1-08r2.v1",
        quality_rule_version="quality-rules.daily-bar-unadjusted.k4.v1",
        threshold_rule_version="comparison-thresholds.daily-bar-unadjusted.k4.v1",
        resolution_rule_version="source-resolution.daily-bar-unadjusted.k4.v1",
        unit_policy_versions={
            "baostock": "baostock-unadjusted-units.v1",
            "akshare": "akshare-1.18.83-returned-units.v1",
        },
    )


def test_manifest_binds_complete_scope_budgets_and_lineage() -> None:
    manifest = _manifest()
    payload = manifest.to_dict()

    assert payload["dataset"] == "daily_bar_unadjusted"
    assert payload["canonical_schema"] == "daily_bar_unadjusted.v1"
    assert payload["symbols"] == list(_SYMBOLS)
    assert payload["symbols_sha256"] == symbols_sha256(_SYMBOLS)
    assert payload["target_window"] == {"start": "2026-06-11", "end": "2026-08-09"}
    assert payload["upstream_window"] == {"start": "2026-05-27", "end": "2026-08-09"}
    assert payload["lookback_days"] == 15
    assert payload["calendar"] == {
        "version_id": "exchange-calendar-baostock.v1",
        "sha256": "a" * 64,
    }
    assert payload["sources"]["roles"] == {
        "A": "BAOSTOCK/BAOSTOCK",
        "B": "AKSHARE/TENCENT",
        "C": "AKSHARE/SINA",
    }
    assert payload["publish_intent"] == "PUBLISH_AFTER_QUALITY"
    # 6 symbols x 2 chunks = 12 chunks per role; 3 roles.
    assert len(payload["a_chunks"]) == 12
    assert payload["a_chunks"] == payload["b_chunks"] == payload["c_max_chunks"]
    assert payload["budgets"] == {
        "a_baostock_endpoints": 12,
        "b_tencent_endpoints": 12,
        "c_sina_max_endpoints": 12,
        "endpoint_total": 36,
        "akshare_http_max": 12 * 3 + 12 * 2,
        "baostock_protocol_max": 36,
    }
    assert payload["lineage_caps"]["raw_batches"] == 36
    assert payload["lineage_caps"]["published_versions"] == 1
    assert payload["would_write_directories"] == list(manifest.would_write_directories)


def test_manifest_binds_the_frozen_adr027_legacy_rejection() -> None:
    payload = _manifest().to_dict()
    legacy = payload["legacy"]

    assert legacy["status"] == "REJECTED"
    assert legacy["adr"] == "ADR-027"
    assert legacy["evidence_sha256"] == [
        K4D_INVENTORY_EVIDENCE_SHA256,
        K4D_PILOT_ATTEMPT2_DIAGNOSTIC_SHA256,
    ]
    assert legacy["included_batches"] == 0
    assert legacy["included_lineage"] == 0
    assert legacy["published_count"] == 0
    assert legacy["import_manifest_sha256"] is None
    assert "XTQUANT" not in json.dumps(payload["sources"])


def test_manifest_cannot_raise_the_independent_safety_caps() -> None:
    too_many_symbols = tuple(
        f"{index:06d}.SZ" for index in range(1, RELEASE_SAFETY_CAPS["symbols"] + 2)
    )
    with pytest.raises(ReleaseManifestError):
        _manifest(symbols=too_many_symbols)

    # 64 symbols over a long window exceed chunks_per_role even though each
    # individual value stays small: the cap must bind the aggregate plan.
    sixty_four = tuple(f"{index:06d}.SZ" for index in range(1, 65))
    with pytest.raises(ReleaseManifestError):
        _manifest(symbols=sixty_four, target_start=date(2025, 1, 1))


def test_manifest_parse_fails_closed_on_tampering() -> None:
    manifest = _manifest()

    duplicated = manifest.to_dict()
    duplicated["symbols"] = [*duplicated["symbols"], duplicated["symbols"][0]]
    with pytest.raises(ReleaseManifestError):
        ReleaseManifest.from_dict(duplicated)

    misordered = manifest.to_dict()
    misordered["symbols"] = list(reversed(misordered["symbols"]))
    with pytest.raises(ReleaseManifestError):
        ReleaseManifest.from_dict(misordered)

    wrong_sha = manifest.to_dict()
    wrong_sha["symbols_sha256"] = "b" * 64
    with pytest.raises(ReleaseManifestError):
        ReleaseManifest.from_dict(wrong_sha)

    missing = manifest.to_dict()
    del missing["calendar"]
    with pytest.raises(ReleaseManifestError):
        ReleaseManifest.from_dict(missing)

    over_budget = manifest.to_dict()
    over_budget["budgets"]["endpoint_total"] = RELEASE_SAFETY_CAPS["endpoint_invocations_total"] + 1
    with pytest.raises(ReleaseManifestError):
        ReleaseManifest.from_dict(over_budget)

    tampered_chunk = manifest.to_dict()
    tampered_chunk["a_chunks"][0]["target_end"] = "2027-01-01"
    with pytest.raises(ReleaseManifestError):
        ReleaseManifest.from_dict(tampered_chunk)

    resurrected_legacy = manifest.to_dict()
    resurrected_legacy["legacy"]["status"] = "INCLUDED"
    with pytest.raises(ReleaseManifestError):
        ReleaseManifest.from_dict(resurrected_legacy)


def test_execution_config_from_manifest_binds_tight_budgets() -> None:
    from quantking.data.eod.contracts import R2_EOD_CONFIG_SCHEMA
    from quantking.data.eod.service import EodExecutionConfig

    manifest = _manifest(symbols=("000001.SZ",))
    config = EodExecutionConfig.from_release_manifest(manifest)

    assert config.is_r2
    assert config.release_manifest_sha256 == manifest.manifest_sha256()
    assert config.endpoint_invocation_limit == manifest.budgets["endpoint_total"]
    assert config.c_endpoint_invocation_limit == manifest.budgets["c_sina_max_endpoints"]
    rendered = config.to_dict(selected=("daily_bar_unadjusted",), bases={})
    assert rendered["schema_version"] == R2_EOD_CONFIG_SCHEMA
    assert rendered["release_manifest_sha256"] == manifest.manifest_sha256()


def test_dry_run_is_deterministic_and_byte_stable() -> None:
    manifest = _manifest()
    first = build_zero_network_dry_run(manifest, data_root_label="E:\\QuantKingV3Data")
    second = build_zero_network_dry_run(manifest, data_root_label="E:\\QuantKingV3Data")

    first_bytes = json.dumps(first, ensure_ascii=False, sort_keys=True).encode("utf-8")
    second_bytes = json.dumps(second, ensure_ascii=False, sort_keys=True).encode("utf-8")
    assert first_bytes == second_bytes
    assert first["release_manifest_sha256"] == manifest.manifest_sha256()
    assert first["requires_new_real_authorization"] is True
    assert first["network_invocations"] == 0
    assert first["writes_data_root"] is False
    assert first["reads_legacy_root"] is False
    assert first["legacy_disposition"]["status"] == "REJECTED"
    assert first["network_budget"]["application_retries"] == 0
    assert "warehouse/daily_bar_unadjusted/<version_id>" in first["would_write_directories"]

    different = build_zero_network_dry_run(
        build_release_manifest(
            symbols=_SYMBOLS,
            target_start=date(2026, 6, 11),
            target_end=date(2026, 8, 9),
            lookback_days=15,
            calendar_version_id="exchange-calendar-baostock.v1",
            calendar_sha256="a" * 64,
            source_route_version="BAO-TX-SINA.k4.v1",
            adapter_version="baostock-akshare-daily-bar.k4.v1",
            code_version="s1-08r2-different.v1",
            quality_rule_version="quality-rules.daily-bar-unadjusted.k4.v1",
            threshold_rule_version="comparison-thresholds.daily-bar-unadjusted.k4.v1",
            resolution_rule_version="source-resolution.daily-bar-unadjusted.k4.v1",
            unit_policy_versions={"baostock": "b"},
        ),
        data_root_label="E:\\QuantKingV3Data",
    )
    assert different["release_manifest_sha256"] != first["release_manifest_sha256"]
