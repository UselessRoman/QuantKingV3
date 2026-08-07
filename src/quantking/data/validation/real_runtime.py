"""S1-07C1A-R1 fixed offline REAL XTQuant control chain.

The only shipped adapter stays CAPABILITY_UNSUPPORTED.  This runtime proves
authorization, claim and forensic evidence controls without importing xtquant,
making a network request, or claiming a completed XTQuant validation.
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
from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.adapters.sources.xtquant_metadata import (
    ExplicitXtQuantPackageMetadataProbe,
    XtQuantPackageMetadataError,
)
from quantking.data.adapters.warehouse.manifest import compute_sha256, path_safe_id
from quantking.data.models import DatasetKind
from quantking.data.schemas import descriptor_for
from quantking.data.validation.artifacts_v3 import V3EvidenceTrustContext
from quantking.data.validation.authority_bundle import (
    CONTROLLED_AUTHORITY_BUNDLE_ID,
    CONTROLLED_AUTHORITY_BUNDLE_MANIFEST_SHA256,
    AuthorityBundleVerificationError,
    AuthorityReferenceBundleVerifier,
)
from quantking.data.validation.contracts_v3 import (
    AttemptOutcomeV3,
    PromotionProfileV3,
    SourceDatasetContractV3,
    ValidationAttemptIdentityV3,
)
from quantking.data.validation.models import ValidationVersions
from quantking.data.validation.real_artifacts import (
    RealRunInputs,
    RealValidationEvidenceVerifier,
    RealValidationEvidenceWriter,
)
from quantking.data.validation.real_authorization import (
    ExplicitLocalRealKey,
    LoadedRealRuntimeAuthorization,
    RealAuthorizationClaimStore,
    RealAuthorizationError,
    RealClaimReceipt,
    RealExecutionAuthorizationVerifier,
    RealRuntimeAuthorizationFileLoader,
    RealVerifierTrustAnchor,
)
from quantking.data.validation.samples import CaseEligibility, load_fixed_sample_v4

_RUNNER_MODULE = "quantking.data.validation.runners.xtquant_real"
_RUNNER_FACTORY = "create_runner"
_RUNNER_ADAPTER_VERSION = "xtquant-readonly.s1-07c1a-r1.v1"
_RUNNER_CODE_VERSION = "s1-07c1a-r1.xtquant.runtime.v1"
_RUNNER_RULE_VERSION = "quality-rules.v1"
_RUNNER_THRESHOLD_VERSION = "comparison-thresholds.v1"
_SOURCE = "XTQUANT"
_DATASET = DatasetKind.TRADING_STATUS
_CASE_IDS = ("st_warning_603377_sh",)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


class RealXtQuantExecutionError(Exception):
    """Stable, sanitized R1 execution failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RealXtQuantTrustConfiguration:
    """Read-side trust: public anchor only, never a private signing key."""

    context: V3EvidenceTrustContext
    verifier_anchor: RealVerifierTrustAnchor


class RealXtQuantTrustResolver(Protocol):
    def resolve(self) -> RealXtQuantTrustConfiguration: ...


class ExplicitRealXtQuantTrustResolver:
    """Rebuild only REAL read trust from a bundle and independent public key."""

    def __init__(
        self,
        *,
        reference_bundle_root: Path,
        runner_public_key_file: Path,
        allowed_runner_key_ids: frozenset[str],
    ) -> None:
        if not isinstance(reference_bundle_root, Path) or not reference_bundle_root.is_absolute():
            raise ValueError("reference bundle root must be an absolute Path")
        self._reference_bundle_root = reference_bundle_root
        self._runner_public_key_file = runner_public_key_file
        self._allowed_runner_key_ids = frozenset(allowed_runner_key_ids)

    def resolve(self) -> RealXtQuantTrustConfiguration:
        return RealXtQuantTrustConfiguration(
            context=build_real_xtquant_context(reference_bundle_root=self._reference_bundle_root),
            verifier_anchor=RealVerifierTrustAnchor(
                public_key_file=self._runner_public_key_file,
                allowed_key_ids=self._allowed_runner_key_ids,
            ),
        )


def build_real_xtquant_context(*, reference_bundle_root: Path) -> V3EvidenceTrustContext:
    """Build the fixed-unit context from the accepted local authority bundle."""
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
            contract_version="xtquant-trading-status-real-contract.c1a-r1.v1",
            semantic_rule_version="semantic-contract-rules.v1",
            unit_rule_version="unit-contract-rules.v1",
            coverage_rule_version="coverage-rules.v1",
            expected_schema_version=descriptor_for(_DATASET.value).schema_version,
        ),
        promotion_profile=PromotionProfileV3(
            source=_SOURCE,
            dataset=_DATASET,
            promotion_profile_version="xtquant-trading-status-real-promotion.c1a-r1.v1",
            promotion_config_version="real-validation-config.c1a-r1.v1",
            required_case_ids=_CASE_IDS,
            reference_bundle_id=verified.bundle_id,
            reference_bundle_manifest_sha256=verified.manifest_sha256,
        ),
    )


@dataclass(frozen=True, slots=True)
class RealXtQuantValidationPlan:
    output_root: Path
    identity: ValidationAttemptIdentityV3
    context: V3EvidenceTrustContext
    versions: ValidationVersions
    runner_key_id: str
    plan_sha256: str
    plan_bytes: bytes

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
        return _output_directory(self.output_root, self.identity)


def _plan_unsigned(
    *,
    output_root: Path,
    identity: ValidationAttemptIdentityV3,
    context: V3EvidenceTrustContext,
    versions: ValidationVersions,
    runner_key_id: str,
) -> dict[str, object]:
    return {
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


def build_real_xtquant_plan(
    *,
    output_root: Path,
    identity: ValidationAttemptIdentityV3,
    context: V3EvidenceTrustContext,
    package_version: str,
    runner_key_id: str,
) -> RealXtQuantValidationPlan:
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
    root = output_root.resolve(strict=False)
    unsigned = _plan_unsigned(
        output_root=root,
        identity=identity,
        context=context,
        versions=versions,
        runner_key_id=runner_key_id,
    )
    plan_sha = compute_sha256(_canonical(unsigned))
    return RealXtQuantValidationPlan(
        output_root=root,
        identity=identity,
        context=context,
        versions=versions,
        runner_key_id=runner_key_id,
        plan_sha256=plan_sha,
        plan_bytes=_canonical({**unsigned, "plan_sha256": plan_sha}),
    )


@dataclass(frozen=True, slots=True)
class RealXtQuantRunPlan:
    output_root: Path
    identity: ValidationAttemptIdentityV3
    selected_case_ids: tuple[str, ...]
    context: V3EvidenceTrustContext
    versions: ValidationVersions
    plan_sha256: str
    plan_bytes: bytes
    authorization_bytes: bytes
    authorization_envelope_sha256: str
    claim_receipt: RealClaimReceipt
    runner_signer: ExplicitLocalRealKey
    valid_from: UtcTimestamp
    expires_at: UtcTimestamp

    @property
    def output_directory(self) -> Path:
        return _output_directory(self.output_root, self.identity)

    def evidence_inputs(self) -> RealRunInputs:
        return RealRunInputs(
            identity=self.identity,
            context=self.context,
            versions=self.versions,
            plan_sha256=self.plan_sha256,
            plan_bytes=self.plan_bytes,
            authorization_bytes=self.authorization_bytes,
            authorization_envelope_sha256=self.authorization_envelope_sha256,
            claim_receipt=self.claim_receipt,
            signer=self.runner_signer,
            valid_from=self.valid_from,
            expires_at=self.expires_at,
        )


@dataclass(frozen=True, slots=True)
class _PreparedRealXtQuantRun:
    output_root: Path
    identity: ValidationAttemptIdentityV3
    selected_case_ids: tuple[str, ...]
    context: V3EvidenceTrustContext
    versions: ValidationVersions
    plan_sha256: str
    plan_bytes: bytes
    runner_signer: ExplicitLocalRealKey
    valid_from: UtcTimestamp
    expires_at: UtcTimestamp


class RealXtQuantRunner(Protocol):
    def execute(self, *, plan: RealXtQuantRunPlan, output_root: Path) -> Path: ...


@dataclass(frozen=True, slots=True)
class RealXtQuantExecutionResult:
    directory: Path
    event_order: tuple[str, ...]
    outcome: AttemptOutcomeV3


class RealXtQuantValidationRuntime:
    """Canonical file → complete precheck → immutable claim → fixed runner."""

    def __init__(
        self,
        *,
        authorization_file_loader: RealRuntimeAuthorizationFileLoader,
        authorization_verifier: RealExecutionAuthorizationVerifier,
        claim_store: RealAuthorizationClaimStore,
        trust_resolver: RealXtQuantTrustResolver,
        runner_signer: ExplicitLocalRealKey,
        package_metadata_probe: ExplicitXtQuantPackageMetadataProbe,
        clock: Callable[[], UtcTimestamp] = utc_now,
        import_runner: Callable[[str], object] = importlib.import_module,
        runner_preflight: Callable[[], bool] | None = None,
    ) -> None:
        self._file_loader = authorization_file_loader
        self._authorization_verifier = authorization_verifier
        self._claim_store = claim_store
        self._trust_resolver = trust_resolver
        self._runner_signer = runner_signer
        self._package_probe = package_metadata_probe
        self._clock = clock
        self._import_runner = import_runner
        self._runner_preflight = runner_preflight or _fixed_runner_is_available

    def plan_for_authorization(
        self, *, output_root: Path, identity: ValidationAttemptIdentityV3
    ) -> RealXtQuantValidationPlan:
        try:
            trust = self._trust_resolver.resolve()
            package_version = self._package_probe.exact_version()
        except (RealAuthorizationError, ValueError, XtQuantPackageMetadataError) as error:
            raise RealXtQuantExecutionError(
                "REAL_RUNTIME_NOT_READY", "REAL trust or package metadata is unavailable"
            ) from error
        _assert_runner_ready(
            trust=trust, signer=self._runner_signer, runner_preflight=self._runner_preflight
        )
        return build_real_xtquant_plan(
            output_root=output_root,
            identity=identity,
            context=trust.context,
            package_version=package_version,
            runner_key_id=self._runner_signer.key_id,
        )

    def execute(self, *, authorization_file: Path) -> RealXtQuantExecutionResult:
        try:
            loaded = self._file_loader.load(authorization_file)
        except RealAuthorizationError as error:
            raise RealXtQuantExecutionError(
                "REAL_RUNTIME_AUTHORIZATION_INVALID", "REAL authorization was rejected"
            ) from error
        prepared = self._preflight(loaded)
        events = ["PRECHECK"]
        try:
            receipt = self._claim_store.claim(
                loaded=loaded, verifier=self._authorization_verifier, at=self._clock()
            )
        except RealAuthorizationError as error:
            raise RealXtQuantExecutionError(
                str(error), "REAL authorization claim was rejected"
            ) from None
        plan = RealXtQuantRunPlan(
            output_root=prepared.output_root,
            identity=prepared.identity,
            selected_case_ids=prepared.selected_case_ids,
            context=prepared.context,
            versions=prepared.versions,
            plan_sha256=prepared.plan_sha256,
            plan_bytes=prepared.plan_bytes,
            runner_signer=prepared.runner_signer,
            valid_from=prepared.valid_from,
            expires_at=prepared.expires_at,
            claim_receipt=receipt,
            authorization_bytes=loaded.raw_bytes,
            authorization_envelope_sha256=loaded.envelope_sha256,
        )
        events.append("CLAIM")
        try:
            module = self._import_runner(_RUNNER_MODULE)
            factory = getattr(module, _RUNNER_FACTORY)
            runner = factory()
            if not hasattr(runner, "execute"):
                raise TypeError
        except Exception:
            self._write_incomplete(plan, "REAL_RUNNER_IMPORT_FAILED")
            raise RealXtQuantExecutionError(
                "REAL_RUNNER_IMPORT_FAILED", "fixed REAL runner could not be loaded"
            ) from None
        events.append("IMPORT")
        try:
            directory = runner.execute(plan=plan, output_root=plan.output_root)
        except Exception:
            self._write_incomplete(plan, "REAL_RUNNER_CRASHED")
            raise RealXtQuantExecutionError(
                "REAL_RUNNER_CRASHED", "fixed REAL runner failed after claim"
            ) from None
        if directory != plan.output_directory or not directory.is_dir():
            self._write_incomplete(plan, "REAL_RUNNER_OUTPUT_INVALID")
            raise RealXtQuantExecutionError(
                "REAL_RUNNER_OUTPUT_INVALID", "fixed REAL runner output is invalid"
            )
        events.append("OUTPUT")
        try:
            verified = RealValidationEvidenceVerifier(
                validation_root=plan.output_root,
                context=plan.context,
                verifier_anchor=self._trust_resolver.resolve().verifier_anchor,
            ).verify_run(directory=directory)
        except Exception:
            self._write_incomplete(plan, "REAL_RUNNER_EVIDENCE_INVALID")
            raise RealXtQuantExecutionError(
                "REAL_RUNNER_EVIDENCE_INVALID", "fixed REAL evidence is invalid"
            ) from None
        if verified.identity != plan.identity:
            self._write_incomplete(plan, "REAL_RUNNER_IDENTITY_MISMATCH")
            raise RealXtQuantExecutionError(
                "REAL_RUNNER_IDENTITY_MISMATCH", "REAL evidence identity is invalid"
            )
        return RealXtQuantExecutionResult(
            directory=directory, event_order=tuple(events), outcome=verified.outcome
        )

    def _preflight(self, loaded: LoadedRealRuntimeAuthorization) -> _PreparedRealXtQuantRun:
        envelope = loaded.envelope
        output_root = Path(envelope.output_scope)
        _validate_output_root(output_root)
        try:
            trust = self._trust_resolver.resolve()
        except (RealAuthorizationError, ValueError):
            raise RealXtQuantExecutionError(
                "REAL_TRUST_NOT_CONFIGURED", "REAL runner trust is unavailable"
            ) from None
        _assert_runner_ready(
            trust=trust, signer=self._runner_signer, runner_preflight=self._runner_preflight
        )
        try:
            package_version = self._package_probe.exact_version()
        except XtQuantPackageMetadataError:
            raise RealXtQuantExecutionError(
                "XTQUANT_PACKAGE_METADATA_UNAVAILABLE", "explicit XTQuant metadata is unavailable"
            ) from None
        expected = build_real_xtquant_plan(
            output_root=output_root,
            identity=envelope.identity,
            context=trust.context,
            package_version=package_version,
            runner_key_id=self._runner_signer.key_id,
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
            or envelope.runner_key_id != self._runner_signer.key_id
            or expected.output_directory.exists()
            or expected.output_directory.with_name(
                expected.output_directory.name + ".incomplete"
            ).exists()
        ):
            raise RealXtQuantExecutionError(
                "REAL_RUNTIME_AUTHORIZATION_BINDING_MISMATCH",
                "REAL authorization does not match the fixed plan",
            )
        return _PreparedRealXtQuantRun(
            output_root=expected.output_root,
            identity=expected.identity,
            selected_case_ids=_CASE_IDS,
            context=trust.context,
            versions=expected.versions,
            plan_sha256=expected.plan_sha256,
            plan_bytes=expected.plan_bytes,
            runner_signer=self._runner_signer,
            valid_from=envelope.authorization.valid_from,
            expires_at=envelope.authorization.expires_at,
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
                        "lineage": plan.evidence_inputs().lineage.to_dict(),
                        "retryable": False,
                    }
                )
            )
        except OSError:
            return


class RealXtQuantAssessmentPersistence(Protocol):
    def persist_from_directory(self, absolute_assessment_directory: Path) -> object: ...


class RealXtQuantAssessmentService:
    """Finalize R1 evidence and pass only its managed directory to SQLite."""

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
            verifier = RealValidationEvidenceVerifier(
                validation_root=self._root,
                context=trust.context,
                verifier_anchor=trust.verifier_anchor,
            )
            RealValidationEvidenceWriter(validation_root=self._root).finalize_assessment(
                assessment_directory=assessment_directory, verifier=verifier
            )
            return self._persistence.persist_from_directory(assessment_directory)
        except Exception:
            raise RealXtQuantExecutionError(
                "REAL_ASSESSMENT_PERSISTENCE_FAILED", "REAL assessment persistence failed"
            ) from None


def _assert_runner_ready(
    *,
    trust: RealXtQuantTrustConfiguration,
    signer: ExplicitLocalRealKey,
    runner_preflight: Callable[[], bool],
) -> None:
    if (
        signer.key_id != trust.verifier_anchor.key_id
        or signer.key_id not in trust.verifier_anchor.allowed_key_ids
    ):
        raise RealXtQuantExecutionError(
            "REAL_SIGNER_NOT_READY", "REAL signer is not independently trusted"
        )
    if not runner_preflight():
        raise RealXtQuantExecutionError("REAL_RUNNER_NOT_READY", "fixed REAL runner is unavailable")


def _fixed_runner_is_available() -> bool:
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


def _output_directory(root: Path, identity: ValidationAttemptIdentityV3) -> Path:
    return (
        root
        / "source-assessments-real-v1"
        / "xtquant"
        / "trading_status"
        / path_safe_id(str(identity.assessment_id))
        / path_safe_id(str(identity.run_id))
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
