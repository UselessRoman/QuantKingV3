"""S1-04-R4 repository rejection tests for forged application state."""

from __future__ import annotations

from pathlib import Path

import pytest

from quantking.common.errors import InvalidIdError
from quantking.common.ids import new_entity_id
from quantking.common.time import utc_now
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.jobs.adapters.persistence.sqlite import initialize_database


@pytest.fixture()
def uow_factory(tmp_path: Path) -> DataSqliteUnitOfWorkFactory:
    database = tmp_path / "data.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    yield DataSqliteUnitOfWorkFactory(engine)
    engine.dispose()


def test_claim_rejects_forged_override_and_child_ids(
    uow_factory: DataSqliteUnitOfWorkFactory,
) -> None:
    with uow_factory() as uow, pytest.raises(InvalidIdError):
        uow.data.claim_override_application(
            override_id=str(new_entity_id("data_override")),
            child_version_id=str(new_entity_id("data_version")),
            input_fingerprint="a" * 64,
            actor="attacker",
            at=utc_now(),
        )


def test_failure_recording_rejects_forged_application_ids(
    uow_factory: DataSqliteUnitOfWorkFactory,
) -> None:
    with uow_factory() as uow, pytest.raises(InvalidIdError):
        uow.data.record_override_application_failure(
            override_id=str(new_entity_id("data_override")),
            attempt_id=str(new_entity_id("data_override_attempt")),
            child_version_id=str(new_entity_id("data_version")),
            failure_stage="publish",
            message="forged failure",
            at=utc_now(),
        )
