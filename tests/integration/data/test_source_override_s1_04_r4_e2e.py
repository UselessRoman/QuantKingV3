"""S1-04-R4 production-path SourceOverride end-to-end regression."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.ids import EntityId, new_entity_id
from quantking.common.time import TradingDate, utc_now
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.adapters.persistence.sqlite import (
    DataRepository,
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.adapters.warehouse.canonical_warehouse import CanonicalWarehouse
from quantking.data.adapters.warehouse.publish_coordinator import (
    CanonicalPublishCoordinator,
    CanonicalPublishCoordinatorRequest,
)
from quantking.data.adapters.warehouse.query import CanonicalQueryAdapter
from quantking.data.comparison.artifacts import DifferenceReportArtifactWriter
from quantking.data.comparison.comparator import (
    CrossSourceComparator,
    CrossSourceComparisonRequest,
)
from quantking.data.comparison.thresholds import default_thresholds
from quantking.data.comparison.verifier import DifferenceReportVerifier
from quantking.data.models import (
    DatasetKind,
    DatasetVersion,
    DataSource,
    IngestionBatch,
    IngestionBatchStatus,
    ManifestFileEntry,
    QualityResult,
    QualityRun,
    SourceOverrideStatus,
)
from quantking.data.override.service import CreateOverrideRequest, SourceOverrideService
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
from quantking.jobs.adapters.persistence.sqlite import initialize_database


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
        [{"exchange": "SZ", "trade_date": date(2026, 7, 15), "is_open": True}],
    )


def _daily(close: float) -> pa.Table:
    return _table(
        "daily_bar_unadjusted",
        [
            {
                "code": "000001.SZ",
                "trade_date": date(2026, 7, 15),
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": close,
                "pre_close": 10.0,
                "volume": 1_000_000,
                "amount": close * 1_000_000,
            }
        ],
    )


def _batch(source: DataSource, dataset: DatasetKind) -> IngestionBatch:
    return IngestionBatch(
        batch_id=new_entity_id("data_batch"),
        source=source,
        dataset=dataset,
        status=IngestionBatchStatus.SUCCEEDED,
        adapter_version="s1-04-r4",
        request_params={"scope": "fixed"},
        request_params_json='{"scope":"fixed"}',
        started_at=utc_now(),
        ended_at=utc_now(),
        row_count=1,
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
    source: DataSource,
    table: pa.Table,
    data_root: Path,
    artifacts_root: Path,
    uow_factory: DataSqliteUnitOfWorkFactory,
    coordinator: CanonicalPublishCoordinator,
    runner: QualityRunner,
    reference_data: dict[str, ReferenceDataBinding],
) -> tuple[DatasetVersion, EntityId, IngestionBatch]:
    batch = _batch(source, dataset)
    version = DatasetVersion.building(
        version_id=new_entity_id("data_version"),
        dataset=dataset,
        schema_version=f"{dataset.value}.v1",
        code_version="s1-04-r4",
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
        expected_securities=("000001.SZ",) if dataset is not DatasetKind.EXCHANGE_CALENDAR else (),
        expected_exchanges=("SZ",),
        coverage_start=TradingDate(date(2026, 7, 15)),
        coverage_end=TradingDate(date(2026, 7, 15)),
        reference_data=reference_data,
        empty_result=None,
    )
    outcome = runner.run(request=request, table=table)
    writer = QualityArtifactWriter(artifacts_root=artifacts_root)
    directory = writer.write(outcome=outcome, row_evidence_tables={})
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


@pytest.mark.parametrize("mode", ["normal", "concurrent", "post_publish_interrupt"])
def test_source_override_full_real_production_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    database = tmp_path / "data.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=1_000)
    uow_factory = DataSqliteUnitOfWorkFactory(engine)
    artifacts_root = tmp_path / "artifacts"
    warehouse = CanonicalWarehouse(data_root=tmp_path)
    coordinator = CanonicalPublishCoordinator(warehouse=warehouse, uow_factory=uow_factory)
    loader = PublishedReferenceLoader(data_root=tmp_path, uow_factory=uow_factory)
    runner = QualityRunner(
        engine=QualityEngine(rule_set=default_rule_set()), reference_loader=loader
    )
    calendar, _, _ = _publish(
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        source=DataSource.XTQUANT,
        table=_calendar(),
        data_root=tmp_path,
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
        source=DataSource.XTQUANT,
        table=primary_table,
        data_root=tmp_path,
        artifacts_root=artifacts_root,
        uow_factory=uow_factory,
        coordinator=coordinator,
        runner=runner,
        reference_data={"exchange_calendar": calendar_binding},
    )
    fallback, fallback_run_id, fallback_batch = _publish(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        source=DataSource.BAOSTOCK,
        table=fallback_table,
        data_root=tmp_path,
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
            primary_source=DataSource.XTQUANT.value,
            fallback_source=DataSource.BAOSTOCK.value,
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
            sample_spec=("fixed:000001.SZ:2026-07-15",),
            created_at=utc_now(),
        )
    )
    DifferenceReportArtifactWriter(artifacts_root=artifacts_root).write(outcome=comparison)
    verifier = DifferenceReportVerifier(artifacts_root=artifacts_root)
    verified = verifier.verify(report_id=comparison.report_id)
    with uow_factory() as uow:
        uow.data.save_difference_report(verified)
        uow.commit()

    service = SourceOverrideService(
        uow_factory=uow_factory,
        query_adapter=CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory),
        quality_engine=QualityEngine(rule_set=default_rule_set()),
        reference_loader=loader,
        quality_artifact_writer=QualityArtifactWriter(artifacts_root=artifacts_root),
        publish_coordinator=coordinator,
        difference_report_verifier=verifier,
    )
    pending = service.create_override(
        CreateOverrideRequest(
            difference_report_id=comparison.report_id,
            primary_batch_id=primary_batch.batch_id,
            fallback_batch_id=fallback_batch.batch_id,
            primary_source=DataSource.XTQUANT.value,
            fallback_source=DataSource.BAOSTOCK.value,
            scope_codes=("000001.SZ",),
            scope_dates=(TradingDate(date(2026, 7, 15)),),
            fields=("close",),
            reason="verified close difference",
        ),
        created_at=utc_now(),
    )
    service.approve_override(pending.override_id, actor="analyst", at=utc_now())
    quality_request = QualityRequest(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        version_id=new_entity_id("data_version"),
        rule_version="quality-rules.v1",
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-04-r4",
        content_sha256="0" * 64,
        input_batch_ids=(),
        expected_securities=("000001.SZ",),
        expected_exchanges=("SZ",),
        coverage_start=TradingDate(date(2026, 7, 15)),
        coverage_end=TradingDate(date(2026, 7, 15)),
        reference_data={"exchange_calendar": calendar_binding},
        empty_result=None,
    )
    apply = lambda: service.apply_override(  # noqa: E731
        pending.override_id,
        utc_now(),
        actor="analyst",
        quality_request=quality_request,
    )
    interrupted_child = None
    if mode == "concurrent":

        def concurrent_apply() -> object:
            try:
                return apply()
            except Exception as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _item: concurrent_apply(), range(2)))
        applied_results = [item for item in results if not isinstance(item, Exception)]
        assert applied_results
        applied = applied_results[0]
        assert all(
            item.applied_version_id == applied.applied_version_id for item in applied_results
        )
    elif mode == "post_publish_interrupt":
        original_bind = DataRepository.bind_published_override_child
        calls = 0

        def interrupt_once(self: DataRepository, **kwargs: object):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("synthetic interruption after publish")
            return original_bind(self, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(DataRepository, "bind_published_override_child", interrupt_once)
        with pytest.raises(RuntimeError, match="after publish"):
            apply()
        with uow_factory() as uow:
            interrupted_child = uow.data.find_recoverable_override_child(
                str(pending.override_id)
            ).version_id
        monkeypatch.setattr(DataRepository, "bind_published_override_child", original_bind)
        applied = apply()
        assert applied.applied_version_id == interrupted_child
    else:
        applied = apply()
    assert applied.status is SourceOverrideStatus.APPLIED
    assert applied.applied_version_id is not None
    result = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory).query_by_id(
        version_id=str(applied.applied_version_id),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
    )
    assert result.column("close").to_pylist() == [10.8]
    engine.dispose()
