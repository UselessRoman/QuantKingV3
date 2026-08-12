from __future__ import annotations

from datetime import date

from quantking.data.eod.contracts import EodPublishIntent, EodRunRequest
from quantking.data.eod.fingerprint import eod_input_fingerprint
from quantking.data.eod.frozen import FrozenEodCommand, FrozenEodDataset
from quantking.data.models import DatasetKind
from quantking.data.resolution.models import K2_RESOLUTION_RULE_VERSION, K3_RESOLUTION_RULE_VERSION

REAL_MANIFEST_SHA = "1" * 64
OTHER_MANIFEST_SHA = "2" * 64


def _command(*, resolution_rule: str, legacy_sha: str | None) -> FrozenEodCommand:
    return FrozenEodCommand(
        request=EodRunRequest(
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 10),
            datasets=("daily_bar_unadjusted",),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        ),
        request_sha256="a" * 64,
        config_sha256="b" * 64,
        code_version="s1-07k3.v1",
        adapter_version="baostock-akshare-daily-bar.k3.v1",
        rule_version="data-quality.daily-bar.s1-08.v1",
        resolution_rule_version=resolution_rule,
        source_route_version="BAO-EAST-TX-SINA.k3.v1",
        baostock_version="0.9.3",
        baostock_unit_policy_version="baostock-0.9.3-returned-units.v1",
        planner_lookback_days=0,
        adapter_context_lookback_days=15,
        max_days_per_chunk=31,
        max_items_per_chunk=1,
        endpoint_invocation_limit=16,
        c_endpoint_invocation_limit=4,
        d_endpoint_invocation_limit=4,
        estimated_http_request_limit=24,
        estimated_baostock_protocol_message_limit=12,
        legacy_import_manifest_sha256=legacy_sha,
        datasets=(
            FrozenEodDataset(
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                schema_version="daily_bar_unadjusted.v1",
                scope=("000001.SZ",),
                known_coverage=(),
                base_version=None,
                references={},
            ),
        ),
    )


def test_k3_fingerprint_is_deterministic_and_frozen_with_none_manifest_sha() -> None:
    first = eod_input_fingerprint(
        _command(resolution_rule=K3_RESOLUTION_RULE_VERSION, legacy_sha=None)
    )
    second = eod_input_fingerprint(
        _command(resolution_rule=K3_RESOLUTION_RULE_VERSION, legacy_sha=None)
    )
    assert first == second
    assert len(first) == 64


def test_real_legacy_import_manifest_sha_replaces_none_and_binds_fingerprint() -> None:
    none_fingerprint = eod_input_fingerprint(
        _command(resolution_rule=K3_RESOLUTION_RULE_VERSION, legacy_sha=None)
    )
    real_fingerprint = eod_input_fingerprint(
        _command(resolution_rule=K3_RESOLUTION_RULE_VERSION, legacy_sha=REAL_MANIFEST_SHA)
    )
    other_fingerprint = eod_input_fingerprint(
        _command(resolution_rule=K3_RESOLUTION_RULE_VERSION, legacy_sha=OTHER_MANIFEST_SHA)
    )
    assert none_fingerprint != real_fingerprint
    assert real_fingerprint != other_fingerprint
    assert (
        eod_input_fingerprint(
            _command(resolution_rule=K3_RESOLUTION_RULE_VERSION, legacy_sha=REAL_MANIFEST_SHA)
        )
        == real_fingerprint
    )


def test_k3_fingerprint_differs_from_legacy_k2_fingerprint() -> None:
    k3_fingerprint = eod_input_fingerprint(
        _command(resolution_rule=K3_RESOLUTION_RULE_VERSION, legacy_sha=REAL_MANIFEST_SHA)
    )
    k2_fingerprint = eod_input_fingerprint(
        _command(resolution_rule=K2_RESOLUTION_RULE_VERSION, legacy_sha=REAL_MANIFEST_SHA)
    )
    assert k3_fingerprint != k2_fingerprint
    assert eod_input_fingerprint(
        _command(resolution_rule=K2_RESOLUTION_RULE_VERSION, legacy_sha=None)
    ) == eod_input_fingerprint(
        _command(resolution_rule=K2_RESOLUTION_RULE_VERSION, legacy_sha=REAL_MANIFEST_SHA)
    )
