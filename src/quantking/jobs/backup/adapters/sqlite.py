"""Bounded SQLite online-backup adapter for active WAL databases."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from time import monotonic

from quantking.jobs.backup.models import BackupError, BackupFailureCode


class SQLiteOnlineBackup:
    """Copy committed pages with sqlite3.Connection.backup, never file copy."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        pages_per_step: int = 64,
        busy_sleep_seconds: float = 0.01,
    ) -> None:
        if timeout_seconds <= 0 or pages_per_step <= 0 or busy_sleep_seconds < 0:
            raise ValueError("SQLite backup bounds must be positive")
        self._timeout_seconds = timeout_seconds
        self._pages_per_step = pages_per_step
        self._busy_sleep_seconds = busy_sleep_seconds

    def backup(self, source: Path, target: Path) -> None:
        deadline = monotonic() + self._timeout_seconds

        def progress(_status: int, _remaining: int, _total: int) -> None:
            if monotonic() > deadline:
                raise BackupError(
                    BackupFailureCode.SQLITE_BACKUP_TIMEOUT,
                    "SQLite online backup exceeded its bounded timeout",
                )

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise BackupError(
                    BackupFailureCode.DESTINATION_EXISTS,
                    "SQLite backup target already exists",
                )
            with (
                closing(sqlite3.connect(source, timeout=self._timeout_seconds)) as source_db,
                closing(sqlite3.connect(target, timeout=self._timeout_seconds)) as target_db,
            ):
                source_db.backup(
                    target_db,
                    pages=self._pages_per_step,
                    progress=progress,
                    sleep=self._busy_sleep_seconds,
                )
                quick_check = target_db.execute("PRAGMA quick_check").fetchone()
                if quick_check != ("ok",):
                    raise BackupError(
                        BackupFailureCode.SQLITE_BACKUP_FAILED,
                        "SQLite online backup failed quick_check",
                    )
        except BackupError:
            raise
        except sqlite3.Error as error:
            raise BackupError(
                BackupFailureCode.SQLITE_BACKUP_FAILED,
                "SQLite online backup failed",
            ) from error
        except OSError as error:
            raise BackupError(
                BackupFailureCode.IO_FAILED,
                "SQLite backup filesystem operation failed",
            ) from error
