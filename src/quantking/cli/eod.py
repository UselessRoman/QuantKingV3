"""Injected, offline-safe CLI transport for EOD and explicit Worker execution."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, TextIO

from quantking.common.errors import QuantKingError
from quantking.data.eod.contracts import EodPublishIntent, EodRunRequest
from quantking.data.eod.service import EodEnqueueResult
from quantking.jobs.models import JobRun

if TYPE_CHECKING:
    from collections.abc import Mapping

    from quantking.common.config import AppSettings
    from quantking.data.adapters.sources.ingestion import MarketDataGateway
    from quantking.data.models import DataUpstream


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
    dry_run = commands.add_parser("eod-dry-run")
    dry_run.add_argument("--manifest", required=True)
    dry_run.add_argument("--data-root-label", default=r"E:\QuantKingV3Data")
    formal = commands.add_parser("eod-formal-run")
    formal.add_argument("--manifest", required=True)
    formal.add_argument("--envelope", required=True)
    formal.add_argument("--attempts-root", required=True)
    formal.add_argument(
        "--authorization-env",
        # Authorization tokens must NOT use the QUANTKING_ namespace:
        # load_runtime_settings fails closed on unknown QUANTKING_* keys.
        default="R3_FORMAL_AUTHORIZATION",
    )
    calendar = commands.add_parser("eod-calendar-publish")
    calendar.add_argument("--plan", required=True)
    calendar.add_argument("--attempts-root", required=True)
    calendar.add_argument(
        "--authorization-env",
        default="R3_CALENDAR_AUTHORIZATION",
    )
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


class FormalEntryError(Exception):
    """Stable refusal/failure code for the single formal R3 entry."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _read_git_head(repository_root: Path) -> str:
    git_directory = repository_root / ".git"
    if not git_directory.is_dir():
        raise FormalEntryError("FORMAL_GIT_HEAD_UNREADABLE")
    head_file = git_directory / "HEAD"
    if not head_file.is_file():
        raise FormalEntryError("FORMAL_GIT_HEAD_UNREADABLE")
    head = head_file.read_text(encoding="utf-8").strip()
    if not head.startswith("ref: "):
        return head
    reference = git_directory / head[5:].strip()
    if not reference.is_file():
        raise FormalEntryError("FORMAL_GIT_HEAD_UNREADABLE")
    return reference.read_text(encoding="utf-8").strip()


def run_formal_entry(
    *,
    manifest_path: Path,
    envelope_path: Path,
    attempts_root: Path,
    authorization: str | None,
    settings: AppSettings,
    code_root: Path,
    gateways: Mapping[DataUpstream, MarketDataGateway] | None = None,
    git_head_reader: Callable[[], str] | None = None,
    declared_data_root: Path | None = None,
    attempts_root_base: Path | None = None,
) -> dict[str, object]:
    """Execute the one authorized formal R3 release through the shared stack.

    Every guard fails closed with a stable code before the first provider
    call and before any formal business file is written: the ADR-028 content
    bound token must match the marker, authorization id and the recomputed
    envelope SHA, the envelope must match the manifest, the code tree and git
    HEAD must still match the frozen SHAs, the write-path template, calendar
    source, data root and attempts root must sit inside the declared entry
    boundary, and the attempt ledger must prove this authorization id was
    never used. The default path (no authorization environment) always
    refuses.
    """
    from quantking.data.eod.formal_authorization import (
        DECLARED_FORMAL_DATA_ROOT_LABEL,
        FormalAttemptLedger,
        FormalAuthorizationEnvelope,
        FormalAuthorizationError,
        assert_envelope_matches_manifest,
        assert_formal_entry_boundary,
        compute_code_tree_sha256,
    )
    from quantking.data.eod.release_manifest import ReleaseManifest, ReleaseManifestError
    from quantking.data.eod.service import EodExecutionConfig
    from quantking.runtime import build_eod_runtime

    if not isinstance(authorization, str) or len(authorization.split(":")) != 3:
        raise FormalEntryError("FORMAL_AUTHORIZATION_REQUIRED")
    try:
        manifest = ReleaseManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ReleaseManifestError, TypeError):
        raise FormalEntryError("FORMAL_MANIFEST_INVALID") from None
    try:
        envelope = FormalAuthorizationEnvelope.from_dict(
            json.loads(envelope_path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, TypeError):
        raise FormalEntryError("FORMAL_ENVELOPE_INVALID") from None
    # ADR-028 content binding: the token must carry the SHA of the exact
    # envelope content being executed, so any drift in the envelope file
    # invalidates previously issued marker/id tokens before anything runs.
    token_marker, token_authorization_id, token_envelope_sha = authorization.split(":")
    if (
        token_marker != envelope.authorization_marker
        or token_authorization_id != envelope.authorization_id
        or token_envelope_sha != envelope.envelope_sha256()
    ):
        raise FormalEntryError("FORMAL_AUTHORIZATION_MISMATCH")
    try:
        assert_envelope_matches_manifest(envelope, manifest)
    except FormalAuthorizationError:
        raise FormalEntryError("FORMAL_ENVELOPE_MANIFEST_MISMATCH") from None
    if not envelope.calendar_version_id.startswith("data_version:"):
        raise FormalEntryError("FORMAL_CALENDAR_BINDING_INVALID")
    try:
        actual_tree = compute_code_tree_sha256(code_root / "src" / "quantking")
    except (OSError, FormalAuthorizationError):
        raise FormalEntryError("FORMAL_CODE_TREE_UNREADABLE") from None
    if actual_tree != envelope.code_tree_sha256:
        raise FormalEntryError("FORMAL_CODE_TREE_DRIFT")
    try:
        actual_head = git_head_reader() if git_head_reader else _read_git_head(code_root)
    except FormalEntryError:
        raise
    except OSError:
        raise FormalEntryError("FORMAL_GIT_HEAD_UNREADABLE") from None
    if actual_head != envelope.git_head:
        raise FormalEntryError("FORMAL_GIT_HEAD_DRIFT")
    boundary_code_map = {
        "ENVELOPE_WRITE_PATHS_INVALID": "FORMAL_WRITE_PATHS_INVALID",
        "CALENDAR_SOURCE_INVALID": "FORMAL_CALENDAR_SOURCE_INVALID",
        "CALENDAR_BINDING_INVALID": "FORMAL_CALENDAR_BINDING_INVALID",
        "DATA_ROOT_INVALID": "FORMAL_DATA_ROOT_INVALID",
        "ATTEMPTS_ROOT_INVALID": "FORMAL_ATTEMPTS_ROOT_INVALID",
    }
    try:
        assert_formal_entry_boundary(
            envelope,
            settings_data_root=settings.paths.data_root,
            declared_data_root=(
                Path(DECLARED_FORMAL_DATA_ROOT_LABEL)
                if declared_data_root is None
                else declared_data_root
            ),
            attempts_root=attempts_root,
            attempts_root_base=code_root if attempts_root_base is None else attempts_root_base,
        )
    except FormalAuthorizationError as error:
        raise FormalEntryError(
            boundary_code_map.get(error.code, "FORMAL_BOUNDARY_INVALID")
        ) from None

    from quantking.common.time import utc_now

    ledger = FormalAttemptLedger(attempts_root)
    try:
        ledger.begin(
            envelope_sha256=envelope.envelope_sha256(),
            marker=envelope.authorization_marker,
            authorization_id=envelope.authorization_id,
            idempotency_key=envelope.idempotency_key,
            now_iso=utc_now().to_iso(),
        )
    except FormalAuthorizationError:
        raise FormalEntryError("AUTHORIZATION_ALREADY_CONSUMED") from None

    config = EodExecutionConfig.from_release_manifest(manifest)
    # The envelope owns the concrete published calendar binding; graft it
    # into the frozen dataset config so the worker preflight verifies the
    # exact PUBLISHED calendar version by id and content SHA.
    from dataclasses import replace as _replace

    from quantking.data.eod.contracts import EodReferenceVersion
    from quantking.data.models import DatasetKind

    dataset_config = config.datasets["daily_bar_unadjusted"]
    config = _replace(
        config,
        datasets={
            "daily_bar_unadjusted": _replace(
                dataset_config,
                references={
                    **dict(dataset_config.references),
                    "exchange_calendar": EodReferenceVersion(
                        dataset=DatasetKind.EXCHANGE_CALENDAR.value,
                        version_id=envelope.calendar_version_id,
                        content_sha256=envelope.calendar_sha256,
                    ),
                },
            ),
        },
    )
    runtime = build_eod_runtime(settings, gateways=gateways, execution_config=config)
    request = EodRunRequest(
        start_date=manifest.target_start,
        end_date=manifest.target_end,
        datasets=("daily_bar_unadjusted",),
        publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
        base_versions={},
    )
    outcome = runtime.eod_commands.enqueue(
        request,
        idempotency_key=envelope.idempotency_key,
        requester="formal-r3",
    )
    from quantking.jobs.models import JobStatus

    try:
        runtime.worker.run(outcome.job_id)
        final = runtime.jobs.get_job(outcome.job_id)
        if final.status is not JobStatus.SUCCEEDED:
            raise RuntimeError("formal job did not succeed")
        ledger.record(
            envelope.authorization_id,
            status="COMPLETED",
            now_iso=utc_now().to_iso(),
            detail={"job_id": outcome.job_id},
        )
    except Exception:
        ledger.record(
            envelope.authorization_id,
            status="FAILED",
            now_iso=utc_now().to_iso(),
            detail={"job_id": outcome.job_id},
        )
        raise FormalEntryError("FORMAL_EXECUTION_FAILED") from None
    return {
        "job_id": outcome.job_id,
        "status": "SUCCEEDED",
        "created": outcome.created,
        "authorization_id": envelope.authorization_id,
        "envelope_sha256": envelope.envelope_sha256(),
        "release_manifest_sha256": envelope.release_manifest_sha256,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the installed local CLI; runtime construction is deferred until after help."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if any(item in {"-h", "--help"} for item in arguments):
        _parser().parse_args(list(arguments))
        return 0
    if arguments and arguments[0] == "eod-formal-run":
        try:
            parsed = _parser().parse_args(list(arguments))
            from quantking.cli.eod import FormalEntryError, run_formal_entry

            repository_root = Path(__file__).resolve().parents[3]
            from quantking.runtime import load_runtime_settings

            payload = run_formal_entry(
                manifest_path=Path(parsed.manifest),
                envelope_path=Path(parsed.envelope),
                attempts_root=Path(parsed.attempts_root),
                authorization=os.environ.get(parsed.authorization_env),
                settings=load_runtime_settings(),
                code_root=repository_root,
            )
        except FormalEntryError as error:
            _write(
                sys.stdout,
                {
                    "error": {
                        "code": error.code,
                        "message": "The formal R3 execution was refused or failed.",
                    }
                },
            )
            return 1
        except Exception:
            _write(
                sys.stdout,
                {
                    "error": {
                        "code": "FORMAL_ENTRY_UNAVAILABLE",
                        "message": "The formal R3 entry could not be initialized.",
                    }
                },
            )
            return 1
        _write(sys.stdout, payload)
        return 0
    if arguments and arguments[0] == "eod-calendar-publish":
        try:
            parsed = _parser().parse_args(list(arguments))
            from quantking.common.ids import new_entity_id
            from quantking.data.adapters.sources.baostock import BaostockCalendarRunner
            from quantking.data.eod.calendar_bootstrap import (
                CalendarBootstrapError,
                execute_calendar_bootstrap,
            )
            from quantking.runtime import build_eod_runtime, load_runtime_settings

            repository_root = Path(__file__).resolve().parents[3]

            def _runtime_factory() -> Any:
                return build_eod_runtime(load_runtime_settings())

            payload = execute_calendar_bootstrap(
                plan=json.loads(Path(parsed.plan).read_text(encoding="utf-8")),
                authorization=os.environ.get(parsed.authorization_env),
                runtime_factory=_runtime_factory,
                runner=BaostockCalendarRunner(),
                attempts_root=Path(parsed.attempts_root),
                code_root=repository_root,
                version_id=new_entity_id("data_version"),
                batch_id=new_entity_id("data_batch"),
            )
        except CalendarBootstrapError as error:
            _write(
                sys.stdout,
                {
                    "error": {
                        "code": error.code,
                        "message": "The authorized C1 calendar bootstrap was refused or failed.",
                    }
                },
            )
            return 1
        except Exception:
            _write(
                sys.stdout,
                {
                    "error": {
                        "code": "CALENDAR_BOOTSTRAP_UNAVAILABLE",
                        "message": "The C1 calendar bootstrap entry could not be initialized.",
                    }
                },
            )
            return 1
        _write(sys.stdout, payload)
        return 0
    if arguments and arguments[0] == "eod-dry-run":
        try:
            parsed = _parser().parse_args(list(arguments))
            from quantking.data.eod.release_manifest import (
                ReleaseManifest,
                build_zero_network_dry_run,
            )

            manifest = ReleaseManifest.from_dict(
                json.loads(Path(parsed.manifest).read_text(encoding="utf-8"))
            )
            _write(
                sys.stdout,
                build_zero_network_dry_run(
                    manifest,
                    data_root_label=parsed.data_root_label,
                ),
            )
            return 0
        except Exception:
            _write(
                sys.stdout,
                {
                    "error": {
                        "code": "DRY_RUN_FAILED",
                        "message": "The release dry-run plan could not be produced.",
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
