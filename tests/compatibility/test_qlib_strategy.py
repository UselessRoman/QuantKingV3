from __future__ import annotations

import ast
import inspect
from datetime import date, datetime
from textwrap import dedent

import pytest

from quantking.backtest.adapters.qlib_weight_strategy import (
    SyntheticTargetWeights,
    SyntheticTargetWeightStrategy,
)
from quantking.common.compatibility import CompatibilityProbeError, ProbeErrorCode


def _strategy() -> SyntheticTargetWeightStrategy:
    targets = SyntheticTargetWeights(
        {
            date(2026, 1, 5): {"000001.SZ": 0.6, "600000.SH": 0.3},
            date(2026, 1, 6): {"000001.SZ": 0.4},
        }
    )
    return SyntheticTargetWeightStrategy(targets)


@pytest.mark.parametrize(
    "targets",
    (
        {},
        {date(2026, 1, 5): {}},
        {date(2026, 1, 5): {"000001.SZ": -0.1}},
        {date(2026, 1, 5): {"000001.SZ": 1.01}},
        {date(2026, 1, 5): {"000001.SZ": 0.8, "600000.SH": 0.4}},
        {date(2026, 1, 5): {"": 0.5}},
    ),
)
def test_synthetic_target_weights_reject_illegal_inputs(
    targets: dict[date, dict[str, float]],
) -> None:
    with pytest.raises(CompatibilityProbeError) as captured:
        SyntheticTargetWeights(targets)

    assert captured.value.code is ProbeErrorCode.INVALID_INPUT


def test_strategy_rejects_a_missing_target_date() -> None:
    with pytest.raises(CompatibilityProbeError) as captured:
        _strategy().generate_target_weight_position(
            score={"anything": 99.0},
            current=object(),
            trade_start_time=datetime(2026, 1, 7),
            trade_end_time=datetime(2026, 1, 7, 23, 59),
        )

    assert captured.value.code is ProbeErrorCode.INVALID_INPUT
    assert captured.value.message == "synthetic target weights are missing for trade date"


def test_arbitrary_score_changes_do_not_change_target_weights() -> None:
    strategy = _strategy()
    call = {
        "current": object(),
        "trade_start_time": datetime(2026, 1, 5),
        "trade_end_time": datetime(2026, 1, 5, 23, 59),
    }

    first = strategy.generate_target_weight_position(score={"A": -999.0}, **call)
    second = strategy.generate_target_weight_position(score={"A": 999.0, "B": 1.0}, **call)

    assert first == second == {"000001.SZ": 0.6, "600000.SH": 0.3}


def test_strategy_locks_weight_strategy_base_interface_and_has_no_builtin_constructor() -> None:
    method = inspect.signature(SyntheticTargetWeightStrategy.generate_target_weight_position)
    assert tuple(method.parameters) == (
        "self",
        "score",
        "current",
        "trade_start_time",
        "trade_end_time",
    )

    source = dedent(inspect.getsource(SyntheticTargetWeightStrategy))
    tree = ast.parse(source)
    strategy_names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id.endswith("Strategy")
    }
    class_node = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef))

    assert source.find("TopkDropoutStrategy") == -1
    assert strategy_names == set()
    assert [base.id for base in class_node.bases if isinstance(base, ast.Name)] == [
        "WeightStrategyBase"
    ]
