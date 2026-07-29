"""Typed local composition root for the explicit EOD CLI, Worker and Web API."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import cast

from quantking.common.config import AppSettings, load_settings
from quantking.common.errors import ConfigurationError
from quantking.common.time import utc_now
from quantking.data.adapters.eod import EodWorkerHandler, register_eod_handler
from quantking.data.adapters.eod_base import PublishedEodBaseResolver
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.adapters.sources.ingestion import (
    MarketDataGateway,
    XtQuantIngestionService,
)
from quantking.data.adapters.sources.xtquant import (
    ExplicitXtQuantModuleLoader,
    XtQuantMarketDataGateway,
)
from quantking.data.adapters.warehouse.canonical_warehouse import CanonicalWarehouse
from quantking.data.adapters.warehouse.publish_coordinator import (
    CanonicalPublishCoordinator,
)
from quantking.data.adapters.warehouse.query import CanonicalQueryAdapter
from quantking.data.adapters.warehouse.raw_store import RawBatchPublisher
from quantking.data.center import (
    CanonicalPreviewPort,
    DataCenterQueryService,
    DataUnitOfWorkFactory,
    DifferenceVerifierPort,
)
from quantking.data.comparison.verifier import DifferenceReportVerifier
from quantking.data.eod.artifacts import EodEvidenceWriter
from quantking.data.eod.service import EodCommandService, EodExecutionConfig
from quantking.data.quality.artifacts import QualityArtifactWriter
from quantking.data.quality.engine import QualityEngine
from quantking.data.quality.references import PublishedReferenceLoader
from quantking.data.quality.rules import default_rule_set
from quantking.data.quality.runner import QualityRunner
from quantking.jobs.adapters.persistence.sqlite import (
    SqliteUnitOfWorkFactory,
    create_sqlite_engine,
    initialize_database,
)
from quantking.jobs.models import RetryPolicy
from quantking.jobs.services import JobService
from quantking.jobs.worker import Worker, WorkerRegistry
from quantking.web.app import create_app

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _REPOSITORY_ROOT / "configs" / "default.toml"
EOD_CODE_VERSION = "s1-05-r5"
EXPECTED_DATABASE_REVISION = "20260729_0011"
WORKER_LEASE_TIMEOUT = timedelta(minutes=2)


@dataclass(frozen=True, slots=True)
class EodRuntime:
    """All concrete local ports shared by CLI, explicit Worker and FastAPI."""

    settings: AppSettings
    jobs: JobService
    eod_commands: EodCommandService
    registry: WorkerRegistry
    worker: Worker
    eod_handler: EodWorkerHandler
    web_app: object
    data_uow_factory: DataSqliteUnitOfWorkFactory


def load_runtime_settings(config_path: Path = DEFAULT_CONFIG_PATH) -> AppSettings:
    """Load only the repository-controlled settings contract and known environment keys."""
    return load_settings(config_path)


def _gateway(settings: AppSettings) -> MarketDataGateway:
    root = settings.xtquant_site_packages_root
    return XtQuantMarketDataGateway(module_loader=lambda: ExplicitXtQuantModuleLoader(root).load())


def build_eod_runtime(
    settings: AppSettings,
    *,
    gateway: MarketDataGateway | None = None,
) -> EodRuntime:
    """Build the production object graph without importing the XTQuant SDK.

    Filesystem/database initialization happens only when this explicit function is
    called. Module import and ``--help`` do not call it.
    """
    data_root = settings.paths.data_root
    data_root.mkdir(parents=True, exist_ok=True)
    metadata_root = data_root / "metadata"
    metadata_root.mkdir(parents=True, exist_ok=True)
    database = metadata_root / "quantking.db"
    if not database.exists():
        initialize_database(
            database,
            busy_timeout_ms=settings.sqlite_busy_timeout_ms,
        )
    try:
        with sqlite3.connect(database) as connection:
            rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    except sqlite3.DatabaseError as error:
        raise ConfigurationError("runtime database revision is unreadable") from error
    if rows != [(EXPECTED_DATABASE_REVISION,)]:
        raise ConfigurationError("runtime database revision does not match the required head")
    jobs_engine = create_sqlite_engine(
        database,
        busy_timeout_ms=settings.sqlite_busy_timeout_ms,
    )
    data_engine = create_data_engine(
        database,
        busy_timeout_ms=settings.sqlite_busy_timeout_ms,
    )
    retry = RetryPolicy(
        max_attempts=settings.sqlite_max_attempts,
        base_delay_ms=settings.sqlite_retry_base_delay_ms,
        cap_delay_ms=settings.sqlite_retry_cap_delay_ms,
        jitter_ratio=settings.sqlite_retry_jitter_ratio,
        heartbeat_min_interval_ms=settings.job_heartbeat_min_interval_ms,
        progress_min_interval_ms=settings.job_progress_min_interval_ms,
    )
    jobs = JobService(SqliteUnitOfWorkFactory(jobs_engine), retry_policy=retry)
    data_uow = DataSqliteUnitOfWorkFactory(data_engine)
    ingestion = XtQuantIngestionService(
        gateway=gateway or _gateway(settings),
        raw_publisher=RawBatchPublisher(data_root=data_root),
        data_root=data_root,
        uow_factory=data_uow,
        clock=utc_now,
    )
    quality_runner = QualityRunner(
        engine=QualityEngine(rule_set=default_rule_set()),
        reference_loader=PublishedReferenceLoader(
            data_root=data_root,
            uow_factory=data_uow,
        ),
    )
    handler = EodWorkerHandler(
        ingestion=ingestion,
        quality_runner=quality_runner,
        quality_writer=QualityArtifactWriter(artifacts_root=data_root / "artifacts" / "quality"),
        publish_coordinator=CanonicalPublishCoordinator(
            warehouse=CanonicalWarehouse(data_root=data_root),
            uow_factory=data_uow,
        ),
        data_uow_factory=data_uow,
        evidence_writer=EodEvidenceWriter(artifacts_root=data_root / "artifacts" / "eod"),
        data_root=data_root,
        clock=utc_now,
        log_root=data_root,
    )
    registry = WorkerRegistry()
    register_eod_handler(registry, handler)
    worker = Worker(jobs, registry, worker_id=f"local-worker-{os.getpid()}")
    commands = EodCommandService(
        jobs,
        config=EodExecutionConfig.production(code_version=EOD_CODE_VERSION),
        base_resolver=PublishedEodBaseResolver(
            data_root=data_root,
            uow_factory=data_uow,
        ),
    )
    web_app = create_app(
        jobs,
        eod_command_port=commands,
        job_command_port=jobs,
        data_center_port=DataCenterQueryService(
            data_uow_factory=cast(DataUnitOfWorkFactory, data_uow),
            preview=cast(
                CanonicalPreviewPort,
                CanonicalQueryAdapter(data_root=data_root, uow_factory=data_uow),
            ),
            difference_verifier=cast(
                DifferenceVerifierPort,
                DifferenceReportVerifier(artifacts_root=data_root / "artifacts"),
            ),
            jobs=jobs,
        ),
    )
    return EodRuntime(
        settings=settings,
        jobs=jobs,
        eod_commands=commands,
        registry=registry,
        worker=worker,
        eod_handler=handler,
        web_app=web_app,
        data_uow_factory=data_uow,
    )


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "EOD_CODE_VERSION",
    "EXPECTED_DATABASE_REVISION",
    "WORKER_LEASE_TIMEOUT",
    "EodRuntime",
    "build_eod_runtime",
    "load_runtime_settings",
]
