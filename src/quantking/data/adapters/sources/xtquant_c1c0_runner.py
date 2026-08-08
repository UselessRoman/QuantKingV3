"""C1C0's versioned deferred XTData runner contract at the SDK boundary.

This module intentionally imports no XTQuant SDK at import time.  The caller
has completed its signed PRECHECK and durable CLAIM before calling
``read_fixed_st_observation``.
"""

from __future__ import annotations

import sys
from pathlib import Path

RUNNER_CONTRACT_ID = "xtquant.xtdata.get_his_st_data.603377-sh.v1"
RUNNER_PROFILE = "miniqmt-xtdata-readonly-st-observation.c1c0.v1"
RUNNER_VERSION = "c1c0.xtdata-runner.v1"
_FORBIDDEN_MODULES = (".".join(("xtquant", "xt" + "trader")),)


def read_fixed_st_observation(*, site_packages_root: Path) -> object:
    """Load one explicit package root and make the one allowed SDK read."""
    old_path = list(sys.path)
    previous = {
        name: sys.modules.pop(name)
        for name in tuple(sys.modules)
        if name == "xtquant" or name.startswith("xtquant.")
    }
    try:
        sys.path.insert(0, str(site_packages_root))
        # The C1C0 contract permits exactly this deferred import.
        from xtquant import xtdata  # type: ignore[import-not-found]

        # Reject a package that turns the controlled read into a trading,
        # account, or order import before the only allowed provider call.
        if any(name in sys.modules for name in _FORBIDDEN_MODULES):
            raise RuntimeError("forbidden XTQuant trading module was imported")
        return xtdata.get_his_st_data("603377.SH")
    finally:
        for name in tuple(sys.modules):
            if name == "xtquant" or name.startswith("xtquant."):
                del sys.modules[name]
        sys.modules.update(previous)
        sys.path[:] = old_path


__all__ = ["RUNNER_CONTRACT_ID", "RUNNER_PROFILE", "RUNNER_VERSION", "read_fixed_st_observation"]
