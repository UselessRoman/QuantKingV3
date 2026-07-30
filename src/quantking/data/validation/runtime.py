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
from quantking.data.validation.authorization import (
    AuthorizationClaimError,
    AuthorizationClaimStore,
    AuthorizationVerifier,
    ExecutionAuthorization,
)
from quantking.data.validation.samples import load_fixed_sample_v3

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
    authorization: ExecutionAuthorization
    plan_sha256: str
    frozen_versions: Mapping[str, str]
    signature: str

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
    """S1-07B readiness runtime; REAL signer/runner enablement remains absent."""

    def __init__(
        self,
        *,
        registry: ControlledRunnerRegistry,
        package_probe: PackageVersionProbe,
        claim_store: AuthorizationClaimStore | None = None,
        authorization_verifier: AuthorizationVerifier | None = None,
        runtime_authorization_verifier: RuntimeAuthorizationVerifier | None = None,
        invocation_id: EntityId | None = None,
    ) -> None:
        self.registry = registry
        self._package_probe = package_probe
        self._claim_store = claim_store
        self._authorization_verifier = authorization_verifier
        self._runtime_authorization_verifier = runtime_authorization_verifier
        self._invocation_id = invocation_id

    def plan(
        self,
        *,
        source: ValidationSource,
        dataset: DatasetKind,
        output_root: Path,
    ) -> RuntimeValidationPlan:
        if not isinstance(source, ValidationSource):
            raise TypeError("source must be a ValidationSource")
        if not isinstance(dataset, DatasetKind):
            raise TypeError("dataset must be a DatasetKind")
        if not isinstance(output_root, Path) or not output_root.is_absolute():
            raise ValueError("output_root must be an absolute Path")
        descriptor = self.registry.descriptor_for(source)
        package_version = self._package_probe.exact_version(descriptor.distribution_name)
        package_display = package_version or "unavailable"
        sample = load_fixed_sample_v3()
        cases = tuple(
            case
            for case in sample.cases
            if case.dataset is dataset and source.value in case.applicable_sources
        )
        reasons = ["REAL_SIGNER_NOT_INITIALIZED", "PROVIDER_RUNNER_NOT_ENABLED"]
        if not sample.fixed_sample_gate_eligible:
            reasons.append("AUTHORITATIVE_SAMPLE_FRAMEWORK_ONLY")
        if not cases:
            reasons.append("SAMPLE_SCOPE_EMPTY")
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
    ) -> Path:
        if acknowledgement != _REAL_ACK:
            raise RealRuntimeExecutionError(
                "ACKNOWLEDGEMENT_REQUIRED",
                "exact real-execution acknowledgement is required",
            )
        try:
            plan = self.plan(source=source, dataset=dataset, output_root=output_root)
        except ValueError:
            if not isinstance(output_root, Path) or not output_root.is_absolute():
                raise RealRuntimeExecutionError(
                    "OUTPUT_ROOT_UNSAFE",
                    "output root is not a safe absolute managed path",
                ) from None
            raise
        if not plan.sample_production_eligible:
            raise RealRuntimeExecutionError(
                "SAMPLE_NOT_PRODUCTION_ELIGIBLE",
                "authoritative fixed sample is not production eligible",
            )
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
        if authorization is None:
            raise RealRuntimeExecutionError(
                "AUTHORIZATION_REQUIRED",
                "matching version-scoped authorization is required",
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
    "LocalPackageVersionProbe",
    "RealRuntimeExecutionError",
    "RealValidationRuntime",
    "RunnerDescriptor",
    "RuntimeAuthorizationEnvelope",
    "RuntimeAuthorizationVerifier",
    "RuntimeValidationPlan",
    "ValidationSource",
]
