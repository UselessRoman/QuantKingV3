# ruff: noqa: RUF001
"""S1-06-R1 may be handed off for main acceptance without advancing later stages."""

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
def test_s1_06_is_submitted_only_and_later_tasks_are_unauthorized(relative: str) -> None:
    text = (_ROOT / relative).read_text(encoding="utf-8")
    assert "S1-06" in text
    assert "S1-06-R1 修复已提交" in text
    assert "等待主验收" in text or "待主验收" in text
    assert "Stage 1 已完成" not in text
    assert "S1-07～S1-08 未开始、未获授权" in text or "尚未实现且未获授权" in text


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
