"""Managed root separation and containment checks."""

from __future__ import annotations

import ntpath
import sys
from dataclasses import dataclass
from pathlib import Path

from quantking.common.errors import InvalidPathError


def _windows_normalized(path: Path) -> str:
    return ntpath.normcase(ntpath.normpath(str(path)))


def _same_or_descendant(candidate: Path, parent: Path) -> bool:
    normalized_candidate = _windows_normalized(candidate)
    normalized_parent = _windows_normalized(parent)
    try:
        return ntpath.commonpath([normalized_candidate, normalized_parent]) == normalized_parent
    except ValueError:
        return False


def is_reparse_point(path: Path) -> bool:
    """Return True when ``path`` is a Windows reparse point (junction/symlink).

    On non-Windows platforms this delegates to :meth:`Path.is_symlink`.
    On Windows it uses ``GetFileAttributesW`` to detect the
    ``FILE_ATTRIBUTE_REPARSE_POINT`` flag, which covers both symlinks and
    junctions. ``Path.is_junction`` only detects junctions and is not
    available on Python < 3.12; ``Path.is_symlink`` does not detect
    junctions. The Win32 attribute check is the only reliable single call
    that covers both reparse-point flavours.
    """
    if sys.platform != "win32":
        return path.is_symlink()
    try:
        import ctypes

        FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == -1:
            return False
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except (OSError, AttributeError):
        return path.is_symlink() or path.is_junction()


def _validate_absolute(path: Path, *, field: str) -> None:
    if not isinstance(path, Path) or not path.is_absolute():
        raise InvalidPathError(
            "managed roots must be absolute paths",
            context={"field": field, "path": str(path)},
        )
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            if current.exists() and is_reparse_point(current):
                raise InvalidPathError(
                    "managed roots must not contain symbolic links or junctions",
                    context={"field": field},
                )
    except InvalidPathError:
        raise
    except OSError as error:
        raise InvalidPathError(
            "managed root components could not be inspected",
            context={"field": field},
        ) from error


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """The three intentionally separate code, data, and offsite roots."""

    code_root: Path
    data_root: Path
    offsite_backup_root: Path

    def __post_init__(self) -> None:
        named_roots = (
            ("code_root", self.code_root),
            ("data_root", self.data_root),
            ("offsite_backup_root", self.offsite_backup_root),
        )
        for field, root in named_roots:
            _validate_absolute(root, field=field)

        for index, (left_name, left) in enumerate(named_roots):
            for right_name, right in named_roots[index + 1 :]:
                if _same_or_descendant(left, right) or _same_or_descendant(right, left):
                    raise InvalidPathError(
                        "managed roots must not overlap",
                        context={"left": left_name, "right": right_name},
                    )

    @property
    def local_backup_root(self) -> Path:
        """Return the sole intentional nested root without creating it."""
        return self.data_root / "backups"


def resolve_under(root: Path, relative: str | Path) -> Path:
    """Resolve a relative path while rejecting lexical and filesystem escapes."""
    _validate_absolute(root, field="root")
    if not isinstance(relative, (str, Path)):
        raise InvalidPathError("managed relative path must be text or a Path")

    relative_path = Path(relative)
    if relative_path.is_absolute() or relative_path.drive:
        raise InvalidPathError(
            "managed path must be relative", context={"path": str(relative_path)}
        )
    if ".." in relative_path.parts:
        raise InvalidPathError(
            "managed path must not contain parent traversal",
            context={"path": str(relative_path)},
        )

    # Walk every intermediate component from ``root`` to the candidate and
    # reject any that is a symlink or Windows junction/reparse point.
    # ``Path.resolve(strict=False)`` does not follow junctions on Windows,
    # so a pure resolve-and-compare would miss junction escapes.
    current = root
    try:
        for part in relative_path.parts:
            current = current / part
            if current.exists() and is_reparse_point(current):
                raise InvalidPathError(
                    "managed path must not traverse a symbolic link or junction",
                    context={"path": str(relative_path)},
                )
    except InvalidPathError:
        raise
    except OSError as error:
        raise InvalidPathError(
            "managed path could not be inspected",
            context={"path": str(relative_path)},
        ) from error

    try:
        resolved_root = root.resolve(strict=False)
        resolved_candidate = (root / relative_path).resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise InvalidPathError(
            "managed path could not be resolved", context={"path": str(relative_path)}
        ) from error

    if not _same_or_descendant(resolved_candidate, resolved_root):
        raise InvalidPathError(
            "managed path resolves to an escape outside its root",
            context={"path": str(relative_path)},
        )
    return resolved_candidate
