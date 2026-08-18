"""S1-08R3-R3V2 authorized pure read-only re-verification of the formal E1 release.

marker=S1-08R3-R3V2-READONLY-VERIFY-V1
authorization_id=S1-08R3-R3V2-7e4c1b9d2a68

ADR-028 scope: one authorized execution against the real data root
``E:\\QuantKingV3Data`` with network_calls=0, writes=0 (inside the root),
sqlite_checkpoints=0, new jobs/versions/batches/artifacts=0. The metadata
database opens through ``mode=ro&immutable=1`` with ``query_only`` and the
run refuses to start while any WAL/SHM/journal sidecar exists.

Two modes:

* ``--plan`` builds and freezes the verification plan without touching the
  data root (repository reads only) and refuses to overwrite an existing
  plan file;
* the default mode verifies the token
  ``<marker>:<authorization_id>:<plan_sha256>``, the running script SHA, the
  code tree SHA and git HEAD against the frozen plan, consumes the one-time
  attempt ledger, captures a light before/after aggregate root fingerprint
  (file count, total bytes, aggregate SHA over name/size/mtime — no full
  per-file manifest), runs ``verify_release_row_proof`` once with a JSONL
  emitter, and writes the three evidence files. Fail fast: the authorization
  is consumed either way and is never reusable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "docs" / "stage1" / "evidence"
PLAN_PATH = EVIDENCE_DIR / "s1_08r3_r3v2_plan_2026-08-18.json"
RESULT_PATH = EVIDENCE_DIR / "s1_08r3_r3v2_result_2026-08-18.json"
ROW_PROOF_PATH = EVIDENCE_DIR / "s1_08r3_r3v2_row_proof_2026-08-18.jsonl"
ARTIFACT_MANIFEST_PATH = EVIDENCE_DIR / "s1_08r3_r3v2_artifact_manifest_2026-08-18.json"
ATTEMPTS_ROOT = EVIDENCE_DIR / "s1_08r3_r3v2_attempt_2026-08-18"

ALLOWED_ROOT = Path(r"E:\QuantKingV3Data")
MARKER = "S1-08R3-R3V2-READONLY-VERIFY-V1"
AUTHORIZATION_ID = "S1-08R3-R3V2-7e4c1b9d2a68"
IDEMPOTENCY_KEY = "s1-08r3-r3v2-readonly-verify-20260818-v1"
PLAN_SCHEMA = "s1-08r3-r3v2-plan.v1"
ROW_CAP = 500
FILE_CAP = 500
BYTES_CAP = 50_000_000
METADATA_TABLES = (
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
ARTIFACT_PREFIXES = (
    "artifacts/quality/",
    "artifacts/data-diff/",
    "artifacts/data-resolution/",
    "warehouse/daily_bar_unadjusted/",
    "warehouse/exchange_calendar/",
    "raw/",
    "logs/jobs/",
)
SIDE_SUFFIXES = ("-wal", "-shm", "-journal")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _git_head() -> str:
    head_reference = (REPO_ROOT / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    if head_reference.startswith("ref: "):
        return (REPO_ROOT / ".git" / head_reference[5:].strip()).read_text(encoding="utf-8").strip()
    return head_reference


def _payload_without_sha(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key != "plan_sha256"}


def build_plan() -> dict[str, Any]:
    """Zero data-root I/O: repository reads only."""
    from quantking.data.adapters.release_verifier import (
        E1_CALENDAR_VERSION_ID,
        E1_JOB_ID,
        E1_PUBLISHED_VERSION_ID,
    )
    from quantking.data.eod.formal_authorization import compute_code_tree_sha256

    payload: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA,
        "marker": MARKER,
        "authorization_id": AUTHORIZATION_ID,
        "idempotency_key": IDEMPOTENCY_KEY,
        "allowed_root": ALLOWED_ROOT.as_posix(),
        "verifier_script_sha256": _sha256_bytes(Path(__file__).read_bytes()),
        "code_tree_sha256": compute_code_tree_sha256(REPO_ROOT / "src" / "quantking"),
        "git_head": _git_head(),
        "e1_job_id": E1_JOB_ID,
        "published_version_id": E1_PUBLISHED_VERSION_ID,
        "calendar_version_id": E1_CALENDAR_VERSION_ID,
        "metadata_tables_readonly": list(METADATA_TABLES),
        "sqlite_mode": "ro&immutable=1",
        "query_only": True,
        "artifact_prefixes": list(ARTIFACT_PREFIXES),
        "row_cap": ROW_CAP,
        "file_cap": FILE_CAP,
        "bytes_cap": BYTES_CAP,
        "evidence_outputs": {
            "result": RESULT_PATH.name,
            "row_proof": ROW_PROOF_PATH.name,
            "artifact_manifest": ARTIFACT_MANIFEST_PATH.name,
        },
        "attempts_root": ATTEMPTS_ROOT.name,
        "network_calls": 0,
        "writes": 0,
        "sqlite_checkpoints": 0,
        "wal_shm_journal_creation": "forbidden",
        "new_jobs": 0,
        "new_dataset_versions": 0,
        "new_published_versions": 0,
        "new_raw_quality_difference_resolution_warehouse": 0,
        "mutations": "forbidden: no move, delete, recovery or repair",
        "execution_count": 1,
        "failure_policy": "fail fast; authorization consumed; no rerun",
        "immutability_proof": (
            "aggregate root fingerprint (file count, total bytes, aggregate SHA over"
            " name/size/mtime) must be identical before and after"
        ),
        "requires_new_real_authorization": True,
    }
    payload["plan_sha256"] = _sha256_bytes(_canonical(payload).encode("utf-8"))
    return payload


def _aggregate_fingerprint(root: Path) -> dict[str, Any]:
    files = 0
    total_bytes = 0
    digest = hashlib.sha256()
    sidecars: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            stat = full.stat()
            files += 1
            total_bytes += stat.st_size
            digest.update(f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}\0".encode())
            if rel.startswith("metadata/") and name.endswith(
                tuple(f"quantking.db{suffix}" for suffix in SIDE_SUFFIXES)
            ):
                sidecars.append(rel)
    return {
        "files": files,
        "bytes": total_bytes,
        "aggregate_sha256": digest.hexdigest(),
        "sqlite_sidecars": sorted(sidecars),
    }


def _consume_ledger(plan: dict[str, Any]) -> None:
    from quantking.data.eod.formal_authorization import FormalAttemptLedger

    FormalAttemptLedger(ATTEMPTS_ROOT).begin(
        envelope_sha256=str(plan["plan_sha256"]),
        marker=MARKER,
        authorization_id=AUTHORIZATION_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        now_iso=_utc_now(),
    )


def _record_ledger(status: str, detail: dict[str, Any]) -> None:
    import contextlib

    from quantking.data.eod.formal_authorization import (
        FormalAttemptLedger,
        FormalAuthorizationError,
    )

    with contextlib.suppress(FormalAuthorizationError):
        FormalAttemptLedger(ATTEMPTS_ROOT).record(
            AUTHORIZATION_ID, status=status, now_iso=_utc_now(), detail=detail
        )


def _write_json(path: Path, payload: Any) -> str:
    path.write_text(_canonical(payload), encoding="utf-8")
    return _sha256_bytes(path.read_bytes())


def run_plan_mode() -> int:
    if PLAN_PATH.exists():
        print(f"plan file already exists: {PLAN_PATH.name}", file=sys.stderr)
        return 1
    plan = build_plan()
    plan_sha = _write_json(PLAN_PATH, plan)
    print(_canonical({"plan_file": PLAN_PATH.name, "plan_sha256": plan_sha, "mode": "plan"}))
    return 0


def run_verify_mode(authorization: str | None) -> int:
    from quantking.data.adapters.release_verifier import (
        ReleaseRowProofError,
        verify_release_row_proof,
    )
    from quantking.data.eod.formal_authorization import (
        FormalAuthorizationError,
        compute_code_tree_sha256,
    )

    for path in (RESULT_PATH, ROW_PROOF_PATH, ARTIFACT_MANIFEST_PATH):
        if path.exists():
            print(f"evidence file already exists: {path.name}", file=sys.stderr)
            return 1
    if not PLAN_PATH.is_file():
        print("plan file is missing", file=sys.stderr)
        return 1
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    recomputed = _sha256_bytes(_canonical(_payload_without_sha(plan)).encode("utf-8"))
    if recomputed != plan.get("plan_sha256"):
        print("plan file does not match its own plan_sha256", file=sys.stderr)
        return 1
    expected_token = f"{MARKER}:{AUTHORIZATION_ID}:{plan['plan_sha256']}"
    if authorization != expected_token:
        print("authorization token is missing or mismatched", file=sys.stderr)
        return 1
    if _sha256_bytes(Path(__file__).read_bytes()) != plan["verifier_script_sha256"]:
        print("verifier script drifted from the frozen plan", file=sys.stderr)
        return 1
    try:
        if compute_code_tree_sha256(REPO_ROOT / "src" / "quantking") != plan["code_tree_sha256"]:
            print("code tree drifted from the frozen plan", file=sys.stderr)
            return 1
        if _git_head() != plan["git_head"]:
            print("git HEAD drifted from the frozen plan", file=sys.stderr)
            return 1
    except (OSError, FormalAuthorizationError):
        print("preflight hashing failed", file=sys.stderr)
        return 1

    try:
        _consume_ledger(plan)
    except FormalAuthorizationError as error:
        print(f"attempt ledger refused this authorization: {error}", file=sys.stderr)
        return 1

    def _fail(phase: str, message: str) -> int:
        result = {
            "schema_version": "s1-08r3-r3v2-result.v1",
            "marker": MARKER,
            "authorization_id": AUTHORIZATION_ID,
            "plan_sha256": plan["plan_sha256"],
            "status": "FAILED",
            "phase": phase,
            "error": message[:400],
            "executed_at_utc": _utc_now(),
        }
        _write_json(RESULT_PATH, result)
        _record_ledger("FAILED", {"phase": phase})
        print(f"{phase} failed: {message}", file=sys.stderr)
        return 1

    if not ALLOWED_ROOT.is_dir():
        return _fail("preflight", "allowed root is missing")
    before = _aggregate_fingerprint(ALLOWED_ROOT)
    if before["sqlite_sidecars"]:
        return _fail("preflight", "sqlite sidecar present before the run")

    proof_lines: list[str] = []

    def _emit(row: dict[str, Any]) -> None:
        proof_lines.append(_canonical(row))

    database = ALLOWED_ROOT / "metadata" / "quantking.db"
    try:
        connection = sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro&immutable=1", uri=True, timeout=0
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            tables = {
                name: int(connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                for name in METADATA_TABLES
            }
        finally:
            connection.close()
    except sqlite3.Error as error:
        return _fail("metadata-read", str(error))
    if tables["job_runs"] > ROW_CAP or tables["job_events"] > ROW_CAP:
        return _fail("metadata-read", "metadata row counts exceed the frozen caps")

    try:
        summary = verify_release_row_proof(
            data_root=ALLOWED_ROOT,
            version_id=str(plan["published_version_id"]),
            job_id=str(plan["e1_job_id"]),
            emit=_emit,
        )
    except ReleaseRowProofError as error:
        return _fail("row-proof", str(error))

    ROW_PROOF_PATH.write_text("\n".join(proof_lines) + "\n", encoding="utf-8")
    after = _aggregate_fingerprint(ALLOWED_ROOT)
    if after != before:
        return _fail("immutability", "aggregate root fingerprint changed during the run")

    result = {
        "schema_version": "s1-08r3-r3v2-result.v1",
        "marker": MARKER,
        "authorization_id": AUTHORIZATION_ID,
        "plan_sha256": plan["plan_sha256"],
        "status": "VERIFIED",
        "executed_at_utc": _utc_now(),
        "e1_job_id": str(plan["e1_job_id"]),
        "published_version_id": str(plan["published_version_id"]),
        "calendar_version_id": str(plan["calendar_version_id"]),
        "metadata_table_counts": tables,
        "row_proof_summary": summary,
        "row_proof_line_count": len(proof_lines),
        "immutability": {
            "before": before,
            "after": after,
            "identical": True,
        },
        "network_calls": 0,
        "writes": 0,
        "sqlite_checkpoints": 0,
    }
    result_sha = _write_json(RESULT_PATH, result)
    row_proof_sha = _sha256_bytes(ROW_PROOF_PATH.read_bytes())
    artifact_manifest = {
        "schema_version": "s1-08r3-r3v2-artifact-manifest.v1",
        "files": [
            {"path": RESULT_PATH.name, "size": RESULT_PATH.stat().st_size, "sha256": result_sha},
            {
                "path": ROW_PROOF_PATH.name,
                "size": ROW_PROOF_PATH.stat().st_size,
                "sha256": row_proof_sha,
            },
        ],
        "attempts_root": ATTEMPTS_ROOT.name,
        "authorization_id": AUTHORIZATION_ID,
    }
    manifest_sha = _write_json(ARTIFACT_MANIFEST_PATH, artifact_manifest)
    _record_ledger(
        "COMPLETED",
        {
            "rows_verified": summary.get("rows_verified"),
            "result_sha256": result_sha,
            "row_proof_sha256": row_proof_sha,
            "artifact_manifest_sha256": manifest_sha,
        },
    )
    print(
        _canonical(
            {
                "status": "VERIFIED",
                "rows_verified": summary.get("rows_verified"),
                "result_sha256": result_sha,
                "row_proof_sha256": row_proof_sha,
                "artifact_manifest_sha256": manifest_sha,
            }
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--authorization-env", default="R3V2_AUTHORIZATION")
    parser.add_argument("--authorization", default=None)
    arguments = parser.parse_args()
    if arguments.plan:
        return run_plan_mode()
    authorization = arguments.authorization or os.environ.get(arguments.authorization_env)
    return run_verify_mode(authorization)


if __name__ == "__main__":
    raise SystemExit(main())
