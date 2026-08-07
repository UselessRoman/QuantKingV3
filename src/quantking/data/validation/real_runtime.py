"""S1-07C1A's fixed, offline-testable XTQuant REAL control chain.

The code establishes authorization, signer, runner, evidence, persistence, and
read-side boundaries only.  It does not perform a supplier request.  The fixed
``XTQUANT x trading_status x st_warning_603377_sh`` adapter remains incapable
of historical status validation and therefore emits a trusted REAL/BLOCKED
``CAPABILITY_UNSUPPORTED`` attempt after a valid claim.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from quantking.common.paths import is_reparse_point
from quantking.common.time import utc_now
from quantking.data.adapters.sources.xtquant_metadata import (
    ExplicitXtQuantPackageMetadataProbe,
    XtQuantPackageMetadataError,
)
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
)
from quantking.data.validation.contracts_v3 import (
    AttemptOutcomeV3,
    PromotionProfileV3,
    SourceDatasetContractV3,
    ValidationAttemptIdentityV3,
)
from quantking.data.validation.models import ValidationVersions
from quantking.data.validation.real_authorization import (
    LoadedRealRuntimeAuthorization,
    LocalRealV3Signer,
    RealAuthorizationError,
    RealRuntimeAuthorizationFileLoader,
    RealV3RunnerTrustPolicy,
)
from quantking.data.validation.samples import CaseEligibility, load_fixed_sample_v4

_RUNNER_MODULE = "quantking.data.validation.runners.xtquant_real"
_RUNNER_FACTORY = "create_runner"
_RUNNER_ADAPTER_VERSION = "xtquant-readonly.s1-07c1a.v1"
_RUNNER_CODE_VERSION = "s1-07c1a.xtquant.runtime.v1"
_RUNNER_RULE_VERSION = "quality-rules.v1"
_RUNNER_THRESHOLD_VERSION = "comparison-thresholds.v1"
_SOURCE = "XTQUANT"
_DATASET = DatasetKind.TRADING_STATUS
_CASE_IDS = ("st_warning_603377_sh",)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


class RealXtQuantExecutionError(Exception):
    """Stable, sanitized C1A execution failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RealXtQuantTrustConfiguration:
    """Verifier-owned context and signer; no artifact supplies this trust."""

    context: V3EvidenceTrustContext
    signer: LocalRealV3Signer
    runner_trust_policy: RealV3RunnerTrustPolicy


class RealXtQuantTrustResolver(Protocol):
    def resolve(self) -> RealXtQuantTrustConfiguration: ...


class ExplicitRealXtQuantTrustResolver:
    """Rebuild REAL trust from an authority bundle and absolute key file on each use."""

    def __init__(
        self,
        *,
        reference_bundle_root: Path,
        runner_key_id: str,
        runner_private_key_file: Path,
    ) -> None:
        if not isinstance(reference_bundle_root, Path) or not reference_bundle_root.is_absolute():
            raise ValueError("reference bundle root must be an absolute Path")
        self._reference_bundle_root = reference_bundle_root
        self._runner_key_id = runner_key_id
        self._runner_private_key_file = runner_private_key_file

    def resolve(self) -> RealXtQuantTrustConfiguration:
        signer = LocalRealV3Signer(
            key_id=self._runner_key_id,
            private_key_file=self._runner_private_key_file,
        )
        return RealXtQuantTrustConfiguration(
            context=build_real_xtquant_context(reference_bundle_root=self._reference_bundle_root),
            signer=signer,
            runner_trust_policy=RealV3RunnerTrustPolicy(
                verifier_key_ids=frozenset({signer.key_id})
            ),
        )


def build_real_xtquant_context(*, reference_bundle_root: Path) -> V3EvidenceTrustContext:
    """Build C1A's REAL-only context from the accepted per-case authority bundle."""
    if not isinstance(reference_bundle_root, Path) or not reference_bundle_root.is_absolute():
        raise ValueError("reference bundle root must be an absolute Path")
    if reference_bundle_root.exists() and is_reparse_point(reference_bundle_root):
        raise ValueError("reference bundle root is unsafe")
    sample = load_fixed_sample_v4()
    directory = reference_bundle_root / "reference-bundles" / CONTROLLED_AUTHORITY_BUNDLE_ID
    try:
        verified = AuthorityReferenceBundleVerifier(
            root=reference_bundle_root, record_failures=False
        ).verify(directory=directory, sample=sample)
    except AuthorityBundleVerificationError as error:
        raise ValueError("REAL authority bundle is invalid") from error
    if (
        verified.bundle_id != CONTROLLED_AUTHORITY_BUNDLE_ID
        or verified.manifest_sha256 != CONTROLLED_AUTHORITY_BUNDLE_MANIFEST_SHA256
        or verified.case_eligibilities.get(_CASE_IDS[0]) is not CaseEligibility.AUTHORITY_APPROVED
    ):
        raise ValueError("REAL authority bundle is not eligible")
    return V3EvidenceTrustContext(
        sample=sample,
        source_dataset_contract=SourceDatasetContractV3(
            source=_SOURCE,
            dataset=_DATASET,
            contract_version="xtquant-trading-status-real-contract.c1a.v1",
            semantic_rule_version="semantic-contract-rules.v1",
            unit_rule_version="unit-contract-rules.v1",
            coverage_rule_version="coverage-rules.v1",
            expected_schema_version=descriptor_for(_DATASET.value).schema_version,
        ),
        promotion_profile=PromotionProfileV3(
            source=_SOURCE,
            dataset=_DATASET,
            promotion_profile_version="xtquant-trading-status-real-promotion.c1a.v1",
            promotion_config_version="real-validation-config.c1a.v1",
            required_case_ids=_CASE_IDS,
            reference_bundle_id=verified.bundle_id,
            reference_bundle_manifest_sha256=verified.manifest_sha256,
        ),
    )


@dataclass(frozen=True, slots=True)
class RealXtQuantValidationPlan:
    """Fully frozen fixed-unit plan used both to issue and preflight an envelope."""

    output_root: Path
    identity: ValidationAttemptIdentityV3
    context: V3EvidenceTrustContext
    versions: ValidationVersions
    runner_key_id: str
    plan_sha256: str

    @property
    def source(self) -> str:
        return _SOURCE

    @property
    def dataset(self) -> DatasetKind:
        return _DATASET

    @property
    def selected_case_ids(self) -> tuple[str, ...]:
        return _CASE_IDS

    @property
    def output_directory(self) -> Path:
        return (
            self.output_root
            / "source-assessments-v3"
            / _SOURCE.lower()
            / _DATASET.value
            / path_safe_id(str(self.identity.assessment_id))
            / path_safe_id(str(self.identity.run_id))
        )


def build_real_xtquant_plan(
    *,
    output_root: Path,
    identity: ValidationAttemptIdentityV3,
    context: V3EvidenceTrustContext,
    package_version: str,
    runner_key_id: str,
) -> RealXtQuantValidationPlan:
    """Pure builder for a precise C1A plan before an envelope is issued."""
    _validate_output_root(output_root)
    if (
        context.source_dataset_contract.source != _SOURCE
        or context.source_dataset_contract.dataset is not _DATASET
        or context.promotion_profile.required_case_ids != _CASE_IDS
        or not isinstance(package_version, str)
        or not package_version
    ):
        raise ValueError("REAL fixed plan inputs are invalid")
    versions = ValidationVersions(
        adapter_version=_RUNNER_ADAPTER_VERSION,
        package_version=package_version,
        code_version=_RUNNER_CODE_VERSION,
        schema_version=context.source_dataset_contract.expected_schema_version,
        rule_version=_RUNNER_RULE_VERSION,
        threshold_version=_RUNNER_THRESHOLD_VERSION,
        sample_spec_version=context.sample.sample_spec_version,
        config_version=context.promotion_profile.promotion_config_version,
    )
    plan_sha = compute_sha256(
        _canonical(
            {
                "case_ids": list(_CASE_IDS),
                "dataset": _DATASET.value,
                "identity": identity.to_dict(),
                "output_scope": str(output_root.resolve(strict=False)),
                "promotion_profile": context.promotion_profile.to_dict(),
                "runner_key_id": runner_key_id,
                "source": _SOURCE,
                "source_dataset_contract": context.source_dataset_contract.to_dict(),
                "versions": versions.to_dict(),
            }
        )
    )
    return RealXtQuantValidationPlan(
        output_root=output_root.resolve(strict=False),
        identity=identity,
        context=context,
        versions=versions,
        runner_key_id=runner_key_id,
        plan_sha256=plan_sha,
    )


class RealXtQuantRunner(Protocol):
    def execute(self, *, plan: RealXtQuantRunPlan, output_root: Path) -> Path: ...


@dataclass(frozen=True, slots=True)
class RealXtQuantRunPlan:
    """Runtime-only plan adds verifier-owned signing capability after preflight."""

    output_root: Path
    identity: ValidationAttemptIdentityV3
    selected_case_ids: tuple[str, ...]
    context: V3EvidenceTrustContext
    versions: ValidationVersions
    runner_signer: LocalRealV3Signer
    runner_trust_policy: RealV3RunnerTrustPolicy
    plan_sha256: str

    @property
    def output_directory(self) -> Path:
        return (
            self.output_root
            / "source-assessments-v3"
            / _SOURCE.lower()
            / _DATASET.value
            / path_safe_id(str(self.identity.assessment_id))
            / path_safe_id(str(self.identity.run_id))
        )


@dataclass(frozen=True, slots=True)
class RealXtQuantExecutionResult:
    directory: Path
    event_order: tuple[str, ...]
    outcome: AttemptOutcomeV3


class RealXtQuantValidationRuntime:
    """The only executable C1A REAL route: canonical file → claim → runner."""

    def __init__(
        self,
        *,
        authorization_file_loader: RealRuntimeAuthorizationFileLoader,
        authorization_verifier: AuthorizationVerifier,
        claim_store: AuthorizationClaimStore,
        trust_resolver: RealXtQuantTrustResolver,
        package_metadata_probe: ExplicitXtQuantPackageMetadataProbe,
        import_runner: Callable[[str], object] = importlib.import_module,
        runner_preflight: Callable[[], bool] | None = None,
    ) -> None:
        self._file_loader = authorization_file_loader
        self._authorization_verifier = authorization_verifier
        self._claim_store = claim_store
        self._trust_resolver = trust_resolver
        self._package_probe = package_metadata_probe
        self._import_runner = import_runner
        self._runner_preflight = runner_preflight or _fixed_runner_is_available

    def plan_for_authorization(
        self, *, output_root: Path, identity: ValidationAttemptIdentityV3
    ) -> RealXtQuantValidationPlan:
        """Expose the fixed plan needed by an authorized file issuer, without claim/import."""
        try:
            trust = self._trust_resolver.resolve()
            package_version = self._package_probe.exact_version()
        except (RealAuthorizationError, ValueError, XtQuantPackageMetadataError) as error:
            raise RealXtQuantExecutionError(
                "REAL_RUNTIME_NOT_READY", "REAL runtime trust or package metadata is unavailable"
            ) from error
        _assert_runner_ready(trust=trust, runner_preflight=self._runner_preflight)
        return build_real_xtquant_plan(
            output_root=output_root,
            identity=identity,
            context=trust.context,
            package_version=package_version,
            runner_key_id=trust.signer.key_id,
        )

    def execute(self, *, authorization_file: Path) -> RealXtQuantExecutionResult:
        try:
            loaded = self._file_loader.load(authorization_file)
        except RealAuthorizationError as error:
            raise RealXtQuantExecutionError(
                "REAL_RUNTIME_AUTHORIZATION_INVALID", "REAL authorization was rejected"
            ) from error
        plan = self._preflight(loaded)
        events = ["PRECHECK"]
        envelope = loaded.envelope
        try:
            self._claim_store.claim(
                envelope.authorization,
                invocation_id=plan.identity.invocation_id,
                verifier=self._authorization_verifier,
                source=_SOURCE,
                dataset=_DATASET,
                output_root=plan.output_root,
                sample_sha256=plan.context.sample.sample_spec_sha256,
                at=utc_now(),
            )
        except AuthorizationClaimError as error:
            raise RealXtQuantExecutionError(
                str(error), "REAL authorization claim was rejected"
            ) from None
        events.append("CLAIM")
        try:
            module = self._import_runner(_RUNNER_MODULE)
            factory = getattr(module, _RUNNER_FACTORY)
            runner = factory()
            if not hasattr(runner, "execute"):
                raise TypeError("fixed XTQuant REAL runner is invalid")
        except Exception:
            self._write_incomplete(plan, "REAL_RUNNER_IMPORT_FAILED")
            raise RealXtQuantExecutionError(
                "REAL_RUNNER_IMPORT_FAILED", "fixed XTQuant REAL runner could not be loaded"
            ) from None
        events.append("IMPORT")
        try:
            directory = runner.execute(plan=plan, output_root=plan.output_root)
        except Exception:
            self._write_incomplete(plan, "REAL_RUNNER_CRASHED")
            raise RealXtQuantExecutionError(
                "REAL_RUNNER_CRASHED", "fixed XTQuant REAL runner failed after claim"
            ) from None
        if directory != plan.output_directory or not directory.is_dir():
            self._write_incomplete(plan, "REAL_RUNNER_OUTPUT_INVALID")
            raise RealXtQuantExecutionError(
                "REAL_RUNNER_OUTPUT_INVALID", "fixed XTQuant REAL runner output is invalid"
            )
        events.append("OUTPUT")
        try:
            verified = ValidationEvidenceVerifierV3(
                validation_root=plan.output_root,
                context=plan.context,
                runner_attestation_verifier=plan.runner_signer,
                runner_trust_policy=plan.runner_trust_policy,
            ).verify_run(directory=directory)
        except Exception:
            self._write_incomplete(plan, "REAL_RUNNER_EVIDENCE_INVALID")
            raise RealXtQuantExecutionError(
                "REAL_RUNNER_EVIDENCE_INVALID", "fixed XTQuant REAL evidence is invalid"
            ) from None
        if verified.identity != plan.identity or verified.evidence_kind is not EvidenceKind.REAL:
            self._write_incomplete(plan, "REAL_RUNNER_IDENTITY_MISMATCH")
            raise RealXtQuantExecutionError(
                "REAL_RUNNER_IDENTITY_MISMATCH", "fixed XTQuant REAL evidence identity is invalid"
            )
        return RealXtQuantExecutionResult(
            directory=directory,
            event_order=tuple(events),
            outcome=verified.outcome,
        )

    def _preflight(self, loaded: LoadedRealRuntimeAuthorization) -> RealXtQuantRunPlan:
        envelope = loaded.envelope
        output_root = Path(envelope.output_scope)
        _validate_output_root(output_root)
        try:
            trust = self._trust_resolver.resolve()
        except (RealAuthorizationError, ValueError):
            raise RealXtQuantExecutionError(
                "REAL_TRUST_NOT_CONFIGURED", "REAL runner trust is unavailable"
            ) from None
        _assert_runner_ready(trust=trust, runner_preflight=self._runner_preflight)
        try:
            package_version = self._package_probe.exact_version()
        except XtQuantPackageMetadataError:
            raise RealXtQuantExecutionError(
                "XTQUANT_PACKAGE_METADATA_UNAVAILABLE",
                "explicit XTQuant package metadata is unavailable",
            ) from None
        expected = build_real_xtquant_plan(
            output_root=output_root,
            identity=envelope.identity,
            context=trust.context,
            package_version=package_version,
            runner_key_id=trust.signer.key_id,
        )
        if (
            envelope.authorization.source != _SOURCE
            or envelope.authorization.dataset is not _DATASET
            or envelope.authorization.sample_sha256 != trust.context.sample.sample_spec_sha256
            or envelope.selected_case_ids != _CASE_IDS
            or envelope.reference_bundle_id != trust.context.promotion_profile.reference_bundle_id
            or envelope.reference_bundle_sha256
            != trust.context.promotion_profile.reference_bundle_manifest_sha256
            or envelope.source_dataset_contract != trust.context.source_dataset_contract
            or envelope.promotion_profile != trust.context.promotion_profile
            or envelope.output_scope != str(expected.output_root)
            or dict(envelope.frozen_versions) != expected.versions.to_dict()
            or envelope.plan_sha256 != expected.plan_sha256
            or envelope.runner_key_id != trust.signer.key_id
        ):
            raise RealXtQuantExecutionError(
                "REAL_RUNTIME_AUTHORIZATION_BINDING_MISMATCH",
                "REAL authorization does not match the fixed unit plan",
            )
        if (
            expected.output_directory.exists()
            or expected.output_directory.with_name(
                expected.output_directory.name + ".incomplete"
            ).exists()
        ):
            raise RealXtQuantExecutionError(
                "RUN_ID_ALREADY_EXISTS", "REAL run identity already has output evidence"
            )
        return RealXtQuantRunPlan(
            output_root=expected.output_root,
            identity=expected.identity,
            selected_case_ids=_CASE_IDS,
            context=trust.context,
            versions=expected.versions,
            runner_signer=trust.signer,
            runner_trust_policy=trust.runner_trust_policy,
            plan_sha256=expected.plan_sha256,
        )

    @staticmethod
    def _write_incomplete(plan: RealXtQuantRunPlan, category: str) -> None:
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
            # A consumed one-time claim is intentionally never rolled back,
            # including when the terminal forensic record cannot be written.
            return


class RealXtQuantAssessmentPersistence(Protocol):
    def persist_from_directory(self, absolute_assessment_directory: Path) -> object: ...


class RealXtQuantAssessmentService:
    """Finalize C1A v3 evidence and pass only the managed directory to SQLite."""

    def __init__(
        self,
        *,
        validation_root: Path,
        trust_resolver: RealXtQuantTrustResolver,
        persistence: RealXtQuantAssessmentPersistence,
    ) -> None:
        if not isinstance(validation_root, Path) or not validation_root.is_absolute():
            raise ValueError("validation root must be an absolute Path")
        self._root = validation_root
        self._trust_resolver = trust_resolver
        self._persistence = persistence

    def finalize_and_persist(self, *, assessment_directory: Path) -> object:
        if not isinstance(assessment_directory, Path) or not assessment_directory.is_absolute():
            raise RealXtQuantExecutionError(
                "ASSESSMENT_DIRECTORY_UNSAFE", "REAL assessment directory is unsafe"
            )
        try:
            assessment_directory.relative_to(self._root)
            trust = self._trust_resolver.resolve()
            verifier = ValidationEvidenceVerifierV3(
                validation_root=self._root,
                context=trust.context,
                runner_attestation_verifier=trust.signer,
                runner_trust_policy=trust.runner_trust_policy,
            )
            ValidationEvidenceWriterV3(validation_root=self._root).finalize_assessment(
                assessment_directory=assessment_directory,
                verifier=verifier,
            )
            return self._persistence.persist_from_directory(assessment_directory)
        except Exception:
            raise RealXtQuantExecutionError(
                "REAL_ASSESSMENT_PERSISTENCE_FAILED", "REAL assessment persistence failed"
            ) from None


def _assert_runner_ready(
    *, trust: RealXtQuantTrustConfiguration, runner_preflight: Callable[[], bool]
) -> None:
    try:
        kind = trust.runner_trust_policy.evidence_kind_for(
            attestation_key_id=trust.signer.key_id,
            verifier_key_id=trust.signer.key_id,
        )
    except (RealAuthorizationError, ValueError):
        raise RealXtQuantExecutionError(
            "REAL_SIGNER_NOT_READY", "REAL signer is not ready"
        ) from None
    if kind is not EvidenceKind.REAL:
        raise RealXtQuantExecutionError("REAL_SIGNER_NOT_READY", "REAL signer is not trusted")
    if not runner_preflight():
        raise RealXtQuantExecutionError("REAL_RUNNER_NOT_READY", "fixed REAL runner is unavailable")


def _fixed_runner_is_available() -> bool:
    """Verify the fixed module and factory declaration without importing it."""
    try:
        spec = importlib.util.find_spec(_RUNNER_MODULE)
        if spec is None or not isinstance(spec.origin, str) or not spec.origin.endswith(".py"):
            return False
        source = Path(spec.origin)
        if not source.is_file() or source.is_symlink() or is_reparse_point(source):
            return False
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, SyntaxError, ValueError):
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == _RUNNER_FACTORY
        for node in tree.body
    )


def _validate_output_root(root: Path) -> None:
    if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts:
        raise RealXtQuantExecutionError("OUTPUT_ROOT_UNSAFE", "REAL output root is unsafe")
    current = Path(root.anchor)
    try:
        for part in root.parts[1:]:
            current /= part
            if current.exists() and is_reparse_point(current):
                raise RealXtQuantExecutionError("OUTPUT_ROOT_UNSAFE", "REAL output root is unsafe")
    except OSError:
        raise RealXtQuantExecutionError(
            "OUTPUT_ROOT_UNSAFE", "REAL output root is unsafe"
        ) from None


__all__ = [
    "ExplicitRealXtQuantTrustResolver",
    "RealXtQuantAssessmentService",
    "RealXtQuantExecutionError",
    "RealXtQuantExecutionResult",
    "RealXtQuantRunPlan",
    "RealXtQuantTrustConfiguration",
    "RealXtQuantTrustResolver",
    "RealXtQuantValidationPlan",
    "RealXtQuantValidationRuntime",
    "build_real_xtquant_context",
    "build_real_xtquant_plan",
]
