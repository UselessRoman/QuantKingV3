"""S1-03-R1 status must remain under repair and pending main acceptance."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (_ROOT / relative).read_text(encoding="utf-8")


def test_s1_03_r1_is_under_repair_and_not_main_accepted() -> None:
    for relative in (
        "docs/stage1/TASKS.md",
        "docs/stage1/ACCEPTANCE.md",
        "docs/stage1/README.md",
        "docs/stage1/DATA_CONTRACTS.md",
    ):
        text = _read(relative)
        assert "S1-03-R1 修复中/待主验收" in text
        assert "S1-03 已主验收" not in text
        assert "Stage 1 已完成" not in text


def test_s1_06_acceptance_does_not_authorize_external_work() -> None:
    tasks = _read("docs/stage1/TASKS.md")
    assert "S1-05" in tasks and "完成主验收" in tasks
    assert "S1-06" in tasks
    assert "413805074b3a82c2d2a090d2b73c854a238e2e19" in tasks
    assert "完成主验收" in tasks
    assert "S1-07A-R2 修复已提交、等待主验收" in tasks
    assert "S1-07B/C/D、S1-08 未开始、未获授权" in tasks
    assert "S1-08 未开始、未获授权" in tasks
    contracts = _read("docs/stage1/DATA_CONTRACTS.md")
    assert "真实 XTQuant 样本验证仍须逐项授权" in contracts
    assert "合成测试不构成真实 XTQuant 能力证明" in contracts


def test_capability_gaps_are_documented_without_fabrication() -> None:
    contracts = _read("docs/stage1/DATA_CONTRACTS.md")
    for dataset in (
        "security_master",
        "exchange_calendar",
        "daily_bar_unadjusted",
        "adjustment_factor",
        "trading_status",
        "benchmark_index_daily",
    ):
        assert dataset in contracts
    assert "point-in-time" in contracts
    assert "单位语义未验证" in contracts
    assert "历史交易状态" in contracts
