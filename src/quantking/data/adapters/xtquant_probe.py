"""Explicit-root, no-side-effect XTQuant import probe."""

from __future__ import annotations

import builtins
import importlib
import io
import os
import socket
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from unittest.mock import patch

from quantking.common.compatibility import (
    CompatibilityProbeError,
    ProbeErrorCode,
    ProbePayload,
    ProbeResult,
    capture_probe,
)


def probe_xtquant(site_packages_root: Path) -> ProbeResult:
    """Import only XTQuant market-data modules from one explicit cp312 root."""
    return capture_probe("xtquant", lambda: _run_xtquant(site_packages_root))


def _run_xtquant(site_packages_root: Path) -> ProbePayload:
    root = _validated_root(site_packages_root)
    package_root = (root / "xtquant").resolve(strict=False)
    if not package_root.is_dir() or not (package_root / "__init__.py").is_file():
        raise CompatibilityProbeError(
            ProbeErrorCode.SECURITY_BOUNDARY,
            "XTQuant package is missing from explicit root",
        )
    native_files = tuple(
        path
        for path in package_root.rglob("*.pyd")
        if "cp312" in path.name.casefold() and _is_below(path.resolve(strict=False), root)
    )
    if not native_files:
        raise CompatibilityProbeError(
            ProbeErrorCode.SECURITY_BOUNDARY,
            "XTQuant cp312 native module is missing from explicit root",
        )
    if any(name == "xtquant" or name.startswith("xtquant.") for name in sys.modules):
        raise CompatibilityProbeError(
            ProbeErrorCode.SECURITY_BOUNDARY,
            "XTQuant must not be imported before the isolated probe",
        )

    imported_names: set[str] = set()
    previous_bytecode_policy = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    try:
        importlib.invalidate_caches()
        with _blocked_external_effects():
            package = importlib.import_module("xtquant")
            imported_names.update(
                name for name in sys.modules if name == "xtquant" or name.startswith("xtquant.")
            )
            market_data = importlib.import_module("xtquant.xtdata")
            imported_names.update(
                name for name in sys.modules if name == "xtquant" or name.startswith("xtquant.")
            )
            _verify_module_root(package, root)
            _verify_module_root(market_data, root)
            for name in imported_names:
                module = sys.modules.get(name)
                if module is not None:
                    _verify_module_root(module, root)
            version = getattr(package, "__version__", None)
            if not isinstance(version, str) or not version.strip():
                raise CompatibilityProbeError(
                    ProbeErrorCode.VERSION_UNAVAILABLE,
                    "XTQuant version is unavailable",
                )
            read_api = getattr(market_data, "get_market_data_ex", None)
            if not callable(read_api):
                raise CompatibilityProbeError(
                    ProbeErrorCode.INTERFACE_MISMATCH,
                    "XTQuant minimal read-only market-data API is unavailable",
                )
            trading_module = ".".join(("xtquant", "xttrader"))
            if trading_module in sys.modules:
                raise CompatibilityProbeError(
                    ProbeErrorCode.SECURITY_BOUNDARY,
                    "XTQuant trading module was imported by the read-only probe",
                )
    finally:
        sys.dont_write_bytecode = previous_bytecode_policy
        if sys.path and sys.path[0] == str(root):
            sys.path.pop(0)
        else:
            with suppress(ValueError):
                sys.path.remove(str(root))
        for name in tuple(sys.modules):
            if name == "xtquant" or name.startswith("xtquant."):
                sys.modules.pop(name, None)
        importlib.invalidate_caches()

    return ProbePayload(
        version,
        (
            "explicit_package_root_verified",
            "native_cp312_verified",
            "network_guards_installed",
            "filesystem_write_guards_installed",
            "xtquant_imported",
            "xtdata_imported",
            "read_only_api_present",
            "trading_module_absent",
            "zero_qmt_calls",
            "zero_market_data_reads",
        ),
    )


def _validated_root(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        raise CompatibilityProbeError(
            ProbeErrorCode.SECURITY_BOUNDARY,
            "XTQuant site-packages root must be an explicit absolute path",
        )
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise CompatibilityProbeError(
            ProbeErrorCode.SECURITY_BOUNDARY,
            "XTQuant site-packages root is unavailable",
        ) from error
    if not resolved.is_dir():
        raise CompatibilityProbeError(
            ProbeErrorCode.SECURITY_BOUNDARY,
            "XTQuant site-packages root is not a directory",
        )
    return resolved


def _is_below(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _verify_module_root(module: ModuleType, root: Path) -> None:
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str) or not _is_below(Path(module_file).resolve(), root):
        raise CompatibilityProbeError(
            ProbeErrorCode.SECURITY_BOUNDARY,
            "XTQuant module escaped the explicit site-packages root",
        )


@contextmanager
def _blocked_external_effects() -> Iterator[None]:
    import requests

    def deny_network(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("XTQuant probe blocks network access")

    def deny_write(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("XTQuant probe blocks filesystem writes")

    original_builtin_open = cast(Callable[..., Any], builtins.open)
    original_io_open = cast(Callable[..., Any], io.open)
    original_os_open = cast(Callable[..., int], os.open)

    def guarded_open(
        file: Any,
        mode: str = "r",
        *args: object,
        **kwargs: object,
    ) -> Any:
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            deny_write()
        return original_builtin_open(file, mode, *args, **kwargs)

    def guarded_io_open(
        file: Any,
        mode: str = "r",
        *args: object,
        **kwargs: object,
    ) -> Any:
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            deny_write()
        return original_io_open(file, mode, *args, **kwargs)

    def guarded_os_open(path: Any, flags: int, *args: object, **kwargs: object) -> int:
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
        if flags & write_flags:
            deny_write()
        return original_os_open(path, flags, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(requests, "get", deny_network))
        stack.enter_context(patch.object(requests.sessions.Session, "request", deny_network))
        stack.enter_context(patch.object(socket, "create_connection", deny_network))
        stack.enter_context(patch.object(socket, "getaddrinfo", deny_network))
        stack.enter_context(patch.object(socket.socket, "connect", deny_network))
        stack.enter_context(patch.object(socket.socket, "connect_ex", deny_network))
        stack.enter_context(patch.object(builtins, "open", guarded_open))
        stack.enter_context(patch.object(io, "open", guarded_io_open))
        stack.enter_context(patch.object(os, "open", guarded_os_open))
        for target, name in (
            (os, "mkdir"),
            (os, "makedirs"),
            (os, "remove"),
            (os, "unlink"),
            (os, "rmdir"),
            (os, "rename"),
            (os, "replace"),
        ):
            stack.enter_context(patch.object(target, name, deny_write))
        yield
