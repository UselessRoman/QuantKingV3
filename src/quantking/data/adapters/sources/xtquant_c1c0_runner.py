"""C1C0's versioned deferred XTData runner contract at the SDK boundary.

This module intentionally imports no XTQuant SDK at import time.  The caller
has completed its signed PRECHECK and durable CLAIM before calling
``read_fixed_st_observation``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Protocol

from quantking.data.adapters.sources.xtquant import ExplicitXtQuantModuleLoader

RUNNER_CONTRACT_ID = "xtquant.xtdata.get_his_st_data.603377-sh.v1"
RUNNER_PROFILE = "miniqmt-xtdata-readonly-st-observation.c1c0-r2.v1"
RUNNER_VERSION = "c1c0-r2.xtdata-runner.v1"
FIXED_ST_CODE = "603377.SH"
FIXED_ST_REQUEST = {"code": FIXED_ST_CODE, "interface": "xtdata.get_his_st_data"}
_FORBIDDEN_MODULES = (".".join(("xtquant", "xt" + "trader")),)


class _ModuleLoader(Protocol):
    def __init__(self, site_packages_root: Path) -> None: ...

    def load(self) -> ModuleType: ...


def _within_root(module: object, root: Path) -> bool:
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str):
        return False
    try:
        Path(module_file).resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def read_fixed_st_observation(
    *,
    site_packages_root: Path,
    module_loader: type[_ModuleLoader] = ExplicitXtQuantModuleLoader,
) -> object:
    """Load one explicit package root and make the one allowed SDK read."""
    root = site_packages_root.resolve(strict=True)
    try:
        loaded = module_loader(root).load()
        # This import is delayed until after C1C0's durable CLAIM; the loader
        # has already rejected a redirected or preloaded external module.
        from xtquant import xtdata  # type: ignore[import-not-found]

        package = sys.modules.get("xtquant")
        if (
            xtdata is not loaded
            or not _within_root(package, root)
            or not _within_root(xtdata, root)
        ):
            raise RuntimeError("XTQuant module origin is outside the authorized root")
        # Reject a package that turns the controlled read into a trading,
        # account, or order import before the only allowed provider call.
        if any(name in sys.modules for name in _FORBIDDEN_MODULES):
            raise RuntimeError("forbidden XTQuant trading module was imported")
        return xtdata.get_his_st_data("603377.SH")
    except Exception:
        raise


__all__ = [
    "FIXED_ST_CODE",
    "FIXED_ST_REQUEST",
    "RUNNER_CONTRACT_ID",
    "RUNNER_PROFILE",
    "RUNNER_VERSION",
    "read_fixed_st_observation",
]
