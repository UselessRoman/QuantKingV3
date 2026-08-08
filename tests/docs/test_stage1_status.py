"""The simplified Stage 1 route must stay explicit and internally consistent."""

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
        "TASKS.md",
        "ACCEPTANCE.md",
        "HANDOFF.md",
        "STAGE1_COMPLETION_HANDOFF.md",
        "SOURCE_MATRIX.md",
        "S1-07K1_TASK.md",
    ),
)
def test_simplified_stage1_documents_exist(name: str) -> None:
    assert (_STAGE1 / name).is_file()


def test_s1_01_through_s1_06_evidence_is_preserved() -> None:
    tasks = _read("docs/stage1/TASKS.md")
    for task in ("S1-01", "S1-02", "S1-03", "S1-04", "S1-05", "S1-06"):
        assert task in tasks
        assert (_STAGE1 / f"{task}_ACCEPTANCE.md").is_file()
    assert "Stage 1 尚未完成" in _read("docs/stage1/ACCEPTANCE.md")


@pytest.mark.parametrize(
    "relative",
    (
        "docs/stage1/README.md",
        "docs/stage1/TASKS.md",
        "docs/stage1/STAGE1_COMPLETION_HANDOFF.md",
        "docs/stage1/SOURCE_MATRIX.md",
    ),
)
def test_akshare_three_upstream_route_is_consistent(relative: str) -> None:
    text = _read(relative)
    for value in ("AKShare", "东财", "腾讯", "新浪"):
        assert value in text


def test_later_stage_has_not_started() -> None:
    for relative in ("docs/stage1/README.md", "docs/stage1/TASKS.md"):
        assert "Stage 2" in _read(relative)


def test_source_matrix_keeps_provider_and_upstream_separate() -> None:
    text = _read("docs/stage1/SOURCE_MATRIX.md")
    for source_id in ("AKSHARE_EASTMONEY", "AKSHARE_TENCENT", "AKSHARE_SINA"):
        assert source_id in text
    for endpoint in ("stock_zh_a_hist", "stock_zh_a_hist_tx", "stock_zh_a_daily"):
        assert endpoint in text
    assert "最少两源一致" in text


def test_next_task_is_bounded_and_rejects_old_evidence_chain() -> None:
    text = _read("docs/stage1/S1-07K1_TASK.md")
    for phrase in (
        "600000.SH",
        "000001.SZ",
        "最近 60 个自然日",
        "每个 upstream、每只证券最多一次调用",
        "不写 E:\\QuantKingV3Data",
        "不开始 K2、S1-08 或 Stage 2",
        "不恢复任何旧 S1-07",
    ):
        assert phrase in text
    for retired in ("Ed25519", "nonce", "claim", "seal"):
        assert retired in text


def test_retired_s1_07_current_documents_and_modules_are_absent() -> None:
    assert not list((_ROOT / "src" / "quantking" / "data" / "validation").glob("*.py"))
    assert not list((_ROOT / "tests" / "data" / "validation").glob("*.py"))
    assert not (_STAGE1 / "S1-07C1C1_RUNBOOK.md").exists()
    handoff = _read("docs/stage1/HANDOFF.md")
    assert "44d268b3db8bd9c690225f62e60fa12bf56a289d" in handoff


def test_stage1_does_not_claim_real_akshare_or_completion() -> None:
    combined = "\n".join(
        _read(relative)
        for relative in (
            "docs/stage1/README.md",
            "docs/stage1/TASKS.md",
            "docs/stage1/ACCEPTANCE.md",
            "docs/stage1/HANDOFF.md",
        )
    )
    assert "S1-07K1 | 未开始" in combined
    assert "Stage 1 尚未完成" in combined
    assert "Stage 1 已完成验收" not in combined
