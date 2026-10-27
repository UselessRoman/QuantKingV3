"""Documentation governance after the Stage 1 closeout."""

from __future__ import annotations

import re
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
        "S1-07K4_IMPLEMENTATION.md",
        "S1-07K4_ACCEPTANCE.md",
        "S1-08R2_IMPLEMENTATION.md",
        "S1-08R2_ACCEPTANCE.md",
        "S1-08R3_IMPLEMENTATION.md",
        "S1-08_ACCEPTANCE.md",
    ),
)
def test_stage1_index_targets_exist(name: str) -> None:
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
def test_superseded_current_documents_stay_removed(name: str) -> None:
    assert not (_STAGE1 / name).exists()


def test_roadmap_is_the_single_detailed_stage_status_entry() -> None:
    roadmap = _read("docs/ROADMAP.md")
    readme = _read("README.md")
    assert "本文件是阶段状态的唯一入口" in roadmap
    assert "| Stage 0 | 已完成 |" in roadmap
    assert "| Stage 1 | 已完成 |" in roadmap
    assert "| Stage 2 | S2-01 开发中；后续任务未授权 |" in roadmap  # noqa: RUF001
    assert "阶段状态、权威验收与下一步只在" in readme
    assert "Stage 0：已完成" not in readme  # noqa: RUF001
    assert "Stage 1：已完成" not in readme  # noqa: RUF001


@pytest.mark.parametrize(
    "relative",
    (
        "AGENTS.md",
        "README.md",
        "docs/ARCHITECTURE.md",
        "docs/DATA_ARCHITECTURE.md",
        "docs/IMPLEMENTATION_PLAN.md",
        "docs/DECISIONS.md",
        "docs/stage1/README.md",
        "docs/stage1/CONTRACTS.md",
        "docs/stage2/README.md",
    ),
)
def test_active_documents_do_not_copy_attempt_level_history(relative: str) -> None:
    text = _read(relative)
    for detail in ("36/36", "82/82", "5,208", "R3D2-R2", "K4C-R4"):
        assert detail not in text


def test_stage1_contract_has_only_the_current_three_source_route() -> None:
    contracts = _read("docs/stage1/CONTRACTS.md")
    assert "| A | BAOSTOCK / BAOSTOCK |" in contracts
    assert "| B | AKSHARE / TENCENT |" in contracts
    assert "| C | AKSHARE / SINA |" in contracts
    assert "| X |" not in contracts
    assert "一致时可选择 X" not in contracts
    assert "正式来源集合只有 A/B/C" in contracts
    assert "不得进入新 Job" in contracts


def test_data_architecture_does_not_reintroduce_legacy_arbitration() -> None:
    text = _read("docs/DATA_ARCHITECTURE.md")
    assert "历史导入的 X" not in text
    assert "XTQuant/QMT" in text
    assert "不能\n进入 Raw、Canonical、Warehouse 或发布候选" in text


@pytest.mark.parametrize(
    "relative",
    (
        "docs/stage1/S1-07K4_IMPLEMENTATION.md",
        "docs/stage1/S1-08R2_IMPLEMENTATION.md",
        "docs/stage1/S1-08R3_IMPLEMENTATION.md",
    ),
)
def test_long_running_records_are_explicitly_historical(relative: str) -> None:
    assert any(
        line.startswith("> 历史记录：")  # noqa: RUF001
        for line in _read(relative).split("\n", 8)[1:]
    )


def test_current_document_roles_remain_bounded() -> None:
    line_limits = {
        "docs/ARCHITECTURE.md": 120,
        "docs/DATA_ARCHITECTURE.md": 120,
        "docs/IMPLEMENTATION_PLAN.md": 150,
        "docs/DECISIONS.md": 260,
        "docs/stage1/README.md": 50,
        "docs/stage1/CONTRACTS.md": 110,
        "docs/stage2/README.md": 50,
    }
    for relative, limit in line_limits.items():
        assert len(_read(relative).splitlines()) <= limit, relative


def test_stage_specific_tools_are_archived_outside_the_active_scripts_root() -> None:
    active_names = {path.name for path in (_ROOT / "scripts").glob("*") if path.is_file()}
    assert not any(re.search(r"(?i)(s1[_-]|k4c|r3v2|r3c)", name) for name in active_names)
    history = _ROOT / "scripts" / "history" / "stage1"
    assert (history / "README.md").is_file()
    assert (history / "verify_fixed_release.py").is_file()
    assert (history / "probe_release_idempotency.py").is_file()


def test_retired_importer_and_one_shot_entries_are_not_installed() -> None:
    project = _read("pyproject.toml")
    cli = _read("src/quantking/cli/eod.py")
    assert "quantking-legacy-import" not in project
    parser_section = cli[cli.index("def _parser") : cli.index("def _write")]
    assert "eod-formal-run" not in parser_section
    assert "eod-calendar-publish" not in parser_section


def test_current_runtime_uses_domain_names_not_stage_revisions() -> None:
    combined = "\n".join(
        _read(relative)
        for relative in (
            "src/quantking/runtime.py",
            "src/quantking/data/adapters/eod_worker.py",
            "src/quantking/data/adapters/resolution_sources.py",
            "src/quantking/data/adapters/sources/budget.py",
            "src/quantking/data/adapters/sources/legal_empty.py",
        )
    )
    for retired_name in (
        "K4EndpointBudget",
        "K4ExpectedSessionResolver",
        "K4ResolutionSourcePort",
        "k4_daily_rule_set",
        "\nEOD_CODE_VERSION =",
        "S1-08R",
    ):
        assert retired_name not in combined


def test_frozen_evidence_and_protocol_values_remain_available() -> None:
    assert (_STAGE1 / "evidence" / "k4c_r4_composite_evidence_2026-08-16.json").is_file()
    assert (_STAGE1 / "evidence" / "s1_08r3_r3v2_result_2026-08-18.json").is_file()
    thresholds = _read("src/quantking/data/comparison/thresholds.py")
    assert '"comparison-thresholds.daily-bar-unadjusted.k4.v1"' in thresholds
