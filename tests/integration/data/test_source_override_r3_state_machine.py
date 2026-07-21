"""S1-04-R3 red tests for recoverable SourceOverride application attempts."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate, utc_now
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.models import (
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    SourceOverrideStatus,
)
from quantking.jobs.adapters.persistence.sqlite import initialize_database


@pytest.fixture()
def uow_factory(tmp_path: Path) -> DataSqliteUnitOfWorkFactory:
    database = tmp_path / "data.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    yield DataSqliteUnitOfWorkFactory(engine)
    engine.dispose()


def test_repository_claims_single_active_attempt_per_override(
    uow_factory: DataSqliteUnitOfWorkFactory,
) -> None:
    override_id = new_entity_id("data_override")
    child_id = new_entity_id("data_version")

    with uow_factory() as uow:
        first = uow.data.claim_override_application(
            override_id=str(override_id),
            child_version_id=str(child_id),
            input_fingerprint="a" * 64,
            actor="worker-a",
            at=utc_now(),
        )
        second = uow.data.claim_override_application(
            override_id=str(override_id),
            child_version_id=str(new_entity_id("data_version")),
            input_fingerprint="a" * 64,
            actor="worker-b",
            at=utc_now(),
        )
        uow.commit()

    assert first.child_version_id == second.child_version_id == child_id
    assert first.attempt_id == second.attempt_id


def test_failed_attempt_releases_override_for_new_child(
    uow_factory: DataSqliteUnitOfWorkFactory,
) -> None:
    override_id = new_entity_id("data_override")
    child_id = new_entity_id("data_version")
    with uow_factory() as uow:
        attempt = uow.data.claim_override_application(
            override_id=str(override_id),
            child_version_id=str(child_id),
            input_fingerprint="a" * 64,
            actor="worker-a",
            at=utc_now(),
        )
        uow.data.finish_override_application_attempt(
            attempt_id=str(attempt.attempt_id),
            status="FAILED",
            failure_message="C:\\secret\\path token=abc123 raw failure",
            at=utc_now(),
        )
        replacement = uow.data.claim_override_application(
            override_id=str(override_id),
            child_version_id=str(new_entity_id("data_version")),
            input_fingerprint="b" * 64,
            actor="worker-b",
            at=utc_now(),
        )
        uow.commit()

    assert replacement.child_version_id != child_id
    assert "C:" not in replacement.failure_message
    assert "token=" not in replacement.failure_message


def test_published_child_before_applied_is_reused(uow_factory: DataSqliteUnitOfWorkFactory) -> None:
    override_id = new_entity_id("data_override")
    child = DatasetVersion.building(
        version_id=new_entity_id("data_version"),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-04",
        parent_version_id=new_entity_id("data_version"),
        input_batch_ids=(),
        created_at=utc_now(),
    )
    with uow_factory() as uow:
        uow.data.bind_published_override_child(
            override_id=str(override_id),
            child_version_id=str(child.version_id),
            report_sha256="a" * 64,
            contribution_scope_sha256="b" * 64,
            at=utc_now(),
        )
        loaded = uow.data.find_recoverable_override_child(str(override_id))
        uow.commit()

    assert loaded.version_id == child.version_id
    assert loaded.status is DatasetVersionStatus.PUBLISHED


def test_create_override_requires_verified_report_scope(
    uow_factory: DataSqliteUnitOfWorkFactory,
) -> None:
    from quantking.data.override.service import CreateOverrideRequest, SourceOverrideService

    request = CreateOverrideRequest(
        difference_report_id=new_entity_id("data_diff"),
        primary_batch_id=new_entity_id("data_batch"),
        fallback_batch_id=new_entity_id("data_batch"),
        primary_source="XTQUANT",
        fallback_source="BAOSTOCK",
        scope_codes=("000002.SZ",),
        scope_dates=(TradingDate(date(2026, 7, 15)),),
        fields=("close",),
        reason="scope must exist in verified report units",
    )
    service = SourceOverrideService(uow_factory=uow_factory, difference_report_verifier=object())

    with pytest.raises(Exception, match="verified report"):
        service.create_override(request=request, created_at=utc_now())


def test_apply_failure_keeps_override_approved_and_marks_child_failed(
    uow_factory: DataSqliteUnitOfWorkFactory,
) -> None:
    override_id = new_entity_id("data_override")

    with uow_factory() as uow:
        state = uow.data.record_override_application_failure(
            override_id=str(override_id),
            failure_stage="publish",
            message="E:\\QuantKingV3Data\\secret provider token=abc123 failed",
            at=utc_now(),
        )
        uow.commit()

    assert state.override_status is SourceOverrideStatus.APPROVED
    assert state.child_status is DatasetVersionStatus.FAILED
    assert "E:" not in state.failure_message
    assert "token=" not in state.failure_message
