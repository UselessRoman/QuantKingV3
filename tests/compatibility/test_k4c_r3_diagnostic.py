"""Bounded BaoStock diagnostic for the K4C-R3-R1 suspended-day row shape.

The first R3 Phase B attempt stopped on ``BAOSTOCK_SCHEMA_DRIFT`` because one
BaoStock row for 688237.SH (window 2026-06-29..07-16) carried numeric OHLC
with an empty volume. This diagnostic makes exactly ONE BaoStock session
against the identical request window and persists the raw provider rows
before any adapter parsing, so the repair can be driven by the observed row
shape instead of guesswork. Without the marker the test skips immediately and
never touches the network.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from quantking.data.adapters.sources.baostock import BaostockSessionRunner
from quantking.data.adapters.sources.parameters import DailyBarRequest

_DIAGNOSTIC_MARKER = "K4C-R3-DIAGNOSTIC-2026-08-16-v1"
_ENV_MARKER = "QUANTKING_K4C_R3_DIAGNOSTIC_AUTHORIZATION"
_ENV_OUTPUT = "QUANTKING_K4C_R3_DIAGNOSTIC_OUTPUT"
_SYMBOL = "688237.SH"
_START = date(2026, 7, 14)
_END = date(2026, 7, 16)


@pytest.mark.real_k4c_probe
def test_k4c_r3_baostock_suspended_day_diagnostic() -> None:
    if os.environ.get(_ENV_MARKER) != _DIAGNOSTIC_MARKER:
        pytest.skip("K4C-R3 diagnostic requires the frozen marker")
    output = os.environ.get(_ENV_OUTPUT)
    if not output:
        pytest.skip("K4C-R3 diagnostic requires QUANTKING_K4C_R3_DIAGNOSTIC_OUTPUT")
    result = BaostockSessionRunner().run(DailyBarRequest(_SYMBOL, _START, _END))
    payload = {
        "schema_version": "s1-07k4c-r3-diagnostic.v1",
        "diagnostic_marker": _DIAGNOSTIC_MARKER,
        "request": {
            "symbol": _SYMBOL,
            "target_start": _START.isoformat(),
            "target_end": _END.isoformat(),
            "upstream_start": "2026-06-29",
            "upstream_end": "2026-07-16",
        },
        "retrieved_at": datetime.now(UTC).isoformat(),
        "session": {
            "distribution_version": result.distribution_version,
            "runtime_version": result.runtime_version,
            "login_messages": result.login_messages,
            "query_messages": result.query_messages,
            "logout_messages": result.logout_messages,
            "complete": result.complete,
            "partial_reason": result.partial_reason,
            "row_count": len(result.rows),
        },
        "rows": [list(row) for row in result.rows],
    }
    Path(output).write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print("K4C_R3_DIAGNOSTIC_JSON " + json.dumps(payload, ensure_ascii=False, sort_keys=True))
