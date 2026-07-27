# ruff: noqa: RUF001
"""Status-wording and link checks for Stage 1 documentation.

Fullwidth CJK punctuation in the checked status phrases is intentional and
matches the canonical Chinese wording used across project documents.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STAGE1_DIR = _REPO_ROOT / "docs" / "stage1"

_REQUIRED_STAGE1_DOCS = (
    "README.md",
    "TASKS.md",
    "ACCEPTANCE.md",
    "DATA_CONTRACTS.md",
    "S1-02_ACCEPTANCE.md",
)

# The canonical status phrase required across updated documents.
_STATUS_PHRASES = (
    "Stage 0 已完成；Stage 1 已获实施授权并进行中，但尚未完成",
    "Stage 0 已完成；Stage 1 已获实施授权并进行中，但尚未完成。",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestStage1DocsExist:
    @pytest.mark.parametrize("name", _REQUIRED_STAGE1_DOCS)
    def test_required_stage1_doc_exists(self, name: str) -> None:
        assert (_STAGE1_DIR / name).is_file(), f"missing required Stage 1 doc: {name}"

    def test_stage1_readme_states_authorized_in_progress(self) -> None:
        text = _read(_STAGE1_DIR / "README.md")
        assert any(phrase in text for phrase in _STATUS_PHRASES), (
            "Stage 1 README must state the canonical authorized-in-progress status"
        )

    def test_stage1_readme_does_not_claim_accepted(self) -> None:
        text = _read(_STAGE1_DIR / "README.md")
        # Must not claim Stage 1 itself is accepted; negative phrasing is allowed.
        assert "Stage 1 已验收" not in text, "Stage 1 must not be described as accepted"
        assert "Stage 1 已完成验收" not in text
        assert "已获实施授权" in text

    @pytest.mark.parametrize(
        "path",
        (
            _STAGE1_DIR / "README.md",
            _STAGE1_DIR / "TASKS.md",
            _STAGE1_DIR / "ACCEPTANCE.md",
            _STAGE1_DIR / "DATA_CONTRACTS.md",
            _REPO_ROOT / "docs" / "ARCHITECTURE.md",
            _REPO_ROOT / "docs" / "ROADMAP.md",
        ),
    )
    def test_s1_05_r1_status_is_pending_main_acceptance(self, path: Path) -> None:
        text = _read(path)
        assert "S1-05-R1 修复已提交，等待主验收" in text
        assert "S1-05 已主验收" not in text

    def test_stage1_tasks_lists_s1_01_through_s1_08(self) -> None:
        text = _read(_STAGE1_DIR / "TASKS.md")
        for code in ("S1-01", "S1-02", "S1-03", "S1-04", "S1-05", "S1-06", "S1-07", "S1-08"):
            assert code in text, f"{code} must be listed in TASKS.md"

    def test_s1_01_is_main_accepted(self) -> None:
        for name in ("README.md", "TASKS.md", "ACCEPTANCE.md"):
            text = _read(_STAGE1_DIR / name)
            assert "7e4e5b38b7fc91edaf8727ed55b1e26435373aea" in text, (
                f"{name} must record the S1-01 main-acceptance commit"
            )
            assert "S1-01 主验收" in text, f"{name} must reference the S1-01 main acceptance status"
        # S1-01 acceptance evidence file must exist and point at the main-acceptance commit.
        evidence = _read(_STAGE1_DIR / "S1-01_ACCEPTANCE.md")
        assert "7e4e5b38b7fc91edaf8727ed55b1e26435373aea" in evidence
        assert "S1-01 通过" not in evidence and "Stage 1 完成" not in evidence
        tasks = _read(_STAGE1_DIR / "TASKS.md")
        # S1-02 main acceptance is independently recorded.
        assert "S1-02 主验收证据" in tasks

    def test_s1_02_is_main_accepted(self) -> None:
        for name in ("README.md", "TASKS.md", "ACCEPTANCE.md"):
            text = _read(_STAGE1_DIR / name)
            assert "79f9fa3e682e212cf74e7886a04c6fb52158f302" in text
            assert "S1-02 主验收证据" in text
            assert "S1-03～S1-08 未开始、未获授权" in text or "未开始、未获授权" in text, (
                f"{name} must record S1-03～S1-08 are not started and not authorized"
            )
        evidence = _read(_STAGE1_DIR / "S1-02_ACCEPTANCE.md")
        assert "S1-02（Warehouse、原子发布与显式版本查询）完成主验收" in evidence

    def test_main_acceptance_task_order_is_restored(self) -> None:
        text = _read(_STAGE1_DIR / "README.md")
        ordered = (
            "S1-02 Warehouse、原子发布与查询",
            "S1-03 XTQuant",
            "S1-04 质量、跨源差异与 SourceOverride",
            "S1-05 EOD、Worker、CLI 与 API",
            "S1-06 数据中心 Web",
            "S1-07 经授权真实样本与旧数据 DATA_ONLY 判定",
            "S1-08 真实或明确范围 Canonical 构建及 Stage 1 最终验收",
        )
        positions = [text.index(item) for item in ordered]
        assert positions == sorted(positions)

    def test_offline_gates_are_not_described_as_main_acceptance(self) -> None:
        text = _read(_STAGE1_DIR / "ACCEPTANCE.md")
        # S1-02..S1-08 may still have their offline gates; the rule applies only to those tasks,
        # not to S1-01 which has been main-accepted.
        assert "已不再是验收判据" in text or "不得写成主验收通过" in text

    def test_stage1_acceptance_matrix_is_pending(self) -> None:
        text = _read(_STAGE1_DIR / "ACCEPTANCE.md")
        # The acceptance matrix must still show that the Stage 1 overall acceptance is pending.
        assert "进行中" in text or "未完成" in text or "待" in text, (
            "ACCEPTANCE.md must reflect that Stage 1 is not yet accepted"
        )

    def test_stage1_data_contracts_lists_six_datasets(self) -> None:
        text = _read(_STAGE1_DIR / "DATA_CONTRACTS.md")
        for dataset in (
            "security_master",
            "exchange_calendar",
            "daily_bar_unadjusted",
            "adjustment_factor",
            "trading_status",
            "benchmark_index_daily",
        ):
            assert dataset in text, f"{dataset} must be documented in DATA_CONTRACTS.md"


class TestRootStatusWordingSynced:
    """The stale 'Stage 1 未获授权/未开始' wording must be corrected."""

    @pytest.mark.parametrize(
        "rel",
        (
            "AGENTS.md",
            "README.md",
            "docs/IMPLEMENTATION_PLAN.md",
            "docs/ROADMAP.md",
            "docs/ARCHITECTURE.md",
        ),
    )
    def test_no_stale_stage1_unauthorized_wording(self, rel: str) -> None:
        text = _read(_REPO_ROOT / rel)
        # These exact stale phrases must no longer appear.
        stale = (
            "Stage 1 尚未开始，也未获得实施授权",
            "Stage 1 尚未获得授权",
            "Stage 1 未开始，也未获得实施授权",
        )
        for phrase in stale:
            assert phrase not in text, f"{rel} still contains stale phrase: {phrase!r}"

    def test_roadmap_stage1_row_is_in_progress(self) -> None:
        text = _read(_REPO_ROOT / "docs" / "ROADMAP.md")
        # The Stage 1 status cell must reflect authorized in-progress, not 未开始.
        assert "进行中" in text or "已获实施授权" in text

    def test_final_acceptance_history_preserved(self) -> None:
        text = _read(_REPO_ROOT / "docs" / "stage0" / "FINAL_ACCEPTANCE.md")
        assert "Stage 0 完成" in text
        # The historical conclusion must remain intact.
        assert "Stage 1" in text


class TestStage1NotDescribedAsAccepted:
    @pytest.mark.parametrize("rel", ("README.md", "docs/ARCHITECTURE.md"))
    def test_no_stage1_accepted_claim(self, rel: str) -> None:
        text = _read(_REPO_ROOT / rel)
        assert "Stage 1 已验收" not in text
        assert "Stage 1 已完成验收" not in text
