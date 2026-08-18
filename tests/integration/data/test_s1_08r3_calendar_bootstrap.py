"""S1-08R3-C1 calendar bootstrap integration: full chain, guards, and the E1 loop.

Everything here is synthetic: a fake BaoStock calendar runner, a temporary
data root, and a fake repository tree. Zero network, zero formal-root
access. The final test closes the C1→E1 loop by binding the freshly
published calendar into the formal entry and publishing daily bars through
the shared stack.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from tests.integration.data.test_s1_08r3_formal_entry import (
    _FormalEnv,
    _gateways,
    _settings,
)

from quantking.common.ids import new_entity_id
from quantking.data.adapters.sources.baostock import BaostockCalendarResult
from quantking.data.adapters.warehouse.manifest import path_safe_id
from quantking.data.adapters.warehouse.verifier import (
    verify_canonical_directory,
    verify_raw_directory,
)
from quantking.data.eod.calendar_bootstrap import (
    CalendarBootstrapError,
    build_calendar_bootstrap_plan,
    execute_calendar_bootstrap,
)
from quantking.data.models import DatasetKind, DatasetVersionStatus
from quantking.runtime import build_eod_runtime

_MARKER = "S1-08R3-C1-CALENDAR-BOOTSTRAP-V1"
_AUTH_ID = "S1-08R3-C1-9d4e6f8a0c21"
_DEFAULT_TOKEN = object()  # sentinel: compute the ADR-028 token from the plan
_START = date(2026, 6, 11)
_END = date(2026, 6, 30)


class _FakeCalendarRunner:
    """Duck-typed BaostockCalendarRunner; weekdays open, weekends closed."""

    def __init__(
        self,
        *,
        query_messages: int = 1,
        duplicate_one_day: bool = False,
        all_open: bool = False,
    ) -> None:
        self.calls: list[tuple[date, date]] = []
        self._query_messages = query_messages
        self._duplicate_one_day = duplicate_one_day
        self._all_open = all_open

    def run(self, *, start_date: date, end_date: date) -> BaostockCalendarResult:
        self.calls.append((start_date, end_date))
        days: list[tuple[str, str]] = []
        current = start_date
        while current <= end_date:
            flag = "1" if (self._all_open or current.weekday() < 5) else "0"
            days.append((current.isoformat(), flag))
            if self._duplicate_one_day and current == start_date:
                days.append((current.isoformat(), flag))
            current += timedelta(days=1)
        return BaostockCalendarResult(
            days=tuple(days),
            login_messages=1,
            query_messages=self._query_messages,
            logout_messages=1,
        )


class _FakeRepo:
    """Minimal repository tree satisfying the code-tree and git preflight."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "repo"
        source = self.root / "src" / "quantking"
        source.mkdir(parents=True)
        (source / "__init__.py").write_text("", encoding="utf-8")
        (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        git = self.root / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        self.head = "9f5b9ea0deadbeef00c0ffee"
        (git / "refs" / "heads" / "main").write_text(self.head + "\n", encoding="utf-8")
        from quantking.data.eod.formal_authorization import compute_code_tree_sha256

        self.tree_sha = compute_code_tree_sha256(source)


class _BootstrapEnv:
    def __init__(self, tmp_path: Path, *, query_messages: int = 1) -> None:
        self.tmp = tmp_path
        self.settings = _settings(tmp_path)
        self.repo = _FakeRepo(tmp_path)
        self.plan = build_calendar_bootstrap_plan(
            marker=_MARKER,
            authorization_id=_AUTH_ID,
            start=_START,
            end=_END,
            code_tree_sha256=self.repo.tree_sha,
            data_root_baseline_sha256="c" * 64,
        )
        self.runner = _FakeCalendarRunner(query_messages=query_messages)
        self.attempts_root = tmp_path / "calendar-attempts"
        self.runtime_calls = 0
        self._runtime = None

    def runtime_factory(self):
        if self._runtime is None:
            self.runtime_calls += 1
            self._runtime = build_eod_runtime(self.settings, gateways=_gateways()[0])
        return self._runtime

    def token(self) -> str:
        """ADR-028 content-bound token for the current plan content."""

        from quantking.data.eod.calendar_bootstrap import (
            calendar_bootstrap_token,
            parse_calendar_bootstrap_plan,
        )

        return calendar_bootstrap_token(parse_calendar_bootstrap_plan(self.plan))

    def execute(
        self,
        *,
        authorization: object = _DEFAULT_TOKEN,
        version_id=None,
        batch_id=None,
    ) -> dict[str, object]:
        return execute_calendar_bootstrap(
            plan=self.plan,
            authorization=self.token() if authorization is _DEFAULT_TOKEN else authorization,  # type: ignore[arg-type]
            runtime_factory=self.runtime_factory,
            runner=self.runner,  # type: ignore[arg-type]
            attempts_root=self.attempts_root,
            code_root=self.repo.root,
            version_id=version_id or new_entity_id("data_version"),
            batch_id=batch_id or new_entity_id("data_batch"),
        )

    def published_calendar_versions(self) -> list:
        runtime = self.runtime_factory()
        with runtime.data_uow_factory() as uow:
            return list(
                uow.data.list_versions(
                    dataset=DatasetKind.EXCHANGE_CALENDAR,
                    status=DatasetVersionStatus.PUBLISHED,
                )
            )


@pytest.fixture()
def env(tmp_path: Path) -> _BootstrapEnv:
    return _BootstrapEnv(tmp_path)


def test_bootstrap_publishes_one_calendar_version_end_to_end(env: _BootstrapEnv) -> None:
    payload = env.execute()
    assert payload["status"] == "SUCCEEDED"
    assert payload["endpoint_invocations"] == 1
    assert payload["protocol_messages"] == 3
    assert payload["rows"] == 2 * ((_END - _START).days + 1)  # SH + SZ projections
    versions = env.published_calendar_versions()
    assert len(versions) == 1
    assert str(versions[0].version_id) == payload["version_id"]
    assert versions[0].content_sha256 == payload["content_sha256"]
    data_root = env.settings.paths.data_root
    verify_raw_directory(
        data_root
        / "raw"
        / "baostock"
        / "exchange_calendar"
        / path_safe_id(str(payload["batch_id"])),
        data_root=data_root,
    )
    warehouse_dir = (
        data_root / "warehouse" / "exchange_calendar" / path_safe_id(str(payload["version_id"]))
    )
    verify_canonical_directory(warehouse_dir, data_root=data_root)
    assert (env.attempts_root / _AUTH_ID / "completed.json").is_file()
    assert env.runner.calls == [(_START, _END)]
    assert env.runtime_calls == 1


def test_missing_authorization_touches_neither_runner_nor_runtime(env: _BootstrapEnv) -> None:
    with pytest.raises(CalendarBootstrapError) as caught:
        env.execute(authorization=None)
    assert caught.value.code == "CALENDAR_AUTHORIZATION_REQUIRED"
    assert env.runner.calls == []
    assert env.runtime_calls == 0
    assert not env.attempts_root.exists()
    assert not env.settings.paths.data_root.exists()


def test_code_tree_drift_fails_before_any_call_or_runtime(env: _BootstrapEnv) -> None:
    env.plan["code_tree_sha256"] = "0" * 64
    with pytest.raises(CalendarBootstrapError) as caught:
        env.execute()
    assert caught.value.code == "CALENDAR_CODE_TREE_DRIFT"
    assert env.runner.calls == []
    assert env.runtime_calls == 0


def test_authorization_replay_is_rejected_before_a_second_call(env: _BootstrapEnv) -> None:
    env.execute()
    with pytest.raises(CalendarBootstrapError) as caught:
        env.execute(version_id=new_entity_id("data_version"), batch_id=new_entity_id("data_batch"))
    assert caught.value.code == "CALENDAR_AUTHORIZATION_ALREADY_CONSUMED"
    assert len(env.runner.calls) == 1


def test_runtime_failure_records_stable_code_and_bounded_message(env: _BootstrapEnv) -> None:
    original = env.runtime_factory

    def factory():
        original()
        raise RuntimeError("boom-with-a-fairly-long-message-" + "x" * 300)

    env.runtime_factory = factory
    with pytest.raises(CalendarBootstrapError) as caught:
        env.execute()
    assert caught.value.code == "CALENDAR_RUNTIME_UNAVAILABLE"
    failed = json.loads((env.attempts_root / _AUTH_ID / "failed.json").read_text(encoding="utf-8"))
    detail = failed["detail"]
    assert detail["error_type"] == "RuntimeError"
    assert detail["message"].startswith("boom-with-a-fairly-long-message-")
    assert len(detail["message"]) == 200
    assert env.runner.calls == []


def test_provider_failure_records_stable_code_without_any_data_write(
    env: _BootstrapEnv,
) -> None:
    class _ExplodingRunner:
        def run(self, *, start_date: date, end_date: date):
            raise RuntimeError("baostock session exploded")

    env.runner = _ExplodingRunner()
    with pytest.raises(CalendarBootstrapError) as caught:
        env.execute()
    assert caught.value.code == "CALENDAR_PROVIDER_FAILED"
    failed = json.loads((env.attempts_root / _AUTH_ID / "failed.json").read_text(encoding="utf-8"))
    assert failed["detail"]["message"] == "baostock session exploded"
    assert env.published_calendar_versions() == []
    assert not (env.settings.paths.data_root / "raw").exists()


def test_protocol_overshoot_fails_fast_without_any_data_write(tmp_path: Path) -> None:
    env = _BootstrapEnv(tmp_path, query_messages=5)
    with pytest.raises(CalendarBootstrapError) as caught:
        env.execute()
    assert caught.value.code == "CALENDAR_BUDGET_EXCEEDED"
    assert (env.attempts_root / _AUTH_ID / "failed.json").is_file()
    assert env.published_calendar_versions() == []
    assert not (env.settings.paths.data_root / "raw").exists()
    assert not (env.settings.paths.data_root / "warehouse").exists()


def test_quality_failure_preserves_the_raw_batch_as_evidence(tmp_path: Path) -> None:
    env = _BootstrapEnv(tmp_path)
    env.runner = _FakeCalendarRunner(duplicate_one_day=True)
    with pytest.raises(CalendarBootstrapError) as caught:
        env.execute()
    assert caught.value.code == "CALENDAR_QUALITY_FAILED"
    assert (env.attempts_root / _AUTH_ID / "failed.json").is_file()
    assert env.published_calendar_versions() == []
    assert (env.settings.paths.data_root / "raw" / "baostock").is_dir()
    assert not (env.settings.paths.data_root / "warehouse").exists()


def test_published_calendar_binds_into_the_formal_entry(
    env: _BootstrapEnv,
    tmp_path: Path,
) -> None:
    # The shared synthetic bar gateways emit rows for every natural day, so
    # the bound calendar must mark every day open (same semantics as the
    # original formal-entry fixtures); weekend handling is covered above.
    env.runner = _FakeCalendarRunner(all_open=True)
    payload = env.execute()
    from quantking.data.eod.release_manifest import build_release_manifest

    # Rebuild a formal environment on the SAME data root / repo so the
    # worker reference loader resolves the calendar this bootstrap published.
    formal = _FormalEnv.__new__(_FormalEnv)
    formal.root = tmp_path
    formal.settings = env.settings
    formal.code_root = env.repo.root
    formal.code_tree_sha = env.repo.tree_sha
    formal.head = env.repo.head
    formal.attempts_root = tmp_path / "formal-attempts"
    formal.calendar_version_id = payload["version_id"]
    formal.calendar_sha = payload["content_sha256"]
    formal.manifest = build_release_manifest(
        symbols=("000001.SZ", "600000.SH"),
        target_start=_START,
        target_end=_END,
        lookback_days=15,
        calendar_version_id="s1-08r3-calendar-baostock.v1",
        calendar_sha256=str(payload["content_sha256"]),
        source_route_version="BAO-TX-SINA.k4.v1",
        adapter_version="baostock-akshare-daily-bar.k4.v1",
        code_version="s1-08r3.v1",
        quality_rule_version="quality-rules.daily-bar-unadjusted.k4.v1",
        threshold_rule_version="comparison-thresholds.daily-bar-unadjusted.k4.v1",
        resolution_rule_version="source-resolution.daily-bar-unadjusted.k4.v1",
        unit_policy_versions={"baostock": "b", "akshare": "a"},
    )
    formal.manifest_path = tmp_path / "release-manifest-bound.json"
    formal.manifest_path.write_text(
        json.dumps(formal.manifest.to_dict(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    gateways, modules = _gateways()
    outcome = formal.run(
        authorization=formal.token(),
        envelope_path=formal.envelope_path(
            tmp_path,
            calendar_version_id=str(payload["version_id"]),
        ),
        gateways=gateways,
    )
    assert outcome["status"] == "SUCCEEDED"
    assert modules[1].calls == []  # C is never invoked when A/B agree


def test_cli_without_authorization_never_touches_the_data_root(tmp_path: Path) -> None:
    repo = _FakeRepo(tmp_path)
    plan = build_calendar_bootstrap_plan(
        marker=_MARKER,
        authorization_id=_AUTH_ID,
        start=_START,
        end=_END,
        code_tree_sha256=repo.tree_sha,
        data_root_baseline_sha256="c" * 64,
    )
    plan_path = tmp_path / "calendar-plan.json"
    plan_path.write_text(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    data_root = tmp_path / "must-not-exist"
    environment = dict(os.environ)
    environment["QUANTKING_DATA_ROOT"] = str(data_root.resolve())
    environment["QUANTKING_OFFSITE_BACKUP_ROOT"] = str((tmp_path / "offsite").resolve())
    environment.pop("R3_CALENDAR_AUTHORIZATION", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "quantking.cli.eod",
            "eod-calendar-publish",
            "--plan",
            str(plan_path),
            "--attempts-root",
            str(tmp_path / "attempts"),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    payload = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert payload["error"]["code"] == "CALENDAR_AUTHORIZATION_REQUIRED"
    assert not data_root.exists()
