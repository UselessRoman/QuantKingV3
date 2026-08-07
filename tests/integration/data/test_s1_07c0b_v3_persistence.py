"""S1-07C0B v3 sealed persistence and DataCenter fail-closed coverage."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace
from pathlib import Path

import pytest

from quantking.common.errors import InvalidDataTransitionError
from quantking.data.adapters.persistence.assessment import (
    SealedAssessmentReadVerifier,
    SqliteV3SourceAssessmentPersistenceAdapter,
)
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.center import DataCenterQueryService, DataCenterReadIntegrityError
from quantking.data.validation.authorization import FakeRunnerTrustStore, SignerTrustPolicy
from quantking.data.validation.contracts_v3 import (
    AttemptOutcomeV3,
    ResultAttestationV3,
    canonical_json,
)
from quantking.data.validation.testing_v3 import build_v3_context, build_v3_run
from quantking.jobs.adapters.persistence.sqlite import initialize_database


class _IsolatedRealSigner:
    """Temporary test-only signer; this key never appears in production code."""

    _KEY = b"test-only-s1-07c0b-isolated-real-signer"
    key_id = "test.isolated-real-signer.v1"

    def sign(self, attestation: ResultAttestationV3) -> ResultAttestationV3:
        assert attestation.signer_key_id == self.key_id
        signature = hmac.new(
            self._KEY,
            canonical_json(attestation.unsigned_payload()),
            hashlib.sha256,
        ).hexdigest()
        return replace(attestation, signature=signature)

    def verify(self, attestation: ResultAttestationV3) -> bool:
        return hmac.compare_digest(
            hmac.new(
                self._KEY,
                canonical_json(attestation.unsigned_payload()),
                hashlib.sha256,
            ).hexdigest(),
            attestation.signature,
        )


class _IsolatedRealPolicy:
    def evidence_kind_for(self, *, attestation_key_id: str, verifier_key_id: str):
        from quantking.data.assessment.models import EvidenceKind

        if (
            attestation_key_id != _IsolatedRealSigner.key_id
            or verifier_key_id != _IsolatedRealSigner.key_id
        ):
            raise ValueError("isolated test signer mismatch")
        return EvidenceKind.REAL


class _UnusedPort:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"unexpected port call: {name}")


class _NoJobs:
    def list_jobs_page(self, **filters: object) -> list[object]:
        del filters
        return []

    def count_jobs(self, **filters: object) -> int:
        del filters
        return 0


def _sealed_v3(tmp_path: Path):
    root = tmp_path / "validation"
    context = build_v3_context()
    signer = _IsolatedRealSigner()
    policy = _IsolatedRealPolicy()
    first = build_v3_run(
        root=root,
        run_index=1,
        selected_case_ids=("calendar_bj", "calendar_sh"),
        context=context,
        signer_override=signer,
        runner_trust_policy=policy,
    )
    build_v3_run(
        root=root,
        run_index=2,
        selected_case_ids=("calendar_bj", "calendar_sh"),
        context=context,
        signer_override=signer,
        runner_trust_policy=policy,
    )
    build_v3_run(
        root=root,
        run_index=3,
        selected_case_ids=("calendar_bj", "calendar_sh"),
        context=context,
        signer_override=signer,
        runner_trust_policy=policy,
    )
    first.verifier.verify_run(directory=first.directory)
    evidence = first.verifier.context
    from quantking.data.validation.artifacts_v3 import ValidationEvidenceWriterV3

    finalized = ValidationEvidenceWriterV3(validation_root=root).finalize_assessment(
        assessment_directory=first.directory.parent,
        verifier=first.verifier,
    )
    assert finalized.assessment.status.value == "PRODUCTION_READY"
    return root, first.directory.parent, evidence, signer, policy, finalized


def _reader(*, root: Path, context: object, signer: object, policy: object):
    return SealedAssessmentReadVerifier(
        validation_root=root,
        signer=FakeRunnerTrustStore(),
        signer_trust_policy=SignerTrustPolicy.synthetic_only(),
        v3_context=context,  # type: ignore[arg-type]
        v3_signer=signer,  # type: ignore[arg-type]
        v3_runner_trust_policy=policy,  # type: ignore[arg-type]
    )


def test_v3_persistence_derives_isolated_real_ready_only_from_sealed_evidence(
    tmp_path: Path,
) -> None:
    root, directory, context, signer, policy, finalized = _sealed_v3(tmp_path)
    database = tmp_path / "metadata.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=500)
    try:
        adapter = SqliteV3SourceAssessmentPersistenceAdapter(
            engine=engine,
            validation_root=root,
            context=context,
            signer=signer,
            runner_trust_policy=policy,
        )
        saved = adapter.persist_from_directory(directory)
        assert saved == finalized.assessment
        assert saved.evidence_kind.value == "REAL"
        assert saved.status.value == "PRODUCTION_READY"

        factory = DataSqliteUnitOfWorkFactory(
            engine,
            assessment_read_verifier=_reader(
                root=root, context=context, signer=signer, policy=policy
            ),
        )
        with factory() as uow:
            assert uow.data.get_source_candidate_assessment(str(saved.assessment_id)) == saved

        # A UoW without the matching v3 verifier refuses the same database row.
        unconfigured = DataSqliteUnitOfWorkFactory(
            engine,
            assessment_read_verifier=SealedAssessmentReadVerifier(
                validation_root=root,
                signer=FakeRunnerTrustStore(),
                signer_trust_policy=SignerTrustPolicy.synthetic_only(),
            ),
        )
        with unconfigured() as uow, pytest.raises(InvalidDataTransitionError):
            uow.data.get_source_candidate_assessment(str(saved.assessment_id))
    finally:
        engine.dispose()


def test_datacenter_fails_closed_when_v3_seal_is_tampered(tmp_path: Path) -> None:
    root, directory, context, signer, policy, _finalized = _sealed_v3(tmp_path)
    database = tmp_path / "metadata.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=500)
    try:
        adapter = SqliteV3SourceAssessmentPersistenceAdapter(
            engine=engine,
            validation_root=root,
            context=context,
            signer=signer,
            runner_trust_policy=policy,
        )
        saved = adapter.persist_from_directory(directory)
        factory = DataSqliteUnitOfWorkFactory(
            engine,
            assessment_read_verifier=_reader(
                root=root, context=context, signer=signer, policy=policy
            ),
        )
        with engine.connect() as connection:
            relative = connection.exec_driver_sql(
                "SELECT sealed_evidence_relative_path FROM source_candidate_assessments"
            ).scalar_one()
        seal = root / str(relative)
        seal.write_bytes(b"tampered")
        with factory() as uow, pytest.raises(InvalidDataTransitionError):
            uow.data.get_source_candidate_assessment(str(saved.assessment_id))
        center = DataCenterQueryService(
            data_uow_factory=factory,
            preview=_UnusedPort(),
            difference_verifier=_UnusedPort(),
            jobs=_NoJobs(),
        )
        with pytest.raises(DataCenterReadIntegrityError, match="assessment evidence"):
            center.sources()
    finally:
        engine.dispose()


def test_isolated_real_failure_persists_only_as_blocked_without_success_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "validation"
    context = build_v3_context()
    signer = _IsolatedRealSigner()
    policy = _IsolatedRealPolicy()
    failed = build_v3_run(
        root=root,
        run_index=1,
        outcome=AttemptOutcomeV3.FAILED,
        selected_case_ids=("calendar_sh",),
        context=context,
        signer_override=signer,
        runner_trust_policy=policy,
    )
    from quantking.data.validation.artifacts_v3 import ValidationEvidenceWriterV3

    finalized = ValidationEvidenceWriterV3(validation_root=root).finalize_assessment(
        assessment_directory=failed.directory.parent,
        verifier=failed.verifier,
    )
    assert finalized.assessment.evidence_kind.value == "REAL"
    assert finalized.assessment.status.value == "BLOCKED_EXTERNAL_VALIDATION"
    assert not (failed.directory / "canonical").exists()
    database = tmp_path / "metadata.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=500)
    try:
        saved = SqliteV3SourceAssessmentPersistenceAdapter(
            engine=engine,
            validation_root=root,
            context=context,
            signer=signer,
            runner_trust_policy=policy,
        ).persist_from_directory(failed.directory.parent)
        assert saved == finalized.assessment
        assert saved.status.value == "BLOCKED_EXTERNAL_VALIDATION"
    finally:
        engine.dispose()
