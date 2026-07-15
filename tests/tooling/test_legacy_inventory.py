"""Stage 0 S0-06 contract tests for the read-only legacy inventory."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from quantking.common.legacy_inventory import (
    AssetType,
    Classification,
    EntryKind,
    FileMetadata,
    LocalFileSystem,
    audit_tree,
    render_json,
    render_markdown,
    write_reports,
)


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class ControlledFileSystem:
    """Local test adapter that simulates unreadable and reparse directories."""

    def __init__(self, *, unreadable: str | None = None, link: str | None = None) -> None:
        self._delegate = LocalFileSystem()
        self._unreadable = unreadable
        self._link = link
        self.read_paths: list[Path] = []

    def list_directory(self, directory: Path) -> tuple[FileMetadata, ...]:
        if directory.name == self._unreadable:
            raise PermissionError("simulated without requiring administrator privileges")
        entries = self._delegate.list_directory(directory)
        if self._link is None:
            return entries
        return tuple(
            replace(entry, kind=EntryKind.LINK) if entry.name == self._link else entry
            for entry in entries
        )

    def read_bytes(self, path: Path, maximum_bytes: int) -> bytes:
        self.read_paths.append(path)
        return self._delegate.read_bytes(path, maximum_bytes)


def test_native_walk_includes_git_ignored_files_and_groups_homogeneous_data(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy"
    _write(root / ".gitignore", b"ignored-data/\n")
    _write(root / "ignored-data" / "daily" / "a.csv", b"date,close\n2020-01-01,1\n")
    _write(root / "ignored-data" / "daily" / "b.csv", b"date,close\n2020-01-02,2\n")
    _write(root / "src" / "service.py", b"VALUE = 1\n")

    report = audit_tree(root)

    assert report.summary.file_count == 4
    assert sum(item.count for item in report.items) == report.summary.file_count
    data_items = [item for item in report.items if item.asset_type is AssetType.DATA]
    assert len(data_items) == 1
    assert data_items[0].count == 2
    assert data_items[0].classification is Classification.DATA_ONLY
    assert data_items[0].path_pattern.startswith("ignored-data/daily/")


def test_sensitive_configuration_and_binary_content_is_never_read_or_rendered(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy"
    secret = b"PASSWORD=do-not-leak\nTOKEN=also-private\n"
    _write(root / ".env", secret)
    _write(root / "models" / "account-12345678.pkl", secret)
    _write(root / "src" / "safe.py", b"ANSWER = 42\n")
    filesystem = ControlledFileSystem()

    report = audit_tree(root, filesystem=filesystem)
    rendered = render_json(report) + render_markdown(report)

    assert "do-not-leak" not in rendered
    assert "also-private" not in rendered
    assert "12345678" not in rendered
    assert filesystem.read_paths == []
    assert not report.code_hashing_enabled
    assert report.hashed_file_count == 0
    sensitive = next(item for item in report.items if item.asset_type is AssetType.CONFIG)
    model = next(item for item in report.items if item.asset_type is AssetType.MODEL)
    assert sensitive.checksum_sha256 is None
    assert model.checksum_sha256 is None


def test_optional_hashing_reads_only_synthetic_non_sensitive_code(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    _write(root / ".env", b"SYNTHETIC_SECRET=not-read\n")
    _write(root / "src" / "safe.py", b"ANSWER = 42\n")
    filesystem = ControlledFileSystem()

    report = audit_tree(root, filesystem=filesystem, hash_small_code=True)

    assert filesystem.read_paths == [root / "src" / "safe.py"]
    assert report.code_hashing_enabled
    assert report.hashed_file_count == 1
    code = next(item for item in report.items if item.asset_type is AssetType.CODE)
    assert code.checksum_sha256 is not None


def test_unreadable_items_and_directory_links_are_recorded_without_following(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy"
    _write(root / "blocked" / "unseen.py", b"must not be reached")
    _write(root / "junction" / "escape.py", b"must not be reached")
    _write(root / "visible.py", b"VISIBLE = True\n")
    filesystem = ControlledFileSystem(unreadable="blocked", link="junction")

    report = audit_tree(root, filesystem=filesystem)

    assert [(item.path, item.reason) for item in report.skipped] == [
        ("blocked", "permission_denied"),
        ("junction", "link_not_followed"),
    ]
    rendered = render_json(report)
    assert "unseen.py" not in rendered
    assert "escape.py" not in rendered
    assert report.summary.skipped_count == 2


def test_stable_ordering_summary_conservation_and_complete_classification_contract(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy"
    _write(root / "zeta" / "README.md", b"historical notes")
    _write(root / "alpha" / "settings.toml", b"endpoint = 'redacted'\n")
    _write(root / "data" / "prices.parquet", b"PAR1synthetic")
    _write(root / "logs" / "failed-run.jsonl", b"synthetic log")
    _write(root / "models" / "ranker.joblib", b"synthetic model")
    _write(root / "db" / "quant.db", b"synthetic database")
    _write(root / "src" / "portfolio.py", b"def build():\n    return None\n")

    first = audit_tree(root)
    second = audit_tree(root)

    assert render_json(first) == render_json(second)
    assert render_markdown(first) == render_markdown(second)
    assert [item.path_pattern.casefold() for item in first.items] == sorted(
        item.path_pattern.casefold() for item in first.items
    )
    assert first.summary.file_count == sum(item.count for item in first.items)
    assert first.summary.total_bytes == sum(item.total_bytes for item in first.items)
    assert set(first.summary.by_asset_type) == {item.value for item in AssetType}
    assert set(first.summary.by_classification) == {item.value for item in Classification}
    assert all(item.rationale for item in first.items)
    assert all(item.dependencies for item in first.items)
    assert all(item.global_state for item in first.items)
    assert all(item.external_side_effects for item in first.items)
    assert all(item.semantic_risks for item in first.items)
    assert all(item.existing_tests for item in first.items)
    assert all(item.missing_tests for item in first.items)
    assert all(item.target_stage for item in first.items)
    assert all(item.target_module for item in first.items)
    assert all(item.disposition for item in first.items)
    assert all(not item.migrated for item in first.items)
    assert all(
        item.classification is not Classification.REUSE
        for item in first.items
        if item.asset_type is AssetType.CODE
    )


def test_code_extension_wins_over_data_directory_and_pyc_is_generated(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    _write(root / ".worktrees" / "v3-stage-0" / "data" / "adapter.py", b"VALUE = 1\n")
    _write(root / ".worktrees" / "v3-stage-0" / "data" / "adapter.pyc", b"compiled")
    _write(root / ".worktrees" / "v3-stage-0" / "tests" / "test_adapter.pyc", b"compiled")

    report = audit_tree(root)

    source = next(item for item in report.items if item.path_pattern.endswith("adapter.py"))
    generated = next(item for item in report.items if item.path_pattern.endswith("*.pyc"))
    assert source.asset_type is AssetType.CODE
    assert source.classification is not Classification.DATA_ONLY
    assert generated.asset_type is AssetType.OTHER
    assert generated.classification is Classification.DISCARD
    patterns = [item.path_pattern for item in report.items]
    assert len(patterns) == len(set(patterns))


def test_ide_metadata_is_not_classified_as_business_data(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    _write(root / ".idea" / "workspace.xml", b"<project />")

    report = audit_tree(root)

    item = report.items[0]
    assert item.asset_type is AssetType.OTHER
    assert item.classification is Classification.DISCARD


def test_git_metadata_path_tokens_cannot_create_model_candidates(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    _write(root / ".git" / "refs" / "heads" / "model" / "candidate", b"deadbeef\n")

    report = audit_tree(root)

    item = report.items[0]
    assert item.asset_type is AssetType.OTHER
    assert item.classification is Classification.DISCARD


def test_dry_run_does_not_write_source_and_fingerprints_match(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    _write(root / "src" / "tool.py", b"VALUE = 1\n")
    before = {path: path.stat().st_mtime_ns for path in root.rglob("*")}

    report = audit_tree(root)

    after = {path: path.stat().st_mtime_ns for path in root.rglob("*")}
    assert before == after
    assert report.source_unchanged
    assert report.before_fingerprint == report.after_fingerprint
    assert not (root / "legacy_inventory.json").exists()


def test_report_writes_are_confined_to_allowed_root_and_never_source(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    output_root = tmp_path / "v3"
    _write(source / "README.md", b"legacy")
    report = audit_tree(source)

    with pytest.raises(ValueError, match="fingerprint changed"):
        write_reports(
            replace(report, source_unchanged=False),
            json_path=output_root / "inventory.json",
            markdown_path=output_root / "inventory.md",
            allowed_output_root=output_root,
            source_root=source,
        )

    with pytest.raises(ValueError, match="source tree"):
        write_reports(
            report,
            json_path=source / "inventory.json",
            markdown_path=source / "inventory.md",
            allowed_output_root=tmp_path,
            source_root=source,
        )
    with pytest.raises(ValueError, match="allowed output root"):
        write_reports(
            report,
            json_path=tmp_path.parent / "inventory.json",
            markdown_path=tmp_path.parent / "inventory.md",
            allowed_output_root=output_root,
            source_root=source,
        )

    write_reports(
        report,
        json_path=output_root / "legacy_inventory.json",
        markdown_path=output_root / "legacy_inventory.md",
        allowed_output_root=output_root,
        source_root=source,
    )
    assert (output_root / "legacy_inventory.json").read_text(encoding="utf-8").endswith("\n")
    assert "候选审计" in (output_root / "legacy_inventory.md").read_text(encoding="utf-8")
