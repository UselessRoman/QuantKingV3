"""Audit hand-written fixture arithmetic, without a production research engine.

S2-02/03 must consume the expected values unchanged to test their implementations.
These checks establish fixture consistency, not completion of those later tasks.
"""

import json
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from statistics import median

from quantking.common.time import UtcTimestamp
from quantking.research.contracts import CorporateAction, CorporateActionType, SourceFact

FIXTURE = Path(__file__).parents[1] / "fixtures/research/s2_01_v1.json"


def test_continuous_prices_and_rolling_factor_have_hand_computable_expectations() -> None:
    sample = json.loads(FIXTURE.read_text())
    for row in sample["prices"]:
        assert Decimal(row["close"]) * Decimal(row["factor"]) == Decimal(row["research_close"])
    rolling = sample["rolling_expected"]
    inputs = [Fraction(v) for v in rolling["inputs"]]
    mean = sum(inputs) / 5
    assert mean == Fraction("13.6")
    assert inputs[-1] / mean - 1 == Fraction(*rolling["value_fraction"]) == Fraction(7, 68)
    # A future factor cannot rebase these fixed historical expected prices.
    assert [r["research_close"] for r in sample["prices"][:5]] == ["10", "11", "12", "13", "14"]


def test_weekly_label_exact_indices_include_holiday_and_missing_tail() -> None:
    sample = json.loads(FIXTURE.read_text())
    sessions = sample["calendar"]["sessions"]
    assert "2026-01-16" not in sessions
    prices = {r["date"]: Decimal(r["open"]) * Decimal(r["factor"]) for r in sample["prices"]}
    signals = sample["calendar"]["weekly_signals"]
    for index, label in enumerate(sample["labels"]):
        signal = label["signal"]
        assert label["entry"] == sessions[sessions.index(signal) + 1]
        if label["exit"] is None:
            assert label["value"] is None and label["reason"] == "LABEL_TAIL_INSUFFICIENT"
            continue
        exit_date = sessions[sessions.index(signals[index + 1]) + 1]
        assert exit_date == label["exit"]
        assert (
            sessions.index(exit_date) - sessions.index(label["entry"]) == label["holding_sessions"]
        )
        assert prices[exit_date] / prices[label["entry"]] - 1 == Decimal(label["value"])
    for signal in signals:
        week = date.fromisoformat(signal).isocalendar()[:2]
        assert signal == max(d for d in sessions if date.fromisoformat(d).isocalendar()[:2] == week)


def test_liquidity_and_listing_boundary_expected_values() -> None:
    sample = json.loads(FIXTURE.read_text())
    amounts = [Decimal(v) for v in sample["liquidity_amounts_cny"]]
    assert len(amounts) == 20
    assert median(amounts) == Decimal(sample["liquidity_expected_median_cny"]) == 10000000
    normal, young = sample["universe_cases"][:2]
    assert normal["as_of_session_ordinal"] - normal["listing_session_ordinal"] + 1 == 120
    assert young["as_of_session_ordinal"] - young["listing_session_ordinal"] + 1 == 119
    assert normal["reasons"] == [] and young["reasons"] == ["LISTING_AGE"]
    # Absolute rule is stable when unrelated securities join the cross-section.
    assert median(amounts) >= 10000000
    assert median([Decimal("9999999")] * 20) < 10000000


def test_company_action_facts_cash_entitlement_and_gaps() -> None:
    sample = json.loads(FIXTURE.read_text())
    timestamp = UtcTimestamp.from_iso("2026-01-08T07:00:00Z")
    source = SourceFact("synthetic", "v1", "a" * 64, timestamp, timestamp)
    for row in sample["corporate_actions"]:
        action = CorporateAction(
            row["event_id"],
            "000001.SZ",
            CorporateActionType(row["kind"]),
            timestamp,
            date.fromisoformat(row["record_date"]),
            date.fromisoformat(row["ex_date"]),
            Decimal(row["cash_per_share"]) if row["cash_per_share"] else None,
            Decimal(row["shares_per_share"]) if row["shares_per_share"] else None,
            date.fromisoformat(row["pay_date"]) if row["pay_date"] else None,
            date.fromisoformat(row["shares_available_date"])
            if row["shares_available_date"]
            else None,
            Decimal(row["reference_price"]) if row["reference_price"] else None,
            row["tax_policy"],
            source,
        )
        assert list(action.execution_gaps()) == row["execution_gaps"]
        if action.execution_gaps():
            continue
        shares = row["held_shares"]
        old_equity = shares * Decimal(row["pre_close"])
        assert old_equity == Decimal(row["expected_equity"])
        assert Decimal(row["reference_price"]) * Decimal(row["factor_after"]) == Decimal(
            row["pre_close"]
        )
        if row["kind"] == "CASH_DIVIDEND":
            receivable = shares * Decimal(row["cash_per_share"])
            assert receivable == Decimal(row["expected_receivable"]) == 100
            assert shares * Decimal(row["reference_price"]) + receivable == old_equity
            assert row["expected_ex_cash"] == "0" and row["expected_pay_cash"] == "100"
        else:
            bonus = shares * Decimal(row["shares_per_share"])
            assert bonus == row["expected_bonus_pending"] == 50
            assert shares + bonus == row["expected_ex_shares"] == 150
            assert (shares + bonus) * Decimal(row["reference_price"]) == old_equity
            assert row["expected_ex_sellable"] == 100
            assert row["expected_available_sellable"] == 150
