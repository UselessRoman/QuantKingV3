# ruff: noqa: RUF001
"""S1-04 status must remain offline-implemented and pending main acceptance."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (_ROOT / relative).read_text(encoding="utf-8")


def test_s1_03_acceptance_doc_records_offline_only_main_acceptance() -> None:
    text = _read("docs/stage1/S1-03_ACCEPTANCE.md")
    # S1-03 离线/防御性合同已确认验收。
    assert "S1-03" in text
    assert "离线" in text
    # 真实验证延期到 S1-07/专项验收
    assert "S1-07" in text or "专项验收" in text
    # 用户已例外授权 S1-04 离线推进
    assert "S1-04" in text
    # 凭据不得进入 Agent 或 QuantKing
    assert "账号" in text or "凭据" in text or "密码" in text


def test_s1_04_remains_pending_main_acceptance() -> None:
    for relative in (
        "docs/stage1/README.md",
        "docs/stage1/TASKS.md",
        "docs/stage1/ACCEPTANCE.md",
        "docs/stage1/DATA_CONTRACTS.md",
    ):
        text = _read(relative)
        # S1-04-R3 修复已提交, 等待主验收 (不得写成已主验收)
        assert (
            "S1-04-R3 修复已提交，等待主验收" in text or "S1-04-R3 修复已提交、等待主验收" in text
        )
        assert "S1-04 已主验收" not in text
        assert "S1-04 已完成" not in text
        assert "Stage 1 已完成" not in text


def test_quality_and_source_matrix_documents_exist() -> None:
    quality = _read("docs/stage1/QUALITY_CONTRACTS.md")
    assert "rule_version" in quality
    assert "PASSED" in quality
    assert "QUALITY_FAILURE" in quality
    assert "SourceOverride" in quality
    matrix = _read("docs/stage1/SOURCE_MATRIX.md")
    assert "BAOSTOCK" in matrix
    assert "AKSHARE" in matrix
    assert "TUSHARE" in matrix
    # 备用源当前状态必须保守
    assert "BLOCKED_EXTERNAL_VALIDATION" in matrix or "UNVERIFIED" in matrix
    # 不得加入自动切换
    assert "自动切换" not in matrix
    assert "自动接管" not in matrix


def test_later_tasks_and_real_sources_remain_unauthorized() -> None:
    tasks = _read("docs/stage1/TASKS.md")
    assert "S1-05" in tasks
    assert "未开始" in tasks or "未获授权" in tasks
    contracts = _read("docs/stage1/DATA_CONTRACTS.md")
    # 真实 XTQuant 样本验证仍须逐项授权
    assert "真实 XTQuant" in contracts or "逐项授权" in contracts
    # 合成数据不能标生产可用
    assert "合成" in contracts
    assert "生产可用" in contracts or "生产状态" in contracts
