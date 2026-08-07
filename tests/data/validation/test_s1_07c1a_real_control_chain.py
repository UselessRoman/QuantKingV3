"""S1-07C1A offline REAL authorization, runner, seal, and read-side tests."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantking.common.config import AppSettings
from quantking.common.errors import InvalidDataTransitionError
from quantking.common.ids import new_entity_id
from quantking.common.paths import RuntimePaths
from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.adapters.persistence.assessment import (
    SealedAssessmentReadVerifier,
    SqliteV3SourceAssessmentPersistenceAdapter,
)
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.adapters.sources.xtquant_metadata import ExplicitXtQuantPackageMetadataProbe
from quantking.data.adapters.sources.xtquant_real_validation import XtQuantValidationAdapterResult
from quantking.data.assessment.models import AssessmentStatus, EvidenceKind
from quantking.data.center import DataCenterQueryService
from quantking.data.models import DatasetKind
from quantking.data.validation.artifacts_v3 import ValidationEvidenceVerifierV3
from quantking.data.validation.authorization import (
    AuthorizationClaimStore,
    FakeRunnerTrustStore,
    SignerTrustPolicy,
)
from quantking.data.validation.contracts_v3 import AttemptOutcomeV3
from quantking.data.validation.real_authorization import (
    LocalRealV3Signer,
    RealAuthorizationError,
    RealExecutionAuthorizationAuthority,
    RealRuntimeAuthorizationAuthority,
    RealRuntimeAuthorizationFileLoader,
)
from quantking.data.validation.real_runtime import (
    ExplicitRealXtQuantTrustResolver,
    RealXtQuantAssessmentService,
    RealXtQuantExecutionError,
    RealXtQuantValidationRuntime,
    build_real_xtquant_context,
    build_real_xtquant_plan,
)
from quantking.data.validation.runners.xtquant_real import XtQuantRealProviderRunner
from quantking.jobs.adapters.persistence.sqlite import initialize_database
from quantking.runtime import build_eod_runtime


def _write_verified_authority_bundle(root: Path) -> Path:
    from scripts.build_s1_07b_reference_bundle import (
        EXPECTED_P2B_MANIFEST_SHA,
        EXPECTED_P2F_MANIFEST_SHA,
        EXPECTED_PDF_SHA,
        _build_draft,
    )

    from quantking.data.validation.authority_bundle import AuthorityReferenceBundleWriter
    from quantking.data.validation.samples import load_fixed_sample_v4

    return AuthorityReferenceBundleWriter(root=root).write(
        _build_draft(EXPECTED_PDF_SHA, EXPECTED_P2B_MANIFEST_SHA, EXPECTED_P2F_MANIFEST_SHA),
        sample=load_fixed_sample_v4(),
    )


def _write_metadata(root: Path, version: str = "1.2.3") -> Path:
    metadata = root / f"xtquant-{version}.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(f"Name: xtquant\nVersion: {version}\n", encoding="utf-8")
    return root


def _keys(root: Path) -> tuple[Path, Path]:
    authorization = root / "keys" / "authorization.key"
    runner = root / "keys" / "runner.key"
    authorization.parent.mkdir(parents=True)
    authorization.write_bytes(b"a" * 48)
    runner.write_bytes(b"r" * 48)
    return authorization, runner


def _identity(authorization_id: object) -> object:
    from quantking.data.validation.contracts_v3 import ValidationAttemptIdentityV3

    return ValidationAttemptIdentityV3(
        assessment_id=new_entity_id("data_assessment"),
        authorization_id=authorization_id,  # type: ignore[arg-type]
        invocation_id=new_entity_id("data_invocation"),
        session_id=new_entity_id("data_session"),
        run_id=new_entity_id("data_validation"),
    )


def _runtime(
    root: Path,
    *,
    output_root: Path | None = None,
    metadata: bool = True,
    runner_preflight: object | None = None,
    trust_key: Path | None = None,
) -> tuple[
    RealXtQuantValidationRuntime,
    RealExecutionAuthorizationAuthority,
    RealRuntimeAuthorizationAuthority,
    ExplicitRealXtQuantTrustResolver,
    Path,
]:
    authority_root = root / "authority"
    _write_verified_authority_bundle(authority_root)
    site_packages = root / "site-packages"
    if metadata:
        _write_metadata(site_packages)
    authorization_key, runner_key = _keys(root)
    trust = ExplicitRealXtQuantTrustResolver(
        reference_bundle_root=authority_root,
        runner_key_id="local.real.runner.v1",
        runner_private_key_file=trust_key or runner_key,
    )
    file_authority = RealRuntimeAuthorizationAuthority(
        key_id="local.real.authorization.v1",
        private_key_file=authorization_key,
    )
    runtime = RealXtQuantValidationRuntime(
        authorization_file_loader=RealRuntimeAuthorizationFileLoader(verifier=file_authority),
        authorization_verifier=RealExecutionAuthorizationAuthority(
            key_id="local.real.authorization.v1",
            private_key_file=authorization_key,
        ),
        claim_store=AuthorizationClaimStore(claim_root=root / "claims"),
        trust_resolver=trust,
        package_metadata_probe=ExplicitXtQuantPackageMetadataProbe(
            site_packages_root=site_packages
        ),
        runner_preflight=runner_preflight,  # type: ignore[arg-type]
    )
    return (
        runtime,
        RealExecutionAuthorizationAuthority(
            key_id="local.real.authorization.v1",
            private_key_file=authorization_key,
        ),
        file_authority,
        trust,
        output_root or (root / "output"),
    )


def _issued(
    root: Path,
    *,
    metadata: bool = True,
    runner_preflight: object | None = None,
    valid_from: UtcTimestamp | None = None,
    expires_at: UtcTimestamp | None = None,
    trust_key: Path | None = None,
) -> tuple[Path, RealXtQuantValidationRuntime, ExplicitRealXtQuantTrustResolver, Path]:
    runtime, execution_authority, file_authority, trust, output = _runtime(
        root,
        metadata=metadata,
        runner_preflight=runner_preflight,
        trust_key=trust_key,
    )
    configuration = trust.resolve() if trust_key is None else None
    context = (
        configuration.context
        if configuration is not None
        else build_real_xtquant_context(reference_bundle_root=root / "authority")
    )
    authorization = execution_authority.issue(
        authorization_id=new_entity_id("data_authorization"),
        source="XTQUANT",
        dataset=DatasetKind.TRADING_STATUS,
        output_root=output,
        sample_sha256=context.sample.sample_spec_sha256,
        valid_from=valid_from,
        expires_at=expires_at or UtcTimestamp(utc_now().value + timedelta(minutes=5)),
    )
    identity = _identity(authorization.authorization_id)
    plan = build_real_xtquant_plan(
        output_root=Path(authorization.output_root),
        identity=identity,  # type: ignore[arg-type]
        context=context,
        package_version="1.2.3",
        runner_key_id="local.real.runner.v1",
    )
    envelope = file_authority.issue(
        authorization=authorization,
        identity=identity,  # type: ignore[arg-type]
        selected_case_ids=plan.selected_case_ids,
        reference_bundle_id=context.promotion_profile.reference_bundle_id,
        reference_bundle_sha256=context.promotion_profile.reference_bundle_manifest_sha256,
        source_dataset_contract=context.source_dataset_contract,
        promotion_profile=context.promotion_profile,
        output_scope=str(Path(authorization.output_root)),
        frozen_versions=plan.versions.to_dict(),
        plan_sha256=plan.plan_sha256,
        claim_nonce="c" * 64,
        runner_key_id="local.real.runner.v1",
    )
    file = root / "real-runtime-authorization.json"
    file.write_bytes(envelope.to_bytes())
    return file, runtime, trust, output


def test_default_fixed_unit_preserves_unsupported_capability_after_claim(tmp_path: Path) -> None:
    authorization_file, runtime, _trust, output = _issued(tmp_path)
    before = set(sys.modules)

    result = runtime.execute(authorization_file=authorization_file)

    assert result.event_order == ("PRECHECK", "CLAIM", "IMPORT", "OUTPUT")
    assert result.outcome is AttemptOutcomeV3.FAILED
    assert json.loads((result.directory / "failure" / "attempt.json").read_bytes())["failure"] == {
        "category": "CAPABILITY_UNSUPPORTED",
        "retryable": False,
    }
    assert not (result.directory / "canonical").exists()
    assert len(list((tmp_path / "claims").glob("*.json"))) == 1
    assert "xtquant" not in set(sys.modules) - before
    assert output.exists()


def test_real_file_rejects_fake_key_id_and_c0b_file_before_claim(tmp_path: Path) -> None:
    authorization_file, runtime, trust, _output = _issued(tmp_path)
    authority = RealRuntimeAuthorizationAuthority(
        key_id="local.real.authorization.v1",
        private_key_file=tmp_path / "keys" / "authorization.key",
    )
    from quantking.data.validation.contracts_v3 import FAKE_V3_RUNNER_KEY_ID
    from quantking.data.validation.real_authorization import RealRuntimeAuthorizationEnvelope

    current = RealRuntimeAuthorizationEnvelope.from_bytes(authorization_file.read_bytes())
    with pytest.raises(RealAuthorizationError):
        authority.issue(
            authorization=current.authorization,
            identity=current.identity,
            selected_case_ids=current.selected_case_ids,
            reference_bundle_id=current.reference_bundle_id,
            reference_bundle_sha256=current.reference_bundle_sha256,
            source_dataset_contract=current.source_dataset_contract,
            promotion_profile=current.promotion_profile,
            output_scope=current.output_scope,
            frozen_versions=current.frozen_versions,
            plan_sha256=current.plan_sha256,
            claim_nonce=current.claim_nonce,
            runner_key_id=FAKE_V3_RUNNER_KEY_ID,
        )
    authorization_file.write_bytes(b'{"schema_version":"runtime-authorization-envelope.v2"}')
    with pytest.raises(RealXtQuantExecutionError) as captured:
        runtime.execute(authorization_file=authorization_file)
    assert captured.value.code == "REAL_RUNTIME_AUTHORIZATION_INVALID"
    assert not (tmp_path / "claims").exists()
    assert (
        trust.resolve().runner_trust_policy.evidence_kind_for(
            attestation_key_id="local.real.runner.v1",
            verifier_key_id="local.real.runner.v1",
        )
        is EvidenceKind.REAL
    )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda raw: raw.update({"unexpected": "field"}),
        lambda raw: raw.update({"claim_nonce": 7}),
        lambda raw: raw.update({"case_ids": ["st_warning_603377_sh", "calendar_sh"]}),
        lambda raw: raw["promotion_profile"].update({"minimum_stability_runs": "3"}),
        lambda raw: raw["source_dataset_contract"].update({"unexpected": "field"}),
        lambda raw: raw["authorization"].update({"output_root": "C:\\replaced-output"}),
    ),
    ids=("unknown", "type", "case", "nested-type", "nested-unknown", "output-root"),
)
def test_real_authorization_is_canonical_typed_and_file_only_before_claim(
    tmp_path: Path, mutate: object
) -> None:
    authorization_file, runtime, _trust, output = _issued(tmp_path)
    raw = json.loads(authorization_file.read_bytes())
    mutate(raw)  # type: ignore[operator]
    authorization_file.write_bytes(
        json.dumps(raw, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    )

    with pytest.raises(RealXtQuantExecutionError) as captured:
        runtime.execute(authorization_file=authorization_file)

    assert captured.value.code == "REAL_RUNTIME_AUTHORIZATION_INVALID"
    assert not (tmp_path / "claims").exists()
    assert not output.exists()


def test_expired_authorization_and_missing_metadata_stop_before_claim_or_import(
    tmp_path: Path,
) -> None:
    expired_file, expired_runtime, _trust, output = _issued(
        tmp_path / "expired",
        valid_from=UtcTimestamp(utc_now().value - timedelta(minutes=2)),
        expires_at=UtcTimestamp(utc_now().value - timedelta(seconds=1)),
    )
    with pytest.raises(RealXtQuantExecutionError) as expired:
        expired_runtime.execute(authorization_file=expired_file)
    assert expired.value.code == "AUTHORIZATION_INVALID_OR_EXPIRED"
    assert not (tmp_path / "expired" / "claims").exists()
    assert not output.exists()

    missing_file, missing_runtime, _missing_trust, missing_output = _issued(
        tmp_path / "metadata", metadata=False
    )
    with pytest.raises(RealXtQuantExecutionError) as missing:
        missing_runtime.execute(authorization_file=missing_file)
    assert missing.value.code == "XTQUANT_PACKAGE_METADATA_UNAVAILABLE"
    assert not (tmp_path / "metadata" / "claims").exists()
    assert not missing_output.exists()


def test_missing_trust_or_runner_stops_before_claim_import_and_output(tmp_path: Path) -> None:
    missing_key = tmp_path / "missing-runner.key"
    authorization_file, runtime, _trust, output = _issued(
        tmp_path / "missing-trust", trust_key=missing_key
    )
    with pytest.raises(RealXtQuantExecutionError) as trust_error:
        runtime.execute(authorization_file=authorization_file)
    assert trust_error.value.code == "REAL_TRUST_NOT_CONFIGURED"
    assert not (tmp_path / "missing-trust" / "claims").exists()
    assert not output.exists()

    authorization_file, runtime, _trust, output = _issued(
        tmp_path / "missing-runner", runner_preflight=lambda: False
    )
    with pytest.raises(RealXtQuantExecutionError) as runner_error:
        runtime.execute(authorization_file=authorization_file)
    assert runner_error.value.code == "REAL_RUNNER_NOT_READY"
    assert not (tmp_path / "missing-runner" / "claims").exists()
    assert not output.exists()


def test_bundle_output_version_and_key_substitution_fail_closed_before_claim(
    tmp_path: Path,
) -> None:
    authorization_file, runtime, _trust, output = _issued(tmp_path)
    authority = RealRuntimeAuthorizationAuthority(
        key_id="local.real.authorization.v1",
        private_key_file=tmp_path / "keys" / "authorization.key",
    )
    from quantking.data.validation.real_authorization import RealRuntimeAuthorizationEnvelope

    current = RealRuntimeAuthorizationEnvelope.from_bytes(authorization_file.read_bytes())
    changed_profile = replace(
        current.promotion_profile,
        reference_bundle_manifest_sha256="a" * 64,
    )
    changed = authority.issue(
        authorization=current.authorization,
        identity=current.identity,
        selected_case_ids=current.selected_case_ids,
        reference_bundle_id=current.reference_bundle_id,
        reference_bundle_sha256="a" * 64,
        source_dataset_contract=current.source_dataset_contract,
        promotion_profile=changed_profile,
        output_scope=current.output_scope,
        frozen_versions={**current.frozen_versions, "package_version": "9.9.9"},
        plan_sha256=current.plan_sha256,
        claim_nonce=current.claim_nonce,
        runner_key_id="local.real.replaced.v1",
    )
    authorization_file.write_bytes(changed.to_bytes())
    with pytest.raises(RealXtQuantExecutionError) as captured:
        runtime.execute(authorization_file=authorization_file)
    assert captured.value.code == "REAL_RUNTIME_AUTHORIZATION_BINDING_MISMATCH"
    assert not (tmp_path / "claims").exists()
    assert not output.exists()


def test_replay_does_not_overwrite_the_first_real_attempt(tmp_path: Path) -> None:
    authorization_file, runtime, _trust, output = _issued(tmp_path)
    first = runtime.execute(authorization_file=authorization_file)

    with pytest.raises(RealXtQuantExecutionError) as replay:
        runtime.execute(authorization_file=authorization_file)

    assert replay.value.code == "RUN_ID_ALREADY_EXISTS"
    assert (first.directory / "COMPLETE").is_file()
    assert len(list((tmp_path / "claims").glob("*.json"))) == 1
    assert not list(output.rglob("*.incomplete"))


def test_claim_is_retained_for_import_and_runner_crash(tmp_path: Path) -> None:
    authorization_file, runtime, _trust, output = _issued(tmp_path / "import")
    runtime._import_runner = lambda _name: (_ for _ in ()).throw(RuntimeError("missing"))  # type: ignore[attr-defined]
    with pytest.raises(RealXtQuantExecutionError) as imported:
        runtime.execute(authorization_file=authorization_file)
    assert imported.value.code == "REAL_RUNNER_IMPORT_FAILED"
    assert len(list((tmp_path / "import" / "claims").glob("*.json"))) == 1
    assert len(list(output.rglob("*.incomplete/failure.json"))) == 1

    authorization_file, runtime, _trust, output = _issued(tmp_path / "crash")

    class CrashRunner:
        def execute(self, **_kwargs: object) -> Path:
            raise RuntimeError("crash")

    runtime._import_runner = lambda _name: SimpleNamespace(  # type: ignore[attr-defined]
        create_runner=lambda: CrashRunner()
    )
    with pytest.raises(RealXtQuantExecutionError) as crashed:
        runtime.execute(authorization_file=authorization_file)
    assert crashed.value.code == "REAL_RUNNER_CRASHED"
    assert len(list((tmp_path / "crash" / "claims").glob("*.json"))) == 1
    assert len(list(output.rglob("*.incomplete/failure.json"))) == 1


def test_isolated_fake_adapter_can_exercise_success_without_changing_default_capability(
    tmp_path: Path,
) -> None:
    authorization_file, runtime, trust, _output = _issued(tmp_path)

    class SuccessAdapter:
        def validate_fixed_case(self) -> XtQuantValidationAdapterResult:
            return XtQuantValidationAdapterResult(AttemptOutcomeV3.SUCCEEDED, None)

    runtime._import_runner = lambda _name: SimpleNamespace(  # type: ignore[attr-defined]
        create_runner=lambda: XtQuantRealProviderRunner(adapter=SuccessAdapter())
    )
    result = runtime.execute(authorization_file=authorization_file)
    configuration = trust.resolve()
    verified = ValidationEvidenceVerifierV3(
        validation_root=result.directory.parents[4],
        context=configuration.context,
        runner_attestation_verifier=configuration.signer,
        runner_trust_policy=configuration.runner_trust_policy,
    ).verify_run(directory=result.directory)
    assert result.outcome is AttemptOutcomeV3.SUCCEEDED
    assert verified.evidence_kind is EvidenceKind.REAL


def test_real_blocked_evidence_seals_persists_and_formal_read_requires_explicit_trust(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "runtime-data"
    authorization_file, runtime, trust, _output = _issued(data_root)
    result = runtime.execute(authorization_file=authorization_file)
    settings = AppSettings(
        paths=RuntimePaths(
            code_root=Path.cwd(),
            data_root=data_root,
            offsite_backup_root=tmp_path / "offsite",
        ),
        xtquant_site_packages_root=tmp_path / "missing-sdk",
        web_host="127.0.0.1",
        web_port=8765,
        sqlite_busy_timeout_ms=250,
        sqlite_max_attempts=7,
        sqlite_retry_base_delay_ms=10,
        sqlite_retry_cap_delay_ms=160,
        sqlite_retry_jitter_ratio=0.0,
        job_heartbeat_min_interval_ms=1000,
        job_progress_min_interval_ms=250,
    )
    without_real = build_eod_runtime(settings)
    engine = create_data_engine(
        data_root / "metadata" / "quantking.db",
        busy_timeout_ms=250,
    )
    try:
        configuration = trust.resolve()
        adapter = SqliteV3SourceAssessmentPersistenceAdapter(
            engine=engine,
            validation_root=data_root,
            context=configuration.context,
            signer=configuration.signer,
            runner_trust_policy=configuration.runner_trust_policy,
        )
        saved = RealXtQuantAssessmentService(
            validation_root=data_root,
            trust_resolver=trust,
            persistence=adapter,
        ).finalize_and_persist(assessment_directory=result.directory.parent)
        reader = SealedAssessmentReadVerifier(
            validation_root=data_root,
            signer=FakeRunnerTrustStore(),
            signer_trust_policy=SignerTrustPolicy.synthetic_only(),
            v3_context=configuration.context,
            v3_signer=configuration.signer,
            v3_runner_trust_policy=configuration.runner_trust_policy,
        )
        with DataSqliteUnitOfWorkFactory(engine, assessment_read_verifier=reader)() as uow:
            assert uow.data.get_source_candidate_assessment(str(saved.assessment_id)) == saved
        assert saved.evidence_kind is EvidenceKind.REAL
        assert saved.status is AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION

        with without_real.data_uow_factory() as uow, pytest.raises(InvalidDataTransitionError):
            uow.data.get_source_candidate_assessment(str(saved.assessment_id))
        with_real = build_eod_runtime(settings, real_v3_trust_resolver=trust)
        with with_real.data_uow_factory() as uow:
            assert uow.data.get_source_candidate_assessment(str(saved.assessment_id)) == saved

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

        center = DataCenterQueryService(
            data_uow_factory=with_real.data_uow_factory,
            preview=_UnusedPort(),
            difference_verifier=_UnusedPort(),
            jobs=_NoJobs(),
        )
        assert any(
            item["assessment_status"] == AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION.value
            for item in center.sources()
        )
        bundle_manifest = (
            data_root
            / "authority"
            / "reference-bundles"
            / "st-603377-authority-v3"
            / "manifest.json"
        )
        bundle_manifest.write_bytes(b"tampered")
        with with_real.data_uow_factory() as uow, pytest.raises(InvalidDataTransitionError):
            uow.data.get_source_candidate_assessment(str(saved.assessment_id))

    finally:
        engine.dispose()


def test_seal_or_verifier_key_change_fails_closed_on_read(tmp_path: Path) -> None:
    authorization_file, runtime, trust, output = _issued(tmp_path)
    result = runtime.execute(authorization_file=authorization_file)
    database = tmp_path / "metadata.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    try:
        configuration = trust.resolve()
        saved = RealXtQuantAssessmentService(
            validation_root=output,
            trust_resolver=trust,
            persistence=SqliteV3SourceAssessmentPersistenceAdapter(
                engine=engine,
                validation_root=output,
                context=configuration.context,
                signer=configuration.signer,
                runner_trust_policy=configuration.runner_trust_policy,
            ),
        ).finalize_and_persist(assessment_directory=result.directory.parent)
        reader = SealedAssessmentReadVerifier(
            validation_root=output,
            signer=FakeRunnerTrustStore(),
            signer_trust_policy=SignerTrustPolicy.synthetic_only(),
            v3_context=configuration.context,
            v3_signer=LocalRealV3Signer(
                key_id="local.real.runner.v1",
                private_key_file=tmp_path / "keys" / "authorization.key",
            ),
            v3_runner_trust_policy=configuration.runner_trust_policy,
        )
        with (
            DataSqliteUnitOfWorkFactory(engine, assessment_read_verifier=reader)() as uow,
            pytest.raises(InvalidDataTransitionError),
        ):
            uow.data.get_source_candidate_assessment(str(saved.assessment_id))
    finally:
        engine.dispose()
