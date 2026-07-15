"""SQLite-only Stage 0 compatibility probe."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from quantking.common.compatibility import (
    CompatibilityProbeError,
    ProbeErrorCode,
    ProbePayload,
    ProbeResult,
    capture_probe,
)


def probe_sqlite(root: Path) -> ProbeResult:
    """Create, read and check a temporary SQLite WAL database."""
    return capture_probe("sqlite", lambda: _run_sqlite(root))


def _run_sqlite(root: Path) -> ProbePayload:
    database = root / "sqlite-compatibility.db"
    with sqlite3.connect(database) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
            raise CompatibilityProbeError(
                ProbeErrorCode.INTERFACE_MISMATCH,
                "SQLite WAL mode is unavailable",
            )
        connection.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO probe(value) VALUES ('ok')")
        connection.commit()
        value = connection.execute("SELECT value FROM probe WHERE id = 1").fetchone()
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
    if value != ("ok",) or quick_check != ("ok",):
        raise CompatibilityProbeError(
            ProbeErrorCode.EXECUTION_FAILED,
            "SQLite create/read/quick_check probe failed",
        )
    return ProbePayload(
        sqlite3.sqlite_version,
        ("wal_enabled", "temporary_row_created", "row_read", "quick_check_ok"),
    )
