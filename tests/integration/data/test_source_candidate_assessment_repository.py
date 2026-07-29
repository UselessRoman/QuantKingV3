"""Durable source-candidate assessment evidence tests (S1-04-R1)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from quantking.common.errors import InvalidDataTransitionError
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
from quantking.data.validation.samples import SampleEligibility
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
        sample_spec_version="s1-07.fixed-sample.v1",
        rule_version="comparison-thresholds.v1",
        evidence_relative_path="assessments/baostock/daily.json",
        evidence_sha256="a" * 64,
        created_at=utc_now(),
    )
    try:
        verified = verified_synthetic_assessment(assessment)
        with factory() as uow:
            uow.data.save_source_candidate_assessment(verified)
            uow.commit()
        with factory() as uow:
            assert (
                uow.data.get_source_candidate_assessment(str(assessment.assessment_id))
                == verified.assessment
            )
            assert uow.data.list_source_candidate_assessments(
                source="BAOSTOCK", dataset=DatasetKind.DAILY_BAR_UNADJUSTED
            ) == [verified.assessment]
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
        sample_spec_version="s1-07.fixed-sample.v1",
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


def test_repository_rederives_status_and_rejects_forged_production_evidence(
    tmp_path: Path,
) -> None:
    from quantking.data.validation.testing import build_deep_fake_assessment

    validation_root = tmp_path / "validation"
    verified = build_deep_fake_assessment(root=validation_root)
    forged = SourceCandidateAssessment(
        assessment_id=verified.assessment.assessment_id,
        source=verified.assessment.source,
        dataset=verified.assessment.dataset,
        evidence_kind=EvidenceKind.REAL,
        semantic_contract_verified=True,
        schema_unit_verified=True,
        coverage_adequate=True,
        quality_passed=True,
        fixed_sample_differences_adequate=True,
        stability_runs=3,
        status=AssessmentStatus.PRODUCTION_READY,
        blocking_reason=None,
        assessment_config_version=verified.assessment.assessment_config_version,
        sample_spec_version=verified.assessment.sample_spec_version,
        rule_version=verified.assessment.rule_version,
        evidence_relative_path=verified.evidence_relative_path,
        evidence_sha256=verified.evidence_manifest_sha256,
        created_at=verified.assessment.created_at,
    )
    object.__setattr__(verified, "assessment", forged)
    object.__setattr__(verified, "sample_eligibility", SampleEligibility.PRODUCTION_ELIGIBLE)

    database = tmp_path / "forged-assessment.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    try:
        with (
            factory() as uow,
            pytest.raises(InvalidDataTransitionError, match="controlled fixed sample"),
        ):
            uow.data.save_source_candidate_assessment(verified)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "mutation",
    (
        {"coverage_adequate": True},
        {"stability_runs": 3},
        {"blocking_reason": "caller supplied blocked reason"},
    ),
)
def test_repository_rejects_blocked_assessment_drift(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
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
        blocking_reason="fixture input",
        assessment_config_version="assessment.v1",
        sample_spec_version="s1-07.fixed-sample.v2",
        rule_version="quality-rules.v1",
        evidence_relative_path="assessments/blocked.json",
        evidence_sha256="b" * 64,
        created_at=utc_now(),
    )
    verified = verified_synthetic_assessment(assessment)
    object.__setattr__(verified, "assessment", replace(verified.assessment, **mutation))
    database = tmp_path / "blocked-drift.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    try:
        with (
            factory() as uow,
            pytest.raises(InvalidDataTransitionError, match="deterministic result"),
        ):
            uow.data.save_source_candidate_assessment(verified)
    finally:
        engine.dispose()
