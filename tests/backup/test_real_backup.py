from __future__ import annotations

import os
from pathlib import Path

import pytest

from quantking.jobs.backup.acceptance import run_synthetic_acceptance


@pytest.mark.real_backup
def test_real_backup_synthetic_acceptance() -> None:
    data_root_raw = os.environ.get("QUANTKING_REAL_BACKUP_DATA_ROOT")
    offsite_root_raw = os.environ.get("QUANTKING_REAL_BACKUP_OFFSITE_ROOT")
    run_id = os.environ.get("QUANTKING_REAL_BACKUP_RUN_ID")
    if not data_root_raw or not offsite_root_raw or not run_id:
        pytest.skip("real backup roots and unique run ID are not configured")
    data_root = Path(data_root_raw)
    offsite_root = Path(offsite_root_raw)
    print(f"authorized data root: {data_root}")
    print(f"authorized offsite root: {offsite_root}")
    print(f"unique acceptance run id: {run_id}")
    print("scope: create only new synthetic acceptance directories under both roots")
    print("side effects: zero deletes, zero reads of existing user data, zero network/QMT")

    evidence = run_synthetic_acceptance(data_root, offsite_root, run_id)

    assert evidence["status"] == "passed"
