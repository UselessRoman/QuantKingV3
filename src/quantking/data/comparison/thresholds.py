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
from types import MappingProxyType

from quantking.data.models import DatasetKind

THRESHOLD_RULE_VERSION = "comparison-thresholds.daily-bar-unadjusted.k1.v1"


@dataclass(frozen=True, slots=True)
class ThresholdSpec:
    """One field-level numeric threshold.

    A difference is recorded when ``abs(primary - fallback)`` exceeds
    ``absolute`` AND, when ``relative`` is positive, the relative
    difference also exceeds ``relative``. Either bound may be zero to
    disable that axis; both zero means any numeric difference is a
    difference (effectively exact equality for floats).
    """

    absolute: float
    relative: float

    def __post_init__(self) -> None:
        if not isinstance(self.absolute, (int, float)) or isinstance(self.absolute, bool):
            raise TypeError("absolute must be a number")
        if not isinstance(self.relative, (int, float)) or isinstance(self.relative, bool):
            raise TypeError("relative must be a number")
        if self.absolute < 0:
            raise ValueError("absolute must be non-negative")
        if self.relative < 0:
            raise ValueError("relative must be non-negative")
        object.__setattr__(self, "absolute", float(self.absolute))
        object.__setattr__(self, "relative", float(self.relative))

    def exceeds(self, primary: float, fallback: float) -> bool:
        """Return True when the difference between the two values exceeds the threshold."""
        diff = abs(primary - fallback)
        if self.absolute > 0 and diff <= self.absolute:
            return False
        if self.relative > 0:
            base = max(abs(primary), abs(fallback), 1e-12)
            if (diff / base) <= self.relative:
                return False
        # If both bounds are zero, any non-zero difference is a difference.
        # If only absolute is set (relative == 0), diff > absolute is required.
        # If only relative is set (absolute == 0), relative check above applies.
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


def default_thresholds() -> ThresholdSet:
    """Return the K1-evidence-backed exact daily-bar thresholds."""
    return ThresholdSet(
        rule_version=THRESHOLD_RULE_VERSION,
        thresholds={
            DatasetKind.DAILY_BAR_UNADJUSTED: {
                "open": ThresholdSpec(absolute=0.0, relative=0.0),
                "high": ThresholdSpec(absolute=0.0, relative=0.0),
                "low": ThresholdSpec(absolute=0.0, relative=0.0),
                "close": ThresholdSpec(absolute=0.0, relative=0.0),
                "pre_close": ThresholdSpec(absolute=0.0, relative=0.0),
                "volume": ThresholdSpec(absolute=0.0, relative=0.0),
                "amount": ThresholdSpec(absolute=0.0, relative=0.0),
            },
        },
    )


__all__ = [
    "THRESHOLD_RULE_VERSION",
    "ThresholdSet",
    "ThresholdSpec",
    "default_thresholds",
]
