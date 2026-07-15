from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from quantking.common.compatibility import (
    CompatibilityProbeError,
    ProbeErrorCode,
    ProbePayload,
    ProbeReport,
    ProbeStatus,
    capture_probe,
)


def test_probe_result_and_report_are_immutable_and_json_serializable() -> None:
    result = capture_probe("example", lambda: ProbePayload("1.2.3", ("import", "execute")))
    report = ProbeReport.from_results((result,))

    assert result.status is ProbeStatus.PASSED
    assert result.steps == ("import", "execute")
    assert result.duration_ms >= 0
    assert report.status is ProbeStatus.PASSED
    assert report.process_id == result.process_id
    assert json.loads(report.to_json())["results"][0]["version"] == "1.2.3"
    with pytest.raises(FrozenInstanceError):
        result.status = ProbeStatus.FAILED  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        report.results = ()  # type: ignore[misc]


def test_known_and_unknown_failures_have_stable_sanitized_semantics() -> None:
    def known_failure() -> ProbePayload:
        raise CompatibilityProbeError(
            ProbeErrorCode.VERSION_MISMATCH,
            "locked dependency version mismatch",
        )

    def unknown_failure() -> ProbePayload:
        raise RuntimeError(r"secret at C:\private\sdk and traceback text")

    known = capture_probe("known", known_failure)
    unknown = capture_probe("unknown", unknown_failure)
    rendered = ProbeReport.from_results((known, unknown)).to_json()

    assert known.error_code is ProbeErrorCode.VERSION_MISMATCH
    assert known.message == "locked dependency version mismatch"
    assert unknown.error_code is ProbeErrorCode.EXECUTION_FAILED
    assert unknown.message == "dependency probe execution failed"
    assert "C:\\private" not in rendered
    assert "traceback" not in rendered.casefold()
    assert ProbeReport.from_results((known, unknown)).status is ProbeStatus.FAILED


def test_missing_version_is_a_stable_failure() -> None:
    result = capture_probe("missing-version", lambda: ProbePayload("", ("imported",)))

    assert result.status is ProbeStatus.FAILED
    assert result.error_code is ProbeErrorCode.VERSION_UNAVAILABLE
    assert result.message == "dependency version is unavailable"


def test_report_rejects_results_from_multiple_processes() -> None:
    first = capture_probe("first", lambda: ProbePayload("1", ("done",)))
    second = capture_probe("second", lambda: ProbePayload("1", ("done",)))
    object.__setattr__(second, "process_id", first.process_id + 1)

    with pytest.raises(ValueError, match="same process"):
        ProbeReport.from_results((first, second))
