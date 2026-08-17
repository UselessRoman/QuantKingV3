"""Public deterministic K2/K3/K4 input fingerprint for persistence and verification.

Historical K2/K3 fingerprints remain byte-reproducible so frozen Jobs and
immutable evidence can be re-verified; new Jobs can only be created with the
K4 three-role route, which additionally binds the AKShare returned-unit
policy version.
"""

from __future__ import annotations

import hashlib

from quantking.data.comparison.thresholds import K4_THRESHOLD_RULE_VERSION, THRESHOLD_RULE_VERSION
from quantking.data.eod.contracts import canonical_json
from quantking.data.eod.frozen import FrozenEodCommand
from quantking.data.resolution.models import (
    K2_RESOLUTION_RULE_VERSION,
    K3_RESOLUTION_RULE_VERSION,
    K4_RESOLUTION_RULE_VERSION,
)


def _fingerprint_schema(resolution_rule_version: str) -> str:
    if resolution_rule_version == K2_RESOLUTION_RULE_VERSION:
        return "eod-input-fingerprint.k2.v1"
    if resolution_rule_version == K3_RESOLUTION_RULE_VERSION:
        return "eod-input-fingerprint.k3.v1"
    if resolution_rule_version == K4_RESOLUTION_RULE_VERSION:
        return "eod-input-fingerprint.k4.v1"
    raise ValueError("resolution_rule_version is unsupported")


def eod_input_fingerprint(command: FrozenEodCommand) -> str:
    """Bind the complete frozen request/config/rule/scope facts."""
    if not isinstance(command, FrozenEodCommand):
        raise TypeError("command must be a FrozenEodCommand")
    payload: dict[str, object] = {
        "schema_version": _fingerprint_schema(command.resolution_rule_version),
        "request_sha256": command.request_sha256,
        "config_sha256": command.config_sha256,
        "code_version": command.code_version,
        "adapter_version": command.adapter_version,
        "quality_rule_version": command.rule_version,
        "threshold_rule_version": (
            K4_THRESHOLD_RULE_VERSION
            if command.resolution_rule_version == K4_RESOLUTION_RULE_VERSION
            else THRESHOLD_RULE_VERSION
        ),
        "resolution_rule_version": command.resolution_rule_version,
        "scope": [
            {
                "dataset": plan.dataset.value,
                "symbols": list(plan.scope),
                "start_date": command.request.start_date.isoformat(),
                "end_date": command.request.end_date.isoformat(),
                "base_version_id": (
                    None if plan.base_version is None else plan.base_version.version_id
                ),
            }
            for plan in command.datasets
        ],
    }
    if command.resolution_rule_version != K2_RESOLUTION_RULE_VERSION:
        payload.update(
            {
                "source_route_version": command.source_route_version,
                "baostock_version": command.baostock_version,
                "baostock_unit_policy_version": command.baostock_unit_policy_version,
                "legacy_import_manifest_sha256": command.legacy_import_manifest_sha256,
            }
        )
    if command.resolution_rule_version == K4_RESOLUTION_RULE_VERSION:
        payload["akshare_unit_policy_version"] = command.akshare_unit_policy_version
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = ["eod_input_fingerprint"]
