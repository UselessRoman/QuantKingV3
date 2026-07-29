"""Exact quality-rule catalogue checks for validation evidence."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from quantking.data.models import DatasetKind
from quantking.data.quality.rules import default_rule_set


def expected_quality_rule_codes(dataset: DatasetKind) -> tuple[str, ...]:
    return tuple(rule.code for rule in default_rule_set().for_dataset(dataset))


def validate_quality_rule_catalog(
    *,
    dataset: DatasetKind,
    rule_version: str,
    result_rule_codes: Sequence[str],
) -> bool:
    rules = default_rule_set()
    expected = expected_quality_rule_codes(dataset)
    actual = tuple(result_rule_codes)
    return (
        rule_version == rules.rule_version
        and len(actual) == len(expected)
        and Counter(actual) == Counter(expected)
        and len(set(actual)) == len(actual)
    )


__all__ = ["expected_quality_rule_codes", "validate_quality_rule_catalog"]
