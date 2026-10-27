"""Offline research contract qualification with independent boundary outcomes."""

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from quantking.common.errors import InvalidTimeError
from quantking.common.ids import EntityId
from quantking.common.time import UtcTimestamp
from quantking.data.models import ManifestFileEntry
from quantking.research.contracts import (
    AdjustmentFactorDaily,
    CorporateAction,
    CorporateActionType,
    ParentArtifact,
    ResearchArtifactManifest,
    ResearchArtifactType,
    ResearchContractError,
    ResearchRules,
    ResearchScope,
    RiskStatus,
    SecurityLifecycle,
    SecurityStatusDaily,
    SourceFact,
    validate_reference_rows,
)

ROOT = Path(__file__).parents[2]
DAY = date(2026, 1, 9)
AS_OF = UtcTimestamp.from_iso("2026-01-09T07:00:00Z")
SOURCE = SourceFact("synthetic", "v1", "a" * 64, AS_OF, AS_OF)


def test_reference_knowledge_is_independent_from_effective_date() -> None:
    row = SecurityStatusDaily("000001.SZ", DAY, RiskStatus.NORMAL, False, SOURCE)
    validate_reference_rows((row,), as_of=AS_OF)
    later = UtcTimestamp.from_iso("2026-01-10T07:00:00Z")
    future = replace(row, source=replace(SOURCE, available_at=later, observed_at=later))
    with pytest.raises(ResearchContractError, match="FUTURE_KNOWLEDGE"):
        validate_reference_rows((future,), as_of=AS_OF)
    assert row.risk_status is RiskStatus.NORMAL


def test_reference_missing_duplicate_and_conflicting_rows_fail_closed() -> None:
    row = SecurityStatusDaily("000001.SZ", DAY, RiskStatus.NORMAL, False, SOURCE)
    for rows in ((), (row, row), (row, replace(row, risk_status=RiskStatus.ST))):
        with pytest.raises(ResearchContractError):
            validate_reference_rows(rows, as_of=AS_OF)
    lifecycle = SecurityLifecycle("000001.SZ", date(1991, 4, 3), None, SOURCE)
    with pytest.raises(ResearchContractError, match="DUPLICATE_OR_CONFLICTING"):
        validate_reference_rows((lifecycle, replace(lifecycle, listed_on=DAY)), as_of=AS_OF)


@pytest.mark.parametrize("factor", ["0", "-1", "NaN", "Infinity"])
def test_illegal_adjustment_factor_is_rejected(factor: str) -> None:
    with pytest.raises(ResearchContractError):
        AdjustmentFactorDaily("000001.SZ", DAY, DAY, Decimal(factor), SOURCE)


def test_schema_rejects_unknown_status_and_future_anchor() -> None:
    with pytest.raises(ResearchContractError, match="UNKNOWN_RISK_STATUS"):
        SecurityStatusDaily("000001.SZ", DAY, "NORMAL", False, SOURCE)  # type: ignore[arg-type]
    with pytest.raises(ResearchContractError, match="UNKNOWN_SUSPENSION"):
        SecurityStatusDaily("000001.SZ", DAY, RiskStatus.NORMAL, None, SOURCE)  # type: ignore[arg-type]
    with pytest.raises(ResearchContractError, match="FUTURE_FACTOR_ANCHOR"):
        AdjustmentFactorDaily("000001.SZ", DAY, date(2026, 1, 12), Decimal(1), SOURCE)
    with pytest.raises(ResearchContractError, match="INVALID_LIFECYCLE"):
        SecurityLifecycle("000001.SZ", DAY, DAY, SOURCE)


def test_factor_is_normalized_on_its_anchor_day() -> None:
    with pytest.raises(ResearchContractError, match="FACTOR_ANCHOR_NOT_NORMALIZED"):
        AdjustmentFactorDaily("000001.SZ", DAY, DAY, Decimal(2), SOURCE)


def test_factor_anchor_cannot_change_within_one_security() -> None:
    first = AdjustmentFactorDaily("000001.SZ", DAY, DAY, Decimal(1), SOURCE)
    next_day = date(2026, 1, 12)
    changed = AdjustmentFactorDaily("000001.SZ", next_day, next_day, Decimal(1), SOURCE)
    with pytest.raises(ResearchContractError, match="CONFLICTING_FACTOR_ANCHOR"):
        validate_reference_rows((first, changed), as_of=AS_OF)
    # A later factor may change, while its anchor remains fixed. Other securities
    # have their own anchors; row order must not change the validation outcome.
    stable = replace(changed, anchor_date=DAY, cumulative_factor=Decimal(2))
    other = replace(changed, code="600000.SH")
    validate_reference_rows((other, stable, first), as_of=AS_OF)


def test_source_timestamp_requires_timezone_and_observation_order() -> None:
    with pytest.raises(InvalidTimeError):
        UtcTimestamp.from_iso("2026-01-09T07:00:00")
    earlier = UtcTimestamp.from_iso("2026-01-08T07:00:00Z")
    with pytest.raises(ResearchContractError, match="OBSERVATION_TIME"):
        replace(SOURCE, observed_at=earlier)


def test_rules_are_frozen_with_numeric_canonicalization() -> None:
    payload = json.loads((ROOT / "configs/research_rules.v1.json").read_text())
    payload["liquidity_median_cny"] = Decimal(payload["liquidity_median_cny"])
    rules = ResearchRules(**payload)
    assert (
        rules.fingerprint()
        == replace(rules, liquidity_median_cny=Decimal("10000000.00")).fingerprint()
    )
    with pytest.raises(ResearchContractError, match="RULE_VERSION_MISMATCH"):
        replace(rules, minimum_listing_sessions=119)


def _manifest() -> ResearchArtifactManifest:
    scope = ResearchScope(
        ("000001.SZ",),
        (date(2026, 1, 8), DAY, date(2026, 1, 12)),
        DAY,
        DAY,
        EntityId.parse("data_version:083d7fb7-e268-45a6-8c19-19e144f2d498"),
        "b" * 64,
    )
    return ResearchArtifactManifest(
        ResearchArtifactType.RESEARCH_INPUT,
        EntityId.parse("research_version:00000000-0000-4000-8000-000000000001"),
        (
            ParentArtifact(
                EntityId.parse("data_version:f913f915-0a73-476a-9513-ebdca087dd90"), "c" * 64
            ),
        ),
        scope,
        "d" * 64,
        (ManifestFileEntry("rows.json", 2, "e" * 64),),
        "synthetic-code-v1",
        "f" * 64,
        (),
    )


def test_manifest_fingerprint_binds_each_material_input() -> None:
    manifest = _manifest()
    candidates = (
        replace(manifest, gaps=("EXECUTION_FACTS_MISSING",)),
        replace(manifest, rules_sha256="0" * 64),
        replace(manifest, code_version="synthetic-code-v2"),
        replace(manifest, dependency_lock_sha256="1" * 64),
        replace(manifest, parents=(replace(manifest.parents[0], manifest_sha256="2" * 64),)),
        replace(manifest, scope=replace(manifest.scope, codes=("600000.SH",))),
        replace(manifest, files=(ManifestFileEntry("rows.json", 3, "3" * 64),)),
        replace(manifest, artifact_type=ResearchArtifactType.LABEL),
    )
    assert len({c.fingerprint() for c in (manifest, *candidates)}) == 9
    second_attempt = replace(
        manifest, version_id=EntityId.parse("research_version:00000000-0000-4000-8000-000000000002")
    )
    assert second_attempt.content_sha256() == manifest.content_sha256()
    assert second_attempt.fingerprint() != manifest.fingerprint()


def test_manifest_and_scope_reject_ambiguous_inputs() -> None:
    manifest = _manifest()
    with pytest.raises(ResearchContractError):
        replace(manifest, parents=())
    with pytest.raises(ResearchContractError):
        replace(manifest, parents=manifest.parents * 2)
    with pytest.raises(ResearchContractError):
        replace(manifest, files=manifest.files * 2)
    with pytest.raises(ResearchContractError):
        replace(manifest.scope, sessions=manifest.scope.sessions[::-1])
    with pytest.raises(ResearchContractError):
        replace(manifest.scope, signal_end=date(2026, 2, 1))
    with pytest.raises(ValueError):
        ManifestFileEntry("../escape", 0, "a" * 64)


def test_corporate_action_gaps_are_explicit_and_cannot_be_inferred_from_factor() -> None:
    cash = CorporateAction(
        "event-1",
        "000001.SZ",
        CorporateActionType.CASH_DIVIDEND,
        AS_OF,
        DAY,
        date(2026, 1, 12),
        Decimal(1),
        None,
        None,
        None,
        None,
        None,
        SOURCE,
    )
    assert cash.execution_gaps() == ("cash_pay_date", "reference_price", "tax_policy")
    complete = replace(
        cash,
        cash_pay_date=date(2026, 1, 14),
        reference_price=Decimal(4),
        tax_policy="SYNTHETIC_ZERO_TAX",
    )
    assert complete.execution_gaps() == ()
    with pytest.raises(ResearchContractError, match="ACTION_UNIT_CONFLICT"):
        replace(cash, shares_per_share=Decimal("0.5"))
    with pytest.raises(ResearchContractError, match="INVALID_SETTLEMENT_DATE"):
        replace(cash, cash_pay_date=date(2026, 1, 8))
