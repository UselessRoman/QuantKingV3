"""Directory-authoritative source-candidate assessment repository tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.models import DatasetKind
from quantking.data.validation.authorization import (
    FakeRunnerTrustStore,
    SignerTrustPolicy,
)
from quantking.data.validation.persistence import VerifiedSourceAssessmentService
from quantking.data.validation.testing import build_deep_fake_assessment
from quantking.jobs.adapters.persistence.sqlite import initialize_database


def _assessment_directory(root: Path, relative: str) -> Path:
    return (root / relative).parent


def test_source_candidate_assessment_round_trip_and_dataset_filter(tmp_path: Path) -> None:
    root = tmp_path / "validation"
    verified = build_deep_fake_assessment(root=root)
    database = tmp_path / "assessment.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    service = VerifiedSourceAssessmentService(
        validation_root=root,
        signer=FakeRunnerTrustStore(),
        signer_trust_policy=SignerTrustPolicy.synthetic_only(),
        uow_factory=factory,
    )
    try:
        saved = service.persist(_assessment_directory(root, verified.evidence_relative_path))
        with factory() as uow:
            assert uow.data.get_source_candidate_assessment(str(saved.assessment_id)) == saved
            assert uow.data.list_source_candidate_assessments(
                source="XTQUANT", dataset=DatasetKind.EXCHANGE_CALENDAR
            ) == [saved]
            assert (
                uow.data.list_source_candidate_assessments(dataset=DatasetKind.DAILY_BAR_UNADJUSTED)
                == []
            )
    finally:
        engine.dispose()


def test_repository_has_no_public_verified_evidence_save_entry(tmp_path: Path) -> None:
    database = tmp_path / "boundary.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    try:
        with factory() as uow:
            assert not hasattr(uow.data, "save_source_candidate_assessment")
            with pytest.raises(AttributeError):
                _missing = uow.data.save_source_candidate_assessment  # type: ignore[attr-defined]
    finally:
        engine.dispose()


def test_application_service_persistence_is_immutable_for_same_assessment_id(
    tmp_path: Path,
) -> None:
    root = tmp_path / "validation"
    verified = build_deep_fake_assessment(root=root)
    database = tmp_path / "immutable.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    service = VerifiedSourceAssessmentService(
        validation_root=root,
        signer=FakeRunnerTrustStore(),
        signer_trust_policy=SignerTrustPolicy.synthetic_only(),
        uow_factory=factory,
    )
    directory = _assessment_directory(root, verified.evidence_relative_path)
    try:
        first = service.persist(directory)
        second = service.persist(directory)
        assert second == first
    finally:
        engine.dispose()
