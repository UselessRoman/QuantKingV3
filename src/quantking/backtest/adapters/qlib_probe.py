"""Qlib version, interface and synthetic target-weight compatibility probe."""

from __future__ import annotations

import ast
import inspect
from datetime import date, datetime
from pathlib import Path
from textwrap import dedent
from typing import Any

from quantking.common.compatibility import (
    CompatibilityProbeError,
    ProbeErrorCode,
    ProbePayload,
    ProbeResult,
    capture_probe,
)

_EXPECTED_QLIB_VERSION = "0.9.7"
_EXPECTED_WEIGHT_HOOK = (
    "self",
    "score",
    "current",
    "trade_start_time",
    "trade_end_time",
)


def probe_qlib(root: Path) -> ProbeResult:
    """Verify the locked Qlib weight hook without running a business backtest."""
    return capture_probe("qlib", lambda: _run_qlib(root))


def _run_qlib(_root: Path) -> ProbePayload:
    import qlib  # type: ignore[import-untyped]
    from qlib.contrib.strategy.signal_strategy import (  # type: ignore[import-untyped]
        WeightStrategyBase,
    )

    from quantking.backtest.adapters.qlib_weight_strategy import (
        SyntheticTargetWeights,
        SyntheticTargetWeightStrategy,
    )

    version = str(qlib.__version__)
    if version != _EXPECTED_QLIB_VERSION:
        raise CompatibilityProbeError(
            ProbeErrorCode.VERSION_MISMATCH,
            "locked Qlib version mismatch",
        )
    base_signature = tuple(
        inspect.signature(WeightStrategyBase.generate_target_weight_position).parameters
    )
    implementation_signature = tuple(
        inspect.signature(SyntheticTargetWeightStrategy.generate_target_weight_position).parameters
    )
    if base_signature != _EXPECTED_WEIGHT_HOOK or implementation_signature != _EXPECTED_WEIGHT_HOOK:
        raise CompatibilityProbeError(
            ProbeErrorCode.INTERFACE_MISMATCH,
            "Qlib target-weight interface drifted from the locked signature",
        )

    targets = SyntheticTargetWeights(
        {
            date(2026, 1, 5): {"000001.SZ": 0.6, "600000.SH": 0.3},
            date(2026, 1, 6): {"000001.SZ": 0.4},
        }
    )
    strategy = SyntheticTargetWeightStrategy(targets)
    first = strategy.generate_target_weight_position(
        {"000001.SZ": -1_000.0}, object(), datetime(2026, 1, 5), datetime(2026, 1, 5)
    )
    second = strategy.generate_target_weight_position(
        {"000001.SZ": 1_000.0, "600000.SH": -1.0},
        object(),
        datetime(2026, 1, 5),
        datetime(2026, 1, 5),
    )
    if first != second or first != {"000001.SZ": 0.6, "600000.SH": 0.3}:
        raise CompatibilityProbeError(
            ProbeErrorCode.INTERFACE_MISMATCH,
            "Qlib target weights changed when scores changed",
        )
    _verify_invalid_and_missing_dates(strategy, SyntheticTargetWeights)
    _verify_strategy_source(SyntheticTargetWeightStrategy)
    return ProbePayload(
        version,
        (
            "version_locked",
            "weight_hook_signature_verified",
            "synthetic_target_weights_validated",
            "score_independence_verified",
            "missing_date_rejected",
            "strategy_ast_guard_passed",
        ),
    )


def _verify_invalid_and_missing_dates(strategy: Any, target_type: type[object]) -> None:
    try:
        target_type({date(2026, 1, 5): {"000001.SZ": 1.1}})  # type: ignore[call-arg]
    except CompatibilityProbeError as error:
        if error.code is not ProbeErrorCode.INVALID_INPUT:
            raise
    else:
        raise CompatibilityProbeError(
            ProbeErrorCode.INTERFACE_MISMATCH,
            "Qlib synthetic input accepted an illegal weight",
        )
    try:
        strategy.generate_target_weight_position(
            {}, object(), datetime(2026, 1, 7), datetime(2026, 1, 7)
        )
    except CompatibilityProbeError as error:
        if error.code is not ProbeErrorCode.INVALID_INPUT:
            raise
    else:
        raise CompatibilityProbeError(
            ProbeErrorCode.INTERFACE_MISMATCH,
            "Qlib synthetic input accepted a missing trade date",
        )


def _verify_strategy_source(strategy_type: type[object]) -> None:
    tree = ast.parse(dedent(inspect.getsource(strategy_type)))
    class_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    strategy_names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id.endswith("Strategy")
    }
    bases = (
        [base.id for base in class_nodes[0].bases if isinstance(base, ast.Name)]
        if len(class_nodes) == 1
        else []
    )
    if len(class_nodes) != 1 or bases != ["WeightStrategyBase"] or strategy_names:
        raise CompatibilityProbeError(
            ProbeErrorCode.INTERFACE_MISMATCH,
            "Qlib probe strategy source uses an unapproved strategy constructor",
        )
