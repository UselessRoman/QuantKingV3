from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _runtime_environment(tmp_path: Path, *, data_name: str) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("QUANTKING_")
    }
    environment.update(
        {
            "QUANTKING_CODE_ROOT": str(Path.cwd()),
            "QUANTKING_DATA_ROOT": str(tmp_path / data_name),
            "QUANTKING_OFFSITE_BACKUP_ROOT": str(tmp_path / f"{data_name}-offsite"),
            "QUANTKING_XTQUANT_SITE_PACKAGES_ROOT": str(tmp_path / "missing-sdk"),
        }
    )
    return environment


def test_module_help_does_not_construct_runtime_or_load_sdk(tmp_path: Path) -> None:
    data_root = tmp_path / "help-data"
    completed = subprocess.run(
        [sys.executable, "-m", "quantking.cli.eod", "--help"],
        cwd=Path.cwd(),
        env=_runtime_environment(tmp_path, data_name="help-data"),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0
    assert "eod-enqueue" in completed.stdout
    assert not data_root.exists()


def test_subprocess_cli_uses_formal_composition_to_enqueue_only(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "quantking.cli.eod",
            "eod-enqueue",
            "--trade-date",
            "2026-07-15",
            "--datasets",
            "exchange_calendar",
            "--idempotency-key",
            "subprocess-enqueue",
            "--publish-intent",
            "PUBLISH_AFTER_QUALITY",
        ],
        cwd=Path.cwd(),
        env=_runtime_environment(tmp_path, data_name="enqueue-data"),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "PENDING"
    assert payload["created"] is True
    assert (tmp_path / "enqueue-data" / "metadata" / "quantking.db").is_file()
    assert not (tmp_path / "enqueue-data" / "raw").exists()


def test_web_module_help_is_startup_safe(tmp_path: Path) -> None:
    data_root = tmp_path / "web-help-data"
    completed = subprocess.run(
        [sys.executable, "-m", "quantking.web.main", "--help"],
        cwd=Path.cwd(),
        env=_runtime_environment(tmp_path, data_name="web-help-data"),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0
    assert "quantking-web" in completed.stdout
    assert not data_root.exists()
