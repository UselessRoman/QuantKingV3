from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantking.jobs.backup import acceptance
from quantking.jobs.backup.models import BackupError, BackupFailureCode


def test_synthetic_acceptance_success_persists_incremental_and_restore_evidence(
    tmp_path: Path,
) -> None:
    evidence = acceptance.run_synthetic_acceptance(
        tmp_path / "data",
        tmp_path / "offsite",
        "offline-success",
    )

    assert evidence["status"] == "passed"
    assert evidence["initial"]["copied"] == evidence["initial"]["total_objects"]
    assert evidence["repeated"]["skipped"] == evidence["repeated"]["total_objects"]
    assert evidence["changed"]["copied"] == 1
    result = tmp_path / "data" / "acceptance" / "offline-success" / "acceptance-result.json"
    assert json.loads(result.read_text(encoding="utf-8"))["status"] == "passed"


def test_synthetic_acceptance_setup_failure_keeps_top_level_structured_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_setup(_source: Path) -> None:
        raise BackupError(BackupFailureCode.IO_FAILED, "injected synthetic setup failure")

    monkeypatch.setattr(acceptance, "_write_synthetic_files", fail_setup)
    with pytest.raises(BackupError):
        acceptance.run_synthetic_acceptance(
            tmp_path / "data",
            tmp_path / "offsite",
            "acceptance-failure",
        )

    run = tmp_path / "data" / "acceptance" / "acceptance-failure"
    failure = json.loads((run / "acceptance-failure.json").read_text(encoding="utf-8"))
    assert failure["status"] == "failed"
    assert failure["error_code"] == "io_failed"
    assert not (run / "acceptance-result.json").exists()
