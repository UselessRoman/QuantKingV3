"""ADR-028 offline replay tests for the future R3C probe attempt ledger.

These tests never touch the real data root: they exercise the probe script's
one-time ledger helpers (and its no-authorization refusal path) against
temporary directories only. The 2026-08-18 historical R3C evidence files are
byte-frozen and are not re-executed here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "s1_08r3_r3c_idempotency_probe.py"
_SPEC = importlib.util.spec_from_file_location("s1_08r3_r3c_probe", _SCRIPT)
probe = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(probe)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _consume(attempts_root: Path) -> None:
    probe.consume_attempt_ledger(
        attempts_root,
        marker=probe.MARKER,
        authorization_id=probe.AUTHORIZATION_ID,
        plan_sha256=probe.probe_plan_sha256(
            manifest_sha256="a" * 64,
            envelope_sha256="b" * 64,
        ),
        idempotency_key=probe.PROBE_IDEMPOTENCY_KEY,
        now_iso=_now(),
    )


def test_second_execution_with_the_same_authorization_is_refused(
    tmp_path: Path,
) -> None:
    from quantking.data.eod.formal_authorization import (
        FormalAuthorizationError,
    )

    attempts = tmp_path / "attempts"
    _consume(attempts)
    with pytest.raises(FormalAuthorizationError, match="AUTHORIZATION_ALREADY_CONSUMED"):
        _consume(attempts)
    header = attempts / probe.AUTHORIZATION_ID / "attempt-header.json"
    assert header.is_file()
    payload = json.loads(header.read_text(encoding="utf-8"))
    assert payload["authorization_marker"] == probe.MARKER
    assert payload["idempotency_key"] == probe.PROBE_IDEMPOTENCY_KEY


def test_failed_attempt_cannot_be_reused(tmp_path: Path) -> None:
    from quantking.data.eod.formal_authorization import (
        FormalAuthorizationError,
    )

    attempts = tmp_path / "attempts"
    _consume(attempts)
    probe.record_attempt(
        attempts,
        probe.AUTHORIZATION_ID,
        status="FAILED",
        now_iso=_now(),
        detail={"phase": "execution"},
    )
    assert (attempts / probe.AUTHORIZATION_ID / "failed.json").is_file()
    with pytest.raises(FormalAuthorizationError, match="AUTHORIZATION_ALREADY_CONSUMED"):
        _consume(attempts)


def test_completed_records_are_append_only(tmp_path: Path) -> None:
    from quantking.data.eod.formal_authorization import (
        FormalAuthorizationError,
    )

    attempts = tmp_path / "attempts"
    _consume(attempts)
    probe.record_attempt(
        attempts,
        probe.AUTHORIZATION_ID,
        status="COMPLETED",
        now_iso=_now(),
    )
    with pytest.raises(FormalAuthorizationError, match="ATTEMPT_RECORD_IMMUTABLE"):
        probe.record_attempt(
            attempts,
            probe.AUTHORIZATION_ID,
            status="COMPLETED",
            now_iso=_now(),
        )


def test_plan_sha_binds_the_frozen_inputs() -> None:
    first = probe.probe_plan_sha256(manifest_sha256="a" * 64, envelope_sha256="b" * 64)
    second = probe.probe_plan_sha256(manifest_sha256="c" * 64, envelope_sha256="b" * 64)
    assert first != second
    assert probe.probe_token("M", "ID", first) != probe.probe_token("M", "ID", second)


def test_main_without_authorization_refuses_before_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    attempts = tmp_path / "attempts"
    evidence_out = tmp_path / "evidence.json"
    monkeypatch.delenv("R3C_PROBE_AUTHORIZATION", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe",
            "--attempts-root",
            str(attempts),
            "--evidence-out",
            str(evidence_out),
        ],
    )
    assert probe.main() == 1
    captured = capsys.readouterr()
    assert "authorization" in captured.err.lower()
    # Refusal happens before the ledger and before any data-root access.
    assert not attempts.exists()
    payload = json.loads(evidence_out.read_text(encoding="utf-8"))
    assert payload["phase"] == "authorization"
    assert payload["error"] == "R3C_PROBE_AUTHORIZATION_REQUIRED"
