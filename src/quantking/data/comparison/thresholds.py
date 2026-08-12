"""Versioned, field-level comparison thresholds (S1-04B).

Thresholds are explicit, versioned configuration — no magic constants.
Each numeric field has its own ``absolute`` and/or ``relative`` bound;
strings, dates, booleans and status fields use exact equality (no
threshold). The comparator looks up the per-field threshold by dataset
and field name.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from types import MappingProxyType

from quantking.data.models import DatasetKind

THRESHOLD_RULE_VERSION = "comparison-thresholds.daily-bar-unadjusted.k1.v1"
K3_CANDIDATE_RULE_VERSION = "comparison-thresholds.daily-bar-unadjusted.k3-candidate.v1"
_RELATIVE_BASE_FLOOR = Decimal("0.000000000001")
_EVIDENCE_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)


def decimal_from_number(value: int | float | Decimal, *, field: str) -> Decimal:
    """Convert a Canonical numeric value without importing binary-float noise."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError(f"{field} must be an int, float, or Decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{field} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return result


def canonical_decimal(value: Decimal) -> str:
    """Render one finite Decimal as a unique non-exponent evidence string."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("value must be a finite Decimal")
    if value == 0:
        return "0"
    with localcontext(_EVIDENCE_CONTEXT):
        rendered = format(value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def decimal_difference(
    primary: int | float | Decimal,
    fallback: int | float | Decimal,
) -> tuple[Decimal, Decimal]:
    """Return deterministic absolute/relative evidence under a frozen context."""
    primary_decimal = decimal_from_number(primary, field="primary")
    fallback_decimal = decimal_from_number(fallback, field="fallback")
    with localcontext(_EVIDENCE_CONTEXT):
        difference = abs(primary_decimal - fallback_decimal)
        base = max(abs(primary_decimal), abs(fallback_decimal), _RELATIVE_BASE_FLOOR)
        relative = difference / base
    return difference, relative


@dataclass(frozen=True, slots=True)
class ThresholdSpec:
    """One field-level numeric threshold.

    A difference is recorded when ``abs(primary - fallback)`` exceeds
    ``absolute`` AND, when ``relative`` is positive, the relative
    difference also exceeds ``relative``. Either bound may be zero to
    disable that axis; both zero means any numeric difference is a
    difference (effectively exact equality for floats).
    """

    absolute: Decimal
    relative: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.absolute, Decimal):
            raise TypeError("absolute must be a Decimal")
        if not isinstance(self.relative, Decimal):
            raise TypeError("relative must be a Decimal")
        if not self.absolute.is_finite() or not self.relative.is_finite():
            raise ValueError("thresholds must be finite")
        if self.absolute < 0:
            raise ValueError("absolute must be non-negative")
        if self.relative < 0:
            raise ValueError("relative must be non-negative")

    def exceeds(
        self,
        primary: int | float | Decimal,
        fallback: int | float | Decimal,
    ) -> bool:
        """Return True when the difference between the two values exceeds the threshold."""
        diff, relative_difference = decimal_difference(primary, fallback)
        with localcontext(_EVIDENCE_CONTEXT):
            if self.absolute > 0 and diff <= self.absolute:
                return False
            if self.relative > 0 and relative_difference <= self.relative:
                return False
            # If both bounds are zero, any non-zero difference is a difference.
            if self.absolute > 0:
                return diff > self.absolute
            return diff > 0


@dataclass(frozen=True, slots=True)
class ThresholdSet:
    """A versioned set of per-dataset, per-field thresholds."""

    rule_version: str
    thresholds: Mapping[DatasetKind, Mapping[str, ThresholdSpec]]

    def __post_init__(self) -> None:
        if not isinstance(self.rule_version, str) or not self.rule_version.strip():
            raise ValueError("rule_version must be non-empty text")
        if not isinstance(self.thresholds, Mapping):
            raise TypeError("thresholds must be a mapping")
        normalized: dict[DatasetKind, Mapping[str, ThresholdSpec]] = {}
        for dataset, field_map in self.thresholds.items():
            if not isinstance(dataset, DatasetKind):
                raise TypeError("thresholds key must be a DatasetKind")
            if not isinstance(field_map, Mapping):
                raise TypeError("thresholds value must be a mapping")
            normalized_fields: dict[str, ThresholdSpec] = {}
            for field_name, spec in field_map.items():
                if not isinstance(field_name, str) or not field_name.strip():
                    raise ValueError("field name must be non-empty text")
                if not isinstance(spec, ThresholdSpec):
                    raise TypeError("threshold value must be a ThresholdSpec")
                normalized_fields[field_name] = spec
            normalized[dataset] = MappingProxyType(dict(sorted(normalized_fields.items())))
        object.__setattr__(self, "thresholds", MappingProxyType(dict(sorted(normalized.items()))))

    def for_dataset(self, dataset: DatasetKind) -> Mapping[str, ThresholdSpec]:
        """Return the only K2 production threshold map."""
        if dataset is not DatasetKind.DAILY_BAR_UNADJUSTED:
            raise ValueError("K2 thresholds support daily_bar_unadjusted only")
        return self.thresholds[dataset]


@dataclass(frozen=True, slots=True)
class ThresholdEvidenceCandidate:
    """A non-production rule proposal awaiting representative real evidence."""

    thresholds: ThresholdSet
    active: bool
    evidence_status: str
    activation_requirements: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.thresholds.rule_version != K3_CANDIDATE_RULE_VERSION:
            raise ValueError("candidate must use the fixed K3 candidate rule version")
        if self.active:
            raise ValueError("K3 candidate cannot be activated without a new evidence-backed rule")
        if self.evidence_status != "REPRESENTATIVE_REAL_EVIDENCE_MISSING":
            raise ValueError("K3 candidate evidence status is invalid")
        if not self.activation_requirements:
            raise ValueError("activation requirements must be non-empty")


def default_thresholds() -> ThresholdSet:
    """Return the K1-evidence-backed exact daily-bar thresholds."""
    return ThresholdSet(
        rule_version=THRESHOLD_RULE_VERSION,
        thresholds={
            DatasetKind.DAILY_BAR_UNADJUSTED: {
                "open": ThresholdSpec(absolute=Decimal("0"), relative=Decimal("0")),
                "high": ThresholdSpec(absolute=Decimal("0"), relative=Decimal("0")),
                "low": ThresholdSpec(absolute=Decimal("0"), relative=Decimal("0")),
                "close": ThresholdSpec(absolute=Decimal("0"), relative=Decimal("0")),
                "pre_close": ThresholdSpec(absolute=Decimal("0"), relative=Decimal("0")),
                "volume": ThresholdSpec(absolute=Decimal("0"), relative=Decimal("0")),
                "amount": ThresholdSpec(absolute=Decimal("0"), relative=Decimal("0")),
            },
        },
    )


def k3_evidence_candidate_thresholds() -> ThresholdSet:
    """Return a zero-only, non-production set for decimal artifact evidence tests.

    This is deliberately not returned by ``default_thresholds`` and is not
    referenced by the EOD fingerprint. Representative real evidence is still
    required before any K3 production rule can be created.
    """
    candidate = default_thresholds()
    return ThresholdSet(
        rule_version=K3_CANDIDATE_RULE_VERSION,
        thresholds=candidate.thresholds,
    )


def k3_threshold_candidate_contract() -> ThresholdEvidenceCandidate:
    return ThresholdEvidenceCandidate(
        thresholds=k3_evidence_candidate_thresholds(),
        active=False,
        evidence_status="REPRESENTATIVE_REAL_EVIDENCE_MISSING",
        activation_requirements=(
            "shanghai_and_shenzhen_code_segments",
            "corporate_action_day",
            "suspension_day",
            "st_security",
            "low_volume_and_amount",
            "near_observed_volume_amount_boundaries",
            "missing_or_empty_upstream",
        ),
    )


def uses_decimal_evidence(rule_version: str) -> bool:
    return rule_version == K3_CANDIDATE_RULE_VERSION


__all__ = [
    "K3_CANDIDATE_RULE_VERSION",
    "THRESHOLD_RULE_VERSION",
    "ThresholdEvidenceCandidate",
    "ThresholdSet",
    "ThresholdSpec",
    "canonical_decimal",
    "decimal_difference",
    "decimal_from_number",
    "default_thresholds",
    "k3_evidence_candidate_thresholds",
    "k3_threshold_candidate_contract",
    "uses_decimal_evidence",
]
