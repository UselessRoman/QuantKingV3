from __future__ import annotations

import sys
from pathlib import Path

from quantking.common.compatibility import ProbeErrorCode, ProbeStatus
from quantking.data.adapters.xtquant_probe import probe_xtquant


def _fake_xtquant_root(tmp_path: Path, *, native: bool = True) -> Path:
    site_packages = tmp_path / "isolated-site-packages"
    package = site_packages / "xtquant"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        """
import requests
import socket

REQUEST_BLOCKED = False
SOCKET_BLOCKED = False
try:
    requests.get("https://updates.invalid/check")
except RuntimeError:
    REQUEST_BLOCKED = True
try:
    socket.create_connection(("updates.invalid", 443))
except RuntimeError:
    SOCKET_BLOCKED = True
__version__ = "test-1"
""".lstrip(),
        encoding="utf-8",
    )
    (package / "xtdata.py").write_text(
        """
from . import REQUEST_BLOCKED, SOCKET_BLOCKED
if not REQUEST_BLOCKED or not SOCKET_BLOCKED:
    raise RuntimeError("network guards were not installed before import")

def get_market_data_ex():
    raise AssertionError("read-only API must only be inspected, never called")
""".lstrip(),
        encoding="utf-8",
    )
    if native:
        (package / "native.cp312-win_amd64.pyd").write_bytes(b"synthetic marker")
    return site_packages


def test_xtquant_probe_isolates_root_and_blocks_update_and_socket_calls(tmp_path: Path) -> None:
    root = _fake_xtquant_root(tmp_path)
    before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))

    result = probe_xtquant(root)

    after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
    assert result.status is ProbeStatus.PASSED
    assert result.version == "test-1"
    assert "network_guards_installed" in result.steps
    assert "native_cp312_verified" in result.steps
    assert before == after
    assert not any(name == "xtquant" or name.startswith("xtquant.") for name in sys.modules)


def test_xtquant_probe_requires_a_cp312_native_file_from_explicit_root(tmp_path: Path) -> None:
    result = probe_xtquant(_fake_xtquant_root(tmp_path, native=False))

    assert result.status is ProbeStatus.FAILED
    assert result.error_code is ProbeErrorCode.SECURITY_BOUNDARY
    assert result.message == "XTQuant cp312 native module is missing from explicit root"
