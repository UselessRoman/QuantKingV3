from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantking.cli import compatibility as cli
from quantking.common.compatibility import (
    CompatibilityProbeError,
    ProbeErrorCode,
    ProbePayload,
    ProbeReport,
    ProbeStatus,
    capture_probe,
)
from quantking.data.adapters import compatibility as data_compatibility


def test_joint_offline_probe_executes_every_dependency_in_one_process(tmp_path: Path) -> None:
    report = cli.run_offline_probe(tmp_path)

    assert tuple(result.name for result in report.results) == (
        "sqlite",
        "duckdb",
        "lightgbm",
        "backtrader",
        "fastapi",
        "qlib",
    )
    assert report.status is ProbeStatus.PASSED, report.to_json()
    assert all(result.status is ProbeStatus.PASSED for result in report.results)
    assert all(result.version for result in report.results)
    assert all(result.steps for result in report.results)
    assert {result.process_id for result in report.results} == {report.process_id}
    assert report.python_version.startswith("3.12.")


def test_adapter_converts_third_party_exception_without_leaking_details(
    monkeypatch, tmp_path: Path
) -> None:
    def fail(_root: Path) -> ProbePayload:
        raise RuntimeError(r"vendor failed at C:\private\duck.db")

    monkeypatch.setattr(data_compatibility, "_run_duckdb", fail)
    result = data_compatibility.probe_duckdb(tmp_path)

    assert result.status is ProbeStatus.FAILED
    assert result.error_code is ProbeErrorCode.EXECUTION_FAILED
    assert result.message == "dependency probe execution failed"


def test_joint_probe_keeps_all_results_when_one_subprobe_fails(monkeypatch, tmp_path: Path) -> None:
    def failed(_root: Path):
        def operation() -> ProbePayload:
            raise CompatibilityProbeError(ProbeErrorCode.INTERFACE_MISMATCH, "interface drift")

        return capture_probe("failed", operation)

    def passed(_root: Path):
        return capture_probe("passed", lambda: ProbePayload("1", ("executed",)))

    monkeypatch.setattr(cli, "OFFLINE_PROBES", (failed, passed))

    report = cli.run_offline_probe(tmp_path)

    assert report.status is ProbeStatus.FAILED
    assert [result.name for result in report.results] == ["failed", "passed"]


def test_cli_writes_json_to_stdout_and_returns_status(monkeypatch, capsys, tmp_path: Path) -> None:
    passed = capture_probe("passed", lambda: ProbePayload("1", ("executed",)))
    report = ProbeReport.from_results((passed,))
    monkeypatch.setattr(cli, "run_offline_probe", lambda _root: report)

    exit_code = cli.main([])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"


def test_cli_explicit_output_is_relative_to_managed_current_directory(
    monkeypatch, tmp_path: Path
) -> None:
    failed = capture_probe(
        "failed",
        lambda: (_ for _ in ()).throw(
            CompatibilityProbeError(ProbeErrorCode.EXECUTION_FAILED, "failed")
        ),
    )
    report = ProbeReport.from_results((failed,))
    monkeypatch.setattr(cli, "run_offline_probe", lambda _root: report)
    monkeypatch.chdir(tmp_path)

    exit_code = cli.main(["--output", "probe-report.json"])

    assert exit_code == 1
    assert (
        json.loads((tmp_path / "probe-report.json").read_text(encoding="utf-8"))["status"]
        == "failed"
    )


def test_cli_rejects_an_absolute_output_path_before_running_probe(
    monkeypatch, tmp_path: Path
) -> None:
    called = False

    def unexpected_probe(_root: Path) -> ProbeReport:
        nonlocal called
        called = True
        raise AssertionError("probe must not run for an invalid output path")

    monkeypatch.setattr(cli, "run_offline_probe", unexpected_probe)

    with pytest.raises(SystemExit) as captured:
        cli.main(["--output", str(tmp_path / "probe.json")])

    assert captured.value.code == 2
    assert called is False
