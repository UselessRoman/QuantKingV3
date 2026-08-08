"""Command-line surface for the S1-07C1B formal operation entry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from quantking.data.models import DatasetKind
from quantking.data.validation.c1b_operation import (
    C1BOperationError,
    execute,
    issue_authorization,
    load_authorization,
    load_plan,
    preflight_authorization_output,
    preflight_plan_output,
    prepare_plan,
    write_authorization,
    write_plan,
)


def _add_prepare_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--source", required=True)
    command.add_argument("--dataset", required=True, choices=[DatasetKind.TRADING_STATUS.value])
    command.add_argument("--case-id", required=True)
    command.add_argument("--xtquant-site-packages-root", required=True, type=Path)
    command.add_argument("--reference-bundle-root", required=True, type=Path)
    command.add_argument("--reference-bundle-id", required=True)
    command.add_argument("--reference-bundle-sha256", required=True)
    command.add_argument("--output-root", required=True, type=Path)
    command.add_argument("--claim-root", required=True, type=Path)
    command.add_argument("--metadata-database", required=True, type=Path)
    command.add_argument("--sqlite-busy-timeout-ms", required=True, type=int)
    command.add_argument("--assessment-id", required=True)
    command.add_argument("--session-id", required=True)
    command.add_argument("--invocation-id", required=True)
    command.add_argument("--run-id", required=True)
    command.add_argument("--authorization-id", required=True)
    command.add_argument("--authorization-private-key-file", required=True, type=Path)
    command.add_argument("--authorization-public-key-file", required=True, type=Path)
    command.add_argument("--authorization-allowed-key-id", action="append", required=True)
    command.add_argument("--runner-private-key-file", required=True, type=Path)
    command.add_argument("--runner-public-key-file", required=True, type=Path)
    command.add_argument("--runner-allowed-key-id", action="append", required=True)
    command.add_argument("--valid-from", required=True)
    command.add_argument("--expires-at", required=True)
    command.add_argument("--claim-nonce", required=True)
    command.add_argument("--adapter-version", required=True)
    command.add_argument("--package-version", required=True)
    command.add_argument("--code-version", required=True)
    command.add_argument("--schema-version", required=True)
    command.add_argument("--rule-version", required=True)
    command.add_argument("--threshold-version", required=True)
    command.add_argument("--sample-spec-version", required=True)
    command.add_argument("--config-version", required=True)
    command.add_argument("--plan-file", required=True, type=Path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quantking-s1-07c1b")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("prepare", "plan"):
        _add_prepare_arguments(sub.add_parser(name))
    for name in ("issue", "authorize"):
        command = sub.add_parser(name)
        command.add_argument("--plan-file", required=True, type=Path)
        command.add_argument("--authorization-file", required=True, type=Path)
    command = sub.add_parser("review")
    command.add_argument("--authorization-file", required=True, type=Path)
    command = sub.add_parser("execute")
    command.add_argument("--authorization-file", required=True, type=Path)
    command.add_argument("--ack", required=True)
    return parser


def _prepare(args: argparse.Namespace) -> dict[str, object]:
    preflight_plan_output(plan_file=args.plan_file)
    plan = prepare_plan(
        source=args.source,
        dataset=DatasetKind(args.dataset),
        case_id=args.case_id,
        xtquant_site_packages_root=args.xtquant_site_packages_root,
        reference_bundle_root=args.reference_bundle_root,
        reference_bundle_id=args.reference_bundle_id,
        reference_bundle_sha256=args.reference_bundle_sha256,
        output_root=args.output_root,
        claim_root=args.claim_root,
        metadata_database=args.metadata_database,
        sqlite_busy_timeout_ms=args.sqlite_busy_timeout_ms,
        assessment_id=args.assessment_id,
        session_id=args.session_id,
        invocation_id=args.invocation_id,
        run_id=args.run_id,
        authorization_id=args.authorization_id,
        authorization_private_key_file=args.authorization_private_key_file,
        authorization_public_key_file=args.authorization_public_key_file,
        authorization_allowed_key_ids=tuple(args.authorization_allowed_key_id),
        runner_private_key_file=args.runner_private_key_file,
        runner_public_key_file=args.runner_public_key_file,
        runner_allowed_key_ids=tuple(args.runner_allowed_key_id),
        valid_from=args.valid_from,
        expires_at=args.expires_at,
        claim_nonce=args.claim_nonce,
        adapter_version=args.adapter_version,
        package_version=args.package_version,
        code_version=args.code_version,
        schema_version=args.schema_version,
        rule_version=args.rule_version,
        threshold_version=args.threshold_version,
        sample_spec_version=args.sample_spec_version,
        config_version=args.config_version,
    )
    write_plan(plan=plan, plan_file=args.plan_file)
    return plan.review()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action in {"prepare", "plan"}:
            result = _prepare(args)
        elif args.action in {"issue", "authorize"}:
            plan = load_plan(plan_file=args.plan_file)
            preflight_authorization_output(authorization_file=args.authorization_file)
            authorization = issue_authorization(plan=plan)
            write_authorization(
                authorization=authorization, authorization_file=args.authorization_file
            )
            result = authorization.review()
        elif args.action == "review":
            result = load_authorization(authorization_file=args.authorization_file).review()
        else:
            result = execute(
                authorization_file=args.authorization_file,
                acknowledgement=args.ack,
            ).review()
    except C1BOperationError as error:
        print(
            json.dumps({"error_code": error.code, "message": str(error)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


__all__ = ["main"]
