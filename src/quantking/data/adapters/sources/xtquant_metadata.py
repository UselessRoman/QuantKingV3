"""Explicit-root XTQuant distribution metadata inspection.

The C1A control path needs the installed package version before an
authorization can be claimed, but must not import ``xtquant`` (or inspect the
machine-wide Python environment) to obtain it.  This probe therefore only
reads one caller-supplied, absolute site-packages directory and its local
``*.dist-info/METADATA`` record.
"""

from __future__ import annotations

import re
from pathlib import Path

from quantking.common.paths import is_reparse_point

_DIST_INFO = re.compile(r"^xtquant-[A-Za-z0-9][A-Za-z0-9_.+-]*\.dist-info$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)+$")


class XtQuantPackageMetadataError(ValueError):
    """The explicit metadata root cannot provide one exact XTQuant version."""


class ExplicitXtQuantPackageMetadataProbe:
    """Read exactly one XTQuant distribution record below an explicit root."""

    def __init__(self, *, site_packages_root: Path) -> None:
        if not isinstance(site_packages_root, Path) or not site_packages_root.is_absolute():
            raise XtQuantPackageMetadataError(
                "XTQuant site-packages root must be an explicit absolute Path"
            )
        self._root = site_packages_root

    def exact_version(self) -> str:
        """Return an exact stable version or fail without importing the package."""
        root = self._validated_root()
        try:
            candidates = [item for item in root.iterdir() if _DIST_INFO.fullmatch(item.name)]
        except OSError as error:
            raise XtQuantPackageMetadataError("XTQuant package metadata is unreadable") from error
        if len(candidates) != 1:
            raise XtQuantPackageMetadataError("XTQuant package metadata is unavailable")
        try:
            if candidates[0].is_symlink() or is_reparse_point(candidates[0]):
                raise XtQuantPackageMetadataError("XTQuant package metadata is unsafe")
        except OSError as error:
            raise XtQuantPackageMetadataError("XTQuant package metadata is unreadable") from error
        metadata = candidates[0] / "METADATA"
        if not metadata.is_file() or metadata.is_symlink() or is_reparse_point(metadata):
            raise XtQuantPackageMetadataError("XTQuant package metadata is unavailable")
        try:
            fields = _metadata_fields(metadata.read_text(encoding="utf-8"))
        except OSError as error:
            raise XtQuantPackageMetadataError("XTQuant package metadata is unreadable") from error
        if fields.get("name", "").casefold() != "xtquant":
            raise XtQuantPackageMetadataError("XTQuant package metadata name is invalid")
        version = fields.get("version")
        if version is None or not _VERSION.fullmatch(version):
            raise XtQuantPackageMetadataError("XTQuant package metadata version is invalid")
        return version

    def _validated_root(self) -> Path:
        if self._root.is_symlink() or is_reparse_point(self._root):
            raise XtQuantPackageMetadataError("XTQuant site-packages root is unsafe")
        try:
            root = self._root.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise XtQuantPackageMetadataError(
                "XTQuant site-packages root is unavailable"
            ) from error
        if not root.is_dir() or root.is_symlink() or is_reparse_point(root):
            raise XtQuantPackageMetadataError("XTQuant site-packages root is unsafe")
        return root


def _metadata_fields(payload: str) -> dict[str, str]:
    """Parse only the two RFC-822-like fields the control path permits."""
    fields: dict[str, str] = {}
    for line in payload.splitlines():
        if not line:
            break
        name, separator, value = line.partition(":")
        if not separator or not name or not value.startswith(" "):
            raise XtQuantPackageMetadataError("XTQuant package metadata is malformed")
        lowered = name.casefold()
        if lowered in {"name", "version"}:
            if lowered in fields:
                raise XtQuantPackageMetadataError("XTQuant package metadata is ambiguous")
            fields[lowered] = value[1:]
    return fields


__all__ = ["ExplicitXtQuantPackageMetadataProbe", "XtQuantPackageMetadataError"]
