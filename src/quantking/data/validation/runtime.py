"""Offline-safe readiness boundary for future authorized REAL validation."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from quantking.common.ids import EntityId
from quantking.common.paths import is_reparse_point
from quantking.common.time import utc_now
from quantking.data.adapters.warehouse.manifest import compute_sha256
from quantking.data.comparison.thresholds import THRESHOLD_RULE_VERSION
from quantking.data.models import DatasetKind
from quantking.data.quality.rules import default_rule_set
from quantking.data.schemas import descriptor_for
from quantking.data.validation.authority_bundle import (
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
    PromotionProfileV3,
    SourceDatasetContractV3,
    ValidationAttemptIdentityV3,
)
from quantking.data.validation.samples import (
    CaseEligibility,
    FixedSampleCase,
    load_fixed_sample_v4,
)

_STABLE_PACKAGE_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)+$")
_UNRESOLVED_PACKAGE_VERSIONS = frozenset({"", "latest", "unavailable", "unknown"})
_RUNTIME_VERSION_FIELDS = (
    "adapter_version",
    "package_version",
    "code_version",
    "schema_version",
    "rule_version",
    "threshold_version",
    "sample_spec_version",
    "config_version",
)
_REAL_ACK = "I_ACKNOWLEDGE_AUTHORIZED_REAL_SOURCE_VALIDATION"


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _import_runner(module_name: str) -> object:
    """Single delayed-import seam; never called by plan/status/help/verify."""
    return importlib.import_module(module_name)


class ValidationSource(StrEnum):
    XTQUANT = "XTQUANT"
    BAOSTOCK = "BAOSTOCK"
    AKSHARE = "AKSHARE"
    TUSHARE = "TUSHARE"


@dataclass(frozen=True, slots=True)
class RunnerDescriptor:
    source: str
    module_name: str
    factory_name: str
    distribution_name: str
    adapter_version: str
    network_required: bool
    qmt_required: bool

    def __post_init__(self) -> None:
        ValidationSource(self.source)
        for field in (
            "module_name",
            "factory_name",
            "distribution_name",
            "adapter_version",
        ):
            if not isinstance(getattr(self, field), str) or not getattr(self, field):
                raise ValueError(f"{field} must be non-empty")
        if type(self.network_required) is not bool or type(self.qmt_required) is not bool:
            raise TypeError("runner access flags must be bool values")


_CONTROLLED_DESCRIPTORS = MappingProxyType(
    {
        ValidationSource.XTQUANT: RunnerDescriptor(
            source="XTQUANT",
            module_name="quantking.data.validation.runners.xtquant",
            factory_name="create_runner",
            distribution_name="xtquant",
            adapter_version="xtquant-readonly.s1-07b.v1",
            network_required=True,
            qmt_required=True,
        ),
        ValidationSource.BAOSTOCK: RunnerDescriptor(
            source="BAOSTOCK",
            module_name="quantking.data.validation.runners.baostock",
            factory_name="create_runner",
            distribution_name="baostock",
            adapter_version="baostock-readonly.s1-07b.v1",
            network_required=True,
            qmt_required=False,
        ),
        ValidationSource.AKSHARE: RunnerDescriptor(
            source="AKSHARE",
            module_name="quantking.data.validation.runners.akshare",
            factory_name="create_runner",
            distribution_name="akshare",
            adapter_version="akshare-readonly.s1-07b.v1",
            network_required=True,
            qmt_required=False,
        ),
        ValidationSource.TUSHARE: RunnerDescriptor(
            source="TUSHARE",
            module_name="quantking.data.validation.runners.tushare",
            factory_name="create_runner",
            distribution_name="tushare",
            adapter_version="tushare-readonly.s1-07b.v1",
            network_required=True,
            qmt_required=False,
        ),
    }
)


class ControlledRunnerRegistry:
    """Closed runner metadata registry; it never accepts import paths from callers."""

    def __init__(self, descriptors: Mapping[ValidationSource, RunnerDescriptor]) -> None:
        if dict(descriptors) != dict(_CONTROLLED_DESCRIPTORS):
            raise ValueError("runner descriptors must match the controlled registry")
        self._descriptors = _CONTROLLED_DESCRIPTORS

    @classmethod
    def s1_07b(cls) -> ControlledRunnerRegistry:
        return cls(_CONTROLLED_DESCRIPTORS)

    @property
    def sources(self) -> tuple[ValidationSource, ...]:
        return tuple(self._descriptors)

    def descriptor_for(self, source: ValidationSource) -> RunnerDescriptor:
        if not isinstance(source, ValidationSource):
            raise TypeError("source must be a ValidationSource")
        return self._descriptors[source]

    def with_descriptor(self, descriptor: RunnerDescriptor) -> ControlledRunnerRegistry:
        source = ValidationSource(descriptor.source)
        if descriptor != _CONTROLLED_DESCRIPTORS[source]:
            raise ValueError("runner descriptor does not match the controlled registry")
        return self


class PackageVersionProbe(Protocol):
    def exact_version(self, distribution_name: str) -> str | None: ...


class LocalPackageVersionProbe:
    """Read local distribution metadata without importing a vendor package."""

    def __init__(self, *, overrides: Mapping[str, str]) -> None:
        self._overrides = MappingProxyType(dict(overrides))

    def exact_version(self, distribution_name: str) -> str | None:
        if distribution_name in self._overrides:
            return self._overrides[distribution_name]
        try:
            value = importlib.metadata.version(distribution_name)
        except importlib.metadata.PackageNotFoundError:
            return None
        return value


@dataclass(frozen=True, slots=True)
class RuntimeValidationPlan:
    source: ValidationSource
    dataset: DatasetKind
    sample_spec_version: str
    sample_spec_sha256: str
    sample_production_eligible: bool
    adapter_version: str
    package_version: str
    code_version: str
    schema_version: str
    rule_version: str
    threshold_version: str
    config_version: str
    case_ids: tuple[str, ...]
    request_ranges: tuple[str, ...]
    requested_fields: tuple[str, ...]
    output_root: str
    output_directory: str
    network_required: bool
    qmt_required: bool
    unavoidable_side_effects: tuple[str, ...]
    blocking_reasons: tuple[str, ...]
    sdk_loaded: bool
    reference_bundle_id: str
    reference_bundle_sha256: str
    plan_sha256: str

    def unsigned_payload(self) -> dict[str, object]:
        return {
            field: (
                getattr(self, field).value
                if isinstance(getattr(self, field), StrEnum)
                else list(getattr(self, field))
                if isinstance(getattr(self, field), tuple)
                else getattr(self, field)
            )
            for field in self.__dataclass_fields__
            if field != "plan_sha256"
        }


@dataclass(frozen=True, slots=True)
class RuntimeAuthorizationEnvelope:
    """Version-scoped authorization, with a strict file-only C0B form.

    The four original fields are retained solely for the already accepted
    readiness runtime, which is permanently fail-closed.  An authorization
    that can reach the C0B controlled runner must be created from the strict,
    canonical representation below and loaded by
    :class:`RuntimeAuthorizationFileLoader`; a caller-constructed DTO is not
    an executable authorization.
    """

    authorization: ExecutionAuthorization
    plan_sha256: str
    frozen_versions: Mapping[str, str]
    signature: str
    schema_version: str | None = None
    purpose: str | None = None
    identity: ValidationAttemptIdentityV3 | None = None
    selected_case_ids: tuple[str, ...] | None = None
    reference_bundle_id: str | None = None
    reference_bundle_sha256: str | None = None
    source_dataset_contract: SourceDatasetContractV3 | None = None
    promotion_profile: PromotionProfileV3 | None = None
    output_scope: str | None = None
    claim_nonce: str | None = None

    def __post_init__(self) -> None:
        expected = {
            "adapter_version",
            "package_version",
            "code_version",
            "schema_version",
            "rule_version",
            "threshold_version",
            "sample_spec_version",
            "config_version",
        }
        if set(self.frozen_versions) != expected:
            raise ValueError("runtime authorization versions are incomplete")
        object.__setattr__(
            self,
            "frozen_versions",
            MappingProxyType(dict(sorted(self.frozen_versions.items()))),
        )
        for digest in (self.plan_sha256, self.signature):
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("runtime authorization digest is invalid")

        strict_values = (
            self.schema_version,
            self.purpose,
            self.identity,
            self.selected_case_ids,
            self.reference_bundle_id,
            self.reference_bundle_sha256,
            self.source_dataset_contract,
            self.promotion_profile,
            self.output_scope,
            self.claim_nonce,
        )
        if any(value is not None for value in strict_values) and not all(
            value is not None for value in strict_values
        ):
            raise ValueError("runtime authorization strict fields are incomplete")
        if all(value is not None for value in strict_values):
            assert self.identity is not None
            assert self.selected_case_ids is not None
            assert self.reference_bundle_id is not None
            assert self.reference_bundle_sha256 is not None
            assert self.source_dataset_contract is not None
            assert self.promotion_profile is not None
            assert self.output_scope is not None
            assert self.claim_nonce is not None
            if self.schema_version != "runtime-authorization-envelope.v2":
                raise ValueError("runtime authorization schema version is unsupported")
            if self.purpose != "S1-07C0B_FAKE_PROVIDER":
                raise ValueError("runtime authorization purpose is invalid")
            if self.identity.authorization_id != self.authorization.authorization_id:
                raise ValueError("runtime authorization identity does not bind authorization")
            if (
                self.source_dataset_contract.source != self.authorization.source
                or self.source_dataset_contract.dataset is not self.authorization.dataset
                or self.promotion_profile.source != self.authorization.source
                or self.promotion_profile.dataset is not self.authorization.dataset
            ):
                raise ValueError("runtime authorization source/dataset binding is invalid")
            if (
                self.reference_bundle_id != self.promotion_profile.reference_bundle_id
                or self.reference_bundle_sha256
                != self.promotion_profile.reference_bundle_manifest_sha256
            ):
                raise ValueError("runtime authorization reference bundle binding is invalid")
            if self.selected_case_ids != tuple(sorted(set(self.selected_case_ids))):
                raise ValueError("runtime authorization case IDs must be sorted and unique")
            if not self.selected_case_ids:
                raise ValueError("runtime authorization requires case IDs")
            if self.output_scope != self.authorization.output_root:
                raise ValueError("runtime authorization output scope is invalid")
            if not re.fullmatch(r"[0-9a-f]{64}", self.reference_bundle_sha256):
                raise ValueError("runtime authorization bundle SHA is invalid")
            if not re.fullmatch(r"[0-9a-f]{64}", self.claim_nonce):
                raise ValueError("runtime authorization claim nonce is invalid")

    @property
    def is_strict_file_authorization(self) -> bool:
        return self.schema_version is not None

    def unsigned_payload(self) -> dict[str, object]:
        if not self.is_strict_file_authorization:
            raise ValueError("legacy runtime authorization has no file payload")
        assert self.identity is not None
        assert self.selected_case_ids is not None
        assert self.reference_bundle_id is not None
        assert self.reference_bundle_sha256 is not None
        assert self.source_dataset_contract is not None
        assert self.promotion_profile is not None
        assert self.output_scope is not None
        assert self.claim_nonce is not None
        return {
            "authorization": json.loads(self.authorization.to_bytes()),
            "case_ids": list(self.selected_case_ids),
            "claim_nonce": self.claim_nonce,
            "frozen_versions": dict(self.frozen_versions),
            "identity": self.identity.to_dict(),
            "output_scope": self.output_scope,
            "plan_sha256": self.plan_sha256,
            "promotion_profile": self.promotion_profile.to_dict(),
            "purpose": self.purpose,
            "reference_bundle_id": self.reference_bundle_id,
            "reference_bundle_sha256": self.reference_bundle_sha256,
            "schema_version": self.schema_version,
            "source_dataset_contract": self.source_dataset_contract.to_dict(),
        }

    def to_bytes(self) -> bytes:
        return _canonical({**self.unsigned_payload(), "signature": self.signature})

    @classmethod
    def from_bytes(cls, payload: bytes) -> RuntimeAuthorizationEnvelope:
        try:
            parsed = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("runtime authorization is invalid JSON") from error
        expected = {
            "authorization",
            "case_ids",
            "claim_nonce",
            "frozen_versions",
            "identity",
            "output_scope",
            "plan_sha256",
            "promotion_profile",
            "purpose",
            "reference_bundle_id",
            "reference_bundle_sha256",
            "schema_version",
            "source_dataset_contract",
            "signature",
        }
        if (
            not isinstance(parsed, dict)
            or payload != _canonical(parsed)
            or set(parsed) != expected
            or not isinstance(parsed["authorization"], dict)
            or not isinstance(parsed["identity"], dict)
            or not isinstance(parsed["source_dataset_contract"], dict)
            or not isinstance(parsed["promotion_profile"], dict)
            or not isinstance(parsed["frozen_versions"], dict)
            or not isinstance(parsed["case_ids"], list)
            or set(parsed["source_dataset_contract"])
            != {
                "contract_version",
                "coverage_rule_version",
                "dataset",
                "expected_schema_version",
                "semantic_rule_version",
                "source",
                "unit_rule_version",
            }
            or set(parsed["promotion_profile"])
            != {
                "dataset",
                "maximum_total_differences",
                "minimum_stability_runs",
                "promotion_config_version",
                "promotion_profile_version",
                "reference_bundle_id",
                "reference_bundle_manifest_sha256",
                "required_case_ids",
                "source",
            }
        ):
            raise ValueError("runtime authorization fields do not match the strict schema")
        contract = parsed["source_dataset_contract"]
        profile = parsed["promotion_profile"]
        try:
            # Do not normalize JSON scalars before signature verification.  In
            # particular, ``int(\"3\")`` would otherwise make a string look
            # indistinguishable from the signed JSON number ``3``.  Keep the
            # strict scalar schema here, then require a complete byte-for-byte
            # round trip below as a second line of defence for nested values.
            _require_runtime_authorization_scalar_types(parsed)
            candidate = cls(
                authorization=ExecutionAuthorization.from_bytes(
                    _canonical(parsed["authorization"])
                ),
                plan_sha256=str(parsed["plan_sha256"]),
                frozen_versions=MappingProxyType(
                    {str(key): str(value) for key, value in parsed["frozen_versions"].items()}
                ),
                signature=str(parsed["signature"]),
                schema_version=str(parsed["schema_version"]),
                purpose=str(parsed["purpose"]),
                identity=ValidationAttemptIdentityV3.from_dict(parsed["identity"]),
                selected_case_ids=tuple(str(value) for value in parsed["case_ids"]),
                reference_bundle_id=str(parsed["reference_bundle_id"]),
                reference_bundle_sha256=str(parsed["reference_bundle_sha256"]),
                source_dataset_contract=SourceDatasetContractV3(
                    source=str(contract["source"]),
                    dataset=DatasetKind(str(contract["dataset"])),
                    contract_version=str(contract["contract_version"]),
                    semantic_rule_version=str(contract["semantic_rule_version"]),
                    unit_rule_version=str(contract["unit_rule_version"]),
                    coverage_rule_version=str(contract["coverage_rule_version"]),
                    expected_schema_version=str(contract["expected_schema_version"]),
                ),
                promotion_profile=PromotionProfileV3(
                    source=str(profile["source"]),
                    dataset=DatasetKind(str(profile["dataset"])),
                    promotion_profile_version=str(profile["promotion_profile_version"]),
                    promotion_config_version=str(profile["promotion_config_version"]),
                    required_case_ids=tuple(str(value) for value in profile["required_case_ids"]),
                    reference_bundle_id=str(profile["reference_bundle_id"]),
                    reference_bundle_manifest_sha256=str(
                        profile["reference_bundle_manifest_sha256"]
                    ),
                    minimum_stability_runs=int(profile["minimum_stability_runs"]),
                    maximum_total_differences=int(profile["maximum_total_differences"]),
                ),
                output_scope=str(parsed["output_scope"]),
                claim_nonce=str(parsed["claim_nonce"]),
            )
            if candidate.to_bytes() != payload:
                raise ValueError("runtime authorization is not canonical typed JSON")
            return candidate
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("runtime authorization is invalid") from error


def _require_runtime_authorization_scalar_types(parsed: Mapping[str, object]) -> None:
    """Reject every scalar coercion before creating the signed envelope.

    ``json.loads`` deliberately preserves JSON scalar types.  This helper
    consumes that information before any contract parser calls ``str`` or
    ``int``.  The subsequent serialized-byte equality check protects future
    nested-contract extensions as well.
    """

    text_fields = {
        "claim_nonce",
        "output_scope",
        "plan_sha256",
        "purpose",
        "reference_bundle_id",
        "reference_bundle_sha256",
        "schema_version",
        "signature",
    }
    if any(type(parsed[field]) is not str for field in text_fields):
        raise ValueError("runtime authorization text field has the wrong type")
    frozen_versions = parsed["frozen_versions"]
    case_ids = parsed["case_ids"]
    assert isinstance(frozen_versions, dict)
    assert isinstance(case_ids, list)
    if any(
        type(key) is not str or type(value) is not str for key, value in frozen_versions.items()
    ):
        raise ValueError("runtime authorization frozen version has the wrong type")
    if any(type(value) is not str for value in case_ids):
        raise ValueError("runtime authorization case ID has the wrong type")

    authorization = parsed["authorization"]
    identity = parsed["identity"]
    contract = parsed["source_dataset_contract"]
    profile = parsed["promotion_profile"]
    assert isinstance(authorization, dict)
    assert isinstance(identity, dict)
    assert isinstance(contract, dict)
    assert isinstance(profile, dict)
    if any(type(key) is not str or type(value) is not str for key, value in authorization.items()):
        raise ValueError("runtime authorization nested authorization has the wrong type")
    if any(type(key) is not str or type(value) is not str for key, value in identity.items()):
        raise ValueError("runtime authorization identity has the wrong type")
    if any(type(key) is not str or type(value) is not str for key, value in contract.items()):
        raise ValueError("runtime authorization contract has the wrong type")
    profile_text = {
        "dataset",
        "promotion_config_version",
        "promotion_profile_version",
        "reference_bundle_id",
        "reference_bundle_manifest_sha256",
        "source",
    }
    if any(type(profile[field]) is not str for field in profile_text):
        raise ValueError("runtime authorization profile text has the wrong type")
    if (
        type(profile["minimum_stability_runs"]) is not int
        or type(profile["maximum_total_differences"]) is not int
        or not isinstance(profile["required_case_ids"], list)
        or any(type(value) is not str for value in profile["required_case_ids"])
    ):
        raise ValueError("runtime authorization profile has the wrong type")


class RuntimeAuthorizationFileVerifier(Protocol):
    """Verifies a strict envelope signature; implementations own their keys."""

    def verify_file_authorization(self, envelope: RuntimeAuthorizationEnvelope) -> bool: ...


@dataclass(frozen=True, slots=True)
class LoadedRuntimeAuthorizationEnvelope:
    """Opaque result of the only C0B authorization-file loading entrance."""

    envelope: RuntimeAuthorizationEnvelope
    _seal: object

    def __post_init__(self) -> None:
        if self._seal is not _LOADED_AUTHORIZATION_SEAL:
            raise ValueError("runtime authorization must be loaded from a managed file")


_LOADED_AUTHORIZATION_SEAL = object()


class RuntimeAuthorizationFileLoader:
    """Strict canonical file loader used by every executable C0B path."""

    def __init__(self, *, verifier: RuntimeAuthorizationFileVerifier) -> None:
        self._verifier = verifier

    def load(self, path: Path) -> LoadedRuntimeAuthorizationEnvelope:
        if not isinstance(path, Path) or not path.is_absolute() or is_reparse_point(path):
            raise ValueError("runtime authorization file is unsafe")
        try:
            envelope = RuntimeAuthorizationEnvelope.from_bytes(path.read_bytes())
        except (OSError, ValueError) as error:
            raise ValueError("runtime authorization file is invalid") from error
        trusted = envelope.is_strict_file_authorization and (
            self._verifier.verify_file_authorization(envelope)
        )
        if not trusted:
            raise ValueError("runtime authorization file is untrusted")
        return LoadedRuntimeAuthorizationEnvelope(envelope, _LOADED_AUTHORIZATION_SEAL)


class RuntimeAuthorizationVerifier(Protocol):
    def verify(
        self,
        envelope: RuntimeAuthorizationEnvelope,
        *,
        plan: RuntimeValidationPlan,
    ) -> bool: ...


class RealRuntimeExecutionError(Exception):
    """Stable, sanitized future-runtime failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RuntimeRunner(Protocol):
    def execute(self, *, plan: RuntimeValidationPlan, output_root: Path) -> Path: ...


class RealValidationRuntime:
    """S1-07B readiness runtime; REAL signer/runner enablement remains absent.

    S1-07B-FINAL replaces the all-or-nothing sample-level production gate with
    per-case authority gating. The runtime loads v4 by default and only allows
    cases with AUTHORITY_APPROVED reference bindings to pass preflight. Cases
    that are FRAMEWORK_ONLY, BLOCKED_REFERENCE, or unbound are rejected at the
    case-level gate. Missing purposes that are not selected do not block the
    plan; they remain in the coverage backlog.
    """

    def __init__(
        self,
        *,
        registry: ControlledRunnerRegistry,
        package_probe: PackageVersionProbe,
        claim_store: AuthorizationClaimStore | None = None,
        authorization_verifier: AuthorizationVerifier | None = None,
        runtime_authorization_verifier: RuntimeAuthorizationVerifier | None = None,
        invocation_id: EntityId | None = None,
        reference_bundle_root: Path | None = None,
    ) -> None:
        self.registry = registry
        self._package_probe = package_probe
        self._claim_store = claim_store
        self._authorization_verifier = authorization_verifier
        self._runtime_authorization_verifier = runtime_authorization_verifier
        self._invocation_id = invocation_id
        self._reference_bundle_root = reference_bundle_root

    def plan(
        self,
        *,
        source: ValidationSource,
        dataset: DatasetKind,
        output_root: Path,
        case_ids: tuple[str, ...] | None = None,
        reference_bundle_directory: Path | None = None,
    ) -> RuntimeValidationPlan:
        if not isinstance(source, ValidationSource):
            raise TypeError("source must be a ValidationSource")
        if not isinstance(dataset, DatasetKind):
            raise TypeError("dataset must be a DatasetKind")
        if not isinstance(output_root, Path) or not output_root.is_absolute():
            raise ValueError("output_root must be an absolute Path")
        if case_ids is not None:
            if not isinstance(case_ids, tuple) or not case_ids:
                raise ValueError("case_ids must be a non-empty tuple when provided")
            for case_id in case_ids:
                if not isinstance(case_id, str) or not case_id.strip():
                    raise ValueError("case_ids must contain non-empty strings")
        descriptor = self.registry.descriptor_for(source)
        package_version = self._package_probe.exact_version(descriptor.distribution_name)
        package_display = package_version or "unavailable"
        sample = load_fixed_sample_v4()
        # Select cases: explicit case_ids take precedence; otherwise all cases
        # matching the dataset and source are selected (backward compatibility).
        if case_ids is not None:
            sample_cases = {case.case_id: case for case in sample.cases}
            selected: list[FixedSampleCase] = []
            for cid in case_ids:
                case = sample_cases.get(cid)
                if case is None:
                    raise ValueError(f"case_ids contains unknown case: {cid}")
                if case.dataset is not dataset:
                    raise ValueError(f"case_id {cid} does not belong to dataset {dataset.value}")
                if source.value not in case.applicable_sources:
                    raise ValueError(f"case_id {cid} is not applicable to source {source.value}")
                selected.append(case)
            cases = tuple(selected)
        else:
            cases = tuple(
                case
                for case in sample.cases
                if case.dataset is dataset and source.value in case.applicable_sources
            )

        # Determine per-case eligibility via the authority reference bundle.
        reference_bundle_id = ""
        reference_bundle_sha = ""
        case_eligibilities: dict[str, CaseEligibility] = {
            case.case_id: CaseEligibility.FRAMEWORK_ONLY for case in cases
        }
        if reference_bundle_directory is not None:
            if self._reference_bundle_root is None:
                raise ValueError("reference_bundle_directory requires reference_bundle_root")
            try:
                verified = AuthorityReferenceBundleVerifier(
                    root=self._reference_bundle_root
                ).verify(directory=reference_bundle_directory, sample=sample)
            except AuthorityBundleVerificationError:
                raise ValueError("reference bundle verification failed") from None
            reference_bundle_id = verified.bundle_id
            reference_bundle_sha = verified.manifest_sha256
            for case in cases:
                eligibility = verified.case_eligibilities.get(case.case_id)
                if eligibility is not None:
                    case_eligibilities[case.case_id] = eligibility

        reasons = ["REAL_SIGNER_NOT_INITIALIZED", "PROVIDER_RUNNER_NOT_ENABLED"]
        if not sample.fixed_sample_gate_eligible:
            reasons.append("AUTHORITATIVE_SAMPLE_FRAMEWORK_ONLY")
        if not cases:
            reasons.append("SAMPLE_SCOPE_EMPTY")
        if any(
            case_eligibilities[case.case_id] is not CaseEligibility.AUTHORITY_APPROVED
            for case in cases
        ):
            reasons.append("CASE_NOT_AUTHORITY_APPROVED")
        package_failure = _package_version_failure(package_display)
        if package_failure == "PROVIDER_PACKAGE_VERSION_UNRESOLVED":
            reasons.append("PROVIDER_PACKAGE_VERSION_UNRESOLVED")
        elif package_failure is not None:
            reasons.append("PROVIDER_PACKAGE_VERSION_INVALID")
        root = output_root
        unsigned: dict[str, object] = {
            "adapter_version": descriptor.adapter_version,
            "blocking_reasons": sorted(reasons),
            "case_ids": [case.case_id for case in cases],
            "code_version": "s1-07b.runtime.v1",
            "config_version": "real-validation-config.v1",
            "dataset": dataset.value,
            "network_required": descriptor.network_required,
            "output_directory": str(
                root
                / "source-assessments"
                / source.value.lower()
                / dataset.value
                / "<assessment_id>"
            ),
            "output_root": str(root),
            "package_version": package_display,
            "qmt_required": descriptor.qmt_required,
            "reference_bundle_id": reference_bundle_id,
            "reference_bundle_sha256": reference_bundle_sha,
            "request_ranges": [
                f"{case.code or case.exchange}:{case.start_date}:{case.end_date}" for case in cases
            ],
            "requested_fields": sorted({field for case in cases for field in case.fields}),
            "rule_version": default_rule_set().rule_version,
            "sample_spec_sha256": sample.sample_spec_sha256,
            "sample_spec_version": sample.sample_spec_version,
            "sample_production_eligible": sample.fixed_sample_gate_eligible,
            "schema_version": descriptor_for(dataset.value).schema_version,
            "sdk_loaded": False,
            "source": source.value,
            "threshold_version": THRESHOLD_RULE_VERSION,
            "unavoidable_side_effects": [
                "authorized execute consumes its one-time authorization before signer/runner load",
                "authorized execute may create a provider session and immutable evidence",
                "the provider SDK may create process-local caches",
            ],
        }
        return RuntimeValidationPlan(
            source=source,
            dataset=dataset,
            sample_spec_version=sample.sample_spec_version,
            sample_spec_sha256=sample.sample_spec_sha256,
            sample_production_eligible=sample.fixed_sample_gate_eligible,
            adapter_version=descriptor.adapter_version,
            package_version=package_display,
            code_version="s1-07b.runtime.v1",
            schema_version=descriptor_for(dataset.value).schema_version,
            rule_version=default_rule_set().rule_version,
            threshold_version=THRESHOLD_RULE_VERSION,
            config_version="real-validation-config.v1",
            case_ids=tuple(unsigned["case_ids"]),  # type: ignore[arg-type]
            request_ranges=tuple(unsigned["request_ranges"]),  # type: ignore[arg-type]
            requested_fields=tuple(unsigned["requested_fields"]),  # type: ignore[arg-type]
            output_root=str(root),
            output_directory=str(unsigned["output_directory"]),
            network_required=descriptor.network_required,
            qmt_required=descriptor.qmt_required,
            unavoidable_side_effects=tuple(unsigned["unavoidable_side_effects"]),  # type: ignore[arg-type]
            blocking_reasons=tuple(sorted(reasons)),
            sdk_loaded=False,
            reference_bundle_id=reference_bundle_id,
            reference_bundle_sha256=reference_bundle_sha,
            plan_sha256=compute_sha256(_canonical(unsigned)),
        )

    def execute(
        self,
        *,
        source: ValidationSource,
        dataset: DatasetKind,
        output_root: Path,
        acknowledgement: str,
        authorization: RuntimeAuthorizationEnvelope | None,
        case_ids: tuple[str, ...] | None = None,
        reference_bundle_directory: Path | None = None,
    ) -> Path:
        if acknowledgement != _REAL_ACK:
            raise RealRuntimeExecutionError(
                "ACKNOWLEDGEMENT_REQUIRED",
                "exact real-execution acknowledgement is required",
            )
        try:
            plan = self.plan(
                source=source,
                dataset=dataset,
                output_root=output_root,
                case_ids=case_ids,
                reference_bundle_directory=reference_bundle_directory,
            )
        except ValueError:
            if not isinstance(output_root, Path) or not output_root.is_absolute():
                raise RealRuntimeExecutionError(
                    "OUTPUT_ROOT_UNSAFE",
                    "output root is not a safe absolute managed path",
                ) from None
            raise
        if not plan.case_ids:
            raise RealRuntimeExecutionError(
                "SAMPLE_SCOPE_EMPTY",
                "fixed sample has no case for the requested source and dataset",
            )
        package_failure = _package_version_failure(plan.package_version)
        if package_failure is not None:
            raise RealRuntimeExecutionError(
                package_failure,
                "provider package version is not an exact stable version",
            )
        _validate_output_root(output_root)
        if "CASE_NOT_AUTHORITY_APPROVED" in plan.blocking_reasons:
            raise RealRuntimeExecutionError(
                "CASE_NOT_AUTHORITY_APPROVED",
                "selected case is not authority-approved",
            )
        if authorization is None:
            _raise_preclaim_runtime_blocker(plan)
            raise RealRuntimeExecutionError(
                "AUTHORIZATION_REQUIRED", "matching version-scoped authorization is required"
            )
        expected_versions = {field: str(getattr(plan, field)) for field in _RUNTIME_VERSION_FIELDS}
        if (
            any(not value for value in expected_versions.values())
            or dict(authorization.frozen_versions) != expected_versions
            or authorization.authorization.sample_sha256 != plan.sample_spec_sha256
            or authorization.plan_sha256 != plan.plan_sha256
        ):
            raise RealRuntimeExecutionError(
                "RUNTIME_VERSION_MISMATCH",
                "runtime versions do not match the current frozen plan",
            )
        _raise_preclaim_runtime_blocker(plan)
        if (
            self._runtime_authorization_verifier is None
            or not self._runtime_authorization_verifier.verify(authorization, plan=plan)
        ):
            raise RealRuntimeExecutionError(
                "AUTHORIZATION_INVALID_OR_EXPIRED",
                "version-scoped authorization was rejected",
            )
        if (
            self._claim_store is None
            or self._authorization_verifier is None
            or self._invocation_id is None
        ):
            raise RealRuntimeExecutionError(
                "AUTHORIZATION_RUNTIME_UNAVAILABLE",
                "persistent authorization claim runtime is unavailable",
            )
        try:
            self._claim_store.claim(
                authorization.authorization,
                invocation_id=self._invocation_id,
                verifier=self._authorization_verifier,
                source=source.value,
                dataset=dataset,
                output_root=output_root,
                sample_sha256=plan.sample_spec_sha256,
                at=utc_now(),
            )
        except AuthorizationClaimError as error:
            raise RealRuntimeExecutionError(
                str(error), "authorization claim was rejected"
            ) from None
        raise RealRuntimeExecutionError(
            "REAL_SIGNER_UNAVAILABLE",
            "production REAL signer is not initialized; runner was not imported",
        )


def _raise_preclaim_runtime_blocker(plan: RuntimeValidationPlan) -> None:
    for code, message in (
        ("AUTHORITATIVE_SAMPLE_FRAMEWORK_ONLY", "sample release is not validation-ready"),
        ("PROVIDER_RUNNER_NOT_ENABLED", "provider runner is not enabled"),
        ("REAL_SIGNER_NOT_INITIALIZED", "production REAL signer is not initialized"),
    ):
        if code in plan.blocking_reasons:
            raise RealRuntimeExecutionError(code, message)


def _package_version_failure(value: str | None) -> str | None:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if normalized in _UNRESOLVED_PACKAGE_VERSIONS:
        return "PROVIDER_PACKAGE_VERSION_UNRESOLVED"
    if not _STABLE_PACKAGE_VERSION.fullmatch(normalized):
        return "PROVIDER_PACKAGE_VERSION_INVALID"
    return None


def _validate_output_root(output_root: Path) -> None:
    if (
        not isinstance(output_root, Path)
        or not output_root.is_absolute()
        or ".." in output_root.parts
    ):
        raise RealRuntimeExecutionError(
            "OUTPUT_ROOT_UNSAFE",
            "output root is not a safe absolute managed path",
        )
    current = Path(output_root.anchor)
    try:
        for part in output_root.parts[1:]:
            current /= part
            if current.exists() and is_reparse_point(current):
                raise RealRuntimeExecutionError(
                    "OUTPUT_ROOT_UNSAFE",
                    "output root contains a reparse point",
                )
    except RealRuntimeExecutionError:
        raise
    except OSError:
        raise RealRuntimeExecutionError(
            "OUTPUT_ROOT_UNSAFE",
            "output root could not be safely inspected",
        ) from None


__all__ = [
    "ControlledRunnerRegistry",
    "LoadedRuntimeAuthorizationEnvelope",
    "LocalPackageVersionProbe",
    "RealRuntimeExecutionError",
    "RealValidationRuntime",
    "RunnerDescriptor",
    "RuntimeAuthorizationEnvelope",
    "RuntimeAuthorizationFileLoader",
    "RuntimeAuthorizationFileVerifier",
    "RuntimeAuthorizationVerifier",
    "RuntimeValidationPlan",
    "ValidationSource",
]
