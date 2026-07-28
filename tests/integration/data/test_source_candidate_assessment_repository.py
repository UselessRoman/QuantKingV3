"""Durable source-candidate assessment evidence tests (S1-04-R1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import utc_now
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.assessment.models import (
    AssessmentStatus,
    EvidenceKind,
    SourceCandidateAssessment,
)
from quantking.data.models import DatasetKind
from quantking.data.validation.testing import verified_synthetic_assessment
from quantking.jobs.adapters.persistence.sqlite import initialize_database


def test_source_candidate_assessment_round_trip_and_dataset_filter(tmp_path: Path) -> None:
    database = tmp_path / "assessment.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    assessment = SourceCandidateAssessment(
        assessment_id=new_entity_id("data_assessment"),
        source="BAOSTOCK",
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        evidence_kind=EvidenceKind.SYNTHETIC,
        semantic_contract_verified=True,
        schema_unit_verified=True,
        coverage_adequate=True,
        quality_passed=True,
        fixed_sample_differences_adequate=True,
        stability_runs=3,
        status=AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION,
        blocking_reason="real external validation is required",
        assessment_config_version="assessment.v1",
        sample_spec_version="sample.v1",
        rule_version="comparison-thresholds.v1",
        evidence_relative_path="assessments/baostock/daily.json",
        evidence_sha256="a" * 64,
        created_at=utc_now(),
    )
    try:
        with factory() as uow:
            uow.data.save_source_candidate_assessment(verified_synthetic_assessment(assessment))
            uow.commit()
        with factory() as uow:
            assert (
                uow.data.get_source_candidate_assessment(str(assessment.assessment_id))
                == assessment
            )
            assert uow.data.list_source_candidate_assessments(
                source="BAOSTOCK", dataset=DatasetKind.DAILY_BAR_UNADJUSTED
            ) == [assessment]
            assert (
                uow.data.list_source_candidate_assessments(dataset=DatasetKind.EXCHANGE_CALENDAR)
                == []
            )
    finally:
        engine.dispose()


def test_repository_rejects_unverified_assessment(tmp_path: Path) -> None:
    database = tmp_path / "unverified-assessment.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    assessment = SourceCandidateAssessment(
        assessment_id=new_entity_id("data_assessment"),
        source="BAOSTOCK",
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        evidence_kind=EvidenceKind.SYNTHETIC,
        semantic_contract_verified=False,
        schema_unit_verified=False,
        coverage_adequate=False,
        quality_passed=False,
        fixed_sample_differences_adequate=False,
        stability_runs=0,
        status=AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION,
        blocking_reason="verified evidence required",
        assessment_config_version="assessment.v1",
        sample_spec_version="sample.v1",
        rule_version="quality-rules.v1",
        evidence_relative_path="assessments/unverified.json",
        evidence_sha256="f" * 64,
        created_at=utc_now(),
    )
    try:
        with factory() as uow, pytest.raises(TypeError, match="VerifiedAssessmentEvidence"):
            uow.data.save_source_candidate_assessment(assessment)  # type: ignore[arg-type]
    finally:
        engine.dispose()
