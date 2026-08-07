"""S1-07C0B offline, Fake-provider controlled execution path.

This module has no REAL runner, vendor import, environment authorization, or
network behavior.  It consumes a strict, signed authorization *file*, performs
all validation before the irreversible claim, then delayed-imports exactly one
bundled Fake runner.  Every post-claim error leaves either immutable v3
evidence or a terminal incomplete record; claims are never deleted.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from quantking.common.paths import is_reparse_point
from quantking.common.time import utc_now
from quantking.data.adapters.warehouse.manifest import compute_sha256, path_safe_id
from quantking.data.assessment.models import EvidenceKind
from quantking.data.models import DatasetKind
from quantking.data.schemas import descriptor_for
from quantking.data.validation.artifacts_v3 import (
    V3EvidenceTrustContext,
    ValidationEvidenceVerifierV3,
    ValidationEvidenceWriterV3,
)
from quantking.data.validation.authority_bundle import (
    CONTROLLED_AUTHORITY_BUNDLE_ID,
    CONTROLLED_AUTHORITY_BUNDLE_MANIFEST_SHA256,
    AuthorityBundleVerificationError,
    AuthorityReferenceBundleVerifier,
)
from quantking.data.validation.authorization import (
    AuthorizationClaimError,
    AuthorizationClaimStore,
    AuthorizationVerifier,
    ExecutionAuthorization,
)
from quantking.data.validation.contracts_v3 import (
    AttemptOutcomeV3,
    FakeV3RunnerTrustStore,
    PromotionProfileV3,
    SourceDatasetContractV3,
    ValidationAttemptIdentityV3,
)
from quantking.data.validation.models import ValidationVersions
from quantking.data.validation.runtime import (
    LoadedRuntimeAuthorizationEnvelope,
    RuntimeAuthorizationEnvelope,
    RuntimeAuthorizationFileLoader,
    RuntimeAuthorizationFileVerifier,
)
from quantking.data.validation.samples import CaseEligibility, load_fixed_sample_v4

_FAKE_RUNNER_MODULE = "quantking.data.validation.runners.fake"
_FAKE_RUNNER_FACTORY = "create_runner"
_FAKE_RUNNER_ADAPTER_VERSION = "fake-provider.s1-07c0b.v1"
_FAKE_RUNNER_PACKAGE_VERSION = "1.0.0"
_CONTROLLED_FAKE_SOURCE = "XTQUANT"
_CONTROLLED_FAKE_DATASET = DatasetKind.TRADING_STATUS
_CONTROLLED_FAKE_CASE_IDS = ("st_warning_603377_sh",)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


class ControlledExecutionError(Exception):
    """Sanitized C0B execution error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ControlledV3RunnerTrustPolicy:
    """Production C0B key-id/profile allowlist.

    The only shipped key is the deterministic Fake key and it always derives
    ``SYNTHETIC``.  REAL keys are intentionally absent; C0B test fixtures may
    implement the narrow ``V3RunnerTrustPolicy`` protocol independently, but
    they cannot alter this production allowlist.
    """

    _PRODUCTION_PROFILES = MappingProxyType({FakeV3RunnerTrustStore.KEY_ID: EvidenceKind.SYNTHETIC})

    def evidence_kind_for(self, *, attestation_key_id: str, verifier_key_id: str) -> EvidenceKind:
        if attestation_key_id != verifier_key_id:
            raise ValueError("signer and verifier key IDs do not match")
        profile = self._PRODUCTION_PROFILES.get(attestation_key_id)
        if profile is None:
            raise ValueError("runner key is not in the production allowlist")
        return profile


@dataclass(frozen=True, slots=True)
class ControlledFakeV3TrustConfiguration:
    """Resolved read/write trust from the S1-07B authority bundle only."""

    context: V3EvidenceTrustContext
    signer: FakeV3RunnerTrustStore
    runner_trust_policy: ControlledV3RunnerTrustPolicy


class ControlledFakeV3TrustResolver:
    """Re-verifies the controlled bundle every time a v3 row is read.

    The resolver contains no artifact-supplied trust information and no REAL
    profile.  A missing, altered, or non-approved authority bundle therefore
    makes the v3 reader fail closed.
    """

    def __init__(self, *, reference_bundle_root: Path) -> None:
        if not isinstance(reference_bundle_root, Path) or not reference_bundle_root.is_absolute():
            raise ValueError("reference bundle root must be an absolute Path")
        self._root = reference_bundle_root

    def resolve(self) -> ControlledFakeV3TrustConfiguration:
        return ControlledFakeV3TrustConfiguration(
            context=build_controlled_fake_context(reference_bundle_root=self._root),
            signer=FakeV3RunnerTrustStore(),
            runner_trust_policy=ControlledV3RunnerTrustPolicy(),
        )


def build_controlled_fake_context(*, reference_bundle_root: Path) -> V3EvidenceTrustContext:
    """Build the only production C0B context from an S1-07B verified bundle.

    This intentionally does not consume ``testing_v3.build_v3_context``.  The
    sole permitted Fake attempt is the locally approved ST case; inherited
    framework-only calendar cases cannot satisfy this function or preflight.
    """

    if not isinstance(reference_bundle_root, Path) or not reference_bundle_root.is_absolute():
        raise ValueError("reference bundle root must be an absolute Path")
    if reference_bundle_root.exists() and is_reparse_point(reference_bundle_root):
        raise ValueError("reference bundle root is unsafe")
    sample = load_fixed_sample_v4()
    directory = reference_bundle_root / "reference-bundles" / CONTROLLED_AUTHORITY_BUNDLE_ID
    try:
        verified = AuthorityReferenceBundleVerifier(
            root=reference_bundle_root, record_failures=False
        ).verify(
            directory=directory,
            sample=sample,
        )
    except AuthorityBundleVerificationError as error:
        raise ValueError("controlled authority bundle is invalid") from error
    if (
        verified.bundle_id != CONTROLLED_AUTHORITY_BUNDLE_ID
        or verified.manifest_sha256 != CONTROLLED_AUTHORITY_BUNDLE_MANIFEST_SHA256
        or any(
            verified.case_eligibilities.get(case_id) is not CaseEligibility.AUTHORITY_APPROVED
            for case_id in _CONTROLLED_FAKE_CASE_IDS
        )
    ):
        raise ValueError("controlled authority bundle is not eligible")
    return V3EvidenceTrustContext(
        sample=sample,
        source_dataset_contract=SourceDatasetContractV3(
            source=_CONTROLLED_FAKE_SOURCE,
            dataset=_CONTROLLED_FAKE_DATASET,
            contract_version="xtquant-trading-status-contract.v1",
            semantic_rule_version="semantic-contract-rules.v1",
            unit_rule_version="unit-contract-rules.v1",
            coverage_rule_version="coverage-rules.v1",
            expected_schema_version=descriptor_for(_CONTROLLED_FAKE_DATASET.value).schema_version,
        ),
        promotion_profile=PromotionProfileV3(
            source=_CONTROLLED_FAKE_SOURCE,
            dataset=_CONTROLLED_FAKE_DATASET,
            promotion_profile_version="xtquant-trading-status-promotion.v1",
            promotion_config_version="controlled-fake-validation-config.v1",
            required_case_ids=_CONTROLLED_FAKE_CASE_IDS,
            reference_bundle_id=verified.bundle_id,
            reference_bundle_manifest_sha256=verified.manifest_sha256,
        ),
    )


class ControlledFakeRunner(Protocol):
    def execute(self, *, plan: ControlledFakeRunPlan, output_root: Path) -> Path: ...


class ControlledAssessmentPersistence(Protocol):
    """Narrow port: only a managed directory can cross into persistence."""

    def persist_from_directory(self, absolute_assessment_directory: Path) -> object: ...


@dataclass(frozen=True, slots=True)
class ControlledFakeRunPlan:
    """Fully frozen in-memory plan; it has no caller-selected import path."""

    source: str
    dataset: DatasetKind
    output_root: Path
    identity: ValidationAttemptIdentityV3
    selected_case_ids: tuple[str, ...]
    context: V3EvidenceTrustContext
    versions: ValidationVersions
    plan_sha256: str

    @property
    def output_directory(self) -> Path:
        return (
            self.output_root
            / "source-assessments-v3"
            / self.source.lower()
            / self.dataset.value
            / path_safe_id(str(self.identity.assessment_id))
            / path_safe_id(str(self.identity.run_id))
        )


@dataclass(frozen=True, slots=True)
class ControlledExecutionResult:
    directory: Path
    event_order: tuple[str, ...]
    outcome: AttemptOutcomeV3


class FakeRuntimeAuthorizationAuthority(RuntimeAuthorizationFileVerifier):
    """Test fixture authority only; it cannot issue or validate REAL identity."""

    _KEY = b"quantking-s1-07c0b-fake-runtime-authorization-v1"

    def issue(
        self,
        *,
        authorization: ExecutionAuthorization,
        plan_sha256: str,
        frozen_versions: Mapping[str, str],
        identity: ValidationAttemptIdentityV3,
        selected_case_ids: tuple[str, ...],
        context: V3EvidenceTrustContext,
        claim_nonce: str,
    ) -> RuntimeAuthorizationEnvelope:
        unsigned = RuntimeAuthorizationEnvelope(
            authorization=authorization,
            plan_sha256=plan_sha256,
            frozen_versions=MappingProxyType(dict(frozen_versions)),
            signature="0" * 64,
            schema_version="runtime-authorization-envelope.v2",
            purpose="S1-07C0B_FAKE_PROVIDER",
            identity=identity,
            selected_case_ids=selected_case_ids,
            reference_bundle_id=context.promotion_profile.reference_bundle_id,
            reference_bundle_sha256=(context.promotion_profile.reference_bundle_manifest_sha256),
            source_dataset_contract=context.source_dataset_contract,
            promotion_profile=context.promotion_profile,
            output_scope=authorization.output_root,
            claim_nonce=claim_nonce,
        )
        signature = hmac.new(
            self._KEY,
            _canonical(unsigned.unsigned_payload()),
            hashlib.sha256,
        ).hexdigest()
        return RuntimeAuthorizationEnvelope(
            authorization=unsigned.authorization,
            plan_sha256=unsigned.plan_sha256,
            frozen_versions=unsigned.frozen_versions,
            signature=signature,
            schema_version=unsigned.schema_version,
            purpose=unsigned.purpose,
            identity=unsigned.identity,
            selected_case_ids=unsigned.selected_case_ids,
            reference_bundle_id=unsigned.reference_bundle_id,
            reference_bundle_sha256=unsigned.reference_bundle_sha256,
            source_dataset_contract=unsigned.source_dataset_contract,
            promotion_profile=unsigned.promotion_profile,
            output_scope=unsigned.output_scope,
            claim_nonce=unsigned.claim_nonce,
        )

    def verify_file_authorization(self, envelope: RuntimeAuthorizationEnvelope) -> bool:
        if not envelope.is_strict_file_authorization:
            return False
        expected = hmac.new(
            self._KEY, _canonical(envelope.unsigned_payload()), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, envelope.signature)


class ControlledFakeValidationRuntime:
    """The sole C0B executable path; accepts no authorization DTO or env var."""

    def __init__(
        self,
        *,
        reference_bundle_root: Path,
        authorization_file_loader: RuntimeAuthorizationFileLoader,
        authorization_verifier: AuthorizationVerifier,
        claim_store: AuthorizationClaimStore,
        import_runner: Callable[[str], object] = importlib.import_module,
    ) -> None:
        if not isinstance(reference_bundle_root, Path) or not reference_bundle_root.is_absolute():
            raise ValueError("reference bundle root must be an absolute Path")
        self._reference_bundle_root = reference_bundle_root
        self._file_loader = authorization_file_loader
        self._authorization_verifier = authorization_verifier
        self._claim_store = claim_store
        self._import_runner = import_runner

    def execute(self, *, authorization_file: Path) -> ControlledExecutionResult:
        """Execute one Fake attempt in the asserted precheck→claim→import→output order."""
        try:
            loaded = self._file_loader.load(authorization_file)
        except ValueError as error:
            raise ControlledExecutionError(
                "RUNTIME_AUTHORIZATION_INVALID", "runtime authorization was rejected"
            ) from error
        plan = self._preflight(loaded)
        events = ["PRECHECK"]
        envelope = loaded.envelope
        try:
            self._claim_store.claim(
                envelope.authorization,
                invocation_id=plan.identity.invocation_id,
                verifier=self._authorization_verifier,
                source=plan.source,
                dataset=plan.dataset,
                output_root=plan.output_root,
                sample_sha256=plan.context.sample.sample_spec_sha256,
                at=utc_now(),
            )
        except AuthorizationClaimError as error:
            raise ControlledExecutionError(
                str(error), "runtime authorization claim was rejected"
            ) from None
        events.append("CLAIM")
        try:
            module = self._import_runner(_FAKE_RUNNER_MODULE)
            factory = getattr(module, _FAKE_RUNNER_FACTORY)
            runner = factory()
            if not hasattr(runner, "execute"):
                raise TypeError("fixed Fake runner is invalid")
        except Exception:
            self._write_incomplete(plan, "FAKE_RUNNER_IMPORT_FAILED")
            raise ControlledExecutionError(
                "FAKE_RUNNER_IMPORT_FAILED", "controlled Fake runner could not be loaded"
            ) from None
        events.append("IMPORT")
        try:
            directory = runner.execute(plan=plan, output_root=plan.output_root)
        except Exception:
            self._write_incomplete(plan, "FAKE_RUNNER_CRASHED")
            raise ControlledExecutionError(
                "FAKE_RUNNER_CRASHED", "controlled Fake runner failed after claim"
            ) from None
        if directory != plan.output_directory or not directory.is_dir():
            self._write_incomplete(plan, "FAKE_RUNNER_OUTPUT_INVALID")
            raise ControlledExecutionError(
                "FAKE_RUNNER_OUTPUT_INVALID", "controlled Fake runner output is invalid"
            )
        events.append("OUTPUT")
        try:
            verified = ValidationEvidenceVerifierV3(
                validation_root=plan.output_root,
                context=plan.context,
                runner_attestation_verifier=FakeV3RunnerTrustStore(),
                runner_trust_policy=ControlledV3RunnerTrustPolicy(),
            ).verify_run(directory=directory)
        except Exception:
            self._write_incomplete(plan, "FAKE_RUNNER_EVIDENCE_INVALID")
            raise ControlledExecutionError(
                "FAKE_RUNNER_EVIDENCE_INVALID", "controlled Fake evidence is invalid"
            ) from None
        if verified.identity != plan.identity:
            self._write_incomplete(plan, "FAKE_RUNNER_IDENTITY_MISMATCH")
            raise ControlledExecutionError(
                "FAKE_RUNNER_IDENTITY_MISMATCH", "controlled Fake evidence identity is invalid"
            )
        return ControlledExecutionResult(
            directory=directory,
            event_order=tuple(events),
            outcome=verified.outcome,
        )

    def _preflight(self, loaded: LoadedRuntimeAuthorizationEnvelope) -> ControlledFakeRunPlan:
        envelope = loaded.envelope
        assert envelope.identity is not None
        assert envelope.selected_case_ids is not None
        assert envelope.source_dataset_contract is not None
        assert envelope.promotion_profile is not None
        assert envelope.output_scope is not None
        output_root = Path(envelope.output_scope)
        _validate_output_root(output_root)
        try:
            context = build_controlled_fake_context(
                reference_bundle_root=self._reference_bundle_root
            )
        except ValueError:
            raise ControlledExecutionError(
                "AUTHORITY_BUNDLE_INVALID",
                "controlled authority bundle is unavailable or invalid",
            ) from None
        _verify_preclaim_runner_readiness(context)
        selected_cases = {case.case_id: case for case in context.sample.cases}
        if any(
            (case := selected_cases.get(case_id)) is None
            or case.dataset is not _CONTROLLED_FAKE_DATASET
            or _CONTROLLED_FAKE_SOURCE not in case.applicable_sources
            or case_id not in _CONTROLLED_FAKE_CASE_IDS
            for case_id in envelope.selected_case_ids
        ):
            raise ControlledExecutionError(
                "CASE_NOT_AUTHORITY_APPROVED",
                "selected case is not authority-approved for controlled execution",
            )
        expected_versions = _versions_for(context)
        if (
            envelope.authorization.source != context.source_dataset_contract.source
            or envelope.authorization.dataset is not context.source_dataset_contract.dataset
            or envelope.authorization.sample_sha256 != context.sample.sample_spec_sha256
            or envelope.source_dataset_contract != context.source_dataset_contract
            or envelope.promotion_profile != context.promotion_profile
            or envelope.reference_bundle_id != context.promotion_profile.reference_bundle_id
            or envelope.reference_bundle_sha256
            != context.promotion_profile.reference_bundle_manifest_sha256
            or envelope.selected_case_ids != tuple(sorted(envelope.selected_case_ids))
            or dict(envelope.frozen_versions) != expected_versions
        ):
            raise ControlledExecutionError(
                "RUNTIME_AUTHORIZATION_BINDING_MISMATCH",
                "runtime authorization does not match the controlled plan",
            )
        versions = ValidationVersions(**expected_versions)
        plan_sha = _plan_sha(
            source=envelope.authorization.source,
            dataset=envelope.authorization.dataset,
            output_root=output_root,
            identity=envelope.identity,
            selected_case_ids=envelope.selected_case_ids,
            context=context,
            versions=versions,
        )
        if envelope.plan_sha256 != plan_sha:
            raise ControlledExecutionError(
                "RUNTIME_AUTHORIZATION_PLAN_MISMATCH",
                "runtime authorization plan does not match the controlled plan",
            )
        plan = ControlledFakeRunPlan(
            source=envelope.authorization.source,
            dataset=envelope.authorization.dataset,
            output_root=output_root,
            identity=envelope.identity,
            selected_case_ids=envelope.selected_case_ids,
            context=context,
            versions=versions,
            plan_sha256=plan_sha,
        )
        if (
            plan.output_directory.exists()
            or plan.output_directory.with_name(plan.output_directory.name + ".incomplete").exists()
        ):
            raise ControlledExecutionError(
                "RUN_ID_ALREADY_EXISTS", "controlled run identity already has output evidence"
            )
        return plan

    def _write_incomplete(self, plan: ControlledFakeRunPlan, category: str) -> None:
        target = plan.output_directory.with_name(plan.output_directory.name + ".incomplete")
        try:
            target.mkdir(parents=True, exist_ok=False)
            (target / "failure.json").write_bytes(
                _canonical(
                    {
                        "category": category,
                        "identity": plan.identity.to_dict(),
                        "plan_sha256": plan.plan_sha256,
                        "retryable": False,
                    }
                )
            )
        except OSError:
            # The authorization claim is deliberately retained even if failure
            # evidence persistence itself is unavailable.
            return


class ControlledFakeAssessmentService:
    """C0B's Fake-only run → v3 summary → sealed persistence vertical slice."""

    def __init__(
        self,
        *,
        validation_root: Path,
        reference_bundle_root: Path,
        persistence: ControlledAssessmentPersistence,
    ) -> None:
        if not isinstance(validation_root, Path) or not validation_root.is_absolute():
            raise ValueError("validation root must be an absolute Path")
        if not isinstance(reference_bundle_root, Path) or not reference_bundle_root.is_absolute():
            raise ValueError("reference bundle root must be an absolute Path")
        self._root = validation_root
        self._reference_bundle_root = reference_bundle_root
        self._persistence = persistence

    def finalize_and_persist(self, *, assessment_directory: Path) -> object:
        if not isinstance(assessment_directory, Path) or not assessment_directory.is_absolute():
            raise ControlledExecutionError(
                "ASSESSMENT_DIRECTORY_UNSAFE", "assessment directory is unsafe"
            )
        try:
            assessment_directory.relative_to(self._root)
        except ValueError:
            raise ControlledExecutionError(
                "ASSESSMENT_DIRECTORY_UNSAFE", "assessment directory is unsafe"
            ) from None
        try:
            context = build_controlled_fake_context(
                reference_bundle_root=self._reference_bundle_root
            )
        except ValueError:
            raise ControlledExecutionError(
                "AUTHORITY_BUNDLE_INVALID",
                "controlled authority bundle is unavailable or invalid",
            ) from None
        verifier = ValidationEvidenceVerifierV3(
            validation_root=self._root,
            context=context,
            runner_attestation_verifier=FakeV3RunnerTrustStore(),
            runner_trust_policy=ControlledV3RunnerTrustPolicy(),
        )
        try:
            ValidationEvidenceWriterV3(validation_root=self._root).finalize_assessment(
                assessment_directory=assessment_directory,
                verifier=verifier,
            )
            return self._persistence.persist_from_directory(assessment_directory)
        except Exception:
            raise ControlledExecutionError(
                "ASSESSMENT_PERSISTENCE_FAILED", "controlled assessment persistence failed"
            ) from None


def build_controlled_fake_plan_sha(
    *,
    source: str,
    dataset: DatasetKind,
    output_root: Path,
    identity: ValidationAttemptIdentityV3,
    selected_case_ids: tuple[str, ...],
    context: V3EvidenceTrustContext,
) -> str:
    """Public pure helper for issuing a matching test-only authorization file."""
    return _plan_sha(
        source=source,
        dataset=dataset,
        output_root=output_root,
        identity=identity,
        selected_case_ids=selected_case_ids,
        context=context,
        versions=ValidationVersions(**_versions_for(context)),
    )


def _versions_for(context: V3EvidenceTrustContext) -> dict[str, str]:
    return {
        "adapter_version": _FAKE_RUNNER_ADAPTER_VERSION,
        "package_version": _FAKE_RUNNER_PACKAGE_VERSION,
        "code_version": "s1-07c0b.runtime.v1",
        "schema_version": context.source_dataset_contract.expected_schema_version,
        "rule_version": "quality-rules.v1",
        "threshold_version": "comparison-thresholds.v1",
        "sample_spec_version": context.sample.sample_spec_version,
        "config_version": context.promotion_profile.promotion_config_version,
    }


def _verify_preclaim_runner_readiness(context: V3EvidenceTrustContext) -> None:
    """Check the immutable runner descriptor without importing its module."""

    if (
        _FAKE_RUNNER_MODULE != "quantking.data.validation.runners.fake"
        or _FAKE_RUNNER_FACTORY != "create_runner"
        or not _FAKE_RUNNER_ADAPTER_VERSION
        or not _FAKE_RUNNER_PACKAGE_VERSION
        or context.source_dataset_contract.source != _CONTROLLED_FAKE_SOURCE
        or context.source_dataset_contract.dataset is not _CONTROLLED_FAKE_DATASET
    ):
        raise ControlledExecutionError(
            "FAKE_RUNNER_NOT_READY", "fixed controlled Fake runner is not ready"
        )
    try:
        evidence_kind = ControlledV3RunnerTrustPolicy().evidence_kind_for(
            attestation_key_id=FakeV3RunnerTrustStore.KEY_ID,
            verifier_key_id=FakeV3RunnerTrustStore.KEY_ID,
        )
    except ValueError:
        raise ControlledExecutionError(
            "FAKE_SIGNER_NOT_READY", "fixed controlled Fake signer is not ready"
        ) from None
    if evidence_kind is not EvidenceKind.SYNTHETIC:
        raise ControlledExecutionError(
            "FAKE_SIGNER_NOT_READY", "fixed controlled Fake signer is not synthetic"
        )


def _plan_sha(
    *,
    source: str,
    dataset: DatasetKind,
    output_root: Path,
    identity: ValidationAttemptIdentityV3,
    selected_case_ids: tuple[str, ...],
    context: V3EvidenceTrustContext,
    versions: ValidationVersions,
) -> str:
    return compute_sha256(
        _canonical(
            {
                "case_ids": list(selected_case_ids),
                "dataset": dataset.value,
                "identity": identity.to_dict(),
                "output_scope": str(output_root),
                "promotion_profile": context.promotion_profile.to_dict(),
                "source": source,
                "source_dataset_contract": context.source_dataset_contract.to_dict(),
                "versions": versions.to_dict(),
            }
        )
    )


def _validate_output_root(root: Path) -> None:
    if not root.is_absolute() or ".." in root.parts:
        raise ControlledExecutionError("OUTPUT_ROOT_UNSAFE", "output scope is unsafe")
    current = Path(root.anchor)
    try:
        for part in root.parts[1:]:
            current /= part
            if current.exists() and is_reparse_point(current):
                raise ControlledExecutionError("OUTPUT_ROOT_UNSAFE", "output scope is unsafe")
    except OSError:
        raise ControlledExecutionError("OUTPUT_ROOT_UNSAFE", "output scope is unsafe") from None


__all__ = [
    "ControlledExecutionError",
    "ControlledExecutionResult",
    "ControlledFakeAssessmentService",
    "ControlledFakeRunPlan",
    "ControlledFakeValidationRuntime",
    "ControlledV3RunnerTrustPolicy",
    "FakeRuntimeAuthorizationAuthority",
    "build_controlled_fake_plan_sha",
]
