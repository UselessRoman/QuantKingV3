"""S1-07C1A-R1 pure-offline REAL control-chain regressions."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from time import sleep
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from quantking.common.config import AppSettings
from quantking.common.errors import InvalidDataTransitionError
from quantking.common.ids import new_entity_id
from quantking.common.paths import RuntimePaths
from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.adapters.persistence.assessment import (
    SealedAssessmentReadVerifier,
    SqliteRealSourceAssessmentPersistenceAdapter,
)
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.adapters.sources.xtquant_metadata import ExplicitXtQuantPackageMetadataProbe
from quantking.data.assessment.models import AssessmentStatus, EvidenceKind
from quantking.data.center import DataCenterQueryService
from quantking.data.models import DatasetKind
from quantking.data.validation.artifacts import ValidationEvidenceVerificationError
from quantking.data.validation.authorization import FakeRunnerTrustStore, SignerTrustPolicy
from quantking.data.validation.contracts_v3 import (
    AttemptOutcomeV3,
    PromotionProfileV3,
    SourceDatasetContractV3,
    ValidationAttemptIdentityV3,
)
from quantking.data.validation.models import ValidationFailure
from quantking.data.validation.real_artifacts import (
    RealResultAttestation,
    RealValidationEvidenceVerifier,
    RealValidationEvidenceWriter,
    canonical_json,
)
from quantking.data.validation.real_authorization import (
    ExplicitLocalRealKey,
    RealAuthorizationClaimStore,
    RealAuthorizationError,
    RealClaimReceipt,
    RealClaimReceiptIssuer,
    RealClaimReceiptVerifier,
    RealExecutionAuthorizationAuthority,
    RealExecutionAuthorizationVerifier,
    RealRuntimeAuthorizationAuthority,
    RealRuntimeAuthorizationEnvelope,
    RealRuntimeAuthorizationFileLoader,
    RealRuntimeAuthorizationVerifier,
    RealVerifierTrustAnchor,
)
from quantking.data.validation.real_runtime import (
    ExplicitRealXtQuantTrustResolver,
    RealXtQuantAssessmentService,
    RealXtQuantExecutionError,
    RealXtQuantValidationRuntime,
)
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


def _write_metadata(root: Path, version: str = "1.2.3") -> None:
    metadata = root / f"xtquant-{version}.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(f"Name: xtquant\nVersion: {version}\n", encoding="utf-8")


def _keypair(root: Path, name: str) -> tuple[Path, Path, str]:
    private = Ed25519PrivateKey.generate()
    private_file = root / "keys" / f"{name}-private.pem"
    public_file = root / "keys" / f"{name}-public.pem"
    private_file.parent.mkdir(parents=True, exist_ok=True)
    private_file.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_file.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return private_file, public_file, ExplicitLocalRealKey(private_key_file=private_file).key_id


def _identity(authorization_id: object) -> ValidationAttemptIdentityV3:
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
    metadata: bool = True,
    runner_preflight: object | None = None,
    runner_public: Path | None = None,
    allowed_runner_ids: frozenset[str] | None = None,
    output_root: Path | None = None,
) -> tuple[
    RealXtQuantValidationRuntime,
    RealExecutionAuthorizationAuthority,
    RealRuntimeAuthorizationAuthority,
    ExplicitRealXtQuantTrustResolver,
    Path,
]:
    _write_verified_authority_bundle(root / "authority")
    site_packages = root / "site-packages"
    if metadata:
        _write_metadata(site_packages)
    auth_private, auth_public, auth_id = _keypair(root, "authorization")
    runner_private, default_runner_public, runner_id = _keypair(root, "runner")
    trust = ExplicitRealXtQuantTrustResolver(
        reference_bundle_root=root / "authority",
        runner_public_key_file=runner_public or default_runner_public,
        allowed_runner_key_ids=allowed_runner_ids or frozenset({runner_id}),
        authorization_public_key_file=auth_public,
        allowed_authorization_key_ids=frozenset({auth_id}),
    )
    auth_anchor = RealVerifierTrustAnchor(
        public_key_file=auth_public, allowed_key_ids=frozenset({auth_id})
    )
    runtime = RealXtQuantValidationRuntime(
        authorization_file_loader=RealRuntimeAuthorizationFileLoader(
            verifier=RealRuntimeAuthorizationVerifier(trust_anchor=auth_anchor)
        ),
        authorization_verifier=RealExecutionAuthorizationVerifier(trust_anchor=auth_anchor),
        claim_store=RealAuthorizationClaimStore(
            claim_root=root / "claims",
            receipt_issuer=RealClaimReceiptIssuer(private_key_file=auth_private),
        ),
        trust_resolver=trust,
        runner_signer=ExplicitLocalRealKey(private_key_file=runner_private),
        package_metadata_probe=ExplicitXtQuantPackageMetadataProbe(
            site_packages_root=site_packages
        ),
        runner_preflight=runner_preflight,  # type: ignore[arg-type]
    )
    return (
        runtime,
        RealExecutionAuthorizationAuthority(private_key_file=auth_private),
        RealRuntimeAuthorizationAuthority(private_key_file=auth_private),
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
    runner_public: Path | None = None,
    allowed_runner_ids: frozenset[str] | None = None,
    output_root: Path | None = None,
) -> tuple[Path, RealXtQuantValidationRuntime, ExplicitRealXtQuantTrustResolver, Path]:
    runtime, execution_issuer, file_issuer, trust, output = _runtime(
        root,
        metadata=metadata,
        runner_preflight=runner_preflight,
        runner_public=runner_public,
        allowed_runner_ids=allowed_runner_ids,
        output_root=output_root,
    )
    configuration = trust.resolve()
    start = valid_from or UtcTimestamp(utc_now().value - timedelta(seconds=5))
    authorization = execution_issuer.issue(
        authorization_id=new_entity_id("data_authorization"),
        source="XTQUANT",
        dataset=DatasetKind.TRADING_STATUS,
        output_root=output,
        sample_sha256=configuration.context.sample.sample_spec_sha256,
        valid_from=start,
        expires_at=expires_at or UtcTimestamp(utc_now().value + timedelta(minutes=5)),
    )
    identity = _identity(authorization.authorization_id)
    plan = runtime.plan_for_authorization(output_root=output, identity=identity)
    envelope = file_issuer.issue(
        authorization=authorization,
        identity=identity,
        selected_case_ids=plan.selected_case_ids,
        reference_bundle_id=configuration.context.promotion_profile.reference_bundle_id,
        reference_bundle_sha256=configuration.context.promotion_profile.reference_bundle_manifest_sha256,
        source_dataset_contract=configuration.context.source_dataset_contract,
        promotion_profile=configuration.context.promotion_profile,
        output_scope=str(output),
        frozen_versions=plan.versions.to_dict(),
        plan_sha256=plan.plan_sha256,
        claim_nonce="c" * 64,
        runner_key_id=plan.runner_key_id,
    )
    file = root / "real-runtime-authorization.json"
    file.write_bytes(envelope.to_bytes())
    return file, runtime, trust, output


def _rewrite_test_only_run(
    *,
    key_root: Path,
    directory: Path,
    replacements: dict[str, bytes],
    attestation_mutator: object | None = None,
    manifest_mutator: object | None = None,
) -> None:
    """Test-only signed artifact constructor; production has no success writer."""
    manifest = json.loads((directory / "manifest.json").read_bytes())
    payloads = {
        entry["relative_path"]: replacements.get(entry["relative_path"])
        or (directory / entry["relative_path"]).read_bytes()
        for entry in manifest["files"]
    }
    plan = json.loads(payloads["request/plan.json"])
    declared_plan_sha = plan.pop("plan_sha256", None)
    if (
        not isinstance(declared_plan_sha, str)
        or sha256(canonical_json(plan)).hexdigest() != declared_plan_sha
    ):
        raise AssertionError("test artifact plan SHA is invalid")
    lineage = {
        "authorization_envelope_sha256": sha256(
            payloads["authorization/envelope.json"]
        ).hexdigest(),
        "plan_sha256": declared_plan_sha,
        "claim_receipt_sha256": sha256(payloads["authorization/claim-receipt.json"]).hexdigest(),
    }
    attestation = json.loads(payloads["runner/result-attestation.real.v2.json"])
    attestation["lineage"] = lineage
    if attestation_mutator is not None:
        attestation_mutator(attestation)  # type: ignore[operator]
    unsigned_attestation = dict(attestation)
    unsigned_attestation.pop("signature")
    signer = ExplicitLocalRealKey(private_key_file=key_root / "keys" / "runner-private.pem")
    attestation["signature"] = signer.sign(
        domain=RealValidationEvidenceWriter._DOMAIN, payload=unsigned_attestation
    )
    payloads["runner/result-attestation.real.v2.json"] = canonical_json(attestation)
    manifest["lineage"] = lineage
    if manifest_mutator is not None:
        manifest_mutator(manifest)  # type: ignore[operator]
    manifest["files"] = [
        {"relative_path": name, "sha256": sha256(payload).hexdigest(), "size": len(payload)}
        for name, payload in sorted(payloads.items())
    ]
    unsigned_manifest = dict(manifest)
    unsigned_manifest.pop("manifest_sha256")
    manifest_sha = sha256(canonical_json(unsigned_manifest)).hexdigest()
    manifest["manifest_sha256"] = manifest_sha
    for name, payload in payloads.items():
        (directory / name).write_bytes(payload)
    (directory / "manifest.json").write_bytes(canonical_json(manifest))
    (directory / "COMPLETE").write_bytes(canonical_json({"manifest_sha256": manifest_sha}))


def _replace_with_fully_signed_envelope(
    *, root: Path, directory: Path, envelope: RealRuntimeAuthorizationEnvelope
) -> None:
    """Keep plan, receipt and runner signatures valid while changing only the envelope."""
    envelope_bytes = envelope.to_bytes()
    previous_receipt = RealClaimReceipt.from_bytes(
        (directory / "authorization" / "claim-receipt.json").read_bytes()
    )
    receipt = RealClaimReceiptIssuer(
        private_key_file=root / "keys" / "authorization-private.pem"
    ).issue(
        authorization_id=previous_receipt.authorization_id,
        invocation_id=previous_receipt.invocation_id,
        run_id=previous_receipt.run_id,
        claimed_at=previous_receipt.claimed_at,
        claim_nonce=previous_receipt.claim_nonce,
        authorization_envelope_sha256=sha256(envelope_bytes).hexdigest(),
        plan_sha256=previous_receipt.plan_sha256,
    )
    _rewrite_test_only_run(
        key_root=root,
        directory=directory,
        replacements={
            "authorization/envelope.json": envelope_bytes,
            "authorization/claim-receipt.json": receipt.to_bytes(),
        },
    )


def _reissue_envelope(
    *,
    root: Path,
    envelope: RealRuntimeAuthorizationEnvelope,
    identity: ValidationAttemptIdentityV3 | None = None,
    source_dataset_contract: SourceDatasetContractV3 | None = None,
    promotion_profile: PromotionProfileV3 | None = None,
) -> RealRuntimeAuthorizationEnvelope:
    """Use the trusted authorization key so drift tests do not rely on bad signatures."""
    contract = source_dataset_contract or envelope.source_dataset_contract
    profile = promotion_profile or envelope.promotion_profile
    return RealRuntimeAuthorizationAuthority(
        private_key_file=root / "keys" / "authorization-private.pem"
    ).issue(
        authorization=envelope.authorization,
        identity=identity or envelope.identity,
        selected_case_ids=envelope.selected_case_ids,
        reference_bundle_id=profile.reference_bundle_id,
        reference_bundle_sha256=profile.reference_bundle_manifest_sha256,
        source_dataset_contract=contract,
        promotion_profile=profile,
        output_scope=envelope.output_scope,
        frozen_versions=envelope.frozen_versions,
        plan_sha256=envelope.plan_sha256,
        claim_nonce=envelope.claim_nonce,
        runner_key_id=envelope.runner_key_id,
    )


def _assert_fully_signed_envelope_drift_is_rejected(
    *,
    root: Path,
    directory: Path,
    output: Path,
    trust: ExplicitRealXtQuantTrustResolver,
    envelope: RealRuntimeAuthorizationEnvelope,
) -> None:
    _replace_with_fully_signed_envelope(root=root, directory=directory, envelope=envelope)
    config = trust.resolve()
    persisted_envelope = RealRuntimeAuthorizationEnvelope.from_bytes(
        (directory / "authorization" / "envelope.json").read_bytes()
    )
    assert RealRuntimeAuthorizationVerifier(
        trust_anchor=config.authorization_verifier_anchor
    ).verify_real_file_authorization(persisted_envelope)
    receipt = RealClaimReceipt.from_bytes(
        (directory / "authorization" / "claim-receipt.json").read_bytes()
    )
    assert RealClaimReceiptVerifier(trust_anchor=config.authorization_verifier_anchor).verify(
        receipt
    )
    attestation = RealResultAttestation.from_bytes(
        (directory / "runner" / "result-attestation.real.v2.json").read_bytes()
    )
    assert config.runner_verifier_anchor.verify(
        domain=RealValidationEvidenceWriter._DOMAIN,
        payload=attestation.unsigned_payload(),
        signature=attestation.signature,
    )
    with pytest.raises(ValidationEvidenceVerificationError):
        RealValidationEvidenceVerifier(
            validation_root=output,
            context=config.context,
            runner_verifier_anchor=config.runner_verifier_anchor,
            authorization_verifier_anchor=config.authorization_verifier_anchor,
        ).verify_run(directory=directory)


def test_default_fixed_unit_is_real_blocked_and_does_not_import_xtquant(tmp_path: Path) -> None:
    authorization_file, runtime, _trust, output = _issued(tmp_path)
    before = set(sys.modules)

    result = runtime.execute(authorization_file=authorization_file)

    assert result.event_order == ("PRECHECK", "CLAIM", "IMPORT", "OUTPUT")
    assert result.outcome is AttemptOutcomeV3.FAILED
    assert json.loads((result.directory / "failure" / "attempt.json").read_bytes()) == {
        "failure": {"category": "CAPABILITY_UNSUPPORTED", "retryable": False}
    }
    assert not (result.directory / "success").exists()
    assert len(list((tmp_path / "claims").glob("*.json"))) == 1
    assert "xtquant" not in set(sys.modules) - before
    assert output.exists()


def test_public_fake_bytes_or_caller_key_id_can_never_become_real(tmp_path: Path) -> None:
    _private, public, runner_id = _keypair(tmp_path, "runner")
    fake_public = tmp_path / "fake-public.pem"
    fake_public.write_bytes(b"quantking-s1-07a-r2-fake-runner-key")
    with pytest.raises(RealAuthorizationError):
        RealVerifierTrustAnchor(public_key_file=fake_public, allowed_key_ids=frozenset({runner_id}))
    with pytest.raises(RealAuthorizationError):
        RealVerifierTrustAnchor(
            public_key_file=public, allowed_key_ids=frozenset({"quantking.fake-runner.v1"})
        )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda raw: raw.update({"unexpected": "field"}),
        lambda raw: raw.update({"claim_nonce": 7}),
        lambda raw: raw.update({"case_ids": ["st_warning_603377_sh", "calendar_sh"]}),
        lambda raw: raw["authorization"].update({"output_root": "C:\\replaced-output"}),
    ),
    ids=("unknown", "type", "case", "output-root"),
)
def test_real_authorization_is_canonical_file_only_before_claim(
    tmp_path: Path, mutate: object
) -> None:
    authorization_file, runtime, _trust, output = _issued(tmp_path)
    raw = json.loads(authorization_file.read_bytes())
    mutate(raw)  # type: ignore[operator]
    authorization_file.write_bytes(json.dumps(raw, separators=(",", ":"), sort_keys=True).encode())

    with pytest.raises(RealXtQuantExecutionError) as captured:
        runtime.execute(authorization_file=authorization_file)

    assert captured.value.code == "REAL_RUNTIME_AUTHORIZATION_INVALID"
    assert not (tmp_path / "claims").exists()
    assert not output.exists()


def test_expired_missing_trust_runner_or_package_stops_before_claim(tmp_path: Path) -> None:
    expired_file, expired_runtime, _trust, output = _issued(
        tmp_path / "expired",
        valid_from=UtcTimestamp(utc_now().value - timedelta(minutes=2)),
        expires_at=UtcTimestamp(utc_now().value - timedelta(seconds=1)),
    )
    with pytest.raises(RealXtQuantExecutionError) as expired:
        expired_runtime.execute(authorization_file=expired_file)
    assert expired.value.code == "AUTHORIZATION_INVALID_OR_EXPIRED"
    assert not (tmp_path / "expired" / "claims").exists()

    missing_file, missing_runtime, _trust, _ = _issued(tmp_path / "metadata")
    (tmp_path / "metadata" / "site-packages").rename(
        tmp_path / "metadata" / "site-packages-missing"
    )
    with pytest.raises(RealXtQuantExecutionError) as missing:
        missing_runtime.execute(authorization_file=missing_file)
    assert missing.value.code == "XTQUANT_PACKAGE_METADATA_UNAVAILABLE"
    assert not (tmp_path / "metadata" / "claims").exists()

    runner_file, runner_runtime, _trust, _ = _issued(tmp_path / "runner")
    runner_runtime._runner_preflight = lambda: False  # type: ignore[attr-defined]
    with pytest.raises(RealXtQuantExecutionError) as runner:
        runner_runtime.execute(authorization_file=runner_file)
    assert runner.value.code == "REAL_RUNNER_NOT_READY"
    assert not (tmp_path / "runner" / "claims").exists()
    assert not output.exists()


def test_runner_public_key_substitution_is_rejected_before_claim(tmp_path: Path) -> None:
    _private, changed_public, _changed_id = _keypair(tmp_path, "changed")
    authorization_file, runtime, trust, _output = _issued(tmp_path / "substitution")
    trust._runner_public_key_file.write_bytes(changed_public.read_bytes())  # type: ignore[attr-defined]
    with pytest.raises(RealXtQuantExecutionError) as captured:
        runtime.execute(authorization_file=authorization_file)
    assert captured.value.code == "REAL_TRUST_NOT_CONFIGURED"
    assert not (tmp_path / "substitution" / "claims").exists()


def test_authorization_public_key_substitution_is_rejected_before_claim(tmp_path: Path) -> None:
    _private, changed_public, _changed_id = _keypair(tmp_path, "changed-authorization")
    authorization_file, runtime, trust, _output = _issued(tmp_path / "substitution")
    trust._authorization_public_key_file.write_bytes(changed_public.read_bytes())  # type: ignore[attr-defined]

    with pytest.raises(RealXtQuantExecutionError) as captured:
        runtime.execute(authorization_file=authorization_file)

    assert captured.value.code == "REAL_TRUST_NOT_CONFIGURED"
    assert not (tmp_path / "substitution" / "claims").exists()


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

    runtime._import_runner = lambda _name: SimpleNamespace(create_runner=lambda: CrashRunner())  # type: ignore[attr-defined]
    with pytest.raises(RealXtQuantExecutionError) as crashed:
        runtime.execute(authorization_file=authorization_file)
    assert crashed.value.code == "REAL_RUNNER_CRASHED"
    assert len(list((tmp_path / "crash" / "claims").glob("*.json"))) == 1
    assert len(list(output.rglob("*.incomplete/failure.json"))) == 1


def test_real_attestation_is_within_authorization_and_execution_window(tmp_path: Path) -> None:
    authorization_file, runtime, trust, _output = _issued(tmp_path)
    result = runtime.execute(authorization_file=authorization_file)
    attestation = RealResultAttestation.from_bytes(
        (result.directory / "runner" / "result-attestation.real.v2.json").read_bytes()
    )
    envelope = json.loads(authorization_file.read_bytes())
    valid_from = UtcTimestamp.from_iso(envelope["authorization"]["valid_from"])
    expires_at = UtcTimestamp.from_iso(envelope["authorization"]["expires_at"])
    assert (
        valid_from.value
        <= attestation.started_at.value
        < attestation.ended_at.value
        < expires_at.value
    )
    verified = RealValidationEvidenceVerifier(
        validation_root=result.directory.parents[4],
        context=trust.resolve().context,
        runner_verifier_anchor=trust.resolve().runner_verifier_anchor,
        authorization_verifier_anchor=trust.resolve().authorization_verifier_anchor,
    ).verify_run(directory=result.directory)
    assert verified.outcome is AttemptOutcomeV3.FAILED


def test_fully_signed_envelope_session_identity_drift_is_rejected(tmp_path: Path) -> None:
    authorization_file, runtime, trust, output = _issued(tmp_path)
    result = runtime.execute(authorization_file=authorization_file)
    original = RealRuntimeAuthorizationEnvelope.from_bytes(
        (result.directory / "authorization" / "envelope.json").read_bytes()
    )
    drifted = _reissue_envelope(
        root=tmp_path,
        envelope=original,
        identity=replace(original.identity, session_id=new_entity_id("data_session")),
    )

    _assert_fully_signed_envelope_drift_is_rejected(
        root=tmp_path,
        directory=result.directory,
        output=output,
        trust=trust,
        envelope=drifted,
    )


def test_fully_signed_envelope_bundle_drift_is_rejected(tmp_path: Path) -> None:
    authorization_file, runtime, trust, output = _issued(tmp_path)
    result = runtime.execute(authorization_file=authorization_file)
    original = RealRuntimeAuthorizationEnvelope.from_bytes(
        (result.directory / "authorization" / "envelope.json").read_bytes()
    )
    drifted = _reissue_envelope(
        root=tmp_path,
        envelope=original,
        promotion_profile=replace(
            original.promotion_profile,
            reference_bundle_id="st-603377-alternate-v3",
            reference_bundle_manifest_sha256="a" * 64,
        ),
    )

    _assert_fully_signed_envelope_drift_is_rejected(
        root=tmp_path,
        directory=result.directory,
        output=output,
        trust=trust,
        envelope=drifted,
    )


def test_fully_signed_envelope_contract_drift_is_rejected(tmp_path: Path) -> None:
    authorization_file, runtime, trust, output = _issued(tmp_path)
    result = runtime.execute(authorization_file=authorization_file)
    original = RealRuntimeAuthorizationEnvelope.from_bytes(
        (result.directory / "authorization" / "envelope.json").read_bytes()
    )
    drifted = _reissue_envelope(
        root=tmp_path,
        envelope=original,
        source_dataset_contract=replace(
            original.source_dataset_contract,
            contract_version="xtquant-trading-status-real-contract.c1a-r3.v1",
        ),
    )

    _assert_fully_signed_envelope_drift_is_rejected(
        root=tmp_path,
        directory=result.directory,
        output=output,
        trust=trust,
        envelope=drifted,
    )


def test_fully_signed_envelope_profile_drift_is_rejected(tmp_path: Path) -> None:
    authorization_file, runtime, trust, output = _issued(tmp_path)
    result = runtime.execute(authorization_file=authorization_file)
    original = RealRuntimeAuthorizationEnvelope.from_bytes(
        (result.directory / "authorization" / "envelope.json").read_bytes()
    )
    drifted = _reissue_envelope(
        root=tmp_path,
        envelope=original,
        promotion_profile=replace(
            original.promotion_profile,
            promotion_profile_version="xtquant-trading-status-real-promotion.c1a-r3.v1",
        ),
    )

    _assert_fully_signed_envelope_drift_is_rejected(
        root=tmp_path,
        directory=result.directory,
        output=output,
        trust=trust,
        envelope=drifted,
    )


def test_formal_reader_rechecks_both_authorization_signatures(tmp_path: Path) -> None:
    """A trusted runner cannot turn zeroed authorization signatures into REAL evidence."""
    authorization_file, runtime, trust, output = _issued(tmp_path)
    result = runtime.execute(authorization_file=authorization_file)
    envelope = json.loads((result.directory / "authorization" / "envelope.json").read_bytes())
    envelope["signature"] = "0" * 128
    envelope["authorization"]["signature"] = "0" * 128
    envelope_bytes = canonical_json(envelope)
    receipt = json.loads((result.directory / "authorization" / "claim-receipt.json").read_bytes())
    receipt["authorization_envelope_sha256"] = sha256(envelope_bytes).hexdigest()
    receipt["signature"] = "0" * 128
    _rewrite_test_only_run(
        key_root=tmp_path,
        directory=result.directory,
        replacements={
            "authorization/envelope.json": envelope_bytes,
            "authorization/claim-receipt.json": canonical_json(receipt),
        },
    )

    config = trust.resolve()
    with pytest.raises(ValidationEvidenceVerificationError):
        RealValidationEvidenceVerifier(
            validation_root=output,
            context=config.context,
            runner_verifier_anchor=config.runner_verifier_anchor,
            authorization_verifier_anchor=config.authorization_verifier_anchor,
        ).verify_run(directory=result.directory)


def test_formal_reader_rejects_claim_after_execution_even_when_reissued(tmp_path: Path) -> None:
    """The authorized receipt signer cannot bypass the claim-to-execution ordering rule."""
    authorization_file, runtime, trust, output = _issued(tmp_path)
    result = runtime.execute(authorization_file=authorization_file)
    previous = RealClaimReceipt.from_bytes(
        (result.directory / "authorization" / "claim-receipt.json").read_bytes()
    )
    attestation = RealResultAttestation.from_bytes(
        (result.directory / "runner" / "result-attestation.real.v2.json").read_bytes()
    )
    late_receipt = RealClaimReceiptIssuer(
        private_key_file=tmp_path / "keys" / "authorization-private.pem"
    ).issue(
        authorization_id=previous.authorization_id,
        invocation_id=previous.invocation_id,
        run_id=previous.run_id,
        claimed_at=UtcTimestamp(attestation.ended_at.value + timedelta(seconds=1)),
        claim_nonce=previous.claim_nonce,
        authorization_envelope_sha256=previous.authorization_envelope_sha256,
        plan_sha256=previous.plan_sha256,
    )
    _rewrite_test_only_run(
        key_root=tmp_path,
        directory=result.directory,
        replacements={"authorization/claim-receipt.json": late_receipt.to_bytes()},
    )

    config = trust.resolve()
    with pytest.raises(ValidationEvidenceVerificationError):
        RealValidationEvidenceVerifier(
            validation_root=output,
            context=config.context,
            runner_verifier_anchor=config.runner_verifier_anchor,
            authorization_verifier_anchor=config.authorization_verifier_anchor,
        ).verify_run(directory=result.directory)


def test_only_success_construction_is_rejected_by_formal_runtime_and_reader(tmp_path: Path) -> None:
    """Only test code can form this malicious success-shaped artifact."""
    authorization_file, runtime, _trust, _output = _issued(tmp_path)

    class TestOnlySuccessRunner:
        def execute(self, **kwargs: object) -> Path:
            plan = kwargs["plan"]
            output_root = kwargs["output_root"]
            assert hasattr(plan, "evidence_inputs")
            assert isinstance(output_root, Path)
            started_at = utc_now()
            ended_at = utc_now()
            while ended_at.value <= started_at.value:
                sleep(0.001)
                ended_at = utc_now()
            directory = RealValidationEvidenceWriter(validation_root=output_root).write_failure(
                inputs=plan.evidence_inputs(),  # type: ignore[union-attr]
                failure=ValidationFailure(category="CAPABILITY_UNSUPPORTED", retryable=False),
                started_at=started_at,
                ended_at=ended_at,
            )
            _rewrite_test_only_run(
                key_root=tmp_path,
                directory=directory,
                replacements={},
                attestation_mutator=lambda raw: raw.update(
                    {"attempt_outcome": "SUCCEEDED", "failure": None}
                ),
                manifest_mutator=lambda raw: raw.update({"attempt_outcome": "SUCCEEDED"}),
            )
            return directory

    runtime._import_runner = lambda _name: SimpleNamespace(  # type: ignore[attr-defined]
        create_runner=lambda: TestOnlySuccessRunner()
    )
    with pytest.raises(RealXtQuantExecutionError) as captured:
        runtime.execute(authorization_file=authorization_file)
    assert captured.value.code == "REAL_RUNNER_EVIDENCE_INVALID"
    assert not hasattr(RealValidationEvidenceWriter, "write_success")


def test_missing_or_replaced_lineage_fails_persist_and_formal_read(tmp_path: Path) -> None:
    authorization_file, runtime, trust, output = _issued(tmp_path)
    result = runtime.execute(authorization_file=authorization_file)
    assessment_dir = result.directory.parent
    (result.directory / "request" / "plan.json").unlink()
    database = tmp_path / "metadata.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    try:
        config = trust.resolve()
        service = RealXtQuantAssessmentService(
            validation_root=output,
            trust_resolver=trust,
            persistence=SqliteRealSourceAssessmentPersistenceAdapter(
                engine=engine,
                validation_root=output,
                context=config.context,
                runner_verifier_anchor=config.runner_verifier_anchor,
                authorization_verifier_anchor=config.authorization_verifier_anchor,
            ),
        )
        with pytest.raises(RealXtQuantExecutionError):
            service.finalize_and_persist(assessment_directory=assessment_dir)
    finally:
        engine.dispose()

    authorization_file, runtime, trust, output = _issued(tmp_path / "receipt")
    result = runtime.execute(authorization_file=authorization_file)
    receipt = result.directory / "authorization" / "claim-receipt.json"
    receipt.write_bytes(receipt.read_bytes().replace(b'"plan_sha256"', b'"plan_sha25x"'))
    with pytest.raises(ValidationEvidenceVerificationError):
        RealValidationEvidenceVerifier(
            validation_root=output,
            context=trust.resolve().context,
            runner_verifier_anchor=trust.resolve().runner_verifier_anchor,
            authorization_verifier_anchor=trust.resolve().authorization_verifier_anchor,
        ).verify_run(directory=result.directory)


def test_real_blocked_evidence_seals_persists_and_requires_explicit_read_anchor(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "runtime-data"
    authorization_file, runtime, trust, output = _issued(data_root, output_root=data_root)
    result = runtime.execute(authorization_file=authorization_file)
    settings = AppSettings(
        paths=RuntimePaths(
            code_root=Path.cwd(), data_root=data_root, offsite_backup_root=tmp_path / "offsite"
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
    engine = create_data_engine(data_root / "metadata" / "quantking.db", busy_timeout_ms=250)
    try:
        config = trust.resolve()
        saved = RealXtQuantAssessmentService(
            validation_root=output,
            trust_resolver=trust,
            persistence=SqliteRealSourceAssessmentPersistenceAdapter(
                engine=engine,
                validation_root=output,
                context=config.context,
                runner_verifier_anchor=config.runner_verifier_anchor,
                authorization_verifier_anchor=config.authorization_verifier_anchor,
            ),
        ).finalize_and_persist(assessment_directory=result.directory.parent)
        assert saved.evidence_kind is EvidenceKind.REAL
        assert saved.status is AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION
        reader = SealedAssessmentReadVerifier(
            validation_root=output,
            signer=FakeRunnerTrustStore(),
            signer_trust_policy=SignerTrustPolicy.synthetic_only(),
            real_trust_resolver=trust,
        )
        with DataSqliteUnitOfWorkFactory(engine, assessment_read_verifier=reader)() as uow:
            assert uow.data.get_source_candidate_assessment(str(saved.assessment_id)) == saved
        with without_real.data_uow_factory() as uow, pytest.raises(InvalidDataTransitionError):
            uow.data.get_source_candidate_assessment(str(saved.assessment_id))
        with_real = build_eod_runtime(settings, real_trust_resolver=trust)
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
        (
            data_root
            / "authority"
            / "reference-bundles"
            / "st-603377-authority-v3"
            / "manifest.json"
        ).write_bytes(b"tampered")
        with with_real.data_uow_factory() as uow, pytest.raises(InvalidDataTransitionError):
            uow.data.get_source_candidate_assessment(str(saved.assessment_id))
    finally:
        engine.dispose()
