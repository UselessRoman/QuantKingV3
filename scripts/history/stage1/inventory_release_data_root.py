"""Historical authorized read-only inventory of the release data root.

marker=S1-08R3-I0-INVENTORY-READONLY-V1
authorization_id=S1-08R3-I0-7d3e9a1c4b62
plan_sha256=1117f81a4d6c16c60d9684d919476223ea7da0134ed03480bc9a00687582b5e7

Constraints enforced by this executor:
- network_calls=0, writes=0 (inside allowed root), sqlite_checkpoints=0
- SQLite opened via file URI mode=ro&immutable=1 + PRAGMA query_only=1
  (never creates WAL/SHM/journal, never locks)
- directory listings are single-level for the 7 declared directories only
- full root file-set/size/mtime snapshot before and after (immutability proof),
  SHA256 for every file when the root is small, always for declared targets
- any stale .incomplete / publish lock / WAL / SHM / journal file is a blocker
  -> the script stops and reports without mutating anything
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
MARKER = "S1-08R3-I0-INVENTORY-READONLY-V1"
AUTH_ID = "S1-08R3-I0-7d3e9a1c4b62"
PLAN_SHA256 = "1117f81a4d6c16c60d9684d919476223ea7da0134ed03480bc9a00687582b5e7"
CODE_TREE_SHA256 = "5a0437ab3fe42b898c83daf1e7fe6fcf1b1407d1f582423aef931d07667f55c4"
GIT_HEAD = "9f5b9ea"
ROW_LIMIT = 500
ENTRIES_LIMIT = 500
TABLES = (
    "alembic_version",
    # The inventory plan declared "jobs"; the actual schema table carrying
    # job closeout state is "job_runs" (verified during the authorized I0 run;
    # no other name mapping was needed).
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
    "raw",
    "artifacts",
    "logs",
    "logs/jobs",
)
SHA_TARGETS = ("metadata/quantking.db",)
HASH_ALL_CAP_FILES = 2000
HASH_ALL_CAP_BYTES = 2_000_000_000
RESULT_PATH = Path(os.environ.get("TEMP", ".")) / "s1_08r3_i0_result.json"

_BLOCKER_SUFFIXES = ("-wal", "-shm", "-journal")


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
        if lower.endswith(_BLOCKER_SUFFIXES):
            found["wal_shm_journal"].append(rel)
        parts = rel.split("/")
        if any(part == ".incomplete" for part in parts[:-1]) or parts[-1] == ".incomplete":
            found["stale_incomplete"].append(rel)
        if lower.endswith(".lock"):
            found["publish_locks"].append(rel)
    return found


def _hash_files(
    root: Path,
    files: dict[str, dict[str, object]],
) -> tuple[dict[str, str], bool]:
    total_bytes = int(sum(int(entry["size"]) for entry in files.values()))  # type: ignore[arg-type]
    if len(files) <= HASH_ALL_CAP_FILES and total_bytes <= HASH_ALL_CAP_BYTES:
        hashes = {rel: _sha256_file(root / rel) for rel in sorted(files)}
        return hashes, True
    hashes = {rel: _sha256_file(root / rel) for rel in SHA_TARGETS if rel in files}
    return hashes, False


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
        return {
            "missing": True,
            "discovered_tables": sorted(existing),
            "row_count": 0,
            "returned": 0,
            "rows": [],
        }
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
        "schema_version": "s1-08r3-inventory-evidence.v1",
        "marker": MARKER,
        "authorization_id": AUTH_ID,
        "plan_sha256": PLAN_SHA256,
        "code_tree_sha256_at_plan": CODE_TREE_SHA256,
        "git_head_informational": GIT_HEAD,
        "executed_at_utc": _utc_now(),
        "allowed_root": "E:/QuantKingV3Data",
        "network_calls": 0,
        "writes": 0,
        "sqlite_checkpoints": 0,
    }

    if not ALLOWED_ROOT.is_dir():
        report["status"] = "BLOCKED"
        report["blockers"] = {"root_missing": ["E:/QuantKingV3Data"]}
        RESULT_PATH.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 2

    before_files = _snapshot(ALLOWED_ROOT)
    before_blockers = _blockers(before_files)
    if any(before_blockers.values()):
        report["status"] = "BLOCKED"
        report["blockers"] = before_blockers
        report["immutability"] = {
            "checked": False,
            "reason": "blockers detected before any database read",
        }
        RESULT_PATH.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 2

    listings = {rel: _list_directory(ALLOWED_ROOT, rel) for rel in DIR_LISTINGS}

    database = ALLOWED_ROOT / "metadata" / "quantking.db"
    tables: dict[str, object] = {}
    if database.is_file():
        uri = f"file:{database.as_posix()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=1")
        try:
            tables = {name: _read_table(connection, name) for name in TABLES}
        finally:
            connection.close()
    file_hashes = {
        rel: _sha256_file(ALLOWED_ROOT / rel)
        for rel in SHA_TARGETS
        if (ALLOWED_ROOT / rel).is_file()
    }
    all_hashes, hashed_all = _hash_files(ALLOWED_ROOT, before_files)
    all_hashes.update(file_hashes)

    after_files = _snapshot(ALLOWED_ROOT)
    after_blockers = _blockers(after_files)
    after_hashes, _ = _hash_files(ALLOWED_ROOT, after_files)
    after_hashes.update(
        {
            rel: _sha256_file(ALLOWED_ROOT / rel)
            for rel in SHA_TARGETS
            if (ALLOWED_ROOT / rel).is_file()
        }
    )
    identical = before_files == after_files and all_hashes == after_hashes

    report.update(
        {
            "status": "OK" if identical and not any(after_blockers.values()) else "BLOCKED",
            "blockers": after_blockers,
            "directory_listings": listings,
            "sqlite": {
                "mode": "ro&immutable=1",
                "query_only": True,
                "row_limit": ROW_LIMIT,
                "tables": tables,
            },
            "file_sha256": file_hashes,
            "immutability": {
                "checked": True,
                "files_total": len(before_files),
                "bytes_total": int(
                    sum(int(entry["size"]) for entry in before_files.values())  # type: ignore[arg-type]
                ),
                "all_files_hashed": hashed_all,
                "before_after_identical": identical,
            },
            "root_fingerprint_sha256": _root_fingerprint(before_files, all_hashes),
            "finished_at_utc": _utc_now(),
        }
    )
    RESULT_PATH.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True), encoding="utf-8")
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
