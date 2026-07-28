"""Offline-safe CLI for plan, execute, verify and status."""

from __future__ import annotations

import argparse
import importlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from quantking.data.assessment.matrix import default_source_matrix
from quantking.data.models import DatasetKind
from quantking.data.validation.artifacts import ValidationEvidenceVerifier
from quantking.data.validation.samples import load_packaged_fixed_sample

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
    command: str


class RealValidationRunner(Protocol):
    def execute(self, *, plan: ValidationPlan, output_root: Path) -> Path: ...


def build_plan(*, source: str, dataset: DatasetKind, output_root: Path) -> ValidationPlan:
    source = source.upper()
    if source not in _SDK_PACKAGES:
        raise ValueError("source must be XTQUANT, BAOSTOCK, AKSHARE or TUSHARE")
    root = output_root.resolve(strict=False)
    if not output_root.is_absolute():
        raise ValueError("output_root must be absolute")
    sample = load_packaged_fixed_sample()
    cases = tuple(
        case
        for case in sample.cases
        if case.dataset is dataset and source in case.applicable_sources
    )
    output = root / "source-assessments" / source.lower() / dataset.value / "<assessment_id>"
    command = (
        "python scripts/run_source_validation.py execute "
        f'--source {source} --dataset {dataset.value} --output-root "{root}" '
        f"--ack {REAL_EXECUTION_ACK}"
    )
    return ValidationPlan(
        source=source,
        dataset=dataset.value,
        case_ids=tuple(case.case_id for case in cases),
        request_ranges=tuple(
            f"{case.code or case.exchange}:{case.start_date}:{case.end_date}" for case in cases
        ),
        requested_fields=tuple(sorted({field for case in cases for field in case.fields})),
        output_directory=str(output),
        sdk=_SDK_PACKAGES[source],
        network_access=False,
        qmt_connection=False,
        legacy_read=False,
        execute_network_access=True,
        execute_qmt_connection=source == "XTQUANT",
        sdk_loaded=False,
        unavoidable_side_effects=(
            "execute creates a new immutable validation evidence directory",
            "the provider may create process-local caches or sessions",
        ),
        command=command,
    )


def execute(
    *,
    source: str,
    dataset: DatasetKind,
    output_root: Path,
    acknowledgement: str,
) -> Path:
    if acknowledgement != REAL_EXECUTION_ACK:
        raise ValueError("exact real-execution acknowledgement is required")
    plan = build_plan(source=source, dataset=dataset, output_root=output_root)
    module_name = f"quantking.data.validation.runners.{source.lower()}"
    try:
        module = importlib.import_module(module_name)
        runner: RealValidationRunner = module.create_runner()
        result = runner.execute(plan=plan, output_root=output_root)
        if not isinstance(result, Path):
            raise TypeError("provider runner must return a Path")
        return result
    except ModuleNotFoundError as error:
        if error.name == module_name:
            raise RuntimeError(
                "authorized provider runner is not installed; keep the matrix blocked"
            ) from error
        raise


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
        if name == "execute":
            command.add_argument("--ack", required=True)
    command = sub.add_parser("verify")
    command.add_argument("--validation-root", required=True, type=Path)
    command.add_argument("--directory", required=True, type=Path)
    command = sub.add_parser("status")
    command.add_argument("--validation-root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.action == "plan":
        result: object = asdict(
            build_plan(
                source=args.source,
                dataset=DatasetKind(args.dataset),
                output_root=args.output_root,
            )
        )
    elif args.action == "execute":
        result = {
            "directory": str(
                execute(
                    source=args.source,
                    dataset=DatasetKind(args.dataset),
                    output_root=args.output_root,
                    acknowledgement=args.ack,
                )
            )
        }
    elif args.action == "verify":
        result = verify(validation_root=args.validation_root, directory=args.directory)
    else:
        result = status(validation_root=args.validation_root)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


__all__ = [
    "REAL_EXECUTION_ACK",
    "ValidationPlan",
    "build_plan",
    "execute",
    "main",
    "status",
    "verify",
]
