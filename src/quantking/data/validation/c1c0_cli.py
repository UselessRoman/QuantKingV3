"""Formal file-only entry for S1-07C1C1's XTData observation handoff."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NoReturn

from quantking.data.validation.c1b_operation import (
    C1BOperationError,
)
from quantking.data.validation.c1b_operation import (
    load_authorization as load_c1b,
)
from quantking.data.validation.c1b_operation import (
    precheck_extension_authorization as precheck_c1b_extension,
)
from quantking.data.validation.c1c0_operation import (
    C1C0OperationError,
    execute,
    issue_authorization,
    load_plan,
    preflight_authorization_output,
    preflight_plan_output,
    prepare_plan,
    review_authorization,
    write_authorization,
    write_plan,
)


class _ArgumentParser(argparse.ArgumentParser):
    """Emit no user path or parser detail on the formal operation surface."""

    def error(self, _message: str) -> NoReturn:
        self.exit(2, json.dumps({"error_code": "C1C0_CLI_ARGUMENT_INVALID"}) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="quantking-s1-07c1c0")
    subcommands = parser.add_subparsers(dest="action", required=True)

    prepare = subcommands.add_parser("prepare")
    prepare.add_argument("--c1b-authorization-file", required=True, type=Path)
    prepare.add_argument("--plan-file", required=True, type=Path)

    issue = subcommands.add_parser("issue")
    issue.add_argument("--plan-file", required=True, type=Path)
    issue.add_argument("--authorization-file", required=True, type=Path)

    review = subcommands.add_parser("review")
    review.add_argument("--authorization-file", required=True, type=Path)

    execute_command = subcommands.add_parser("execute")
    execute_command.add_argument("--authorization-file", required=True, type=Path)
    execute_command.add_argument("--ack", required=True)
    return parser


def _prepare(args: argparse.Namespace) -> dict[str, object]:
    preflight_plan_output(plan_file=args.plan_file)
    c1b_authorization = load_c1b(authorization_file=args.c1b_authorization_file)
    precheck_c1b_extension(authorization=c1b_authorization)
    plan = prepare_plan(c1b_authorization=c1b_authorization)
    write_plan(plan=plan, plan_file=args.plan_file)
    return {
        "c1b_authorization_sha256": plan.c1b_authorization_sha256,
        "plan_sha256": plan.plan_sha256,
    }


def _issue(args: argparse.Namespace) -> dict[str, object]:
    plan = load_plan(plan_file=args.plan_file)
    preflight_authorization_output(authorization_file=args.authorization_file)
    authorization = issue_authorization(plan=plan)
    write_authorization(authorization=authorization, authorization_file=args.authorization_file)
    return review_authorization(authorization_file=args.authorization_file)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "prepare":
            result = _prepare(args)
        elif args.action == "issue":
            result = _issue(args)
        elif args.action == "review":
            result = review_authorization(authorization_file=args.authorization_file)
        else:
            directory = execute(
                authorization_file=args.authorization_file, acknowledgement=args.ack
            )
            result = {"directory": str(directory)}
    except (C1BOperationError, C1C0OperationError) as error:
        print(json.dumps({"error_code": error.code}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


__all__ = ["main"]
