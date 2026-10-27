"""Historical authorized read-only release-result evidence extraction.

marker=S1-08R3-E1R-RESULT-READONLY-V1
authorization_id=S1-08R3-E1R-9c42e7a1b6d8

Constraints enforced by this executor (user-approved 2026-08-18):
- network_calls=0, writes=0 (inside allowed root), sqlite_checkpoints=0
- SQLite opened via file URI mode=ro&immutable=1 + PRAGMA query_only=1
  when no WAL sidecar exists; otherwise a WAL-aware mode=ro read-only
  connection (still query_only, never checkpoints, never mutates)
- directory listings single-level (<=500 entries) for declared directories;
  the published daily_bar version directory is walked bounded (<=500 files)
- file SHA256 for the published version's manifest/COMPLETE/parquet set and
  every logs/jobs eod-*.jsonl
- full-root file-set/size/mtime + per-file SHA256 fingerprint before and
  after (I0 spec; doubles as the post-E1 baseline)
- any WAL/SHM/journal at start, stale .incomplete, or publish lock is a
  blocker -> stop and report without mutating anything
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ALLOWED_ROOT = Path(r"E:\QuantKingV3Data")
MARKER = "S1-08R3-E1R-RESULT-READONLY-V1"
AUTH_ID = "S1-08R3-E1R-9c42e7a1b6d8"
ROW_LIMIT = 500
ENTRIES_LIMIT = 500
TABLES = (
    "alembic_version",
    "job_runs",
    "job_events",
    "dataset_versions",
    "dataset_version_batches",
    "ingestion_batches",
    "quality_runs",
    "quality_results",
    "difference_reports",
    "resolution_runs",
)
DIR_LISTINGS = (
    "metadata",
    "warehouse",
    "warehouse/daily_bar_unadjusted",
    "warehouse/exchange_calendar",
    "raw",
    "raw/BAOSTOCK/daily_bar_unadjusted",
    "raw/AKSHARE_TENCENT/daily_bar_unadjusted",
    "raw/AKSHARE_SINA/daily_bar_unadjusted",
    "artifacts/quality",
    "artifacts/data-diff",
    "artifacts/data-resolution",
    "logs",
    "logs/jobs",
)
E1_VERSION_DIR = "warehouse/daily_bar_unadjusted/data_version--f913f915-0a73-476a-9513-ebdca087dd90"
E1_JOB_ID = "job:cd282234-078b-47a9-b17c-12d82e3b7f50"
R3C_PROBE_JOB_ID = "job:b5027b23-6058-4204-8f6e-e81aadfbd8a2"
EVIDENCE_OUT = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "stage1"
    / "evidence"
    / "s1_08r3_e1_result_2026-08-18.json"
)
BLOCKER_SUFFIXES = ("-wal", "-shm", "-journal")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(root: Path) -> dict[str, dict[str, object]]:
    files: dict[str, dict[str, object]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            stat = full.stat()
            files[rel] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    return files


def _blockers(files: dict[str, dict[str, object]]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {
        "wal_shm_journal": [],
        "stale_incomplete": [],
        "publish_locks": [],
    }
    for rel in files:
        lower = rel.lower()
        if lower.endswith(BLOCKER_SUFFIXES):
            found["wal_shm_journal"].append(rel)
        parts = rel.split("/")
        if any(part == ".incomplete" for part in parts[:-1]) or parts[-1] == ".incomplete":
            found["stale_incomplete"].append(rel)
        if lower.endswith(".lock"):
            found["publish_locks"].append(rel)
    return found


def _root_fingerprint(
    files: dict[str, dict[str, object]],
    hashes: dict[str, str],
) -> str:
    payload = {
        rel: {
            "size": files[rel]["size"],
            "mtime_ns": files[rel]["mtime_ns"],
            "sha256": hashes.get(rel),
        }
        for rel in sorted(files)
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _list_directory(root: Path, rel: str) -> dict[str, object]:
    target = root / rel
    if not target.is_dir():
        return {"exists": False, "entries": []}
    entries: list[dict[str, object]] = []
    with os.scandir(target) as iterator:
        for index, entry in enumerate(sorted(iterator, key=lambda item: item.name)):
            if index >= ENTRIES_LIMIT:
                return {"exists": True, "truncated": True, "entries": entries}
            stat = entry.stat()
            entries.append(
                {
                    "name": entry.name,
                    "is_dir": entry.is_dir(),
                    "size": 0 if entry.is_dir() else stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
    return {"exists": True, "truncated": False, "entries": entries}


def _walk_bounded(root: Path, rel: str) -> dict[str, object]:
    """Bounded recursive listing of the published version directory."""
    target = root / rel
    if not target.is_dir():
        return {"exists": False, "files": []}
    files: list[dict[str, object]] = []
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames.sort()
        for name in sorted(filenames):
            full = Path(dirpath) / name
            entry_rel = full.relative_to(root).as_posix()
            stat = full.stat()
            files.append(
                {
                    "rel": entry_rel,
                    "size": stat.st_size,
                    "sha256": _sha256_file(full),
                }
            )
            if len(files) >= ENTRIES_LIMIT:
                return {"exists": True, "truncated": True, "files": files}
    return {"exists": True, "truncated": False, "files": files}


def _dump_value(value: object) -> object:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _read_table(connection: sqlite3.Connection, table: str) -> dict[str, object]:
    existing = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if table not in existing:
        return {"missing": True, "row_count": 0, "returned": 0, "rows": []}
    count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    try:
        cursor = connection.execute(f"SELECT * FROM {table} ORDER BY rowid LIMIT {ROW_LIMIT}")
    except sqlite3.OperationalError:
        cursor = connection.execute(f"SELECT * FROM {table} LIMIT {ROW_LIMIT}")
    columns = [description[0] for description in cursor.description]
    rows = [
        {name: _dump_value(value) for name, value in zip(columns, row, strict=True)}
        for row in cursor.fetchall()
    ]
    return {"row_count": int(count), "returned": len(rows), "rows": rows}


def main() -> int:
    report: dict[str, object] = {
        "schema_version": "s1-08r3-e1-result-evidence.v1",
        "marker": MARKER,
        "authorization_id": AUTH_ID,
        "executed_at_utc": _utc_now(),
        "allowed_root": "E:/QuantKingV3Data",
        "network_calls": 0,
        "writes": 0,
        "sqlite_checkpoints": 0,
        "e1_job_id": E1_JOB_ID,
        "r3c_probe_job_id": R3C_PROBE_JOB_ID,
    }

    if not ALLOWED_ROOT.is_dir():
        report["status"] = "BLOCKED"
        report["blockers"] = {"root_missing": ["E:/QuantKingV3Data"]}
        EVIDENCE_OUT.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 2

    before_files = _snapshot(ALLOWED_ROOT)
    before_blockers = _blockers(before_files)
    wal_present = bool(before_blockers["wal_shm_journal"])
    if any(before_blockers.values()):
        report["status"] = "BLOCKED"
        report["blockers"] = before_blockers
        EVIDENCE_OUT.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 2

    listings = {rel: _list_directory(ALLOWED_ROOT, rel) for rel in DIR_LISTINGS}
    version_listing = _walk_bounded(ALLOWED_ROOT, E1_VERSION_DIR)

    database = ALLOWED_ROOT / "metadata" / "quantking.db"
    tables: dict[str, object] = {}
    if database.is_file():
        mode = "ro" if wal_present else "ro&immutable=1"
        uri = f"file:{database.as_posix()}?mode={mode}"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=1")
        try:
            tables = {name: _read_table(connection, name) for name in TABLES}
        finally:
            connection.close()
        report["sqlite_mode"] = mode

    job_logs = {
        rel: _sha256_file(ALLOWED_ROOT / rel)
        for rel in sorted(before_files)
        if rel.startswith("logs/jobs/eod-") and rel.endswith(".jsonl")
    }

    all_hashes = {rel: _sha256_file(ALLOWED_ROOT / rel) for rel in sorted(before_files)}

    after_files = _snapshot(ALLOWED_ROOT)
    after_blockers = _blockers(after_files)
    after_hashes = {rel: _sha256_file(ALLOWED_ROOT / rel) for rel in sorted(after_files)}
    identical = before_files == after_files and all_hashes == after_hashes

    report.update(
        {
            "status": "OK" if identical and not any(after_blockers.values()) else "BLOCKED",
            "blockers": after_blockers,
            "directory_listings": listings,
            "published_version_listing": version_listing,
            "sqlite": {
                "query_only": True,
                "row_limit": ROW_LIMIT,
                "tables": tables,
            },
            "job_log_sha256": job_logs,
            "immutability": {
                "checked": True,
                "files_total": len(before_files),
                "bytes_total": int(
                    sum(int(entry["size"]) for entry in before_files.values())  # type: ignore[arg-type]
                ),
                "all_files_hashed": True,
                "before_after_identical": identical,
            },
            "root_fingerprint_sha256": _root_fingerprint(before_files, all_hashes),
            "finished_at_utc": _utc_now(),
        }
    )
    EVIDENCE_OUT.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "files_total": len(before_files),
                "immutability": report["immutability"],
                "root_fingerprint_sha256": report["root_fingerprint_sha256"],
                "blockers": after_blockers,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "OK" else 2


if __name__ == "__main__":
    sys.exit(main())
