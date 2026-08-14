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
        "S1-07K4_TASK.md",
        "S1-08_TASK.md",
        "S1-08R2_TASK.md",
        "S1-08R3_TASK.md",
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


def test_single_contract_keeps_history_and_the_accepted_three_role_route() -> None:
    text = _read("docs/stage1/CONTRACTS.md")
    for phrase in (
        "daily_bar_unadjusted.v1",
        "query_history_k_data_plus",
        "stock_zh_a_hist_tx",
        "stock_zh_a_daily",
        "BAOSTOCK",
        "TENCENT",
        "SINA",
        "ADR-021",
        "东财和 FTShare 已退出正式来源集合",
        "XTQUANT_LEGACY",
        "lookback_days=15",
        "comparison-thresholds.daily-bar-unadjusted.k1.v1",
        "ResolutionDecision",
        "SourceOverride",
        "无两源一致",
        "(code, trade_date)",
        "共同缺行不构成",
    ):
        assert phrase in text


def test_s1_08_history_records_the_bounded_failed_formal_run() -> None:
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
        "上界估算 24",
        "应用层零重试",
        "E:\\QuantKingV3Data",
        "S1-07K4",
        "S1-08R2",
        "S1-08R3",
        "Stage 2 未开始",
    ):
        assert phrase in text


def test_k3_history_preserves_rejected_routes_and_migration_baseline() -> None:
    text = _read("docs/stage1/S1-07K3_TASK.md")
    for phrase in (
        "BAOSTOCK / BAOSTOCK",
        "AKSHARE / EASTMONEY",
        "AKSHARE / TENCENT",
        "AKSHARE / SINA",
        "XTQUANT_LEGACY / XTQUANT",
        "5,208",
        "527,708,270",
        "DATA_ONLY",
        "应用层零自动重试",
        "S1-07K4",
        "S1-08R2",
    ):
        assert phrase in text


def test_successor_cards_have_single_ownership_and_strict_order() -> None:
    k4 = _read("docs/stage1/S1-07K4_TASK.md")
    for phrase in (
        "BaoStock A + AKShare/腾讯 B",
        "三角色实现重构",
        "仲裁与质量语义",
        "代表性三源实测与规则冻结",
        "旧 XTQuant 日线 DATA_ONLY 迁移",
        "旧根前后指纹一致",
    ):
        assert phrase in k4

    r2 = _read("docs/stage1/S1-08R2_TASK.md")
    for phrase in (
        "发布引擎离线收口",
        "SQLITE_BUSY/SQLITE_LOCKED",
        "manifest 驱动的范围和预算",
        "只读 verifier",
        "不执行真实网络或正式 Job",
    ):
        assert phrase in r2

    r3 = _read("docs/stage1/S1-08R3_TASK.md")
    for phrase in (
        "正式发布与 Stage 1 终验",
        "唯一正式 Job",
        "PUBLISHED DatasetVersion",
        "独立只读复验与双幂等",
        "S1-08_ACCEPTANCE.md",
    ):
        assert phrase in r3


def test_stage1_history_and_architecture_decisions_remain_auditable() -> None:
    for task in ("S1-01", "S1-02", "S1-03", "S1-04", "S1-05", "S1-06"):
        assert (_STAGE1 / f"{task}_ACCEPTANCE.md").is_file()
    decisions = _read("docs/DECISIONS.md")
    assert "ADR-018" in decisions
    assert "ADR-021" in decisions
    assert "Stage 1 三角色来源路线" in decisions
    assert "Stage 1 控制面简化" in decisions
    assert "生产运行时只支持 `daily_bar_unadjusted`" in decisions


def test_retired_s1_07_modules_and_current_documents_are_absent() -> None:
    assert not list((_ROOT / "src" / "quantking" / "data" / "validation").glob("*.py"))
    assert not list((_ROOT / "tests" / "data" / "validation").glob("*.py"))
    assert not (_STAGE1 / "S1-07C1C1_RUNBOOK.md").exists()
