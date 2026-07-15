"""Serve the built Stage 0 shell over a disposable, deterministic SQLite fixture."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from quantking.jobs.adapters.persistence.sqlite import (
    SqliteUnitOfWorkFactory,
    create_sqlite_engine,
    initialize_database,
)
from quantking.jobs.models import JobStatus
from quantking.jobs.services import CreateJob, JobService
from quantking.web.app import create_app
from quantking.web.main import run_server

FIXED_JOB_ID = "job:00000000-0000-4000-8000-000000000004"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--static-root", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    static_root = args.static_root.resolve(strict=True)
    fixed_now = datetime(2026, 7, 15, 4, 5, 6, tzinfo=UTC)

    with TemporaryDirectory(prefix="quantking-web-e2e-") as temporary:
        database = Path(temporary) / "e2e.db"
        initialize_database(database)
        engine = create_sqlite_engine(database, busy_timeout_ms=250)
        service = JobService(SqliteUnitOfWorkFactory(engine), clock=lambda: fixed_now)
        with patch("quantking.jobs.services.new_entity_id", return_value=FIXED_JOB_ID):
            job = service.create_job(
                CreateJob(
                    job_type="synthetic",
                    idempotency_key="playwright-web-shell",
                    input_manifest={"input": "synthetic-v1"},
                    config_manifest={"config": "default-v1"},
                    version_manifest={"code": "playwright"},
                    requester="playwright",
                    log_id="logs/jobs/playwright-web-shell.jsonl",
                )
            )
        service.transition(job.job_id, JobStatus.RUNNING, worker_id="e2e-worker")
        service.report_progress(
            job.job_id,
            stage="synthetic-step",
            progress=0.5,
            summary="synthetic progress",
        )
        app = create_app(service, static_root=static_root, clock=lambda: fixed_now)
        try:
            run_server(app, port=args.port)
        finally:
            engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
