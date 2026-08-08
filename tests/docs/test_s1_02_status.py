# ruff: noqa: RUF001
"""Status wording tests for the accepted Stage 1 S1-02 task.

These tests assert that S1-02 has durable main-acceptance evidence while
the current documents preserve the latest Stage 1 boundary.

* The S1-02 implementation is present under ``src/quantking/data/adapters/warehouse``.
* The Stage 1 status wording consistently records S1-02 main acceptance.
* S1-03 has not been started.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_WAREHOUSE = _REPO_ROOT / "src" / "quantking" / "data" / "adapters" / "warehouse"
_STAGE1_DIR = _REPO_ROOT / "docs" / "stage1"


class TestWarehouseModuleLayout:
    @pytest.mark.parametrize(
        "name",
        (
            "__init__.py",
            "errors.py",
            "manifest.py",
            "partitioning.py",
            "raw_store.py",
            "canonical_warehouse.py",
            "publish_coordinator.py",
            "query.py",
            "verifier.py",
        ),
    )
    def test_required_module_exists(self, name: str) -> None:
        assert (_SRC_WAREHOUSE / name).is_file(), f"missing required warehouse module: {name}"


class TestStage1StatusWording:
    @pytest.mark.parametrize(
        "doc",
        ("README.md", "TASKS.md", "ACCEPTANCE.md"),
    )
    def test_s1_02_is_main_accepted(self, doc: str) -> None:
        text = (_STAGE1_DIR / doc).read_text(encoding="utf-8")
        assert "79f9fa3e682e212cf74e7886a04c6fb52158f302" in text
        assert "S1-02 主验收证据" in text

    @pytest.mark.parametrize(
        "doc",
        ("README.md", "TASKS.md", "ACCEPTANCE.md"),
    )
    def test_later_external_tasks_not_started(self, doc: str) -> None:
        text = (_STAGE1_DIR / doc).read_text(encoding="utf-8")
        assert "S1-03" in text
        assert "S1-07B 已于 2026-08-04" in text
        assert "d60559601efd61de15b41ffa86b4f2f730a69d5e" in text
        assert "C1C1/C2、S1-07D、S1-08 尚未开始、未获授权" in text

    def test_s1_02_does_not_describe_stage1_complete(self) -> None:
        # S1-02 acceptance must not claim Stage 1 itself is complete.
        for doc in ("README.md", "TASKS.md", "ACCEPTANCE.md"):
            text = (_STAGE1_DIR / doc).read_text(encoding="utf-8")
            assert "Stage 1 已完成验收" not in text
            assert "Stage 1 已验收" not in text

    def test_s1_02_acceptance_evidence_exists(self) -> None:
        evidence = (_STAGE1_DIR / "S1-02_ACCEPTANCE.md").read_text(encoding="utf-8")
        assert "79f9fa3e682e212cf74e7886a04c6fb52158f302" in evidence
        assert "S1-03～S1-08 未开始、未获授权" in evidence
