from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

from quantking.cli.eod import run_cli
from quantking.data.eod.contracts import EodBaseVersion
from quantking.data.eod.service import EodCommandService, EodExecutionConfig
from quantking.jobs.adapters.persistence.sqlite import (
    SqliteUnitOfWorkFactory,
    create_sqlite_engine,
    initialize_database,
)
from quantking.jobs.services import JobService


class NoWorker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, job_id: str) -> None:
        self.calls.append(job_id)


def _ports(tmp_path: Path) -> tuple[EodCommandService, JobService, NoWorker]:
    database = tmp_path / "cli.db"
    initialize_database(database)
    jobs = JobService(SqliteUnitOfWorkFactory(create_sqlite_engine(database, busy_timeout_ms=250)))
    command = EodCommandService(
        jobs,
        config=EodExecutionConfig.production(code_version="test-code"),
    )
    return command, jobs, NoWorker()


def test_cli_enqueue_outputs_stable_created_and_reused_json(tmp_path: Path) -> None:
    command, jobs, worker = _ports(tmp_path)
    argv = [
        "eod-enqueue",
        "--trade-date",
        "2026-07-15",
        "--datasets",
        "exchange_calendar",
        "--idempotency-key",
        "cli-key",
        "--publish-intent",
        "PUBLISH_AFTER_QUALITY",
    ]
    first = io.StringIO()
    replay = io.StringIO()

    assert run_cli(argv, eod_commands=command, worker=worker, jobs=jobs, stdout=first) == 0
    assert run_cli(argv, eod_commands=command, worker=worker, jobs=jobs, stdout=replay) == 0

    first_payload = json.loads(first.getvalue())
    replay_payload = json.loads(replay.getvalue())
    assert first_payload["job_id"] == replay_payload["job_id"]
    assert first_payload["created"] is True
    assert first_payload["reused"] is False
    assert replay_payload["created"] is False
    assert replay_payload["reused"] is True
    assert worker.calls == []


def test_cli_freezes_controlled_exact_base_mapping(tmp_path: Path) -> None:
    database = tmp_path / "cli-base.db"
    initialize_database(database)
    jobs = JobService(SqliteUnitOfWorkFactory(create_sqlite_engine(database, busy_timeout_ms=250)))
    version_id = "data_version:11111111-1111-4111-8111-111111111111"
    base = EodBaseVersion(
        version_id=version_id,
        dataset="exchange_calendar",
        schema_version="exchange_calendar.v1",
        content_sha256="a" * 64,
        manifest_sha256="b" * 64,
        coverage=tuple((exchange, "2026-07-15", "2026-07-15") for exchange in ("BJ", "SH", "SZ")),
    )

    class Resolver:
        def resolve(self, *, dataset: str, version_id: str, scope: tuple[str, ...]):
            assert dataset == base.dataset
            assert version_id == base.version_id
            assert scope == ("BJ", "SH", "SZ")
            return base

    command = EodCommandService(
        jobs,
        config=EodExecutionConfig.production(code_version="test-code"),
        base_resolver=Resolver(),
    )
    output = io.StringIO()
    code = run_cli(
        [
            "eod-enqueue",
            "--trade-date",
            "2026-07-15",
            "--datasets",
            "exchange_calendar",
            "--base-version",
            f"exchange_calendar={version_id}",
            "--idempotency-key",
            "cli-base",
            "--publish-intent",
            "PUBLISH_AFTER_QUALITY",
        ],
        eod_commands=command,
        worker=NoWorker(),
        jobs=jobs,
        stdout=output,
    )

    job = jobs.get_job(json.loads(output.getvalue())["job_id"])
    frozen = json.loads(job.config_manifest["config_json"])
    assert code == 0
    assert frozen["datasets"]["exchange_calendar"]["base_version"]["version_id"] == version_id


def test_cli_invalid_or_forbidden_inputs_return_stable_nonzero_error(tmp_path: Path) -> None:
    command, jobs, worker = _ports(tmp_path)
    for argv in (
        ["eod-enqueue", "--trade-date", "latest"],
        [
            "eod-enqueue",
            "--trade-date",
            "2026-07-15",
            "--datasets",
            "C:\\private",
            "--idempotency-key",
            "x",
            "--publish-intent",
            "PUBLISH_AFTER_QUALITY",
        ],
        ["worker-run", "--handler", "evil"],
    ):
        output = io.StringIO()
        assert run_cli(argv, eod_commands=command, worker=worker, jobs=jobs, stdout=output) == 2
        assert json.loads(output.getvalue()) == {
            "error": {
                "code": "INVALID_ARGUMENT",
                "message": "CLI arguments are invalid.",
            }
        }


def test_module_entrypoint_help_is_available_without_runtime_side_effects() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "quantking.cli.eod", "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert "usage:" in completed.stdout
    assert "eod-enqueue" in completed.stdout


def test_internal_type_error_is_runtime_failure_not_argument_error(tmp_path: Path) -> None:
    _command, jobs, worker = _ports(tmp_path)

    class ExplodingCommand:
        def enqueue(self, request, *, idempotency_key, requester):
            del request, idempotency_key, requester
            raise TypeError("internal persistence bug")

    output = io.StringIO()
    code = run_cli(
        [
            "eod-enqueue",
            "--trade-date",
            "2026-07-15",
            "--datasets",
            "exchange_calendar",
            "--idempotency-key",
            "internal-error",
            "--publish-intent",
            "PUBLISH_AFTER_QUALITY",
        ],
        eod_commands=ExplodingCommand(),
        worker=worker,
        jobs=jobs,
        stdout=output,
    )

    assert code == 1
    assert json.loads(output.getvalue())["error"]["code"] == "COMMAND_FAILED"
