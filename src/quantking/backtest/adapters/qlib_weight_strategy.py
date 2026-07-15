"""Probe-only Qlib adapter for precomputed synthetic target weights."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any

import pandas as pd  # type: ignore[import-untyped]
from qlib.contrib.strategy.signal_strategy import (  # type: ignore[import-untyped]
    WeightStrategyBase,
)

from quantking.common.compatibility import (
    CompatibilityProbeError,
    ProbeErrorCode,
)


@dataclass(frozen=True, slots=True, init=False)
class SyntheticTargetWeights:
    """Deeply immutable date-to-weight input used only by the Stage 0 probe."""

    by_date: Mapping[date, Mapping[str, float]]

    def __init__(self, by_date: Mapping[date, Mapping[str, float]]) -> None:
        if not by_date:
            raise CompatibilityProbeError(
                ProbeErrorCode.INVALID_INPUT,
                "synthetic target weights require at least one trade date",
            )
        normalized: dict[date, Mapping[str, float]] = {}
        for trade_date in by_date:
            if not isinstance(trade_date, date) or isinstance(trade_date, datetime):
                raise CompatibilityProbeError(
                    ProbeErrorCode.INVALID_INPUT,
                    "synthetic target weight keys must be trading dates",
                )
        for trade_date, weights in sorted(by_date.items(), key=lambda item: item[0]):
            if not weights:
                raise CompatibilityProbeError(
                    ProbeErrorCode.INVALID_INPUT,
                    "synthetic target weight rows cannot be empty",
                )
            normalized_weights: dict[str, float] = {}
            for instrument in weights:
                if not isinstance(instrument, str) or not instrument.strip():
                    raise CompatibilityProbeError(
                        ProbeErrorCode.INVALID_INPUT,
                        "synthetic target instruments must be non-empty text",
                    )
            for instrument, weight in sorted(weights.items()):
                if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                    raise CompatibilityProbeError(
                        ProbeErrorCode.INVALID_INPUT,
                        "synthetic target weights must be finite numbers",
                    )
                value = float(weight)
                if not math.isfinite(value) or value < 0.0 or value > 1.0:
                    raise CompatibilityProbeError(
                        ProbeErrorCode.INVALID_INPUT,
                        "synthetic target weights must be within zero and one",
                    )
                normalized_weights[instrument] = value
            if sum(normalized_weights.values()) > 1.0 + 1e-12:
                raise CompatibilityProbeError(
                    ProbeErrorCode.INVALID_INPUT,
                    "synthetic target weight sum cannot exceed one",
                )
            normalized[trade_date] = MappingProxyType(normalized_weights)
        object.__setattr__(self, "by_date", MappingProxyType(normalized))

    def for_date(self, trade_date: date) -> dict[str, float]:
        """Return a Qlib-compatible copy for one exact trade date."""
        try:
            return dict(self.by_date[trade_date])
        except KeyError as error:
            raise CompatibilityProbeError(
                ProbeErrorCode.INVALID_INPUT,
                "synthetic target weights are missing for trade date",
            ) from error


class SyntheticTargetWeightStrategy(WeightStrategyBase):  # type: ignore[misc]
    """Return precomputed weights verbatim; model scores are intentionally ignored."""

    def __init__(self, targets: SyntheticTargetWeights) -> None:
        self._targets = targets
        signal_index = pd.MultiIndex.from_tuples(
            [("000001.SZ", pd.Timestamp("2026-01-02"))],
            names=("instrument", "datetime"),
        )
        super().__init__(signal=pd.Series([0.0], index=signal_index, dtype="float64"))

    def generate_target_weight_position(
        self,
        score: Any,
        current: Any,
        trade_start_time: Any,
        trade_end_time: Any,
    ) -> dict[str, float]:
        """Implement pyqlib 0.9.7's locked five-argument weight hook."""
        return self._targets.for_date(_as_trade_date(trade_start_time))


def _as_trade_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    date_method = getattr(value, "date", None)
    if callable(date_method):
        converted = date_method()
        if isinstance(converted, date):
            return converted
    raise CompatibilityProbeError(
        ProbeErrorCode.INVALID_INPUT,
        "Qlib trade start time does not provide a trading date",
    )
