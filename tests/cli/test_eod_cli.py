from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from tests.helpers import app_settings

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
        config=EodExecutionConfig.production(
            code_version="test-code", settings=app_settings(tmp_path)
        ),
    )
    return command, jobs, NoWorker()


def test_cli_enqueue_outputs_stable_created_and_reused_json(tmp_path: Path) -> None:
    command, jobs, worker = _ports(tmp_path)
    argv = [
        "eod-enqueue",
        "--trade-date",
        "2026-07-15",
        "--datasets",
        "daily_bar_unadjusted",
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
        dataset="daily_bar_unadjusted",
        schema_version="daily_bar_unadjusted.v1",
        content_sha256="a" * 64,
        manifest_sha256="b" * 64,
        coverage=(),
    )

    class Resolver:
        def resolve(self, *, dataset: str, version_id: str, scope: tuple[str, ...]):
            assert dataset == base.dataset
            assert version_id == base.version_id
            assert scope == ("000001.SZ", "600000.SH")
            return base

    command = EodCommandService(
        jobs,
        config=EodExecutionConfig.production(
            code_version="test-code", settings=app_settings(tmp_path)
        ),
        base_resolver=Resolver(),
    )
    output = io.StringIO()
    code = run_cli(
        [
            "eod-enqueue",
            "--trade-date",
            "2026-07-15",
            "--datasets",
            "daily_bar_unadjusted",
            "--base-version",
            f"daily_bar_unadjusted={version_id}",
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
    assert frozen["datasets"]["daily_bar_unadjusted"]["base_version"]["version_id"] == version_id


def test_cli_invalid_or_forbidden_inputs_return_stable_nonzero_error(tmp_path: Path) -> None:
    command, jobs, worker = _ports(tmp_path)
    for argv in (
        ["eod-enqueue", "--trade-date", "latest"],
        ["eod-enqueue", "--start-date", "2026-06-11"],
        [
            "eod-enqueue",
            "--trade-date",
            "2026-06-11",
            "--end-date",
            "2026-06-12",
        ],
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
    assert "eod-dry-run" in completed.stdout


def test_cli_range_and_trade_date_freeze_the_same_request_shape(tmp_path: Path) -> None:
    command, jobs, worker = _ports(tmp_path)
    common = [
        "--datasets",
        "daily_bar_unadjusted",
        "--publish-intent",
        "PUBLISH_AFTER_QUALITY",
    ]
    day_output = io.StringIO()
    range_output = io.StringIO()

    assert (
        run_cli(
            [
                "eod-enqueue",
                "--trade-date",
                "2026-07-15",
                "--idempotency-key",
                "day-key",
                *common,
            ],
            eod_commands=command,
            worker=worker,
            jobs=jobs,
            stdout=day_output,
        )
        == 0
    )
    assert (
        run_cli(
            [
                "eod-enqueue",
                "--start-date",
                "2026-07-15",
                "--end-date",
                "2026-07-15",
                "--idempotency-key",
                "range-key",
                *common,
            ],
            eod_commands=command,
            worker=worker,
            jobs=jobs,
            stdout=range_output,
        )
        == 0
    )
    day = jobs.get_job(json.loads(day_output.getvalue())["job_id"])
    ranged = jobs.get_job(json.loads(range_output.getvalue())["job_id"])
    assert day.input_manifest["request_json"] == ranged.input_manifest["request_json"]
    assert day.config_manifest["config_json"] == ranged.config_manifest["config_json"]


def test_dry_run_subprocess_is_read_only_and_reports_fixed_budget(tmp_path: Path) -> None:
    data_root = tmp_path / "must-not-exist"
    environment = dict(os.environ)
    environment["QUANTKING_DATA_ROOT"] = str(data_root.resolve())
    environment["QUANTKING_OFFSITE_BACKUP_ROOT"] = str((tmp_path / "offsite").resolve())

    completed = subprocess.run(
        [sys.executable, "-m", "quantking.cli.eod", "eod-dry-run"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=environment,
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert payload["scope"] == ["000001.SZ", "600000.SH"]
    assert payload["planner_lookback_days"] == 0
    assert payload["adapter_context_lookback_days"] == 15
    assert len(payload["a_chunks"]) == len(payload["b_chunks"]) == 4
    assert len(payload["max_c_chunks"]) == 4
    assert "max_d_chunks" not in payload
    assert payload["network_budget"] == {
        "a_baostock_endpoint_invocations": 4,
        "application_retries": 0,
        "b_tencent_endpoint_invocations": 4,
        "estimated_max_akshare_http_requests": 20,
        "estimated_max_baostock_protocol_messages": 12,
        "max_c_endpoint_invocations": 4,
        "max_endpoint_invocations": 12,
    }
    assert "akshare_unit_policy" in payload["versions"]
    assert payload["idempotency_key"] == "s1-07k4-first-release-20260611-20260809-v1"
    assert not data_root.exists()


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
            "daily_bar_unadjusted",
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
