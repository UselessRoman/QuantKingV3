# ruff: noqa: RUF001, RUF002, RUF003
"""Status wording tests for the Stage 1 S1-02-R1 repair.

These tests assert the post-R1 status wording: S1-02 main acceptance failed
and S1-02-R1 is in repair pending main acceptance; S1-03～S1-08 are not
started and not authorized.

* The S1-02 implementation is present under ``src/quantking/data/adapters/warehouse``.
* The Stage 1 status wording consistently says S1-02-R1 is in repair.
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
            "query.py",
        ),
    )
    def test_required_module_exists(self, name: str) -> None:
        assert (_SRC_WAREHOUSE / name).is_file(), f"missing required warehouse module: {name}"


class TestStage1StatusWording:
    @pytest.mark.parametrize(
        "doc",
        ("README.md", "TASKS.md", "ACCEPTANCE.md"),
    )
    def test_s1_02_r1_in_repair(self, doc: str) -> None:
        text = (_STAGE1_DIR / doc).read_text(encoding="utf-8")
        assert "S1-02 主验收未通过" in text, f"{doc} must state S1-02 main acceptance failed"
        assert "S1-02-R1 修复中、待主验收" in text, (
            f"{doc} must state S1-02-R1 is in repair pending main acceptance"
        )

    @pytest.mark.parametrize(
        "doc",
        ("README.md", "TASKS.md", "ACCEPTANCE.md"),
    )
    def test_s1_03_not_started(self, doc: str) -> None:
        text = (_STAGE1_DIR / doc).read_text(encoding="utf-8")
        assert "S1-03" in text
        # S1-03～S1-08 must remain not started and not authorized.
        assert "S1-03～S1-08 未开始、未获授权" in text or "未开始、未获授权" in text

    def test_s1_02_does_not_describe_stage1_complete(self) -> None:
        # S1-02-R1 must not claim Stage 1 itself is complete.
        for doc in ("README.md", "TASKS.md", "ACCEPTANCE.md"):
            text = (_STAGE1_DIR / doc).read_text(encoding="utf-8")
            assert "Stage 1 已完成验收" not in text
            assert "Stage 1 已验收" not in text
