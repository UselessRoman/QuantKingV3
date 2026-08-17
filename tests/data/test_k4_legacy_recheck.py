from __future__ import annotations

from datetime import date
from decimal import Decimal

import pyarrow as pa

from quantking.data.adapters.canonical_schema import canonical_schema, ensure_sorted
from quantking.data.adapters.legacy_recheck import LegacyRecheckEngine, LegacyRecheckOutcome
from quantking.data.comparison.thresholds import (
    K4_THRESHOLD_RULE_VERSION,
    ThresholdSet,
    ThresholdSpec,
    default_thresholds,
    thresholds_for,
)
from quantking.data.models import DatasetKind, DataUpstream

_D1 = date(2026, 8, 5)
_D2 = date(2026, 8, 6)
_D3 = date(2026, 8, 7)
_D4 = date(2026, 8, 8)


def _row(
    day: date,
    close: float,
    *,
    volume: int = 100,
    amount: float | None = None,
) -> dict[str, object]:
    return {
        "code": "000001.SZ",
        "trade_date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "pre_close": close,
        "volume": volume,
        "amount": float(volume * close) if amount is None else amount,
    }


def _table(rows: list[dict[str, object]]) -> pa.Table:
    return ensure_sorted(
        DatasetKind.DAILY_BAR_UNADJUSTED.value,
        pa.Table.from_pylist(
            rows,
            schema=canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value),
        ),
    )


def test_recheck_classifies_every_staged_row_with_conservation() -> None:
    x = _table(
        [
            _row(_D1, 10.0),  # A agrees with X -> X_A_CONSENSUS
            _row(_D2, 12.0),  # A/B agree on 11.0, X differs -> SERVICE_CONSENSUS_REPLACED_X
            _row(_D3, 10.9),  # only A holds the row and X disagrees -> QUARANTINED
            _row(_D4, 10.8),  # no service source holds it -> X_ONLY_UNVERIFIED
        ]
    )
    a = _table([_row(_D1, 10.0), _row(_D2, 11.0), _row(_D3, 10.5)])
    b = _table([_row(_D2, 11.0)])
    c = _table([])

    outcome: LegacyRecheckOutcome = LegacyRecheckEngine(
        thresholds=thresholds_for(K4_THRESHOLD_RULE_VERSION)
    ).reevaluate(
        x_table=x,
        service_tables={
            DataUpstream.BAOSTOCK: a,
            DataUpstream.TENCENT: b,
            DataUpstream.SINA: c,
        },
    )

    assert outcome.x_row_count == 4
    assert outcome.evidence.x_a_consensus_rows == 1
    assert outcome.evidence.service_consensus_replaced_x_rows == 1
    assert outcome.evidence.quarantined_rows == 1
    assert outcome.evidence.x_only_unverified_rows == 1
    assert outcome.replaced_keys == 1
    assert outcome.quarantined_keys == 1
    assert outcome.evidence.input_rows == 4
    payload = outcome.to_payload()
    assert payload["schema_version"] == "legacy-recheck.k4d.v1"
    assert payload["x_row_count"] == 4


def test_recheck_consumes_the_shared_threshold_set() -> None:
    tolerant = ThresholdSet(
        rule_version="comparison-thresholds.daily-bar-unadjusted.k3-candidate.v1",
        thresholds={
            DatasetKind.DAILY_BAR_UNADJUSTED: {
                "open": ThresholdSpec(absolute=Decimal("0"), relative=Decimal("0")),
                "high": ThresholdSpec(absolute=Decimal("0"), relative=Decimal("0")),
                "low": ThresholdSpec(absolute=Decimal("0"), relative=Decimal("0")),
                "close": ThresholdSpec(absolute=Decimal("0"), relative=Decimal("0")),
                "pre_close": ThresholdSpec(absolute=Decimal("0"), relative=Decimal("0")),
                "volume": ThresholdSpec(absolute=Decimal("1"), relative=Decimal("0")),
                "amount": ThresholdSpec(absolute=Decimal("1"), relative=Decimal("0")),
            },
        },
    )
    x = _table([_row(_D1, 10.0, volume=100, amount=1_000.0)])
    a = _table([_row(_D1, 10.0, volume=101, amount=1_000.0)])

    tolerant_outcome = LegacyRecheckEngine(thresholds=tolerant).reevaluate(
        x_table=x, service_tables={DataUpstream.BAOSTOCK: a}
    )
    assert tolerant_outcome.evidence.x_a_consensus_rows == 1

    strict_outcome = LegacyRecheckEngine(thresholds=default_thresholds()).reevaluate(
        x_table=x, service_tables={DataUpstream.BAOSTOCK: a}
    )
    assert strict_outcome.evidence.x_a_consensus_rows == 0
    assert strict_outcome.evidence.quarantined_rows == 1


def test_recheck_rejects_non_canonical_inputs() -> None:
    import pytest

    engine = LegacyRecheckEngine(thresholds=thresholds_for(K4_THRESHOLD_RULE_VERSION))
    with pytest.raises(ValueError, match="canonical"):
        engine.reevaluate(x_table=pa.table({"a": [1]}), service_tables={})
    with pytest.raises(ValueError, match="service_tables"):
        engine.reevaluate(
            x_table=_table([_row(_D1, 10.0)]),
            service_tables={DataUpstream.EASTMONEY: _table([_row(_D1, 10.0)])},  # type: ignore[dict-item]
        )
