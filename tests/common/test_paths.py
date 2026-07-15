import subprocess
from pathlib import Path

import pytest

from quantking.common.errors import InvalidPathError
from quantking.common.paths import RuntimePaths, resolve_under


def test_runtime_paths_are_absolute_separate_and_do_not_create_directories(tmp_path: Path) -> None:
    paths = RuntimePaths(
        code_root=tmp_path / "code",
        data_root=tmp_path / "data",
        offsite_backup_root=tmp_path / "offsite",
    )

    assert paths.local_backup_root == tmp_path / "data" / "backups"
    assert not paths.code_root.exists()
    assert not paths.data_root.exists()
    assert not paths.offsite_backup_root.exists()
    assert not paths.local_backup_root.exists()


@pytest.mark.parametrize("field", ["code_root", "data_root", "offsite_backup_root"])
def test_runtime_paths_reject_relative_roots(tmp_path: Path, field: str) -> None:
    values = {
        "code_root": tmp_path / "code",
        "data_root": tmp_path / "data",
        "offsite_backup_root": tmp_path / "offsite",
    }
    values[field] = Path("relative")

    with pytest.raises(InvalidPathError):
        RuntimePaths(**values)  # type: ignore[arg-type]


def test_runtime_paths_reject_case_insensitive_overlap(tmp_path: Path) -> None:
    with pytest.raises(InvalidPathError, match="overlap"):
        RuntimePaths(
            code_root=tmp_path / "ManagedRoot",
            data_root=tmp_path / "managedroot" / "data",
            offsite_backup_root=tmp_path / "offsite",
        )


def test_resolve_under_accepts_only_relative_descendants_without_creating_them(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    result = resolve_under(root, Path("reports") / "result.json")

    assert result == root / "reports" / "result.json"
    assert not result.exists()
    with pytest.raises(InvalidPathError):
        resolve_under(root, Path("..") / "escape.json")
    with pytest.raises(InvalidPathError):
        resolve_under(root, tmp_path / "absolute.json")


def test_resolve_under_rejects_existing_symlink_or_junction_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        junction = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
            check=False,
            capture_output=True,
            text=True,
        )
        if junction.returncode != 0:
            pytest.skip(f"symlink and junction creation unavailable: {error}")

    with pytest.raises(InvalidPathError, match="escape"):
        resolve_under(root, Path("escape") / "artifact.json")
