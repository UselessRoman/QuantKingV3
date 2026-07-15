from __future__ import annotations

import os
from pathlib import Path

import pytest

from quantking.common.compatibility import ProbeStatus
from quantking.data.adapters.xtquant_probe import probe_xtquant


@pytest.mark.real_xtquant
def test_real_xtquant_minimal_read_only_probe() -> None:
    configured = os.environ.get("QUANTKING_XTQUANT_SITE_PACKAGES")
    if not configured:
        pytest.skip("QUANTKING_XTQUANT_SITE_PACKAGES is not configured")
    site_packages = Path(configured)
    print(f"XTQuant explicit site-packages: {site_packages}")
    print("scope: import xtquant/xtdata and inspect one minimal read-only market-data API")
    print("side effects: zero writes, zero QMT connections, zero market-data reads/downloads")

    result = probe_xtquant(site_packages)

    assert result.status is ProbeStatus.PASSED, result.to_dict()
