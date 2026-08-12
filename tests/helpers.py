"""Small typed factories shared by offline tests."""

from __future__ import annotations

from pathlib import Path

from quantking.common.config import AppSettings
from quantking.common.paths import RuntimePaths


def app_settings(root: Path) -> AppSettings:
    """Build validated settings whose managed data roots stay inside a temp parent."""
    return AppSettings(
        paths=RuntimePaths(
            code_root=Path.cwd().resolve(),
            data_root=(root / "runtime-data").resolve(),
            offsite_backup_root=(root / "offsite").resolve(),
        ),
        web_host="127.0.0.1",
        web_port=8765,
        sqlite_busy_timeout_ms=250,
        sqlite_max_attempts=7,
        sqlite_retry_base_delay_ms=10,
        sqlite_retry_cap_delay_ms=160,
        sqlite_retry_jitter_ratio=0.0,
        job_heartbeat_min_interval_ms=1000,
        job_progress_min_interval_ms=250,
    )


__all__ = ["app_settings"]
