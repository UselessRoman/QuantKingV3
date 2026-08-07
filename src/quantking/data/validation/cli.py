"""Offline-safe CLI for plan, execute, verify and status."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from quantking.common.ids import EntityId
from quantking.data.assessment.matrix import default_source_matrix
from quantking.data.models import DatasetKind
from quantking.data.validation.artifacts import ValidationEvidenceVerifier
from quantking.data.validation.authority_bundle import CONTROLLED_AUTHORITY_BUNDLE_ID
from quantking.data.validation.authorization import (
    AuthorizationClaimStore,
    AuthorizationVerifier,
    ExecutionAuthorization,
    FakeAuthorizationAuthority,
)
from quantking.data.validation.controlled_runtime import (
    ControlledExecutionError,
    ControlledFakeValidationRuntime,
    FakeRuntimeAuthorizationAuthority,
)
from quantking.data.validation.runtime import (
    ControlledRunnerRegistry,
    LocalPackageVersionProbe,
    RealRuntimeExecutionError,
    RealValidationRuntime,
    RuntimeAuthorizationEnvelope,
    RuntimeAuthorizationFileLoader,
    ValidationSource,
)

REAL_EXECUTION_ACK = "I_ACKNOWLEDGE_AUTHORIZED_REAL_SOURCE_VALIDATION"
_SDK_PACKAGES = {
    "XTQUANT": "xtquant (frozen environment version; xtdata only)",
    "BAOSTOCK": "baostock (exact version must be frozen before authorization)",
    "AKSHARE": "akshare (exact version must be frozen before authorization)",
    "TUSHARE": "tushare (exact version must be frozen before authorization)",
}


@dataclass(frozen=True, slots=True)
class ValidationPlan:
    source: str
    dataset: str
    sample_spec_version: str
    sample_spec_sha256: str
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
    output_directory: str
    sdk: str
    network_access: bool
    qmt_connection: bool
    legacy_read: bool
    execute_network_access: bool
    execute_qmt_connection: bool
    sdk_loaded: bool
    unavoidable_side_effects: tuple[str, ...]
    blocking_reasons: tuple[str, ...]
    reference_bundle_id: str
    reference_bundle_sha256: str
    command: str


class ValidationExecutionError(Exception):
    """Stable, sanitized execute failure safe for CLI output."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_plan(
    *,
    source: str,
    dataset: DatasetKind,
    output_root: Path,
    case_ids: tuple[str, ...] | None = None,
    reference_bundle_root: Path | None = None,
) -> ValidationPlan:
    source = source.upper()
    if source not in _SDK_PACKAGES:
        raise ValueError("source must be XTQUANT, BAOSTOCK, AKSHARE or TUSHARE")
    runtime = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={}),
        reference_bundle_root=reference_bundle_root,
    )
    bundle_directory = (
        reference_bundle_root / "reference-bundles" / CONTROLLED_AUTHORITY_BUNDLE_ID
        if reference_bundle_root is not None
        else None
    )
    readiness = runtime.plan(
        source=ValidationSource(source),
        dataset=dataset,
        output_root=output_root,
        case_ids=case_ids,
        reference_bundle_directory=bundle_directory,
    )
    root = output_root.resolve(strict=False)
    command = (
        "python scripts/run_source_validation.py execute "
        f'--source {source} --dataset {dataset.value} --output-root "{root}" '
        f"--ack {REAL_EXECUTION_ACK} --authorization-file <one-time-authorization.json>"
    )
    return ValidationPlan(
        source=source,
        dataset=dataset.value,
        sample_spec_version=readiness.sample_spec_version,
        sample_spec_sha256=readiness.sample_spec_sha256,
        adapter_version=readiness.adapter_version,
        package_version=readiness.package_version,
        code_version=readiness.code_version,
        schema_version=readiness.schema_version,
        rule_version=readiness.rule_version,
        threshold_version=readiness.threshold_version,
        config_version=readiness.config_version,
        case_ids=readiness.case_ids,
        request_ranges=readiness.request_ranges,
        requested_fields=readiness.requested_fields,
        output_directory=readiness.output_directory,
        sdk=f"{_SDK_PACKAGES[source]}; local metadata={readiness.package_version}",
        network_access=False,
        qmt_connection=False,
        legacy_read=False,
        execute_network_access=readiness.network_required,
        execute_qmt_connection=readiness.qmt_required,
        sdk_loaded=False,
        unavoidable_side_effects=readiness.unavoidable_side_effects,
        blocking_reasons=readiness.blocking_reasons,
        reference_bundle_id=readiness.reference_bundle_id,
        reference_bundle_sha256=readiness.reference_bundle_sha256,
        command=command,
    )


def execute(
    *,
    source: str,
    dataset: DatasetKind,
    output_root: Path,
    acknowledgement: str,
    authorization: ExecutionAuthorization | None = None,
    authorization_verifier: AuthorizationVerifier | None = None,
    invocation_id: EntityId | None = None,
    authorization_claim_store: AuthorizationClaimStore | None = None,
    runtime_authorization: RuntimeAuthorizationEnvelope | None = None,
    runtime: RealValidationRuntime | None = None,
    case_ids: tuple[str, ...] | None = None,
    reference_bundle_root: Path | None = None,
) -> Path:
    if acknowledgement != REAL_EXECUTION_ACK:
        raise ValueError("exact real-execution acknowledgement is required")
    if any(
        value is not None
        for value in (
            authorization,
            authorization_verifier,
            invocation_id,
            authorization_claim_store,
            runtime_authorization,
        )
    ):
        raise ValidationExecutionError(
            "LEGACY_AUTHORIZATION_DTO_REJECTED",
            "executable authorization must be loaded only by the C0B strict file runtime",
        )
    if runtime is not None and type(runtime) is not RealValidationRuntime:
        raise TypeError("runtime must be the controlled RealValidationRuntime")
    runtime = runtime or RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={}),
        claim_store=authorization_claim_store,
        authorization_verifier=authorization_verifier,
        invocation_id=invocation_id,
        reference_bundle_root=reference_bundle_root,
    )
    bundle_directory = (
        reference_bundle_root / "reference-bundles" / CONTROLLED_AUTHORITY_BUNDLE_ID
        if reference_bundle_root is not None
        else None
    )
    try:
        return runtime.execute(
            source=ValidationSource(source.upper()),
            dataset=dataset,
            output_root=output_root,
            acknowledgement=acknowledgement,
            authorization=runtime_authorization,
            case_ids=case_ids,
            reference_bundle_directory=bundle_directory,
        )
    except RealRuntimeExecutionError as error:
        raise ValidationExecutionError(error.code, str(error)) from None


def verify(*, validation_root: Path, directory: Path) -> dict[str, object]:
    verifier = ValidationEvidenceVerifier(validation_root=validation_root)
    verified = (
        verifier.verify_assessment(assessment_directory=directory.parent)
        if directory.name == "_assessment"
        else verifier.verify(directory=directory)
    )
    assessment = verified.assessment
    return {
        "assessment_id": str(assessment.assessment_id),
        "blocking_reason": assessment.blocking_reason,
        "coverage_adequate": assessment.coverage_adequate,
        "dataset": assessment.dataset.value,
        "evidence_kind": assessment.evidence_kind.value,
        "fixed_sample_differences_adequate": (assessment.fixed_sample_differences_adequate),
        "manifest_sha256": verified.evidence_manifest_sha256,
        "quality_passed": assessment.quality_passed,
        "schema_unit_verified": assessment.schema_unit_verified,
        "semantic_contract_verified": assessment.semantic_contract_verified,
        "source": assessment.source,
        "stability_runs": assessment.stability_runs,
        "status": assessment.status.value,
    }


def status(*, validation_root: Path | None = None) -> list[dict[str, object]]:
    latest: dict[tuple[str, str], dict[str, object]] = {}
    if validation_root is not None and validation_root.exists():
        for manifest in validation_root.glob("source-assessments/*/*/*/_assessment/manifest.json"):
            try:
                item = verify(validation_root=validation_root, directory=manifest.parent)
            except Exception:
                continue
            latest[(str(item["source"]), str(item["dataset"]))] = item
    rows: list[dict[str, object]] = []
    for entry in default_source_matrix().entries:
        persisted = latest.get((entry.source, entry.dataset.value))
        rows.append(
            persisted
            or {
                "source": entry.source,
                "dataset": entry.dataset.value,
                "status": entry.status.value,
                "blocking_reason": entry.blocking_reason,
                "coverage_adequate": None,
                "fixed_sample_differences_adequate": None,
                "quality_passed": None,
                "schema_unit_verified": None,
                "semantic_contract_verified": None,
                "stability_runs": 0,
            }
        )
    return rows


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quantking-source-validation")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("plan", "execute"):
        command = sub.add_parser(name)
        command.add_argument("--source", required=True)
        command.add_argument(
            "--dataset", required=True, choices=[item.value for item in DatasetKind]
        )
        command.add_argument("--output-root", required=True, type=Path)
        command.add_argument("--case-id", action="append", dest="case_ids")
        command.add_argument("--reference-bundle-root", type=Path)
        if name == "execute":
            command.add_argument("--ack", required=True)
            command.add_argument("--authorization-file", required=True, type=Path)
    command = sub.add_parser("verify")
    command.add_argument("--validation-root", required=True, type=Path)
    command.add_argument("--directory", required=True, type=Path)
    command = sub.add_parser("status")
    command.add_argument("--validation-root", type=Path)
    command = sub.add_parser("fake-execute")
    command.add_argument("--authorization-file", required=True, type=Path)
    command.add_argument("--reference-bundle-root", required=True, type=Path)
    return parser


def _load_authorization(path: Path) -> RuntimeAuthorizationEnvelope:
    try:
        return RuntimeAuthorizationEnvelope.from_bytes(path.read_bytes())
    except (OSError, ValueError):
        raise ValidationExecutionError(
            "AUTHORIZATION_REQUIRED",
            "a canonical runtime authorization envelope is required",
        ) from None


def _execute_fake(*, authorization_file: Path, reference_bundle_root: Path) -> Path:
    """C0B's only executable CLI path; its bundle selection is fixed in code."""
    file_authority = FakeRuntimeAuthorizationAuthority()
    loader = RuntimeAuthorizationFileLoader(verifier=file_authority)
    try:
        loaded = loader.load(authorization_file)
    except ValueError:
        raise ValidationExecutionError(
            "AUTHORIZATION_REQUIRED", "a trusted canonical runtime authorization is required"
        ) from None
    try:
        result = ControlledFakeValidationRuntime(
            reference_bundle_root=reference_bundle_root,
            authorization_file_loader=loader,
            authorization_verifier=FakeAuthorizationAuthority(),
            claim_store=AuthorizationClaimStore(
                claim_root=Path(loaded.envelope.authorization.output_root)
                / ".runtime-authorization-claims"
            ),
        ).execute(authorization_file=authorization_file)
    except ControlledExecutionError as error:
        raise ValidationExecutionError(error.code, str(error)) from None
    return result.directory


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "plan":
            result: object = asdict(
                build_plan(
                    source=args.source,
                    dataset=DatasetKind(args.dataset),
                    output_root=args.output_root,
                    case_ids=tuple(args.case_ids) if args.case_ids else None,
                    reference_bundle_root=args.reference_bundle_root,
                )
            )
        elif args.action == "execute":
            if args.ack != REAL_EXECUTION_ACK:
                raise ValidationExecutionError(
                    "ACKNOWLEDGEMENT_REQUIRED",
                    "exact real-execution acknowledgement is required",
                )
            authorization = _load_authorization(args.authorization_file)
            result = str(
                execute(
                    source=args.source,
                    dataset=DatasetKind(args.dataset),
                    output_root=args.output_root,
                    acknowledgement=args.ack,
                    runtime_authorization=authorization,
                    case_ids=tuple(args.case_ids) if args.case_ids else None,
                    reference_bundle_root=args.reference_bundle_root,
                )
            )
        elif args.action == "fake-execute":
            result = str(
                _execute_fake(
                    authorization_file=args.authorization_file,
                    reference_bundle_root=args.reference_bundle_root,
                )
            )
        elif args.action == "verify":
            result = verify(validation_root=args.validation_root, directory=args.directory)
        else:
            result = status(validation_root=args.validation_root)
    except ValidationExecutionError as error:
        print(
            json.dumps({"error_code": error.code, "message": str(error)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


__all__ = [
    "REAL_EXECUTION_ACK",
    "ValidationExecutionError",
    "ValidationPlan",
    "build_plan",
    "execute",
    "main",
    "status",
    "verify",
]
