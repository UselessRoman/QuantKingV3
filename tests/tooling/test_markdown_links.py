from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_markdown_links.py"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_checker_supports_links_images_decode_directories_anchors_and_fences(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    images = docs / "images"
    section = docs / "section"
    images.mkdir(parents=True)
    section.mkdir()
    (images / "My Chart.png").write_bytes(b"png")
    (section / "README.md").write_text("# Section Home\n", encoding="utf-8")
    (docs / "guide.md").write_text(
        "# Install Guide\n\n## Repeated\n## Repeated\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "\n".join(
            [
                "[guide](docs/guide.md#install-guide)",
                "![chart](docs/images/My%20Chart.png)",
                "[section](docs/section/)",
                "[duplicate](docs/guide.md#repeated-1)",
                "[web](https://example.com) [mail](mailto:test@example.com)",
                "```markdown",
                "[ignored](missing.md)",
                "```",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "BROKEN.md").write_text("[bad](none.md)", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "BROKEN.md").write_text("[bad](none.md)", encoding="utf-8")

    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "markdown links: OK" in result.stdout


def test_checker_reports_escape_missing_target_and_anchor_in_stable_order(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "target.md").write_text("# Existing\n", encoding="utf-8")
    (docs / "z.md").write_text("[missing](none.md)\n", encoding="utf-8")
    (docs / "a.md").write_text(
        "[escape](../../outside.md)\n[anchor](target.md#absent)\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Root\n", encoding="utf-8")

    result = _run(tmp_path)

    assert result.returncode == 1
    lines = [line for line in result.stdout.splitlines() if line.startswith("docs/")]
    assert lines == sorted(lines, key=str.casefold)
    assert len(lines) == 3
    assert "escapes repository root" in lines[0]
    assert "missing anchor" in lines[1]
    assert "missing target" in lines[2]
