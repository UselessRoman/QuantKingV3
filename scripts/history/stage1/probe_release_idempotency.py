"""Historical dual release-idempotency probe (authorized 2026-08-18).

Zero-network proof of the two idempotency contracts declared by the frozen
E1 authorization request (``command_idempotency_probe`` /
``published_version_reuse_probe``) against the REAL formal data root:

* Probe A (command idempotency): rebuild the exact frozen command from the
  frozen E1 release manifest + authorization envelope (the same construction
  path as ``run_formal_entry``) and enqueue it with the SAME idempotency key.
  The jobs layer must return the existing E1 Job (``created=False``) after
  verifying the frozen input/config/version manifests are byte-identical,
  with zero provider calls and zero new rows/events.
* Probe B (published version reuse): enqueue the same frozen command under a
  dedicated probe idempotency key and run the formal worker. The production
  ``_reuse_published`` short-circuit must find the PUBLISHED E1 version by
  input fingerprint, strictly re-verify it, and finish the Job SUCCEEDED with
  zero provider calls, zero new versions, batches, quality runs or resolution
  runs.

Every gateway is an injected probe gateway that counts the attempt and fails
closed before any socket: any provider invocation would be both counted and
fatal to the probe.

ADR-028 (2026-08-18): future executions of this probe (or similar probes)
must first consume a simple one-time attempt ledger binding the marker, the
authorization id, the probe plan SHA, the idempotency key and the start time.
A replayed authorization id is refused before the data root is accessed, and
a FAILED attempt can never be reused. The 2026-08-18 historical evidence
files stay byte-frozen; this ledger applies to future probes only.

Bounded footprint (probe authorization S1-08R3-R3C-IDEMPOTENCY-PROBE-V1):
Probe A performs no effective write; Probe B creates exactly one Job row with
its events, one job log file and one job-artifact binding. No other writes.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_DIR = REPO_ROOT / "docs" / "stage1" / "evidence"
MANIFEST_PATH = EVIDENCE_DIR / "s1_08r3_release_manifest_2026-08-17.json"
ENVELOPE_PATH = EVIDENCE_DIR / "s1_08r3_authorization_envelope_2026-08-17.json"
EVIDENCE_OUT = EVIDENCE_DIR / "s1_08r3_r3c_idempotency_probe_2026-08-18.json"
DEFAULT_ATTEMPTS_ROOT = EVIDENCE_DIR / "s1_08r3_r3c_attempt"

MARKER = "S1-08R3-R3C-IDEMPOTENCY-PROBE-V1"
AUTHORIZATION_ID = "S1-08R3-R3C-3f8a51c0d9e2"
E1_JOB_ID = "job:cd282234-078b-47a9-b17c-12d82e3b7f50"
E1_IDEMPOTENCY_KEY = "s1-08r3-e1-formal-release-20260817-v1"
PROBE_IDEMPOTENCY_KEY = "s1-08r3-r3c-reuse-probe-20260818-v1"
EOD_JOB_TYPE = "data.eod.v1"

Counters = dict[str, int]


class _ProbeGateway:
    """Zero-network gateway: counts the attempt and fails closed."""

    def __init__(
        self,
        *,
        provider: str,
        upstream: str,
        endpoint: str,
        library_version: str,
    ) -> None:
        self._provider = provider
        self._upstream = upstream
        self._endpoint = endpoint
        self._library_version = library_version
        self.calls: list[str] = []

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def upstream(self) -> str:
        return self._upstream

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def library_version(self) -> str:
        return self._library_version

    def fetch(self, chunk: object) -> Any:
        self.calls.append(str(getattr(chunk, "chunk_id", "unknown-chunk")))
        from quantking.data.adapters.sources.contracts import (
            SourceAdapterError,
            SourceErrorCode,
        )

        raise SourceAdapterError(
            SourceErrorCode.NETWORK,
            "r3c idempotency probe zero-network boundary",
            retryable=False,
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _snapshot(data_root: Path, *, wal_aware: bool = False) -> dict[str, Any]:
    """Read-only metadata snapshot.

    ``immutable=1`` ignores WAL sidecars and therefore only sees the flushed
    main file: safe only when no sidecar exists. When the jobs layer has the
    database in WAL mode (sidecars present), a WAL-aware ``mode=ro`` read is
    used instead so committed-in-WAL rows are visible. No checkpoint, no
    mutation, ``query_only`` enforced either way.
    """

    database = data_root / "metadata" / "quantking.db"
    sidecars = {
        name: (database.parent / f"quantking.db-{name}").stat().st_size
        if (database.parent / f"quantking.db-{name}").exists()
        else None
        for name in ("wal", "shm", "journal")
    }
    if wal_aware or sidecars["wal"] not in (None, 0):
        uri = f"file:{database.as_posix()}?mode=ro"
    else:
        uri = f"file:{database.as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")

        def scalar(query: str, *params: object) -> int:
            return int(connection.execute(query, params).fetchone()[0])

        eod_jobs = [
            dict(row)
            for row in connection.execute(
                "SELECT job_id, status FROM job_runs WHERE job_type = ?"
                " ORDER BY rowid DESC LIMIT 20",
                (EOD_JOB_TYPE,),
            )
        ]
        daily_bar = [
            dict(row)
            for row in connection.execute(
                "SELECT version_id, status FROM dataset_versions"
                " WHERE dataset = 'daily_bar_unadjusted' ORDER BY rowid"
            )
        ]
        artifacts: dict[str, int] = {}
        artifacts_root = data_root / "artifacts"
        if artifacts_root.is_dir():
            for child in sorted(artifacts_root.iterdir()):
                artifacts[child.name] = len(list(child.rglob("*"))) if child.is_dir() else 1
        return {
            "sidecars": sidecars,
            "job_runs_total": scalar("SELECT COUNT(*) FROM job_runs"),
            "eod_jobs": eod_jobs,
            "job_events_total": scalar("SELECT COUNT(*) FROM job_events"),
            "events_for_e1_job": scalar(
                "SELECT COUNT(*) FROM job_events WHERE job_id = ?", E1_JOB_ID
            ),
            "dataset_versions_total": scalar("SELECT COUNT(*) FROM dataset_versions"),
            "daily_bar_versions": daily_bar,
            "exchange_calendar_published": scalar(
                "SELECT COUNT(*) FROM dataset_versions WHERE dataset ="
                " 'exchange_calendar' AND status = 'PUBLISHED'"
            ),
            "ingestion_batches_total": scalar("SELECT COUNT(*) FROM ingestion_batches"),
            "quality_runs_total": scalar("SELECT COUNT(*) FROM quality_runs"),
            "resolution_runs_total": scalar("SELECT COUNT(*) FROM resolution_runs"),
            "difference_reports_total": scalar("SELECT COUNT(*) FROM difference_reports"),
            "warehouse_daily_bar_entries": sorted(
                child.name for child in (data_root / "warehouse" / "daily_bar_unadjusted").iterdir()
            )
            if (data_root / "warehouse" / "daily_bar_unadjusted").is_dir()
            else [],
            "warehouse_exchange_calendar_entries": sorted(
                child.name for child in (data_root / "warehouse" / "exchange_calendar").iterdir()
            )
            if (data_root / "warehouse" / "exchange_calendar").is_dir()
            else [],
            "logs_jobs_files": sorted(
                child.name for child in (data_root / "logs" / "jobs").iterdir()
            )
            if (data_root / "logs" / "jobs").is_dir()
            else [],
            "artifacts_counts": artifacts,
        }
    finally:
        connection.close()


def _probe_log_path(data_root: Path, idempotency_key: str) -> Path:
    log_key = hashlib.sha256(f"{EOD_JOB_TYPE}\0{idempotency_key}".encode()).hexdigest()
    return data_root / "logs" / "jobs" / f"eod-{log_key}.jsonl"


def _read_log_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.is_file():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"unparsed": line[:200]})
    return events


def _assert(condition: bool, failures: list[str], message: str) -> None:
    if not condition:
        failures.append(message)


PROBE_PLAN_SCHEMA = "s1-08r3-r3c-probe-plan.v1"


def probe_plan_sha256(
    *,
    marker: str = MARKER,
    authorization_id: str = AUTHORIZATION_ID,
    idempotency_key: str = PROBE_IDEMPOTENCY_KEY,
    manifest_sha256: str,
    envelope_sha256: str,
) -> str:
    """Deterministic ADR-028 probe plan SHA over the frozen probe identity."""

    payload = json.dumps(
        {
            "schema_version": PROBE_PLAN_SCHEMA,
            "marker": marker,
            "authorization_id": authorization_id,
            "idempotency_key": idempotency_key,
            "e1_job_id": E1_JOB_ID,
            "manifest_sha256": manifest_sha256,
            "envelope_sha256": envelope_sha256,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def consume_attempt_ledger(
    attempts_root: Path,
    *,
    marker: str,
    authorization_id: str,
    plan_sha256: str,
    idempotency_key: str,
    now_iso: str,
) -> Path:
    """One-time ledger consumption before any data-root access (ADR-028)."""

    from quantking.data.eod.formal_authorization import FormalAttemptLedger

    return FormalAttemptLedger(attempts_root).begin(
        envelope_sha256=plan_sha256,
        marker=marker,
        authorization_id=authorization_id,
        idempotency_key=idempotency_key,
        now_iso=now_iso,
    )


def record_attempt(
    attempts_root: Path,
    authorization_id: str,
    *,
    status: str,
    now_iso: str,
    detail: dict[str, Any] | None = None,
) -> None:
    from quantking.data.eod.formal_authorization import FormalAttemptLedger

    FormalAttemptLedger(attempts_root).record(
        authorization_id, status=status, now_iso=now_iso, detail=detail
    )


def probe_token(marker: str, authorization_id: str, plan_sha256: str) -> str:
    return f"{marker}:{authorization_id}:{plan_sha256}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts-root", default=str(DEFAULT_ATTEMPTS_ROOT))
    parser.add_argument("--authorization-env", default="R3C_PROBE_AUTHORIZATION")
    parser.add_argument("--authorization", default=None)
    parser.add_argument(
        "--evidence-out",
        default=str(EVIDENCE_DIR / "s1_08r3_r3c_idempotency_probe_next.json"),
    )
    arguments = parser.parse_args()
    attempts_root = Path(arguments.attempts_root)
    evidence_out = Path(arguments.evidence_out)
    if evidence_out.exists():
        # Append-only evidence discipline: never overwrite a previous run.
        print(f"evidence file already exists: {evidence_out.name}", file=sys.stderr)
        return 1

    from quantking.data.eod.formal_authorization import (
        FormalAuthorizationEnvelope,
        assert_envelope_matches_manifest,
        compute_code_tree_sha256,
    )
    from quantking.data.eod.release_manifest import ReleaseManifest
    from quantking.runtime import load_runtime_settings

    manifest_file_sha = _sha256_file(MANIFEST_PATH)
    envelope_file_sha = _sha256_file(ENVELOPE_PATH)
    plan_sha = probe_plan_sha256(
        manifest_sha256=manifest_file_sha,
        envelope_sha256=envelope_file_sha,
    )
    evidence: dict[str, Any] = {
        "schema_version": "s1-08r3-r3c-idempotency-probe.v1",
        "marker": MARKER,
        "authorization_id": AUTHORIZATION_ID,
        "executed_at": datetime.now(UTC).isoformat(),
        "inputs": {
            "manifest_path": MANIFEST_PATH.name,
            "manifest_sha256": manifest_file_sha,
            "envelope_path": ENVELOPE_PATH.name,
            "envelope_sha256": envelope_file_sha,
        },
        "probe_plan_sha256": plan_sha,
        "bounds": {
            "network_calls": 0,
            "probe_a_new_jobs": 0,
            "probe_b_new_jobs_max": 1,
            "new_versions": 0,
            "new_raw_batches": 0,
            "new_quality_runs": 0,
            "new_resolution_runs": 0,
            "failure_policy": "stop immediately; evidence preserved; no retry",
        },
    }
    failures: list[str] = []

    # ADR-028: the one-time ledger is consumed before any data-root access
    # (before load_runtime_settings and before the first snapshot).
    authorization = arguments.authorization or os.environ.get(arguments.authorization_env)
    expected_token = probe_token(MARKER, AUTHORIZATION_ID, plan_sha)
    if authorization != expected_token:
        evidence["status"] = "FAILED"
        evidence["phase"] = "authorization"
        evidence["error"] = "R3C_PROBE_AUTHORIZATION_REQUIRED"
        evidence_out.write_text(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        print("probe authorization token is missing or mismatched", file=sys.stderr)
        return 1
    try:
        consume_attempt_ledger(
            attempts_root,
            marker=MARKER,
            authorization_id=AUTHORIZATION_ID,
            plan_sha256=plan_sha,
            idempotency_key=PROBE_IDEMPOTENCY_KEY,
            now_iso=datetime.now(UTC).isoformat(),
        )
    except Exception as error:
        evidence["status"] = "FAILED"
        evidence["phase"] = "ledger"
        evidence["error"] = f"{type(error).__name__}: {error}"
        evidence_out.write_text(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        print(f"attempt ledger refused this authorization: {error}", file=sys.stderr)
        return 1

    def _finalize(status: str, detail: dict[str, Any] | None = None) -> None:
        with contextlib.suppress(Exception):
            record_attempt(
                attempts_root,
                AUTHORIZATION_ID,
                status=status,
                now_iso=datetime.now(UTC).isoformat(),
                detail=detail,
            )

    try:
        manifest = ReleaseManifest.from_dict(json.loads(MANIFEST_PATH.read_text(encoding="utf-8")))
        envelope = FormalAuthorizationEnvelope.from_dict(
            json.loads(ENVELOPE_PATH.read_text(encoding="utf-8"))
        )
        assert_envelope_matches_manifest(envelope, manifest)
        actual_tree = compute_code_tree_sha256(REPO_ROOT / "src" / "quantking")
        if actual_tree != envelope.code_tree_sha256:
            raise RuntimeError("code tree drifted from the frozen envelope")
        head_reference = (REPO_ROOT / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if head_reference.startswith("ref: "):
            actual_head = (
                (REPO_ROOT / ".git" / head_reference[5:].strip())
                .read_text(encoding="utf-8")
                .strip()
            )
        else:
            actual_head = head_reference
        if actual_head != envelope.git_head:
            raise RuntimeError("git HEAD drifted from the frozen envelope")
        evidence["preflight"] = {
            "code_tree_sha256": actual_tree,
            "git_head": actual_head,
            "status": "OK",
        }
    except Exception as error:
        evidence["status"] = "FAILED"
        evidence["phase"] = "preflight"
        evidence["error"] = f"{type(error).__name__}: {error}"
        _finalize("FAILED", {"phase": "preflight"})
        evidence_out.write_text(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        print(f"preflight failed: {error}", file=sys.stderr)
        return 1

    settings = load_runtime_settings()
    data_root = settings.paths.data_root
    evidence["data_root_label"] = data_root.as_posix()

    before = _snapshot(data_root)
    evidence["before"] = before
    if before["sidecars"]["wal"] not in (None, 0):
        evidence["status"] = "FAILED"
        evidence["phase"] = "before-snapshot"
        evidence["error"] = "non-empty WAL sidecar present; refusing immutable read"
        evidence_out.write_text(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return 1

    from quantking.data.eod.contracts import (
        EOD_JOB_TYPE as _JOB_TYPE,
    )
    from quantking.data.eod.contracts import (
        EodPublishIntent,
        EodReferenceVersion,
        EodRunRequest,
    )
    from quantking.data.eod.service import EodExecutionConfig
    from quantking.data.models import DatasetKind, DataUpstream
    from quantking.jobs.models import JobStatus
    from quantking.runtime import build_eod_runtime

    assert _JOB_TYPE == EOD_JOB_TYPE
    gateways: dict[DataUpstream, _ProbeGateway] = {
        DataUpstream.BAOSTOCK: _ProbeGateway(
            provider="BAOSTOCK",
            upstream="BAOSTOCK",
            endpoint="query_history_k_data_plus",
            library_version="0.9.3",
        ),
        DataUpstream.TENCENT: _ProbeGateway(
            provider="AKSHARE",
            upstream="TENCENT",
            endpoint="stock_zh_a_hist_tx",
            library_version="1.18.83",
        ),
        DataUpstream.SINA: _ProbeGateway(
            provider="AKSHARE",
            upstream="SINA",
            endpoint="stock_zh_a_daily",
            library_version="1.18.83",
        ),
    }

    try:
        config = EodExecutionConfig.from_release_manifest(manifest)
        dataset_config = config.datasets["daily_bar_unadjusted"]
        config = replace(
            config,
            datasets={
                "daily_bar_unadjusted": replace(
                    dataset_config,
                    references={
                        **dict(dataset_config.references),
                        "exchange_calendar": EodReferenceVersion(
                            dataset=DatasetKind.EXCHANGE_CALENDAR.value,
                            version_id=envelope.calendar_version_id,
                            content_sha256=envelope.calendar_sha256,
                        ),
                    },
                )
            },
        )
        runtime = build_eod_runtime(
            settings,
            gateways={
                upstream: gateway  # type: ignore[dict-item]
                for upstream, gateway in gateways.items()
            },
            execution_config=config,
        )
        request = EodRunRequest(
            start_date=manifest.target_start,
            end_date=manifest.target_end,
            datasets=("daily_bar_unadjusted",),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        )

        # -- Probe A: command idempotency (same frozen key) -------------------
        outcome_a = runtime.eod_commands.enqueue(
            request,
            idempotency_key=E1_IDEMPOTENCY_KEY,
            requester="formal-r3",
        )
        evidence["probe_a"] = {
            "idempotency_key": E1_IDEMPOTENCY_KEY,
            "returned_job_id": outcome_a.job_id,
            "created": outcome_a.created,
            "status": outcome_a.status.value,
            "gateway_calls": {name: len(gw.calls) for name, gw in gateways.items()},
        }
        _assert(outcome_a.job_id == E1_JOB_ID, failures, "probe A returned a different job")
        _assert(outcome_a.created is False, failures, "probe A created a new job")
        _assert(outcome_a.status is JobStatus.SUCCEEDED, failures, "probe A status not SUCCEEDED")
        _assert(
            all(len(gw.calls) == 0 for gw in gateways.values()),
            failures,
            "probe A invoked a provider gateway",
        )

        # -- Probe B: published version reuse (probe key, formal worker) ------
        started = time.monotonic()
        outcome_b = runtime.eod_commands.enqueue(
            request,
            idempotency_key=PROBE_IDEMPOTENCY_KEY,
            requester="r3c-reuse-probe",
        )
        if outcome_b.created:
            runtime.worker.run(outcome_b.job_id)
        # A replay after a snapshot-layer failure legitimately dedupes to the
        # probe job created by the earlier attempt; verify its recorded state
        # instead of re-running the worker.
        final_b = runtime.jobs.get_job(outcome_b.job_id)
        duration_s = round(time.monotonic() - started, 3)
        log_events = _read_log_events(_probe_log_path(data_root, PROBE_IDEMPOTENCY_KEY))
        event_names = [str(item.get("event")) for item in log_events]
        evidence["probe_b"] = {
            "idempotency_key": PROBE_IDEMPOTENCY_KEY,
            "job_id": outcome_b.job_id,
            "created": outcome_b.created,
            "final_status": final_b.status.value,
            "duration_seconds": duration_s,
            "log_event_names": event_names,
            "eod_reused_event_present": "eod_reused" in event_names,
            "gateway_calls": {name: len(gw.calls) for name, gw in gateways.items()},
        }
        _assert(final_b.status is JobStatus.SUCCEEDED, failures, "probe B status not SUCCEEDED")
        _assert("eod_reused" in event_names, failures, "probe B log lacks the eod_reused event")
        _assert(
            all(len(gw.calls) == 0 for gw in gateways.values()),
            failures,
            "probe B invoked a provider gateway",
        )
    except Exception as error:
        evidence["status"] = "FAILED"
        evidence["phase"] = "execution"
        evidence["error"] = f"{type(error).__name__}: {error}"
        evidence["gateway_calls"] = {name: len(gw.calls) for name, gw in gateways.items()}
        _finalize("FAILED", {"phase": "execution"})
        evidence_out.write_text(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        print(f"execution failed: {error}", file=sys.stderr)
        return 1

    after = _snapshot(data_root, wal_aware=True)
    evidence["after"] = {key: after[key] for key in after if key != "sidecars"}
    evidence["after_sidecars"] = after["sidecars"]

    new_jobs = after["job_runs_total"] - before["job_runs_total"]
    new_events = after["job_events_total"] - before["job_events_total"]
    evidence["deltas"] = {
        "job_runs_total": new_jobs,
        "job_events_total": new_events,
        "dataset_versions_total": after["dataset_versions_total"]
        - before["dataset_versions_total"],
        "ingestion_batches_total": after["ingestion_batches_total"]
        - before["ingestion_batches_total"],
        "quality_runs_total": after["quality_runs_total"] - before["quality_runs_total"],
        "resolution_runs_total": after["resolution_runs_total"] - before["resolution_runs_total"],
        "difference_reports_total": after["difference_reports_total"]
        - before["difference_reports_total"],
        "logs_jobs_files": len(after["logs_jobs_files"]) - len(before["logs_jobs_files"]),
    }
    expected_new_jobs = 1 if outcome_b.created else 0
    _assert(
        new_jobs == expected_new_jobs,
        failures,
        f"probe must add exactly {expected_new_jobs} new job row(s)",
    )
    _assert(
        after["events_for_e1_job"] == before["events_for_e1_job"],
        failures,
        "probe touched the E1 job's events",
    )
    if outcome_b.created:
        _assert(new_events >= 1, failures, "probe job recorded no events")
    else:
        _assert(new_events == 0, failures, "replay must not add job events")
    _assert(
        after["dataset_versions_total"] == before["dataset_versions_total"],
        failures,
        "probe created or removed dataset versions",
    )
    _assert(
        after["daily_bar_versions"] == before["daily_bar_versions"],
        failures,
        "daily_bar version set changed",
    )
    _assert(
        len([v for v in after["daily_bar_versions"] if v["status"] == "PUBLISHED"]) == 1,
        failures,
        "daily_bar PUBLISHED count is not exactly one",
    )
    for key in (
        "ingestion_batches_total",
        "quality_runs_total",
        "resolution_runs_total",
        "difference_reports_total",
    ):
        _assert(after[key] == before[key], failures, f"probe changed {key}")
    for key in (
        "warehouse_daily_bar_entries",
        "warehouse_exchange_calendar_entries",
        "artifacts_counts",
    ):
        _assert(after[key] == before[key], failures, f"probe changed {key}")
    probe_log_name = _probe_log_path(data_root, PROBE_IDEMPOTENCY_KEY).name
    expected_logs = sorted(set(before["logs_jobs_files"]) | {probe_log_name})
    _assert(
        sorted(after["logs_jobs_files"]) == expected_logs,
        failures,
        "logs/jobs changed beyond the single probe log",
    )

    evidence["status"] = "FAILED" if failures else "SUCCEEDED"
    evidence["failures"] = failures
    evidence_out.write_text(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    _finalize(
        "FAILED" if failures else "COMPLETED",
        {"probe_status": evidence["status"], "evidence_file": evidence_out.name},
    )
    print(f"probe status: {evidence['status']}")
    if failures:
        for item in failures:
            print(f"failure: {item}", file=sys.stderr)
        return 1
    print(f"probe A reused job {E1_JOB_ID} (created=False, zero gateway calls)")
    print(
        f"probe B job {evidence['probe_b']['job_id']} SUCCEEDED via eod_reused "
        "(zero gateway calls, zero new versions)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
