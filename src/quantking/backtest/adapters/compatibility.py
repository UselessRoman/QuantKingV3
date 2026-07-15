"""Backtrader Stage 0 compatibility adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from quantking.common.compatibility import (
    CompatibilityProbeError,
    ProbeErrorCode,
    ProbePayload,
    ProbeResult,
    capture_probe,
)


def probe_backtrader(root: Path) -> ProbeResult:
    """Run three synthetic bars through Backtrader without creating orders."""
    return capture_probe("backtrader", lambda: _run_backtrader(root))


def _run_backtrader(_root: Path) -> ProbePayload:
    import backtrader as bt  # type: ignore[import-untyped]
    import pandas as pd  # type: ignore[import-untyped]

    class NoOrderStrategy(bt.Strategy):  # type: ignore[misc]
        bars_seen: int

        def __init__(self) -> None:
            self.bars_seen = 0

        def next(self) -> None:
            self.bars_seen += 1

    frame = pd.DataFrame(
        {
            "open": [10.0, 10.2, 10.1],
            "high": [10.3, 10.4, 10.2],
            "low": [9.9, 10.0, 9.8],
            "close": [10.2, 10.1, 10.0],
            "volume": [100.0, 120.0, 110.0],
            "openinterest": [0.0, 0.0, 0.0],
        },
        index=pd.date_range("2026-01-05", periods=3, freq="D"),
    )
    engine = bt.Cerebro(stdstats=False)
    engine.broker.setcash(10_000.0)
    engine.adddata(bt.feeds.PandasData(dataname=frame))
    engine.addstrategy(NoOrderStrategy)
    runs: list[Any] = engine.run()
    strategy = runs[0]
    if strategy.bars_seen != 3 or engine.broker.getvalue() != 10_000.0:
        raise CompatibilityProbeError(
            ProbeErrorCode.EXECUTION_FAILED,
            "Backtrader no-order smoke evidence is invalid",
        )
    return ProbePayload(
        str(bt.__version__),
        ("three_bar_feed_created", "no_order_strategy_run", "cash_unchanged"),
    )
