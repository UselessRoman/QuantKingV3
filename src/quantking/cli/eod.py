"""Injected, offline-safe CLI transport for EOD and explicit Worker execution."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import NoReturn, Protocol, TextIO

from quantking.common.errors import QuantKingError
from quantking.data.eod.contracts import EodPublishIntent, EodRunRequest
from quantking.data.eod.service import EodEnqueueResult
from quantking.jobs.models import JobRun


class EodCommandPort(Protocol):
    def enqueue(
        self,
        request: EodRunRequest,
        *,
        idempotency_key: str,
        requester: str,
    ) -> EodEnqueueResult: ...


class JobReadPort(Protocol):
    def get_job(self, job_id: str) -> JobRun: ...


class WorkerRunPort(Protocol):
    def run(self, job_id: str) -> None: ...


class _ArgumentError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise _ArgumentError


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="quantking-data", add_help=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("eod-dry-run")
    enqueue = commands.add_parser("eod-enqueue")
    dates = enqueue.add_mutually_exclusive_group(required=True)
    dates.add_argument("--trade-date")
    dates.add_argument("--start-date")
    enqueue.add_argument("--end-date")
    enqueue.add_argument("--datasets", nargs="+", required=True)
    enqueue.add_argument(
        "--base-version",
        action="append",
        default=[],
        metavar="DATASET=VERSION_ID",
    )
    enqueue.add_argument("--idempotency-key", required=True)
    enqueue.add_argument(
        "--publish-intent",
        required=True,
        choices=[EodPublishIntent.PUBLISH_AFTER_QUALITY.value],
    )
    worker = commands.add_parser("worker-run")
    worker.add_argument("--job-id", required=True)
    status = commands.add_parser("job-status")
    status.add_argument("--job-id", required=True)
    return parser


def _write(stream: TextIO, payload: object) -> None:
    stream.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def _job_payload(job: JobRun) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "status": job.status.value,
        "stage": job.stage,
        "progress": job.progress,
        "row_version": job.row_version,
        "failure": (
            None
            if job.failure is None
            else {
                "error_type": job.failure.error_type,
                "message": job.failure.message,
                "retryable": job.failure.retryable,
            }
        ),
    }


def _base_versions(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if not isinstance(value, str) or value.count("=") != 1:
            raise _ArgumentError
        dataset, version_id = value.split("=", 1)
        if not dataset or not version_id or dataset in result:
            raise _ArgumentError
        result[dataset] = version_id
    return result


def run_cli(
    argv: Sequence[str],
    *,
    eod_commands: EodCommandPort,
    worker: WorkerRunPort,
    jobs: JobReadPort,
    stdout: TextIO,
) -> int:
    """Parse one explicit command and emit exactly one stable JSON line."""
    try:
        arguments = _parser().parse_args(list(argv))
    except _ArgumentError:
        _write(
            stdout,
            {
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": "CLI arguments are invalid.",
                }
            },
        )
        return 2
    if arguments.command == "eod-enqueue":
        try:
            try:
                if arguments.trade_date is not None:
                    if arguments.end_date is not None:
                        raise _ArgumentError
                    start_date = date.fromisoformat(arguments.trade_date)
                    end_date = start_date
                else:
                    if arguments.start_date is None or arguments.end_date is None:
                        raise _ArgumentError
                    start_date = date.fromisoformat(arguments.start_date)
                    end_date = date.fromisoformat(arguments.end_date)
            except ValueError as error:
                raise _ArgumentError from error
            request = EodRunRequest(
                start_date=start_date,
                end_date=end_date,
                datasets=tuple(arguments.datasets),
                publish_intent=EodPublishIntent(arguments.publish_intent),
                base_versions=_base_versions(arguments.base_version),
            )
        except (_ArgumentError, TypeError, ValueError):
            _write(
                stdout,
                {
                    "error": {
                        "code": "INVALID_ARGUMENT",
                        "message": "CLI arguments are invalid.",
                    }
                },
            )
            return 2
        try:
            outcome = eod_commands.enqueue(
                request,
                idempotency_key=arguments.idempotency_key,
                requester="cli",
            )
        except QuantKingError as error:
            _write(
                stdout,
                {
                    "error": {
                        "code": error.code.value,
                        "message": "The command could not be completed.",
                    }
                },
            )
            return 1
        except Exception:
            _write(
                stdout,
                {
                    "error": {
                        "code": "COMMAND_FAILED",
                        "message": "The command could not be completed.",
                    }
                },
            )
            return 1
        _write(
            stdout,
            {
                "job_id": outcome.job_id,
                "status": outcome.status.value,
                "created": outcome.created,
                "reused": not outcome.created,
            },
        )
        return 0
    try:
        if arguments.command == "worker-run":
            worker.run(arguments.job_id)
            _write(stdout, _job_payload(jobs.get_job(arguments.job_id)))
            return 0
        if arguments.command == "job-status":
            _write(stdout, _job_payload(jobs.get_job(arguments.job_id)))
            return 0
        raise _ArgumentError
    except _ArgumentError:
        _write(
            stdout,
            {
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": "CLI arguments are invalid.",
                }
            },
        )
        return 2
    except QuantKingError as error:
        _write(
            stdout,
            {
                "error": {
                    "code": error.code.value,
                    "message": "The command could not be completed.",
                }
            },
        )
        return 1
    except Exception:
        _write(
            stdout,
            {
                "error": {
                    "code": "WORKER_FAILED",
                    "message": "The explicit Worker execution failed.",
                }
            },
        )
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run the installed local CLI; runtime construction is deferred until after help."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if any(item in {"-h", "--help"} for item in arguments):
        _parser().parse_args(list(arguments))
        return 0
    if arguments and arguments[0] == "eod-dry-run":
        try:
            _parser().parse_args(list(arguments))
            from quantking.common.config import load_settings
            from quantking.data.eod.release import (
                S107K4_CODE_VERSION,
                build_s108_dry_run,
            )

            repository_root = Path(__file__).resolve().parents[3]
            settings = load_settings(repository_root / "configs" / "default.toml")
            _write(
                sys.stdout,
                build_s108_dry_run(settings, code_version=S107K4_CODE_VERSION),
            )
            return 0
        except Exception:
            _write(
                sys.stdout,
                {
                    "error": {
                        "code": "DRY_RUN_FAILED",
                        "message": "The S1-08 dry-run plan could not be produced.",
                    }
                },
            )
            return 1
    try:
        from quantking.common.time import UtcTimestamp, utc_now
        from quantking.runtime import (
            WORKER_LEASE_TIMEOUT,
            build_eod_runtime,
            load_runtime_settings,
        )

        runtime = build_eod_runtime(load_runtime_settings())
        if arguments and arguments[0] == "worker-run":
            now = utc_now()
            recovered_job_ids = runtime.jobs.recover_stale_running_job_ids(
                reason="expired Worker lease recovered at explicit Worker startup",
                stale_before=UtcTimestamp(now.value - WORKER_LEASE_TIMEOUT),
                at=now,
            )
            for job_id in recovered_job_ids:
                runtime.eod_handler.recover_interrupted(runtime.jobs, job_id)
    except Exception:
        _write(
            sys.stdout,
            {
                "error": {
                    "code": "RUNTIME_UNAVAILABLE",
                    "message": "The local EOD runtime could not be initialized.",
                }
            },
        )
        return 1
    return run_cli(
        arguments,
        eod_commands=runtime.eod_commands,
        worker=runtime.worker,
        jobs=runtime.jobs,
        stdout=sys.stdout,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["WorkerRunPort", "main", "run_cli"]
