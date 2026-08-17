"""Serve the built data center over a disposable real SQLite/Warehouse fixture."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

import pyarrow as pa
import uvicorn

from quantking.common.config import AppSettings
from quantking.common.ids import EntityId, new_entity_id
from quantking.common.paths import RuntimePaths
from quantking.common.time import TradingDate, utc_now
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.adapters.warehouse.canonical_warehouse import CanonicalWarehouse
from quantking.data.adapters.warehouse.publish_coordinator import (
    CanonicalPublishCoordinator,
    CanonicalPublishCoordinatorRequest,
)
from quantking.data.adapters.warehouse.query import CanonicalQueryAdapter
from quantking.data.center import (
    CanonicalPreviewPort,
    DataCenterQueryService,
    DataUnitOfWorkFactory,
    DifferenceVerifierPort,
)
from quantking.data.comparison.artifacts import DifferenceReportArtifactWriter
from quantking.data.comparison.comparator import (
    CrossSourceComparator,
    CrossSourceComparisonRequest,
)
from quantking.data.comparison.thresholds import default_thresholds
from quantking.data.comparison.verifier import DifferenceReportVerifier
from quantking.data.eod.contracts import EodBaseVersion
from quantking.data.eod.service import EodCommandService, EodExecutionConfig
from quantking.data.models import (
    DataProvider,
    DatasetKind,
    DatasetVersion,
    DataUpstream,
    IngestionBatch,
    IngestionBatchStatus,
    ManifestFileEntry,
    QualityResult,
    QualityRun,
)
from quantking.data.quality.artifacts import QualityArtifactWriter
from quantking.data.quality.engine import QualityEngine
from quantking.data.quality.models import (
    QualityRequest,
    QualityRunArtifact,
    ReferenceDataBinding,
    RuleOutcomeStatus,
)
from quantking.data.quality.references import PublishedReferenceLoader
from quantking.data.quality.rules import default_rule_set
from quantking.data.quality.runner import QualityRunner
from quantking.jobs.adapters.persistence.sqlite import (
    SqliteUnitOfWorkFactory,
    initialize_database,
)
from quantking.jobs.models import JobStatus
from quantking.jobs.services import CreateJob, JobService
from quantking.web.app import create_app
from quantking.web.main import LOOPBACK_HOST

FIXED_JOB_ID = "job:00000000-0000-4000-8000-000000000004"


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


def _table(dataset: str, rows: list[dict[str, object]]) -> pa.Table:
    raw = pa.Table.from_pylist(rows)
    schema = canonical_schema(dataset)
    return pa.Table.from_arrays(
        [raw.column(field.name).cast(field.type, safe=False) for field in schema],
        schema=pa.schema(
            [pa.field(field.name, field.type, nullable=field.nullable) for field in schema]
        ),
    )


def _calendar() -> pa.Table:
    return _table(
        "exchange_calendar",
        [
            {"exchange": "SZ", "trade_date": date(2026, 7, 15), "is_open": True},
            {"exchange": "SZ", "trade_date": date(2026, 7, 28), "is_open": True},
        ],
    )


def _daily(close: float) -> pa.Table:
    return _table(
        "daily_bar_unadjusted",
        [
            {
                "code": "000001.SZ",
                "trade_date": trading_date,
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": close,
                "pre_close": 10.0,
                "volume": 1_000_000,
                "amount": close * 1_000_000,
            }
            for trading_date in (date(2026, 7, 15), date(2026, 7, 28))
        ],
    )


def _batch(upstream: DataUpstream, dataset: DatasetKind, row_count: int) -> IngestionBatch:
    endpoint = {
        DataUpstream.BAOSTOCK: "query_history_k_data_plus",
        DataUpstream.TENCENT: "stock_zh_a_hist_tx",
        DataUpstream.SINA: "stock_zh_a_daily",
    }[upstream]
    provider = DataProvider.BAOSTOCK if upstream is DataUpstream.BAOSTOCK else DataProvider.AKSHARE
    version_key = "baostock_version" if provider is DataProvider.BAOSTOCK else "akshare_version"
    return IngestionBatch(
        batch_id=new_entity_id("data_batch"),
        provider=provider,
        upstream=upstream,
        endpoint=endpoint,
        library_version="playwright-fixture",
        dataset=dataset,
        status=IngestionBatchStatus.SUCCEEDED,
        adapter_version="playwright-real-query-chain",
        request_params={
            "provider": provider.value,
            "upstream": upstream.value,
            "endpoint": endpoint,
            version_key: "playwright-fixture",
            "scope": "synthetic-loopback",
        },
        request_params_json='{"scope":"synthetic-loopback"}',
        started_at=utc_now(),
        ended_at=utc_now(),
        row_count=row_count,
        security_count=1,
        manifest_files=(
            ManifestFileEntry(relative_path="raw/part.parquet", size=1, sha256="a" * 64),
        ),
        failure=None,
        job_run_id=None,
        empty_result=None,
    )


def _quality_domain(outcome: QualityRunArtifact, relative_path: str) -> QualityRun:
    return QualityRun(
        run_id=outcome.run_id,
        version_id=outcome.version_id,
        status=outcome.status,
        rule_version=outcome.rule_version,
        schema_version=outcome.schema_version,
        content_sha256=outcome.content_sha256,
        started_at=outcome.started_at,
        ended_at=outcome.ended_at,
        results=tuple(
            QualityResult(
                rule_code=item.rule_code,
                passed=item.status is RuleOutcomeStatus.PASSED,
                affected_count=item.affected_count,
                summary=item.summary,
                report_relative_path=item.row_evidence_relative_path,
            )
            for item in outcome.results
            if item.status is not RuleOutcomeStatus.SKIPPED
        ),
        summary_report_relative_path=relative_path,
        failure=outcome.failure,
    )


def _publish(
    *,
    dataset: DatasetKind,
    upstream: DataUpstream,
    table: pa.Table,
    artifacts_root: Path,
    uow_factory: DataSqliteUnitOfWorkFactory,
    coordinator: CanonicalPublishCoordinator,
    runner: QualityRunner,
    reference_data: dict[str, ReferenceDataBinding],
) -> tuple[DatasetVersion, EntityId, IngestionBatch]:
    batch = _batch(upstream, dataset, table.num_rows)
    version = DatasetVersion.building(
        version_id=new_entity_id("data_version"),
        dataset=dataset,
        schema_version=f"{dataset.value}.v1",
        code_version="playwright-real-query-chain",
        rule_version="quality-rules.v1",
        parent_version_id=None,
        input_batch_ids=(batch.batch_id,),
        created_at=utc_now(),
    )
    with uow_factory() as uow:
        uow.data.save_batch(batch)
        uow.data.save_version(version)
        uow.commit()
    request = QualityRequest(
        dataset=dataset,
        version_id=version.version_id,
        rule_version=version.rule_version,
        schema_version=version.schema_version,
        code_version=version.code_version,
        content_sha256="0" * 64,
        input_batch_ids=version.input_batch_ids,
        expected_securities=(("000001.SZ",) if dataset is DatasetKind.DAILY_BAR_UNADJUSTED else ()),
        expected_exchanges=("SZ",),
        coverage_start=TradingDate(date(2026, 7, 15)),
        coverage_end=TradingDate(date(2026, 7, 28)),
        reference_data=reference_data,
        empty_result=None,
    )
    outcome = runner.run(request=request, table=table)
    directory = QualityArtifactWriter(artifacts_root=artifacts_root).write(
        outcome=outcome,
        row_evidence_tables={},
    )
    run = _quality_domain(outcome, directory.relative_to(artifacts_root).as_posix())
    with uow_factory() as uow:
        uow.data.save_quality_run(run)
        uow.commit()
    published = coordinator.coordinate(
        CanonicalPublishCoordinatorRequest(
            version=version,
            tables=(table,),
            code_version=version.code_version,
            rule_version=version.rule_version,
            quality_run_id=run.run_id,
        )
    ).version
    return published, run.run_id, batch


def _seed_data_center(
    *,
    data_root: Path,
    uow_factory: DataSqliteUnitOfWorkFactory,
    jobs: JobService,
) -> DataCenterQueryService:
    artifacts_root = data_root / "artifacts"
    coordinator = CanonicalPublishCoordinator(
        warehouse=CanonicalWarehouse(data_root=data_root),
        uow_factory=uow_factory,
    )
    loader = PublishedReferenceLoader(data_root=data_root, uow_factory=uow_factory)
    runner = QualityRunner(
        engine=QualityEngine(rule_set=default_rule_set()),
        reference_loader=loader,
    )
    calendar, _, _ = _publish(
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        upstream=DataUpstream.BAOSTOCK,
        table=_calendar(),
        artifacts_root=artifacts_root,
        uow_factory=uow_factory,
        coordinator=coordinator,
        runner=runner,
        reference_data={},
    )
    calendar_binding = ReferenceDataBinding(
        dataset=calendar.dataset,
        version_id=calendar.version_id,
        content_sha256=calendar.content_sha256 or "",
    )
    primary_table = _daily(10.5)
    fallback_table = _daily(10.8)
    primary, primary_run_id, primary_batch = _publish(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        upstream=DataUpstream.BAOSTOCK,
        table=primary_table,
        artifacts_root=artifacts_root,
        uow_factory=uow_factory,
        coordinator=coordinator,
        runner=runner,
        reference_data={"exchange_calendar": calendar_binding},
    )
    fallback, fallback_run_id, fallback_batch = _publish(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        upstream=DataUpstream.TENCENT,
        table=fallback_table,
        artifacts_root=artifacts_root,
        uow_factory=uow_factory,
        coordinator=coordinator,
        runner=runner,
        reference_data={"exchange_calendar": calendar_binding},
    )
    comparison = CrossSourceComparator().compare(
        request=CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version=primary.schema_version,
            primary_source=DataUpstream.BAOSTOCK,
            fallback_source=DataUpstream.TENCENT,
            primary_version_id=primary.version_id,
            fallback_version_id=fallback.version_id,
            primary_batch_id=primary_batch.batch_id,
            fallback_batch_id=fallback_batch.batch_id,
            primary_content_sha256=primary.content_sha256 or "",
            fallback_content_sha256=fallback.content_sha256 or "",
            primary_quality_run_id=primary_run_id,
            fallback_quality_run_id=fallback_run_id,
            primary_table=primary_table,
            fallback_table=fallback_table,
            thresholds=default_thresholds(),
            sample_spec=("fixed:000001.SZ:2026-07-15:2026-07-28",),
            created_at=utc_now(),
        )
    )
    DifferenceReportArtifactWriter(artifacts_root=artifacts_root).write(outcome=comparison)
    verifier = DifferenceReportVerifier(artifacts_root=artifacts_root)
    with uow_factory() as uow:
        uow.data.save_difference_report(verifier.verify(report_id=comparison.report_id))
        uow.commit()

    query_adapter = CanonicalQueryAdapter(data_root=data_root, uow_factory=uow_factory)
    return DataCenterQueryService(
        data_uow_factory=cast(DataUnitOfWorkFactory, uow_factory),
        preview=cast(CanonicalPreviewPort, query_adapter),
        difference_verifier=cast(DifferenceVerifierPort, verifier),
        jobs=jobs,
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
        data_root = Path(temporary)
        database = data_root / "e2e.db"
        initialize_database(database)
        engine = create_data_engine(database, busy_timeout_ms=1_000)
        jobs = JobService(SqliteUnitOfWorkFactory(engine), clock=lambda: fixed_now)
        with patch("quantking.jobs.services.new_entity_id", return_value=FIXED_JOB_ID):
            job = jobs.create_job(
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
        jobs.transition(job.job_id, JobStatus.RUNNING, worker_id="e2e-worker")
        jobs.report_progress(
            job.job_id,
            stage="synthetic-step",
            progress=0.5,
            summary="synthetic progress",
        )
        data_uow_factory = DataSqliteUnitOfWorkFactory(engine)
        center = _seed_data_center(
            data_root=data_root,
            uow_factory=data_uow_factory,
            jobs=jobs,
        )
        eod = EodCommandService(
            jobs,
            config=EodExecutionConfig.production(
                code_version="playwright",
                settings=AppSettings(
                    paths=RuntimePaths(
                        code_root=Path.cwd().resolve(),
                        data_root=data_root.resolve(),
                        offsite_backup_root=(
                            data_root.parent / f"{data_root.name}-offsite"
                        ).resolve(),
                    ),
                    web_host="127.0.0.1",
                    web_port=args.port,
                    sqlite_busy_timeout_ms=250,
                    sqlite_max_attempts=7,
                    sqlite_retry_base_delay_ms=10,
                    sqlite_retry_cap_delay_ms=160,
                    sqlite_retry_jitter_ratio=0.0,
                    job_heartbeat_min_interval_ms=1000,
                    job_progress_min_interval_ms=250,
                    eod_symbols=("000001.SZ", "600000.SH"),
                ),
            ),
            base_resolver=_BaseResolver(),
        )
        app = create_app(
            jobs,
            eod_command_port=eod,
            job_command_port=jobs,
            data_center_port=center,
            static_root=static_root,
            clock=lambda: fixed_now,
        )
        server = uvicorn.Server(uvicorn.Config(app, host=LOOPBACK_HOST, port=args.port))

        @app.post("/__playwright__/shutdown", include_in_schema=False)
        async def _shutdown_fixture() -> dict[str, str]:
            async def stop_after_response() -> None:
                await asyncio.sleep(0.25)
                server.should_exit = True

            app.state.shutdown_task = asyncio.create_task(stop_after_response())
            return {"status": "scheduled"}

        try:
            server.run()
        finally:
            engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
