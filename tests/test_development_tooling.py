from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPOSITORY_ROOT = Path(__file__).parents[1]
_SCRIPTS = _REPOSITORY_ROOT / "scripts"


def _script_text(name: str) -> str:
    return (_SCRIPTS / name).read_text(encoding="utf-8")


def test_toolchain_activation_exports_pinned_executables_and_child_path() -> None:
    activation = _script_text("activate-toolchain.ps1")

    assert '"E:\\Apps\\QuantKingV3Toolchain"' in activation
    assert "E:\\Apps\\uv\\uv.exe" not in activation
    assert "$env:QUANTKING_UV_EXECUTABLE = $UvExecutable" in activation
    assert "$env:QUANTKING_NODE_EXECUTABLE = $NodeExecutable" in activation
    assert "$env:QUANTKING_NPM_EXECUTABLE = $NpmExecutable" in activation
    assert "$RequiredPathEntries = @($UvDirectory, $NodeDirectory, $PythonDirectory)" in activation
    assert "@($RequiredPathEntries) + $ExistingPathEntries" in activation


def test_tool_wrappers_activate_before_invoking_uv_or_npm() -> None:
    uv_wrapper = _script_text("uv.ps1")
    npm_wrapper = _script_text("npm.ps1")

    assert 'Join-Path $PSScriptRoot "activate-toolchain.ps1"' in uv_wrapper
    assert "& $env:QUANTKING_UV_EXECUTABLE @args" in uv_wrapper
    assert 'Join-Path $PSScriptRoot "activate-toolchain.ps1"' in npm_wrapper
    assert "& $env:QUANTKING_NPM_EXECUTABLE @args" in npm_wrapper


def test_quality_gate_uses_activation_exports_and_quiet_read_only_git() -> None:
    quality = _script_text("quality.ps1")

    assert "$Uv = $env:QUANTKING_UV_EXECUTABLE" in quality
    assert "$Npm = $env:QUANTKING_NPM_EXECUTABLE" in quality
    assert '"--no-optional-locks", "-c", "core.excludesFile=", "diff", "--check"' in quality


@pytest.mark.skipif(os.name != "nt", reason="PowerShell development tooling is Windows-only")
def test_git_read_wrapper_accepts_status_and_rejects_mutating_commands() -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell executable is unavailable")

    status = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(_SCRIPTS / "git-read.ps1"),
            "status",
            "--short",
        ],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert status.returncode == 0, status.stderr
    assert "unable to access" not in status.stderr
    assert "index.lock" not in status.stderr

    mutation = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(_SCRIPTS / "git-read.ps1"),
            "add",
        ],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert mutation.returncode != 0
