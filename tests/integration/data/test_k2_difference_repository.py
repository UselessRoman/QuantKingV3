from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.errors import InvalidDataTransitionError
from quantking.common.ids import EntityId, new_entity_id
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.comparison.artifacts import DifferenceReportArtifactWriter
from quantking.data.comparison.comparator import (
    CrossSourceComparator,
    CrossSourceComparisonRequest,
)
from quantking.data.comparison.thresholds import default_thresholds
from quantking.data.comparison.verifier import (
    DifferenceReportVerifier,
    VerifiedDifferenceReport,
)
from quantking.data.content import canonical_content_sha256
from quantking.data.models import (
    DataProvider,
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    DataUpstream,
    EmptyResultInfo,
    IngestionBatch,
    IngestionBatchStatus,
    LegalEmptyClassification,
    ManifestFileEntry,
    QualityResult,
    QualityRun,
    QualityRunStatus,
    transition_dataset_version,
)
from quantking.data.schemas import descriptor_for
from quantking.jobs.adapters.persistence.sqlite import initialize_database

_CREATED = UtcTimestamp.from_iso("2026-08-09T10:00:00Z")
_VALIDATED = UtcTimestamp.from_iso("2026-08-09T10:01:00Z")
_PUBLISHED = UtcTimestamp.from_iso("2026-08-09T10:02:00Z")
_TRADE_DATE = date(2026, 8, 7)


def _table(close: float) -> pa.Table:
    return pa.Table.from_pylist(
        [
            {
                "code": "600000.SH",
                "trade_date": _TRADE_DATE,
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": close,
                "pre_close": 10.0,
                "volume": 1_000_000,
                "amount": 10_500_000.0,
            }
        ],
        schema=canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value),
    )


def _batch(
    upstream: DataUpstream,
    *,
    empty_result: EmptyResultInfo | None = None,
) -> IngestionBatch:
    endpoint = {
        DataUpstream.EASTMONEY: "stock_zh_a_hist",
        DataUpstream.TENCENT: "stock_zh_a_hist_tx",
        DataUpstream.SINA: "stock_zh_a_daily",
    }[upstream]
    return IngestionBatch(
        batch_id=new_entity_id("data_batch"),
        provider=DataProvider.AKSHARE,
        upstream=upstream,
        endpoint=endpoint,
        library_version="1.18.83",
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        status=IngestionBatchStatus.SUCCEEDED,
        adapter_version="akshare-k1.v1",
        request_params={"upstream": upstream.value},
        request_params_json="{}",
        started_at=_CREATED,
        ended_at=_VALIDATED,
        row_count=0 if empty_result is not None else 1,
        security_count=0 if empty_result is not None else 1,
        manifest_files=(
            ManifestFileEntry(
                relative_path=f"raw/{upstream.value.lower()}.parquet",
                size=1,
                sha256=("a" * 64 if upstream is DataUpstream.EASTMONEY else "b" * 64),
            ),
        ),
        failure=None,
        job_run_id=None,
        empty_result=empty_result,
    )


def _quality(version_id: EntityId, content_sha256: str) -> QualityRun:
    return QualityRun(
        run_id=new_entity_id("data_quality"),
        version_id=version_id,
        status=QualityRunStatus.PASSED,
        rule_version="quality-rules.v1",
        schema_version=descriptor_for(DatasetKind.DAILY_BAR_UNADJUSTED.value).schema_version,
        content_sha256=content_sha256,
        started_at=_CREATED,
        ended_at=_VALIDATED,
        results=(
            QualityResult(
                rule_code="schema_exact",
                passed=True,
                affected_count=0,
                summary="passed",
                report_relative_path=None,
            ),
        ),
        summary_report_relative_path=None,
        failure=None,
    )


def _artifact(
    *,
    root: Path,
    version_id: EntityId,
    a_batch: IngestionBatch,
    b_batch: IngestionBatch,
    a_quality: QualityRun,
    b_quality: QualityRun,
    a_table: pa.Table,
    b_table: pa.Table,
    a_empty_result: EmptyResultInfo | None = None,
    b_empty_result: EmptyResultInfo | None = None,
) -> VerifiedDifferenceReport:
    outcome = CrossSourceComparator().compare(
        request=CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version=descriptor_for(DatasetKind.DAILY_BAR_UNADJUSTED.value).schema_version,
            primary_source=DataUpstream.EASTMONEY,
            fallback_source=DataUpstream.TENCENT,
            primary_version_id=version_id,
            fallback_version_id=version_id,
            primary_batch_id=a_batch.batch_id,
            fallback_batch_id=b_batch.batch_id,
            primary_content_sha256=a_quality.content_sha256,
            fallback_content_sha256=b_quality.content_sha256,
            primary_quality_run_id=a_quality.run_id,
            fallback_quality_run_id=b_quality.run_id,
            primary_table=a_table,
            fallback_table=b_table,
            thresholds=default_thresholds(),
            sample_spec=("K2_AFFECTED_SCOPE",),
            created_at=_CREATED,
            comparison_primary_keys=(("600000.SH", _TRADE_DATE),),
            primary_empty_result=a_empty_result,
            fallback_empty_result=b_empty_result,
        )
    )
    DifferenceReportArtifactWriter(artifacts_root=root).write(outcome=outcome)
    return DifferenceReportVerifier(artifacts_root=root).verify(report_id=outcome.report_id)


@pytest.fixture()
def uow_factory(tmp_path: Path) -> DataSqliteUnitOfWorkFactory:
    database = tmp_path / "data.sqlite3"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    yield DataSqliteUnitOfWorkFactory(engine)
    engine.dispose()


def _graph(
    factory: DataSqliteUnitOfWorkFactory,
    artifacts_root: Path,
    *,
    include_fallback_lineage: bool = True,
    primary_empty: bool = False,
) -> tuple[VerifiedDifferenceReport, DatasetVersion, DatasetVersion]:
    empty_result = (
        EmptyResultInfo(
            classification=LegalEmptyClassification.NO_TRADING_SESSION,
            reason="upstream returned no rows for a confirmed no-trading session",
        )
        if primary_empty
        else None
    )
    a_table = (
        pa.Table.from_pylist(
            [],
            schema=canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value),
        )
        if primary_empty
        else _table(10.5)
    )
    b_table = _table(10.6)
    a_sha = canonical_content_sha256(
        DatasetKind.DAILY_BAR_UNADJUSTED,
        [a_table],
        empty_result=empty_result,
    )
    b_sha = canonical_content_sha256(DatasetKind.DAILY_BAR_UNADJUSTED, [b_table])
    a_batch = _batch(DataUpstream.EASTMONEY, empty_result=empty_result)
    b_batch = _batch(DataUpstream.TENCENT)
    lineage = (
        (a_batch.batch_id, b_batch.batch_id) if include_fallback_lineage else (a_batch.batch_id,)
    )
    building = DatasetVersion.building(
        version_id=new_entity_id("data_version"),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        schema_version=descriptor_for(DatasetKind.DAILY_BAR_UNADJUSTED.value).schema_version,
        code_version="k2-test",
        parent_version_id=None,
        input_batch_ids=lineage,
        created_at=_CREATED,
    )
    a_quality = _quality(building.version_id, a_sha)
    b_quality = _quality(building.version_id, b_sha)
    final_quality = _quality(building.version_id, b_sha if primary_empty else a_sha)
    verified = _artifact(
        root=artifacts_root,
        version_id=building.version_id,
        a_batch=a_batch,
        b_batch=b_batch,
        a_quality=a_quality,
        b_quality=b_quality,
        a_table=a_table,
        b_table=b_table,
        a_empty_result=empty_result,
    )
    with factory() as uow:
        uow.data.save_batch(a_batch)
        uow.data.save_batch(b_batch)
        uow.data.save_version(building)
        uow.data.save_quality_run(a_quality)
        uow.data.save_quality_run(b_quality)
        uow.data.save_quality_run(final_quality)
        uow.commit()
    validated = transition_dataset_version(
        building,
        DatasetVersionStatus.VALIDATED,
        at=_VALIDATED,
        manifest_sha256="a" * 64,
        security_count=1,
        row_count=1,
        coverage_start=TradingDate(_TRADE_DATE),
        coverage_end=TradingDate(_TRADE_DATE),
        coverage_summary={"date_count": "1", "security_count": "1"},
        quality_run_id=final_quality.run_id,
        content_sha256=final_quality.content_sha256,
    )
    published = transition_dataset_version(
        validated,
        DatasetVersionStatus.PUBLISHED,
        at=_PUBLISHED,
    )
    return verified, validated, published


def test_k2_report_is_auditable_while_building_and_survives_final_publish(
    tmp_path: Path,
    uow_factory: DataSqliteUnitOfWorkFactory,
) -> None:
    verified, validated, published = _graph(uow_factory, tmp_path / "artifacts")
    with uow_factory() as uow:
        building_report = uow.data.save_difference_report(verified)
        uow.commit()
    assert building_report.primary_version_id == building_report.fallback_version_id
    with uow_factory() as uow:
        uow.data.save_version(validated)
        uow.data.save_version(published)
        assert uow.data.save_difference_report(verified) == building_report
        uow.commit()
    with uow_factory() as uow:
        published_versions = uow.data.list_versions(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            status=DatasetVersionStatus.PUBLISHED,
        )
        assert published_versions == [published]
        stored = uow.data.get_difference_report(str(verified.report_id))
        assert stored.primary_version_id == stored.fallback_version_id == published.version_id
        assert stored.fallback_content_sha256 != published.content_sha256


@pytest.mark.parametrize(
    "mutation",
    [
        {"fallback_content_sha256": "e" * 64},
        {"fallback_batch_id": new_entity_id("data_batch")},
        {"fallback_quality_run_id": new_entity_id("data_quality")},
        {"binding_mode": "DISTINCT_PUBLISHED_VERSIONS"},
        {
            "primary_empty_result": {
                "classification": "NO_TRADING_SESSION",
                "reason": "forged empty result",
            }
        },
    ],
)
def test_k2_repository_rejects_broken_source_or_artifact_binding(
    tmp_path: Path,
    uow_factory: DataSqliteUnitOfWorkFactory,
    mutation: dict[str, object],
) -> None:
    verified, validated, published = _graph(uow_factory, tmp_path / "artifacts")
    with uow_factory() as uow:
        uow.data.save_version(validated)
        uow.data.save_version(published)
        with pytest.raises(InvalidDataTransitionError):
            uow.data.save_difference_report(replace(verified, **mutation))
        uow.rollback()


def test_k2_repository_requires_both_batches_in_final_lineage(
    tmp_path: Path,
    uow_factory: DataSqliteUnitOfWorkFactory,
) -> None:
    verified, validated, published = _graph(
        uow_factory,
        tmp_path / "artifacts",
        include_fallback_lineage=False,
    )
    with uow_factory() as uow:
        uow.data.save_version(validated)
        uow.data.save_version(published)
        with pytest.raises(InvalidDataTransitionError, match="fallback version"):
            uow.data.save_difference_report(verified)
        uow.rollback()


def test_k2_repository_accepts_batch_bound_legal_empty_side(
    tmp_path: Path,
    uow_factory: DataSqliteUnitOfWorkFactory,
) -> None:
    verified, validated, published = _graph(
        uow_factory,
        tmp_path / "artifacts",
        primary_empty=True,
    )
    assert verified.primary_empty_result == {
        "classification": LegalEmptyClassification.NO_TRADING_SESSION.value,
        "reason": "upstream returned no rows for a confirmed no-trading session",
    }
    with uow_factory() as uow:
        uow.data.save_version(validated)
        uow.data.save_version(published)
        stored = uow.data.save_difference_report(verified)
        uow.commit()
    assert stored.primary_batch_id == verified.primary_batch_id


@pytest.mark.parametrize(
    "declared_empty",
    [
        {
            "classification": LegalEmptyClassification.NO_TRADING_SESSION.value,
            "reason": "a different stable reason",
        },
        {
            "classification": LegalEmptyClassification.OUTSIDE_LISTING_RANGE.value,
            "reason": "upstream returned no rows for a confirmed no-trading session",
        },
    ],
)
def test_k2_repository_rejects_empty_metadata_mismatch(
    tmp_path: Path,
    uow_factory: DataSqliteUnitOfWorkFactory,
    declared_empty: dict[str, str],
) -> None:
    verified, validated, published = _graph(
        uow_factory,
        tmp_path / "artifacts",
        primary_empty=True,
    )
    with uow_factory() as uow:
        uow.data.save_version(validated)
        uow.data.save_version(published)
        with pytest.raises(InvalidDataTransitionError, match="empty result"):
            uow.data.save_difference_report(replace(verified, primary_empty_result=declared_empty))
        uow.rollback()
