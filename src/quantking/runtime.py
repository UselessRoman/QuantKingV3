"""Typed local composition root for the explicit EOD CLI, Worker and Web API."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import cast

from quantking.common.config import AppSettings, load_settings
from quantking.common.errors import ConfigurationError
from quantking.common.time import utc_now
from quantking.data.adapters.eod_base import PublishedEodBaseResolver
from quantking.data.adapters.eod_worker import EodWorkerHandler, register_eod_handler
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.adapters.sources.akshare import (
    AkshareDailyBarAdapter,
    AkshareMarketDataGateway,
)
from quantking.data.adapters.sources.baostock import (
    BaostockDailyBarAdapter,
    BaostockMarketDataGateway,
)
from quantking.data.adapters.sources.ingestion import (
    AkshareIngestionService,
    MarketDataGateway,
)
from quantking.data.adapters.sources.registry import (
    AKSHARE_RUNTIME_UPSTREAMS,
    PRODUCTION_UPSTREAMS,
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
    ResolutionVerifierPort,
)
from quantking.data.comparison.verifier import DifferenceReportVerifier
from quantking.data.eod.release import S107K4_CODE_VERSION
from quantking.data.eod.service import EodCommandService, EodExecutionConfig
from quantking.data.models import DataUpstream
from quantking.data.quality.artifacts import QualityArtifactWriter
from quantking.data.quality.engine import QualityEngine
from quantking.data.quality.references import PublishedReferenceLoader
from quantking.data.quality.rules import k4_daily_rule_set
from quantking.data.quality.runner import QualityRunner
from quantking.data.resolution.artifacts import ResolutionArtifactVerifier
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
EOD_CODE_VERSION = S107K4_CODE_VERSION
EXPECTED_DATABASE_REVISION = "20260810_0014"
WORKER_LEASE_TIMEOUT = timedelta(minutes=2)
_S108_BUSINESS_ROOTS = ("metadata", "raw", "artifacts", "warehouse")


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
    data_center: DataCenterQueryService


def load_runtime_settings(config_path: Path = DEFAULT_CONFIG_PATH) -> AppSettings:
    """Load only the repository-controlled settings contract and known environment keys."""
    return load_settings(config_path)


def _gateways() -> Mapping[DataUpstream, MarketDataGateway]:
    gateways: dict[DataUpstream, MarketDataGateway] = {
        upstream: AkshareMarketDataGateway(AkshareDailyBarAdapter(upstream))
        for upstream in AKSHARE_RUNTIME_UPSTREAMS
    }
    gateways[DataUpstream.BAOSTOCK] = BaostockMarketDataGateway(BaostockDailyBarAdapter())
    return gateways


def _preflight_initial_data_root(data_root: Path) -> None:
    """Refuse unknown business state before the first S1-08 database is created."""
    if not data_root.exists():
        return
    if not data_root.is_dir():
        raise ConfigurationError("runtime data root is not a directory")
    database = data_root / "metadata" / "quantking.db"
    if database.is_file():
        return
    unexpected = tuple(name for name in _S108_BUSINESS_ROOTS if (data_root / name).exists())
    if unexpected:
        raise ConfigurationError(
            "initial S1-08 data root contains unknown business state",
            context={"roots": ",".join(unexpected)},
        )


def build_eod_runtime(
    settings: AppSettings,
    *,
    gateway: MarketDataGateway | None = None,
    gateways: Mapping[DataUpstream, MarketDataGateway] | None = None,
    execution_config: EodExecutionConfig | None = None,
    quality_runner: QualityRunner | None = None,
) -> EodRuntime:
    """Build the production object graph without importing AKShare eagerly.

    Filesystem/database initialization happens only when this explicit function is
    called. Module import and ``--help`` do not call it.
    """
    data_root = settings.paths.data_root
    _preflight_initial_data_root(data_root)
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
    if gateway is not None:
        raise ConfigurationError("K4 runtime requires an explicit three-role gateways mapping")
    selected_execution_config = execution_config or EodExecutionConfig.production(
        code_version=EOD_CODE_VERSION,
        settings=settings,
    )
    selected_gateways = dict(gateways or _gateways())
    expected_upstreams = PRODUCTION_UPSTREAMS
    if set(selected_gateways) != set(expected_upstreams):
        raise ConfigurationError("runtime gateways do not match the frozen source route")
    ingestions = {
        upstream: AkshareIngestionService(
            gateway=selected_gateways[upstream],
            raw_publisher=RawBatchPublisher(data_root=data_root),
            data_root=data_root,
            uow_factory=data_uow,
            clock=utc_now,
        )
        for upstream in expected_upstreams
    }
    reference_loader = PublishedReferenceLoader(
        data_root=data_root,
        uow_factory=data_uow,
    )
    selected_quality_runner = quality_runner or QualityRunner(
        engine=QualityEngine(rule_set=k4_daily_rule_set()),
        reference_loader=reference_loader,
    )
    handler = EodWorkerHandler(
        ingestions=ingestions,
        quality_runner=selected_quality_runner,
        quality_writer=QualityArtifactWriter(artifacts_root=data_root / "artifacts" / "quality"),
        publish_coordinator=CanonicalPublishCoordinator(
            warehouse=CanonicalWarehouse(data_root=data_root),
            uow_factory=data_uow,
        ),
        data_uow_factory=data_uow,
        data_root=data_root,
        clock=utc_now,
        log_root=data_root,
        reference_loader=reference_loader,
    )
    registry = WorkerRegistry()
    register_eod_handler(registry, handler)
    worker = Worker(jobs, registry, worker_id=f"local-worker-{os.getpid()}")
    commands = EodCommandService(
        jobs,
        config=selected_execution_config,
        base_resolver=PublishedEodBaseResolver(
            data_root=data_root,
            uow_factory=data_uow,
        ),
    )
    data_center = DataCenterQueryService(
        data_uow_factory=cast(DataUnitOfWorkFactory, data_uow),
        preview=cast(
            CanonicalPreviewPort,
            CanonicalQueryAdapter(data_root=data_root, uow_factory=data_uow),
        ),
        difference_verifier=cast(
            DifferenceVerifierPort,
            DifferenceReportVerifier(artifacts_root=data_root / "artifacts"),
        ),
        resolution_verifier=cast(
            ResolutionVerifierPort,
            ResolutionArtifactVerifier(artifacts_root=data_root / "artifacts"),
        ),
        jobs=jobs,
    )
    web_app = create_app(
        jobs,
        eod_command_port=commands,
        job_command_port=jobs,
        data_center_port=data_center,
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
        data_center=data_center,
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
