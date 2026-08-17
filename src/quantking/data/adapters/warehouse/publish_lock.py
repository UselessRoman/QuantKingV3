"""Inter-process single-publisher lock for canonical version publishes.

The formal topology may run the Web process and one or more Worker processes
against the same data root. The in-process ``threading.RLock`` in the
warehouse cannot prevent two processes from racing into the same
``.incomplete`` staging directory, so every publish additionally takes an
exclusive lock file under ``warehouse/<dataset>/.locks/<version>.lock``.

Mutual exclusion uses ``os.open`` with ``O_CREAT | O_EXCL`` — atomic on
Windows and POSIX alike, with no dependency on ``msvcrt``/``fcntl`` locking
semantics. The lock file records the owner identity (PID) and an ISO
creation timestamp. A live conflict raises :class:`PublishInProgressError`
fail-closed; a holder that died without releasing leaves a stale lock that
only the explicit recovery flow (:func:`recover_stale_publish_locks`) may
classify and rename aside — it is never silently broken or deleted, so a
hard interruption can never be mistaken for a completed publish.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from quantking.common.time import UtcTimestamp, utc_now

LOCK_DIRECTORY_NAME = ".locks"
_LOCK_SUFFIX = ".lock"
_STALE_PREFIX = ".stale-"


class PublishInProgressError(RuntimeError):
    """Another live process owns the publish lock for this version."""

    code = "PUBLISH_LOCK_HELD"

    def __init__(self, *, owner: str) -> None:
        super().__init__(f"{self.code}:owner={owner}")
        self.owner = owner


@dataclass(frozen=True, slots=True)
class LockRecoveryOutcome:
    """Classification of one stale lock file moved aside for a new task."""

    lock_path: Path
    preserved_path: Path
    owner: str


def lock_path_for(data_root: Path, dataset: str, version_directory_name: str) -> Path:
    """Return the managed lock path for one canonical version directory."""
    return (
        data_root
        / "warehouse"
        / dataset
        / LOCK_DIRECTORY_NAME
        / (version_directory_name + _LOCK_SUFFIX)
    )


class VersionPublishLock:
    """Exclusive cross-process lock held across staging and final promotion."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._acquired = False

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise PublishInProgressError(owner=self._read_owner()) from error
        try:
            payload = f"pid={os.getpid()}\nacquired_at={utc_now().to_iso()}\n"
            os.write(descriptor, payload.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._acquired = True

    def release(self) -> None:
        if not self._acquired:
            return
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()
        self._acquired = False

    def _read_owner(self) -> str:
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return "unknown"
        for line in text.splitlines():
            if line.startswith("pid="):
                return line[4:].strip() or "unknown"
        return "unknown"

    def __enter__(self) -> VersionPublishLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


def recover_stale_publish_lock(
    lock_path: Path,
    *,
    stale_after: timedelta,
    now: UtcTimestamp | None = None,
) -> LockRecoveryOutcome | None:
    """Classify one stale lock and preserve it aside for forensic audit.

    A lock is stale when it is older than ``stale_after``. Stale locks are
    renamed to ``.stale-<timestamp>-<name>.lock`` (never deleted) so a new
    task may rerun the publish while the interrupted attempt remains
    auditable. Live locks are left untouched and reported as ``None``.
    """
    from quantking.common.time import utc_now as _utc_now

    current = now or _utc_now()
    if not isinstance(current, UtcTimestamp):
        raise TypeError("now must be a UtcTimestamp")
    if not lock_path.is_file():
        return None
    try:
        text = lock_path.read_text(encoding="utf-8")
    except OSError:
        return None
    acquired: UtcTimestamp | None = None
    owner = "unknown"
    for line in text.splitlines():
        if line.startswith("acquired_at="):
            try:
                acquired = UtcTimestamp.from_iso(line[12:].strip())
            except ValueError:
                acquired = None
        elif line.startswith("pid="):
            owner = line[4:].strip() or "unknown"
    if acquired is None or current.value - acquired.value < stale_after:
        return None
    preserved = lock_path.with_name(
        f"{_STALE_PREFIX}{current.to_iso().replace(':', '').replace('+', '-')}-{lock_path.name}"
    )
    os.replace(lock_path, preserved)
    return LockRecoveryOutcome(lock_path=lock_path, preserved_path=preserved, owner=owner)


__all__ = [
    "LOCK_DIRECTORY_NAME",
    "LockRecoveryOutcome",
    "PublishInProgressError",
    "VersionPublishLock",
    "lock_path_for",
    "recover_stale_publish_lock",
]
