"""Injected, offline-safe CLI transport for EOD and explicit Worker execution."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import date
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
    enqueue = commands.add_parser("eod-enqueue")
    enqueue.add_argument("--trade-date", required=True)
    enqueue.add_argument("--datasets", nargs="+", required=True)
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
        if arguments.command == "eod-enqueue":
            try:
                trade_date = date.fromisoformat(arguments.trade_date)
            except ValueError as error:
                raise _ArgumentError from error
            outcome = eod_commands.enqueue(
                EodRunRequest(
                    start_date=trade_date,
                    end_date=trade_date,
                    datasets=tuple(arguments.datasets),
                    publish_intent=EodPublishIntent(arguments.publish_intent),
                ),
                idempotency_key=arguments.idempotency_key,
                requester="cli",
            )
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
        if arguments.command == "worker-run":
            worker.run(arguments.job_id)
            _write(stdout, _job_payload(jobs.get_job(arguments.job_id)))
            return 0
        if arguments.command == "job-status":
            _write(stdout, _job_payload(jobs.get_job(arguments.job_id)))
            return 0
        raise _ArgumentError
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


__all__ = ["WorkerRunPort", "run_cli"]
