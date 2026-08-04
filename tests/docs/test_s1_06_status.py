"""S1-06 main acceptance is recorded without advancing later stages."""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DOCS = (
    "docs/stage1/README.md",
    "docs/stage1/TASKS.md",
    "docs/stage1/ACCEPTANCE.md",
    "docs/stage1/DATA_CONTRACTS.md",
    "docs/ARCHITECTURE.md",
    "docs/ROADMAP.md",
)


@pytest.mark.parametrize("relative", _DOCS)
def test_s1_06_is_main_accepted_and_later_tasks_are_unauthorized(relative: str) -> None:
    text = (_ROOT / relative).read_text(encoding="utf-8")
    assert "S1-06" in text
    assert "413805074b3a82c2d2a090d2b73c854a238e2e19" in text
    assert "完成主验收" in text
    assert "Stage 1 已完成" not in text
    assert "S1-07A 已于 2026-07-29" in text
    assert "S1-07B 已于 2026-08-04" in text
    assert "d60559601efd61de15b41ffa86b4f2f730a69d5e" in text
    assert "S1-07C/D、S1-08 尚未开始、未获授权" in text
    assert "S1-08 未开始" in text or "S1-08 尚未开始" in text


def test_data_contract_records_explicit_preview_and_no_leakage() -> None:
    text = (_ROOT / "docs/stage1/DATA_CONTRACTS.md").read_text(encoding="utf-8")
    for phrase in (
        "PUBLISHED",
        "latest",
        "keyset",
        "COMPLETE",
        "DifferenceReport",
        "绝对路径",
    ):
        assert phrase in text


def test_s1_06_acceptance_evidence_records_independent_results() -> None:
    text = (_ROOT / "docs/stage1/S1-06_ACCEPTANCE.md").read_text(encoding="utf-8")
    for phrase in (
        "OVERRIDE_ATTACK=REJECTED",
        "SUMMARY_FAILURES=150",
        "38 passed",
        "958 passed, 2 skipped, 3 deselected",
        "17 passed",
        "20260722_0009",
    ):
        assert phrase in text
