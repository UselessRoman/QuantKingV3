"""Serve the built Stage 0 shell over a disposable, deterministic SQLite fixture."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from quantking.data.center import DataCenterPage
from quantking.data.eod.contracts import EodBaseVersion
from quantking.data.eod.service import EodCommandService, EodExecutionConfig
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
VERSION_ID = "data_version:00000000-0000-4000-8000-000000000006"


class _BaseResolver:
    def resolve(self, *, dataset: str, version_id: str, scope: tuple[str, ...]) -> EodBaseVersion:
        return EodBaseVersion(
            version_id=version_id,
            dataset=dataset,
            schema_version=f"{dataset}.v1",
            content_sha256="a" * 64,
            manifest_sha256="b" * 64,
            coverage=tuple((item, "2026-07-15", "2026-07-28") for item in scope),
        )


class _SyntheticDataCenter:
    def __init__(self, jobs: JobService) -> None:
        self._jobs = jobs

    def summary(self) -> dict[str, object]:
        return {
            "datasets": [
                {
                    "dataset": "exchange_calendar",
                    "version": {
                        "version_id": VERSION_ID,
                        "dataset": "exchange_calendar",
                        "status": "PUBLISHED",
                        "coverage_start": "2026-07-15",
                        "coverage_end": "2026-07-28",
                        "quality_run_id": "data_quality:00000000-0000-4000-8000-000000000006",
                    },
                }
            ],
            "failures": 0,
            "blocked_sources": 23,
            "generated_at": "2026-07-28T00:00:00Z",
        }

    def sources(self) -> list[dict[str, object]]:
        return [
            {
                "source": "XTQUANT",
                "dataset": "exchange_calendar",
                "role": "PRIMARY",
                "status": "PRODUCTION_READY",
                "evidence_kind": "REAL",
                "blocking_reason": None,
                "auto_switch": False,
            },
            {
                "source": "XTQUANT",
                "dataset": "daily_bar_unadjusted",
                "role": "PRIMARY",
                "status": "BLOCKED_EXTERNAL_VALIDATION",
                "evidence_kind": None,
                "blocking_reason": "XTQuant dataset semantics require external validation",
                "auto_switch": False,
            },
            {
                "source": "BAOSTOCK",
                "dataset": "daily_bar_unadjusted",
                "role": "FALLBACK",
                "status": "BLOCKED_EXTERNAL_VALIDATION",
                "evidence_kind": None,
                "blocking_reason": "requires authorized external validation",
                "auto_switch": False,
            },
        ]

    def list_batches(self, **_filters: object) -> DataCenterPage:
        return DataCenterPage((), None)

    def list_versions(self, **_filters: object) -> DataCenterPage:
        return DataCenterPage(
            (
                {
                    "version_id": VERSION_ID,
                    "dataset": "exchange_calendar",
                    "status": "PUBLISHED",
                    "coverage_start": "2026-07-15",
                    "coverage_end": "2026-07-28",
                    "row_count": 2,
                    "quality_run_id": "data_quality:00000000-0000-4000-8000-000000000006",
                },
            ),
            None,
        )

    def get_version(self, version_id: str) -> dict[str, object]:
        return {"version_id": version_id, "dataset": "exchange_calendar", "status": "PUBLISHED"}

    def preview(self, **_filters: object) -> DataCenterPage:
        return DataCenterPage(
            (
                {"exchange": "SH", "trade_date": "2026-07-28", "is_open": True},
                {"exchange": "SZ", "trade_date": "2026-07-28", "is_open": True},
            ),
            None,
        )

    def list_quality_runs(self, **_filters: object) -> DataCenterPage:
        return DataCenterPage(
            (
                {
                    "run_id": "data_quality:00000000-0000-4000-8000-000000000006",
                    "version_id": VERSION_ID,
                    "status": "PASSED",
                    "rule_version": "quality-rules.v1",
                },
            ),
            None,
        )

    def get_quality_run(self, run_id: str) -> dict[str, object]:
        return {"run_id": run_id, "status": "PASSED", "results": []}

    def list_difference_reports(self, **_filters: object) -> DataCenterPage:
        return DataCenterPage(
            (
                {
                    "report_id": "data_diff:00000000-0000-4000-8000-000000000006",
                    "version_id": VERSION_ID,
                    "primary_source": "XTQUANT",
                    "fallback_source": "BAOSTOCK",
                    "threshold_rule_version": "comparison-thresholds.v1",
                    "total_differences": 1,
                },
            ),
            None,
        )

    def get_difference_report(self, report_id: str) -> dict[str, object]:
        return {"report_id": report_id, "total_differences": 1}

    def difference_rows(self, **_filters: object) -> DataCenterPage:
        return DataCenterPage(({"primary_key": {"code": "000001.SZ"}, "field": "close"},), None)

    def list_source_overrides(self, **_filters: object) -> DataCenterPage:
        return DataCenterPage(
            (
                {
                    "override_id": "data_override:00000000-0000-4000-8000-000000000006",
                    "dataset": "daily_bar_unadjusted",
                    "status": "APPLIED",
                    "difference_report_id": "data_diff:00000000-0000-4000-8000-000000000006",
                    "exact_scope_sha256": "c" * 64,
                    "applied_version_id": VERSION_ID,
                },
            ),
            None,
        )

    def get_source_override(self, override_id: str) -> dict[str, object]:
        return {
            "override_id": override_id,
            "status": "APPLIED",
            "difference_report_id": "data_diff:00000000-0000-4000-8000-000000000006",
            "field_replacements": [
                {"code": "000001.SZ", "trade_date": "2026-07-28", "field": "close"}
            ],
            "fallback_only_keys": [],
            "exact_scope_sha256": "c" * 64,
            "application": {"status": "APPLIED", "child_version_id": VERSION_ID},
            "attempt": {"status": "APPLIED", "child_version_id": VERSION_ID},
        }

    def list_failures(self, **_filters: object) -> DataCenterPage:
        return DataCenterPage((), None)

    def list_eod_runs(self, **filters: object) -> DataCenterPage:
        status = filters.get("status")
        rows = self._jobs.list_jobs_page(
            job_type="data.eod.v1",
            status=JobStatus(str(status)) if status else None,
            before=None,
            limit=51,
        )
        return DataCenterPage(
            tuple(
                {
                    "job_id": row.job_id,
                    "status": row.status.value,
                    "progress": row.progress,
                    "row_version": row.row_version,
                    "created_at": row.created_at.to_iso(),
                }
                for row in rows
            ),
            None,
        )


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
        (Path(temporary) / "warehouse").mkdir()
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
        eod = EodCommandService(
            service,
            config=EodExecutionConfig.production(code_version="playwright"),
            base_resolver=_BaseResolver(),
        )
        app = create_app(
            service,
            eod_command_port=eod,
            job_command_port=service,
            data_center_port=_SyntheticDataCenter(service),
            static_root=static_root,
            clock=lambda: fixed_now,
        )
        try:
            run_server(app, port=args.port)
        finally:
            engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
