"""Stable streaming file-copy and disk-space adapters."""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from quantking.jobs.backup.models import BackupError, BackupFailureCode
from quantking.jobs.backup.ports import StableCopyResult

_CHUNK_SIZE = 1024 * 1024


class SystemDiskSpace:
    """Read free bytes without reserving or creating a directory."""

    def free_bytes(self, path: Path) -> int:
        probe = path if path.exists() else path.parent
        try:
            return int(shutil.disk_usage(probe).free)
        except OSError as error:
            raise BackupError(
                BackupFailureCode.IO_FAILED,
                "available backup space could not be determined",
            ) from error


class LocalStableFileCopier:
    """Stream a file and reject any source change observed around the copy."""

    def __init__(self, *, after_copy: Callable[[Path], None] | None = None) -> None:
        self._after_copy = after_copy

    def copy_stable(self, source: Path, target: Path) -> StableCopyResult:
        try:
            if source.is_symlink() or source.is_junction():
                raise BackupError(
                    BackupFailureCode.INVALID_PATH,
                    "copy source must be an ordinary non-reparse file",
                )
            before = source.stat()
            digest_before = sha256_file(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest_target = hashlib.sha256()
            size = 0
            with source.open("rb") as reader, target.open("xb") as writer:
                while chunk := reader.read(_CHUNK_SIZE):
                    writer.write(chunk)
                    digest_target.update(chunk)
                    size += len(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if self._after_copy is not None:
                self._after_copy(source)
            after = source.stat()
            digest_after = sha256_file(source)
            if source.is_symlink() or source.is_junction():
                raise BackupError(
                    BackupFailureCode.INVALID_PATH,
                    "copy source became a reparse point during the copy",
                )
        except BackupError:
            raise
        except OSError as error:
            raise BackupError(
                BackupFailureCode.IO_FAILED,
                "file payload copy failed",
            ) from error

        stable_stat = (
            before.st_size == after.st_size
            and before.st_mtime_ns == after.st_mtime_ns
            and before.st_ino == after.st_ino
        )
        target_digest = digest_target.hexdigest()
        if not stable_stat or digest_before != digest_after:
            raise BackupError(
                BackupFailureCode.SOURCE_CHANGED,
                "source file changed while it was being copied",
            )
        if size != before.st_size or target_digest != digest_before:
            raise BackupError(
                BackupFailureCode.INTEGRITY_FAILED,
                "copied file does not match its stable source",
            )
        return StableCopyResult(size=size, sha256=target_digest)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_CHUNK_SIZE):
                digest.update(chunk)
    except OSError as error:
        raise BackupError(
            BackupFailureCode.IO_FAILED,
            "file checksum could not be calculated",
        ) from error
    return digest.hexdigest()


__all__ = ["LocalStableFileCopier", "StableCopyResult", "SystemDiskSpace", "sha256_file"]
