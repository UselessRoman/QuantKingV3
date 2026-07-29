"""S1-07A-R3 attacks on assessment persistence provenance."""

from __future__ import annotations

import json
import os
import subprocess
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
from quantking.data.validation.authorization import (
    FakeRunnerTrustStore,
    SignerTrustPolicy,
)
from quantking.data.validation.models import VerifiedAssessmentEvidence
from quantking.data.validation.testing import (
    build_deep_fake_assessment,
    verified_synthetic_assessment,
)
from quantking.jobs.adapters.persistence.sqlite import initialize_database


def _service(*, root: Path, database: Path):
    from quantking.data.validation.persistence import VerifiedSourceAssessmentService

    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    return (
        VerifiedSourceAssessmentService(
            validation_root=root,
            signer=FakeRunnerTrustStore(),
            signer_trust_policy=SignerTrustPolicy.synthetic_only(),
            uow_factory=DataSqliteUnitOfWorkFactory(engine),
        ),
        engine,
    )


def _assessment_directory(root: Path, verified: VerifiedAssessmentEvidence) -> Path:
    return (root / verified.evidence_relative_path).parent


def _manual_evidence() -> object:
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
        blocking_reason="manual fixture",
        assessment_config_version="validation.v1",
        sample_spec_version="s1-07.fixed-sample.v2",
        rule_version="quality-rules.v1",
        evidence_relative_path="missing/_assessment",
        evidence_sha256="a" * 64,
        created_at=utc_now(),
    )
    return verified_synthetic_assessment(assessment)


def test_repository_rejects_handmade_verified_evidence_without_directory(
    tmp_path: Path,
) -> None:
    database = tmp_path / "manual.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    try:
        with (
            factory() as uow,
            pytest.raises((TypeError, AttributeError), match=r"application service|save_source"),
        ):
            uow.data.save_source_candidate_assessment(_manual_evidence())
    finally:
        engine.dispose()


def test_imported_private_seal_real_run_cannot_enter_repository(tmp_path: Path) -> None:
    from quantking.data.validation.persistence import _AssessmentPersistenceRecord

    evidence = _manual_evidence()
    run = evidence.verified_runs[0]
    object.__setattr__(run, "evidence_kind", EvidenceKind.REAL)
    object.__setattr__(
        evidence,
        "assessment",
        replace(evidence.assessment, evidence_kind=EvidenceKind.REAL),
    )
    database = tmp_path / "forged-real.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    forged_record = _AssessmentPersistenceRecord(
        evidence=evidence,
        signer_profiles=("REAL",),
        result_attestation_sha256s=("b" * 64,),
        receipt_sha256="c" * 64,
    )
    try:
        with (
            factory() as uow,
            pytest.raises(
                InvalidDataTransitionError,
                match="directory-verification receipt",
            ),
        ):
            uow.data._persist_verified_source_assessment(forged_record)
    finally:
        engine.dispose()


@pytest.mark.parametrize("attack", ("manifest", "complete", "run_sha"))
def test_service_rejects_assessment_file_tampering(tmp_path: Path, attack: str) -> None:
    root = tmp_path / "validation"
    verified = build_deep_fake_assessment(root=root)
    directory = _assessment_directory(root, verified)
    if attack == "manifest":
        (directory / "_assessment" / "manifest.json").write_bytes(b"{}")
    elif attack == "complete":
        (directory / "_assessment" / "COMPLETE").write_bytes(b"{}")
    else:
        run_manifest = next(
            path for path in directory.glob("*/manifest.json") if path.parent.name != "_assessment"
        )
        run_manifest.write_bytes(run_manifest.read_bytes() + b" ")
    service, engine = _service(root=root, database=tmp_path / f"{attack}.db")
    try:
        with pytest.raises(Exception, match=r"manifest|verification|evidence|SHA|canonical"):
            service.persist(directory)
    finally:
        engine.dispose()


def test_service_rejects_missing_and_escaping_assessment_paths(tmp_path: Path) -> None:
    root = tmp_path / "validation"
    service, engine = _service(root=root, database=tmp_path / "paths.db")
    try:
        for directory in (root / "missing", tmp_path / "outside"):
            with pytest.raises(Exception, match=r"managed|outside|missing|directory"):
                service.persist(directory)
    finally:
        engine.dispose()


def test_service_rejects_reparse_assessment_directory(tmp_path: Path) -> None:
    root = tmp_path / "validation"
    target_root = tmp_path / "target-validation"
    verified = build_deep_fake_assessment(root=target_root)
    target = _assessment_directory(target_root, verified)
    root.mkdir()
    link = root / "linked-assessment"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        if created.returncode != 0:
            pytest.skip("directory reparse creation is unavailable")
    service, engine = _service(root=root, database=tmp_path / "reparse.db")
    try:
        with pytest.raises(Exception, match=r"reparse|symlink|unsafe|managed"):
            service.persist(link)
    finally:
        engine.dispose()


@pytest.mark.parametrize("target", ("canonical", "attestation"))
def test_replacement_between_first_verify_and_persist_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    root = tmp_path / "validation"
    verified = build_deep_fake_assessment(root=root)
    directory = _assessment_directory(root, verified)
    service, engine = _service(root=root, database=tmp_path / "replacement.db")
    original = service._verifier.verify_assessment
    calls = 0

    def replace_after_first(*, assessment_directory: Path):
        nonlocal calls
        result = original(assessment_directory=assessment_directory)
        calls += 1
        if calls == 1:
            path = next(
                assessment_directory.glob(
                    "*/canonical/content.parquet"
                    if target == "canonical"
                    else "*/runner/result-attestation.json"
                )
            )
            path.write_bytes(path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(service._verifier, "verify_assessment", replace_after_first)
    try:
        with pytest.raises(
            Exception,
            match=r"evidence|manifest|verification|mismatch|attestation|canonical",
        ):
            service.persist(directory)
    finally:
        engine.dispose()


def test_service_rejects_first_pass_run_fingerprint_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "validation"
    verified = build_deep_fake_assessment(root=root)
    directory = _assessment_directory(root, verified)
    service, engine = _service(root=root, database=tmp_path / "fingerprint.db")
    original = service._verifier.verify_assessment
    calls = 0

    def forge_first(*, assessment_directory: Path):
        nonlocal calls
        result = original(assessment_directory=assessment_directory)
        calls += 1
        if calls == 1:
            object.__setattr__(result.verified_runs[0], "run_fingerprint", "f" * 64)
        return result

    monkeypatch.setattr(service._verifier, "verify_assessment", forge_first)
    try:
        with pytest.raises(InvalidDataTransitionError, match="changed between verification"):
            service.persist(directory)
    finally:
        engine.dispose()


def test_service_rejects_first_pass_signer_summary_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "validation"
    verified = build_deep_fake_assessment(root=root)
    directory = _assessment_directory(root, verified)
    service, engine = _service(root=root, database=tmp_path / "signer.db")
    original = service._attestation_summary
    calls = 0

    def forge_first(
        assessment_directory: Path, evidence: VerifiedAssessmentEvidence
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        nonlocal calls
        result = original(assessment_directory, evidence)
        calls += 1
        return (("FORGED",), result[1]) if calls == 1 else result

    monkeypatch.setattr(service, "_attestation_summary", forge_first)
    try:
        with pytest.raises(InvalidDataTransitionError, match="changed between verification"):
            service.persist(directory)
    finally:
        engine.dispose()


@pytest.mark.parametrize("field", ("coverage_adequate", "stability_runs", "blocking_reason"))
def test_service_rejects_first_pass_assessment_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    root = tmp_path / "validation"
    verified = build_deep_fake_assessment(root=root)
    directory = _assessment_directory(root, verified)
    service, engine = _service(root=root, database=tmp_path / f"{field}.db")
    original = service._verifier.verify_assessment
    calls = 0

    def forge_first(*, assessment_directory: Path):
        nonlocal calls
        result = original(assessment_directory=assessment_directory)
        calls += 1
        if calls == 1:
            value: object = {
                "coverage_adequate": not result.assessment.coverage_adequate,
                "stability_runs": 99,
                "blocking_reason": "forged",
            }[field]
            object.__setattr__(result, "assessment", replace(result.assessment, **{field: value}))
        return result

    monkeypatch.setattr(service._verifier, "verify_assessment", forge_first)
    try:
        with pytest.raises(InvalidDataTransitionError, match="changed between verification"):
            service.persist(directory)
    finally:
        engine.dispose()


def test_deep_verified_synthetic_blocked_assessment_is_persisted(tmp_path: Path) -> None:
    root = tmp_path / "validation"
    verified = build_deep_fake_assessment(root=root)
    directory = _assessment_directory(root, verified)
    service, engine = _service(root=root, database=tmp_path / "valid.db")
    try:
        saved = service.persist(directory)
        assert saved == verified.assessment
        assert saved.evidence_kind is EvidenceKind.SYNTHETIC
        assert saved.status is AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION
        with engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT signer_profiles_json, result_attestation_sha256s_json "
                "FROM source_candidate_assessments"
            ).one()
        assert json.loads(row[0]) == ["SYNTHETIC"]
        assert len(json.loads(row[1])) == 3
    finally:
        engine.dispose()
