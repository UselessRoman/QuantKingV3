"""The compact Stage 1 documentation must stay bounded and internally consistent."""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_STAGE1 = _ROOT / "docs" / "stage1"


def _read(relative: str) -> str:
    return (_ROOT / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    (
        "README.md",
        "CONTRACTS.md",
        "ACCEPTANCE.md",
        "S1-07K3_TASK.md",
        "S1-08_TASK.md",
        "S1-01_ACCEPTANCE.md",
        "S1-02_ACCEPTANCE.md",
        "S1-03_ACCEPTANCE.md",
        "S1-04_ACCEPTANCE.md",
        "S1-05_ACCEPTANCE.md",
        "S1-06_ACCEPTANCE.md",
    ),
)
def test_compact_stage1_documents_exist(name: str) -> None:
    assert (_STAGE1 / name).is_file()


@pytest.mark.parametrize(
    "name",
    (
        "DATA_CONTRACTS.md",
        "QUALITY_CONTRACTS.md",
        "SOURCE_MATRIX.md",
        "HANDOFF.md",
        "STAGE1_COMPLETION_HANDOFF.md",
        "TASKS.md",
        "S1-07K1_TASK.md",
        "S1-07K1_IMPLEMENTATION_HANDOFF.md",
        "S1-07K2_TASK.md",
        "S1-07K2_IMPLEMENTATION_HANDOFF.md",
    ),
)
def test_superseded_current_documents_are_removed(name: str) -> None:
    assert not (_STAGE1 / name).exists()


def test_current_status_records_commits_without_claiming_stage_completion() -> None:
    combined = "\n".join(
        _read(relative)
        for relative in (
            "README.md",
            "docs/ARCHITECTURE.md",
            "docs/ROADMAP.md",
            "docs/stage1/README.md",
            "docs/stage1/ACCEPTANCE.md",
        )
    )
    assert "f98f1d9" in combined
    assert "0ebf84b" in combined
    assert "S1-08" in combined
    assert "S1-08 首次" in combined
    assert "RESOLUTION_BLOCKED" in combined
    assert "授权已结束" in combined or "授权已经结束" in combined
    assert "Stage 1 尚未完成" in combined
    assert "Stage 2 未开始" in combined
    assert "Stage 1 已完成验收" not in combined


def test_single_contract_keeps_history_and_the_accepted_k3_route() -> None:
    text = _read("docs/stage1/CONTRACTS.md")
    for phrase in (
        "daily_bar_unadjusted.v1",
        "query_history_k_data_plus",
        "stock_zh_a_hist",
        "stock_zh_a_hist_tx",
        "stock_zh_a_daily",
        "BAOSTOCK",
        "EASTMONEY",
        "TENCENT",
        "SINA",
        "XTQUANT_LEGACY",
        "lookback_days=15",
        "comparison-thresholds.daily-bar-unadjusted.k1.v1",
        "ResolutionDecision",
        "SourceOverride",
        "无两源一致",
    ):
        assert phrase in text


def test_s1_08_records_the_bounded_failed_formal_run_without_stage_completion() -> None:
    text = _read("docs/stage1/S1-08_TASK.md")
    for phrase in (
        "首次正式运行的不可变事实",
        "RESOLUTION_BLOCKED",
        "conflict 80",
        "授权已消耗",
        "000001.SZ",
        "600000.SH",
        "2026-06-11..2026-08-09",
        "2026-05-27..2026-08-09",
        "共 12 次",
        "HTTP 上界估算 24",
        "应用层零重试",
        "E:\\QuantKingV3Data",
        "S1-07K3",
        "Stage 2 未开始",
    ):
        assert phrase in text


def test_k3_task_freezes_baostock_four_sources_and_managed_legacy_import() -> None:
    text = _read("docs/stage1/S1-07K3_TASK.md")
    for phrase in (
        "设计已确认",
        "BAOSTOCK / BAOSTOCK",
        "AKSHARE / EASTMONEY",
        "AKSHARE / TENCENT",
        "AKSHARE / SINA",
        "XTQUANT_LEGACY / XTQUANT",
        "5,208",
        "527,708,270",
        "DATA_ONLY",
        "SHA-256",
        "应用层默认零自动重试",
        "旧根前后指纹一致",
        "S1-08R2",
        "不自动重跑",
    ):
        assert phrase in text


def test_stage1_history_and_architecture_decisions_remain_auditable() -> None:
    for task in ("S1-01", "S1-02", "S1-03", "S1-04", "S1-05", "S1-06"):
        assert (_STAGE1 / f"{task}_ACCEPTANCE.md").is_file()
    decisions = _read("docs/DECISIONS.md")
    assert "ADR-018" in decisions
    assert "Stage 1 控制面简化" in decisions
    assert "生产运行时只支持 `daily_bar_unadjusted`" in decisions


def test_retired_s1_07_modules_and_current_documents_are_absent() -> None:
    assert not list((_ROOT / "src" / "quantking" / "data" / "validation").glob("*.py"))
    assert not list((_ROOT / "tests" / "data" / "validation").glob("*.py"))
    assert not (_STAGE1 / "S1-07C1C1_RUNBOOK.md").exists()
