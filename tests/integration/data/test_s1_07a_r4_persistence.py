"""S1-07A-R4 caller-key bypass and locked TOCTOU attacks."""

from __future__ import annotations

import inspect
import sqlite3
from dataclasses import replace
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
from quantking.data.validation.authorization import (
    FakeRunnerTrustStore,
    SignerTrustPolicy,
)
from quantking.data.validation.testing import (
    build_deep_fake_assessment,
    verified_synthetic_assessment,
)
from quantking.jobs.adapters.persistence.sqlite import initialize_database


def _directory(root: Path, evidence: object) -> Path:
    return (root / evidence.evidence_relative_path).parent  # type: ignore[attr-defined]


def _adapter(*, root: Path, database: Path):
    from quantking.data.adapters.persistence.assessment import (
        SqliteSourceAssessmentPersistenceAdapter,
    )

    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    return (
        SqliteSourceAssessmentPersistenceAdapter(
            engine=engine,
            validation_root=root,
            signer=FakeRunnerTrustStore(),
            signer_trust_policy=SignerTrustPolicy.synthetic_only(),
        ),
        engine,
    )


def _manual_evidence() -> object:
    assessment = SourceCandidateAssessment(
        assessment_id=new_entity_id("data_assessment"),
        source="XTQUANT",
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        evidence_kind=EvidenceKind.SYNTHETIC,
        semantic_contract_verified=False,
        schema_unit_verified=False,
        coverage_adequate=False,
        quality_passed=False,
        fixed_sample_differences_adequate=False,
        stability_runs=0,
        status=AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION,
        blocking_reason="manual",
        assessment_config_version="validation.v1",
        sample_spec_version="s1-07.fixed-sample.v2",
        rule_version="quality-rules.v1",
        evidence_relative_path="missing/_assessment",
        evidence_sha256="a" * 64,
        created_at=utc_now(),
    )
    return verified_synthetic_assessment(assessment)


def test_general_uow_factory_has_no_assessment_key_parameter() -> None:
    parameters = inspect.signature(DataSqliteUnitOfWorkFactory.__call__).parameters
    assert "assessment_persistence_key" not in parameters


def test_caller_chosen_key_private_dto_receipt_and_repository_cannot_write(
    tmp_path: Path,
) -> None:
    database = tmp_path / "caller-key.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    key = b"x" * 32
    try:
        with (
            pytest.raises(TypeError),
            factory(assessment_persistence_key=key) as uow,  # type: ignore[call-arg]
        ):
            assert not hasattr(uow.data, "_persist_verified_source_assessment")
    finally:
        engine.dispose()


def test_no_directory_means_no_application_write_path(tmp_path: Path) -> None:
    adapter, engine = _adapter(root=tmp_path / "validation", database=tmp_path / "missing.db")
    try:
        with pytest.raises(Exception, match=r"directory|missing|managed"):
            adapter.persist_from_directory(tmp_path / "validation" / "missing")
    finally:
        engine.dispose()


@pytest.mark.parametrize("evidence_kind", (EvidenceKind.SYNTHETIC, EvidenceKind.REAL))
def test_manual_evidence_cannot_be_supplied_to_public_adapter(
    tmp_path: Path, evidence_kind: EvidenceKind
) -> None:
    adapter, engine = _adapter(
        root=tmp_path / "validation", database=tmp_path / f"{evidence_kind.value}.db"
    )
    evidence = _manual_evidence()
    object.__setattr__(
        evidence,
        "assessment",
        replace(evidence.assessment, evidence_kind=evidence_kind),
    )
    try:
        with pytest.raises((TypeError, ValueError)):
            adapter.persist_from_directory(evidence)  # type: ignore[arg-type]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("stage", "target"),
    (
        ("second_verify", "manifest"),
        ("before_flush", "attestation"),
        ("before_commit", "canonical"),
    ),
)
def test_assessment_files_are_locked_through_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    target: str,
) -> None:
    root = tmp_path / "validation"
    evidence = build_deep_fake_assessment(root=root)
    directory = _directory(root, evidence)
    adapter, engine = _adapter(root=root, database=tmp_path / f"{stage}.db")
    path = {
        "manifest": directory / "_assessment" / "manifest.json",
        "attestation": next(directory.glob("*/runner/result-attestation.json")),
        "canonical": next(directory.glob("*/canonical/content.parquet")),
    }[target]
    blocked: list[bool] = []

    def attempt_replace() -> None:
        try:
            path.write_bytes(path.read_bytes() + b" ")
        except OSError:
            blocked.append(True)
        else:
            blocked.append(False)

    if stage == "second_verify":
        original = adapter._verify_final

        def attack_verify(*args: object, **kwargs: object):
            result = original(*args, **kwargs)
            attempt_replace()
            return result

        monkeypatch.setattr(adapter, "_verify_final", attack_verify)
    elif stage == "before_flush":
        original = adapter._insert_assessment

        def attack_insert(*args: object, **kwargs: object):
            attempt_replace()
            return original(*args, **kwargs)

        monkeypatch.setattr(adapter, "_insert_assessment", attack_insert)
    else:
        original = adapter._commit

        def attack_commit(*args: object, **kwargs: object):
            attempt_replace()
            return original(*args, **kwargs)

        monkeypatch.setattr(adapter, "_commit", attack_commit)
    try:
        saved = adapter.persist_from_directory(directory)
        assert saved.evidence_kind is EvidenceKind.SYNTHETIC
        assert saved.status is AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION
        assert blocked == [True]
    finally:
        engine.dispose()


def test_legal_deep_synthetic_blocked_directory_persists(tmp_path: Path) -> None:
    root = tmp_path / "validation"
    evidence = build_deep_fake_assessment(root=root)
    adapter, engine = _adapter(root=root, database=tmp_path / "valid.db")
    try:
        saved = adapter.persist_from_directory(_directory(root, evidence))
        assert saved == evidence.assessment
    finally:
        engine.dispose()


def test_direct_sql_real_insert_is_rejected_by_database(tmp_path: Path) -> None:
    database = tmp_path / "direct-real.db"
    initialize_database(database)
    with sqlite3.connect(database) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO source_candidate_assessments "
            "(assessment_id,source,dataset,evidence_kind,semantic_contract_verified,"
            "schema_unit_verified,coverage_adequate,quality_passed,"
            "fixed_sample_differences_adequate,stability_runs,status,blocking_reason,"
            "assessment_config_version,sample_spec_version,rule_version,"
            "evidence_relative_path,evidence_sha256,signer_profiles_json,"
            "result_attestation_sha256s_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "data_assessment:11111111-1111-4111-8111-111111111111",
                "XTQUANT",
                "exchange_calendar",
                "REAL",
                1,
                1,
                1,
                1,
                1,
                3,
                "PRODUCTION_READY",
                None,
                "validation.v1",
                "s1-07.fixed-sample.v2",
                "quality-rules.v1",
                "source-assessments/x/_assessment",
                "a" * 64,
                '["REAL"]',
                '["' + "b" * 64 + '"]',
                "2026-07-29T00:00:00Z",
            ),
        )
