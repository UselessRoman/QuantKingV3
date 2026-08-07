"""S1-07C0B strict Fake-provider authorization and execution attacks."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantking.common.ids import new_entity_id
from quantking.data.models import DatasetKind
from quantking.data.validation.authorization import (
    AuthorizationClaimStore,
    FakeAuthorizationAuthority,
)
from quantking.data.validation.contracts_v3 import AttemptOutcomeV3, FakeV3RunnerTrustStore
from quantking.data.validation.controlled_runtime import (
    ControlledExecutionError,
    ControlledFakeValidationRuntime,
    ControlledV3RunnerTrustPolicy,
    FakeRuntimeAuthorizationAuthority,
    build_controlled_fake_context,
    build_controlled_fake_plan_sha,
)
from quantking.data.validation.runtime import RuntimeAuthorizationFileLoader


def _write_verified_authority_bundle(root: Path) -> Path:
    """Write the accepted S1-07B ST bundle into this isolated test root."""
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


def _issued(
    tmp_path: Path,
    *,
    output_root: Path | None = None,
    authority_root: Path | None = None,
) -> tuple[
    Path,
    ControlledFakeValidationRuntime,
    FakeRuntimeAuthorizationAuthority,
]:
    output = output_root or tmp_path / "controlled-output"
    authority_root = authority_root or tmp_path / "authority"
    _write_verified_authority_bundle(authority_root)
    context = build_controlled_fake_context(reference_bundle_root=authority_root)
    execution_authority = FakeAuthorizationAuthority()
    runtime_authority = FakeRuntimeAuthorizationAuthority()
    identity = SimpleNamespace(
        assessment_id=new_entity_id("data_assessment"),
        authorization_id=new_entity_id("data_authorization"),
        invocation_id=new_entity_id("data_invocation"),
        session_id=new_entity_id("data_session"),
        run_id=new_entity_id("data_validation"),
    )
    authorization = execution_authority.issue(
        source="XTQUANT",
        dataset=DatasetKind.TRADING_STATUS,
        output_root=output,
        sample_sha256=context.sample.sample_spec_sha256,
    )
    # A regular namespace-only object cannot replace the contract identity;
    # construct it through the strict envelope's imported contract type.
    from quantking.data.validation.contracts_v3 import ValidationAttemptIdentityV3

    attempt_identity = ValidationAttemptIdentityV3(
        assessment_id=identity.assessment_id,
        authorization_id=authorization.authorization_id,
        invocation_id=identity.invocation_id,
        session_id=identity.session_id,
        run_id=identity.run_id,
    )
    plan_sha = build_controlled_fake_plan_sha(
        source="XTQUANT",
        dataset=DatasetKind.TRADING_STATUS,
        output_root=Path(authorization.output_root),
        identity=attempt_identity,
        selected_case_ids=("st_warning_603377_sh",),
        context=context,
    )
    envelope = runtime_authority.issue(
        authorization=authorization,
        plan_sha256=plan_sha,
        frozen_versions={
            "adapter_version": "fake-provider.s1-07c0b.v1",
            "package_version": "1.0.0",
            "code_version": "s1-07c0b.runtime.v1",
            "schema_version": context.source_dataset_contract.expected_schema_version,
            "rule_version": "quality-rules.v1",
            "threshold_version": "comparison-thresholds.v1",
            "sample_spec_version": context.sample.sample_spec_version,
            "config_version": context.promotion_profile.promotion_config_version,
        },
        identity=attempt_identity,
        selected_case_ids=("st_warning_603377_sh",),
        context=context,
        claim_nonce="c" * 64,
    )
    authorization_file = tmp_path / "runtime-authorization.json"
    authorization_file.write_bytes(envelope.to_bytes())
    runtime = ControlledFakeValidationRuntime(
        reference_bundle_root=authority_root,
        authorization_file_loader=RuntimeAuthorizationFileLoader(verifier=runtime_authority),
        authorization_verifier=execution_authority,
        claim_store=AuthorizationClaimStore(claim_root=tmp_path / "claims"),
    )
    return authorization_file, runtime, runtime_authority


def test_fake_execution_is_precheck_then_claim_then_import_then_output(tmp_path: Path) -> None:
    authorization_file, runtime, _authority = _issued(tmp_path)
    before = set(sys.modules)

    result = runtime.execute(authorization_file=authorization_file)

    assert result.event_order == ("PRECHECK", "CLAIM", "IMPORT", "OUTPUT")
    assert result.outcome is AttemptOutcomeV3.SUCCEEDED
    assert (result.directory / "COMPLETE").is_file()
    assert (tmp_path / "claims").is_dir()
    assert not ({"xtquant", "baostock", "akshare", "tushare"} & (set(sys.modules) - before))


@pytest.mark.parametrize(
    "mutation",
    (
        lambda raw: raw.update({"unexpected": "field"}),
        lambda raw: raw.update({"purpose": "S1-07C1_REAL_PROVIDER"}),
        lambda raw: raw["case_ids"].append("calendar_bj"),
        lambda raw: raw.update({"reference_bundle_sha256": "a" * 64}),
        lambda raw: raw["promotion_profile"].update({"minimum_stability_runs": 1}),
        lambda raw: raw["source_dataset_contract"].update({"contract_version": "changed.v1"}),
    ),
    ids=("unknown", "purpose", "case", "bundle", "profile", "contract"),
)
def test_strict_authorization_rejects_unknown_or_substituted_fields_before_claim(
    tmp_path: Path, mutation: object
) -> None:
    authorization_file, runtime, _authority = _issued(tmp_path)
    raw = json.loads(authorization_file.read_bytes())
    mutation(raw)  # type: ignore[operator]
    authorization_file.write_bytes(
        json.dumps(raw, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    )

    with pytest.raises(ControlledExecutionError, match="runtime authorization") as captured:
        runtime.execute(authorization_file=authorization_file)

    assert captured.value.code == "RUNTIME_AUTHORIZATION_INVALID"
    assert not (tmp_path / "claims").exists()
    assert not (tmp_path / "controlled-output").exists()


@pytest.mark.parametrize(
    "mutation",
    (
        lambda raw: raw["promotion_profile"].update({"minimum_stability_runs": "3"}),
        lambda raw: raw["frozen_versions"].update({"package_version": 1}),
        lambda raw: raw["identity"].update({"run_id": 7}),
        lambda raw: raw["authorization"].update({"output_root": 7}),
    ),
    ids=(
        "profile-int-as-string",
        "version-string-as-int",
        "identity-string-as-int",
        "nested-text-as-int",
    ),
)
def test_signed_authorization_rejects_json_type_normalization_before_claim(
    tmp_path: Path, mutation: object
) -> None:
    """Preserving the old signature cannot turn a differently typed JSON value valid."""
    authorization_file, runtime, _authority = _issued(tmp_path)
    raw = json.loads(authorization_file.read_bytes())
    mutation(raw)  # type: ignore[operator]
    altered = json.dumps(raw, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    assert altered != authorization_file.read_bytes()
    authorization_file.write_bytes(altered)

    with pytest.raises(ControlledExecutionError) as captured:
        runtime.execute(authorization_file=authorization_file)

    assert captured.value.code == "RUNTIME_AUTHORIZATION_INVALID"
    assert not (tmp_path / "claims").exists()
    assert not (tmp_path / "controlled-output").exists()


def test_framework_only_case_is_rejected_by_authority_preflight_before_claim(
    tmp_path: Path,
) -> None:
    authorization_file, runtime, runtime_authority = _issued(tmp_path)
    from quantking.data.validation.runtime import RuntimeAuthorizationEnvelope

    current = RuntimeAuthorizationEnvelope.from_bytes(authorization_file.read_bytes())
    context = build_controlled_fake_context(
        reference_bundle_root=runtime._reference_bundle_root  # type: ignore[attr-defined]
    )
    altered = runtime_authority.issue(
        authorization=current.authorization,
        plan_sha256=build_controlled_fake_plan_sha(
            source=current.authorization.source,
            dataset=current.authorization.dataset,
            output_root=Path(current.authorization.output_root),
            identity=current.identity,  # type: ignore[arg-type]
            selected_case_ids=("calendar_sh",),
            context=context,
        ),
        frozen_versions=current.frozen_versions,
        identity=current.identity,  # type: ignore[arg-type]
        selected_case_ids=("calendar_sh",),
        context=context,
        claim_nonce=current.claim_nonce or "c" * 64,
    )
    authorization_file.write_bytes(altered.to_bytes())

    with pytest.raises(ControlledExecutionError) as captured:
        runtime.execute(authorization_file=authorization_file)

    assert captured.value.code == "CASE_NOT_AUTHORITY_APPROVED"
    assert not (tmp_path / "claims").exists()
    assert not (tmp_path / "controlled-output").exists()


def test_missing_authority_bundle_creates_no_preclaim_output_or_failure_file(
    tmp_path: Path,
) -> None:
    authorization_file, original, authority = _issued(tmp_path)
    missing_root = tmp_path / "missing-authority-root"
    imports: list[str] = []
    runtime = ControlledFakeValidationRuntime(
        reference_bundle_root=missing_root,
        authorization_file_loader=RuntimeAuthorizationFileLoader(verifier=authority),
        authorization_verifier=original._authorization_verifier,  # type: ignore[attr-defined]
        claim_store=original._claim_store,  # type: ignore[attr-defined]
        import_runner=lambda name: imports.append(name),
    )

    with pytest.raises(ControlledExecutionError) as captured:
        runtime.execute(authorization_file=authorization_file)

    assert captured.value.code == "AUTHORITY_BUNDLE_INVALID"
    assert imports == []
    assert not missing_root.exists()
    assert not (tmp_path / "claims").exists()
    assert not (tmp_path / "controlled-output").exists()


def test_authorization_replay_and_concurrent_claim_have_one_winner(tmp_path: Path) -> None:
    authorization_file, first, authority = _issued(tmp_path)
    # Both runtimes use the same exact file and claims root for this replay.
    second = ControlledFakeValidationRuntime(
        reference_bundle_root=first._reference_bundle_root,  # type: ignore[attr-defined]
        authorization_file_loader=RuntimeAuthorizationFileLoader(verifier=authority),
        authorization_verifier=first._authorization_verifier,  # type: ignore[attr-defined]
        claim_store=first._claim_store,  # type: ignore[attr-defined]
    )

    def run(runtime: ControlledFakeValidationRuntime) -> str:
        try:
            return runtime.execute(authorization_file=authorization_file).outcome.value
        except ControlledExecutionError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(run, (first, second)))
    assert results == ["AUTHORIZATION_ALREADY_CONSUMED", "SUCCEEDED"]
    with pytest.raises(ControlledExecutionError) as replay:
        first.execute(authorization_file=authorization_file)
    assert replay.value.code == "RUN_ID_ALREADY_EXISTS"


def test_claim_is_retained_when_runner_crashes_and_no_success_evidence_is_created(
    tmp_path: Path,
) -> None:
    authorization_file, runtime, _authority = _issued(tmp_path)

    class CrashRunner:
        def execute(self, **_kwargs: object) -> Path:
            raise RuntimeError("crash")

    runtime._import_runner = lambda _name: SimpleNamespace(  # type: ignore[attr-defined]
        create_runner=lambda: CrashRunner()
    )
    with pytest.raises(ControlledExecutionError) as captured:
        runtime.execute(authorization_file=authorization_file)

    assert captured.value.code == "FAKE_RUNNER_CRASHED"
    incomplete = list((tmp_path / "controlled-output").rglob("*.incomplete/failure.json"))
    assert len(incomplete) == 1
    assert json.loads(incomplete[0].read_bytes())["category"] == "FAKE_RUNNER_CRASHED"
    assert not list((tmp_path / "controlled-output").rglob("canonical/content.parquet"))
    assert len(list((tmp_path / "claims").glob("*.json"))) == 1


def test_controlled_failure_has_no_success_credentials(tmp_path: Path) -> None:
    authorization_file, runtime, _authority = _issued(tmp_path)
    from quantking.data.validation.runners.fake import FakeProviderRunner

    class FailureRunner:
        def execute(self, *, plan: object, output_root: Path) -> Path:
            return FakeProviderRunner().fail(
                plan=plan,  # type: ignore[arg-type]
                output_root=output_root,
                category="CAPABILITY_UNSUPPORTED",
            )

    runtime._import_runner = lambda _name: SimpleNamespace(  # type: ignore[attr-defined]
        create_runner=lambda: FailureRunner()
    )
    result = runtime.execute(authorization_file=authorization_file)

    assert result.outcome is AttemptOutcomeV3.FAILED
    assert (result.directory / "failure" / "attempt.json").is_file()
    assert not (result.directory / "canonical").exists()
    assert not (result.directory / "quality-root").exists()


def test_fake_key_stays_synthetic_even_when_wrapped_as_real(tmp_path: Path) -> None:
    authorization_file, runtime, _authority = _issued(tmp_path)
    result = runtime.execute(authorization_file=authorization_file)
    from quantking.data.validation.artifacts_v3 import ValidationEvidenceVerifierV3

    verified = ValidationEvidenceVerifierV3(
        validation_root=result.directory.parents[4],
        context=build_controlled_fake_context(
            reference_bundle_root=runtime._reference_bundle_root  # type: ignore[attr-defined]
        ),
        runner_attestation_verifier=FakeV3RunnerTrustStore(),
    ).verify_run(directory=result.directory)
    assert verified.evidence_kind.value == "SYNTHETIC"


def test_cli_fake_execute_accepts_only_the_strict_file(tmp_path: Path) -> None:
    authorization_file, runtime, _authority = _issued(tmp_path)
    from quantking.data.validation.cli import main

    assert (
        main(
            [
                "fake-execute",
                "--authorization-file",
                str(authorization_file),
                "--reference-bundle-root",
                str(runtime._reference_bundle_root),  # type: ignore[attr-defined]
            ]
        )
        == 0
    )
    assert list((tmp_path / "controlled-output").rglob("COMPLETE"))


def test_fake_execution_seal_persistence_and_read_verification_form_one_vertical_slice(
    tmp_path: Path,
) -> None:
    authorization_file, runtime, _authority = _issued(tmp_path)
    result = runtime.execute(authorization_file=authorization_file)
    from quantking.data.adapters.persistence.assessment import (
        SealedAssessmentReadVerifier,
        SqliteV3SourceAssessmentPersistenceAdapter,
    )
    from quantking.data.adapters.persistence.sqlite import (
        DataSqliteUnitOfWorkFactory,
        create_data_engine,
    )
    from quantking.data.validation.authorization import FakeRunnerTrustStore, SignerTrustPolicy
    from quantking.data.validation.controlled_runtime import (
        ControlledFakeAssessmentService,
        ControlledV3RunnerTrustPolicy,
    )
    from quantking.jobs.adapters.persistence.sqlite import initialize_database

    root = result.directory.parents[4]
    database = tmp_path / "metadata.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=500)
    try:
        context = build_controlled_fake_context(
            reference_bundle_root=runtime._reference_bundle_root  # type: ignore[attr-defined]
        )
        policy = ControlledV3RunnerTrustPolicy()
        adapter = SqliteV3SourceAssessmentPersistenceAdapter(
            engine=engine,
            validation_root=root,
            context=context,
            signer=FakeV3RunnerTrustStore(),
            runner_trust_policy=policy,
        )
        saved = ControlledFakeAssessmentService(
            validation_root=root,
            reference_bundle_root=runtime._reference_bundle_root,  # type: ignore[attr-defined]
            persistence=adapter,
        ).finalize_and_persist(assessment_directory=result.directory.parent)
        reader = SealedAssessmentReadVerifier(
            validation_root=root,
            signer=FakeRunnerTrustStore(),
            signer_trust_policy=SignerTrustPolicy.synthetic_only(),
            v3_context=context,
            v3_signer=FakeV3RunnerTrustStore(),
            v3_runner_trust_policy=policy,
        )
        with DataSqliteUnitOfWorkFactory(engine, assessment_read_verifier=reader)() as uow:
            assert uow.data.get_source_candidate_assessment(str(saved.assessment_id)) == saved
        assert saved.evidence_kind.value == "SYNTHETIC"
        assert saved.status.value == "BLOCKED_EXTERNAL_VALIDATION"
    finally:
        engine.dispose()


def test_formal_runtime_datacenter_uow_reverifies_c0b_v3_bundle_and_seal(
    tmp_path: Path,
) -> None:
    """The shipped composition root, not a hand-built UoW, reads C0B v3 rows."""
    from quantking.common.config import AppSettings
    from quantking.common.errors import InvalidDataTransitionError
    from quantking.common.paths import RuntimePaths
    from quantking.data.adapters.persistence.assessment import (
        SqliteV3SourceAssessmentPersistenceAdapter,
    )
    from quantking.data.adapters.persistence.sqlite import create_data_engine
    from quantking.data.center import DataCenterQueryService
    from quantking.data.validation.controlled_runtime import ControlledFakeAssessmentService
    from quantking.runtime import build_eod_runtime

    data_root = tmp_path / "runtime-data"
    authorization_file, runtime, _authority = _issued(
        tmp_path,
        output_root=data_root,
        authority_root=data_root,
    )
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

    class _UnusedGateway:
        def fetch(self, _chunk: object) -> object:
            raise AssertionError("formal read-side test must not fetch provider data")

    formal = build_eod_runtime(settings, gateway=_UnusedGateway())
    engine = create_data_engine(data_root / "metadata" / "quantking.db", busy_timeout_ms=250)
    try:
        context = build_controlled_fake_context(reference_bundle_root=data_root)
        saved = ControlledFakeAssessmentService(
            validation_root=data_root,
            reference_bundle_root=data_root,
            persistence=SqliteV3SourceAssessmentPersistenceAdapter(
                engine=engine,
                validation_root=data_root,
                context=context,
                signer=FakeV3RunnerTrustStore(),
                runner_trust_policy=ControlledV3RunnerTrustPolicy(),
            ),
        ).finalize_and_persist(assessment_directory=result.directory.parent)
        with formal.data_uow_factory() as uow:
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
            data_uow_factory=formal.data_uow_factory,
            preview=_UnusedPort(),
            difference_verifier=_UnusedPort(),
            jobs=_NoJobs(),
        )
        assert any(item["assessment_status"] for item in center.sources())

        bundle_manifest = (
            data_root / "reference-bundles" / "st-603377-authority-v3" / "manifest.json"
        )
        bundle_manifest.write_bytes(b"tampered")
        with formal.data_uow_factory() as uow, pytest.raises(InvalidDataTransitionError):
            uow.data.get_source_candidate_assessment(str(saved.assessment_id))
    finally:
        engine.dispose()


def test_signed_but_expired_authorization_fails_before_import_or_output(tmp_path: Path) -> None:
    authorization_file, runtime, runtime_authority = _issued(tmp_path)
    from quantking.common.time import UtcTimestamp
    from quantking.data.validation.runtime import RuntimeAuthorizationEnvelope

    current = RuntimeAuthorizationEnvelope.from_bytes(authorization_file.read_bytes())
    assert current.identity is not None
    assert current.selected_case_ids is not None
    execution = FakeAuthorizationAuthority().issue(
        source=current.authorization.source,
        dataset=current.authorization.dataset,
        output_root=Path(current.authorization.output_root),
        sample_sha256=current.authorization.sample_sha256,
        valid_from=UtcTimestamp(datetime.now(UTC) - timedelta(minutes=10)),
        expires_at=UtcTimestamp(datetime.now(UTC) - timedelta(minutes=5)),
    )
    expired_identity = replace(current.identity, authorization_id=execution.authorization_id)
    expired_plan_sha = build_controlled_fake_plan_sha(
        source=execution.source,
        dataset=execution.dataset,
        output_root=Path(execution.output_root),
        identity=expired_identity,
        selected_case_ids=current.selected_case_ids,
        context=build_controlled_fake_context(
            reference_bundle_root=runtime._reference_bundle_root  # type: ignore[attr-defined]
        ),
    )
    expired = runtime_authority.issue(
        authorization=execution,
        plan_sha256=expired_plan_sha,
        frozen_versions=current.frozen_versions,
        identity=expired_identity,
        selected_case_ids=current.selected_case_ids,
        context=build_controlled_fake_context(
            reference_bundle_root=runtime._reference_bundle_root  # type: ignore[attr-defined]
        ),
        claim_nonce=current.claim_nonce or "c" * 64,
    )
    authorization_file.write_bytes(expired.to_bytes())
    with pytest.raises(ControlledExecutionError) as captured:
        runtime.execute(authorization_file=authorization_file)
    assert captured.value.code == "AUTHORIZATION_INVALID_OR_EXPIRED"
    assert not (tmp_path / "controlled-output").exists()
    assert not (tmp_path / "claims").exists()


def test_signed_context_substitution_and_direct_dto_both_fail_closed(tmp_path: Path) -> None:
    authorization_file, runtime, runtime_authority = _issued(tmp_path)
    from quantking.data.validation.contracts_v3 import PromotionProfileV3
    from quantking.data.validation.runtime import RuntimeAuthorizationEnvelope

    current = RuntimeAuthorizationEnvelope.from_bytes(authorization_file.read_bytes())
    context = build_controlled_fake_context(
        reference_bundle_root=runtime._reference_bundle_root  # type: ignore[attr-defined]
    )
    substituted_context = replace(
        context,
        promotion_profile=PromotionProfileV3(
            source=context.promotion_profile.source,
            dataset=context.promotion_profile.dataset,
            promotion_profile_version=context.promotion_profile.promotion_profile_version,
            promotion_config_version=context.promotion_profile.promotion_config_version,
            required_case_ids=context.promotion_profile.required_case_ids,
            reference_bundle_id=context.promotion_profile.reference_bundle_id,
            reference_bundle_manifest_sha256=(
                context.promotion_profile.reference_bundle_manifest_sha256
            ),
            minimum_stability_runs=1,
        ),
    )
    plan_sha = build_controlled_fake_plan_sha(
        source=current.authorization.source,
        dataset=current.authorization.dataset,
        output_root=Path(current.authorization.output_root),
        identity=current.identity,  # type: ignore[arg-type]
        selected_case_ids=current.selected_case_ids,  # type: ignore[arg-type]
        context=substituted_context,
    )
    substituted = runtime_authority.issue(
        authorization=current.authorization,
        plan_sha256=plan_sha,
        frozen_versions=current.frozen_versions,
        identity=current.identity,  # type: ignore[arg-type]
        selected_case_ids=current.selected_case_ids,  # type: ignore[arg-type]
        context=substituted_context,
        claim_nonce=current.claim_nonce or "c" * 64,
    )
    authorization_file.write_bytes(substituted.to_bytes())
    with pytest.raises(ControlledExecutionError) as captured:
        runtime.execute(authorization_file=authorization_file)
    assert captured.value.code == "RUNTIME_AUTHORIZATION_BINDING_MISMATCH"

    from quantking.data.validation.cli import REAL_EXECUTION_ACK, ValidationExecutionError, execute

    with pytest.raises(ValidationExecutionError) as legacy:
        execute(
            source="XTQUANT",
            dataset=DatasetKind.TRADING_STATUS,
            output_root=tmp_path,
            acknowledgement=REAL_EXECUTION_ACK,
            runtime_authorization=current,
        )
    assert legacy.value.code == "LEGACY_AUTHORIZATION_DTO_REJECTED"
