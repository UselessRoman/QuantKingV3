"""S1-04-R4 SourceOverride production dependency and failure-path tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from quantking.common.errors import InvalidIdError
from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate, utc_now
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.adapters.warehouse.canonical_warehouse import CanonicalWarehouse
from quantking.data.adapters.warehouse.publish_coordinator import CanonicalPublishCoordinator
from quantking.data.adapters.warehouse.query import CanonicalQueryAdapter
from quantking.data.comparison.verifier import DifferenceReportVerifier
from quantking.data.models import DatasetKind
from quantking.data.override.service import SourceOverrideService
from quantking.data.quality.artifacts import QualityArtifactWriter
from quantking.data.quality.engine import QualityEngine
from quantking.data.quality.models import QualityRequest
from quantking.data.quality.references import PublishedReferenceLoader
from quantking.data.quality.rules import default_rule_set
from quantking.jobs.adapters.persistence.sqlite import initialize_database


def _service(tmp_path: Path) -> tuple[SourceOverrideService, DataSqliteUnitOfWorkFactory]:
    database = tmp_path / "data.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    return (
        SourceOverrideService(
            uow_factory=factory,
            query_adapter=CanonicalQueryAdapter(data_root=tmp_path, uow_factory=factory),
            quality_engine=QualityEngine(rule_set=default_rule_set()),
            reference_loader=PublishedReferenceLoader(data_root=tmp_path, uow_factory=factory),
            quality_artifact_writer=QualityArtifactWriter(artifacts_root=tmp_path / "artifacts"),
            publish_coordinator=CanonicalPublishCoordinator(
                warehouse=CanonicalWarehouse(data_root=tmp_path), uow_factory=factory
            ),
            difference_report_verifier=DifferenceReportVerifier(
                artifacts_root=tmp_path / "artifacts"
            ),
        ),
        factory,
    )


def test_service_rejects_missing_production_dependencies() -> None:
    with pytest.raises(TypeError):
        SourceOverrideService(uow_factory=None)  # type: ignore[call-arg,arg-type]


def test_apply_nonexistent_override_fails_before_any_output(tmp_path: Path) -> None:
    service, factory = _service(tmp_path)
    request = QualityRequest(
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        version_id=new_entity_id("data_version"),
        rule_version="quality-rules.v1",
        schema_version="exchange_calendar.v1",
        code_version="s1-04-r4",
        content_sha256="0" * 64,
        input_batch_ids=(),
        expected_securities=(),
        expected_exchanges=("SZ",),
        coverage_start=TradingDate(date(2026, 7, 15)),
        coverage_end=TradingDate(date(2026, 7, 15)),
        reference_data={},
        empty_result=None,
    )
    with pytest.raises(InvalidIdError):
        service.apply_override(new_entity_id("data_override"), utc_now(), quality_request=request)
    factory._session_factory.kw["bind"].dispose()
