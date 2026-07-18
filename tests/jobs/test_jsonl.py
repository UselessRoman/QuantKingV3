from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
from pathlib import Path

import pytest

from quantking.common.errors import InvalidPathError
from quantking.common.paths import is_reparse_point
from quantking.jobs.jsonl import JsonLinesHandler


def _ensure_link(link: Path, target: Path) -> None:
    """Create ``link`` as a symlink or junction to ``target``, or skip the test."""
    with contextlib.suppress(OSError):
        link.symlink_to(target, target_is_directory=True)
    if not os.path.lexists(link):
        junction = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if junction.returncode != 0:
            pytest.skip("symlink and junction creation unavailable")
    if not is_reparse_point(link):
        pytest.skip("link was not created as a reparse point")


def test_jsonl_handler_emits_schema_and_recursively_masks_secrets(tmp_path: Path) -> None:
    managed_root = tmp_path / "managed-logs"
    managed_root.mkdir()
    target = managed_root / "job.jsonl"
    logger = logging.getLogger("quantking.tests.jsonl")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = JsonLinesHandler(managed_root, "job.jsonl")
    logger.addHandler(handler)

    logger.info(
        "request password=hunter2 Authorization: Bearer visible-token account=6222333344445555",
        extra={
            "event": "synthetic_request",
            "job_id": "job:test",
            "correlation_id": "corr:test",
            "details": {
                "api-key": "top-secret",
                "nested": {"Cookie": "session=secret", "safe": "visible"},
            },
        },
    )
    handler.close()

    payload = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    rendered = json.dumps(payload, ensure_ascii=False)
    assert payload["event"] == "synthetic_request"
    assert payload["timestamp"].endswith("Z")
    assert payload["level"] == "INFO"
    assert payload["logger"] == "quantking.tests.jsonl"
    assert payload["job_id"] == "job:test"
    assert payload["details"]["nested"]["safe"] == "visible"
    for secret in ("hunter2", "visible-token", "6222333344445555", "top-secret", "session=secret"):
        assert secret not in rendered


def test_jsonl_handler_keeps_redacted_exception_chain_and_stack(tmp_path: Path) -> None:
    managed_root = tmp_path / "managed-logs"
    managed_root.mkdir()
    target = managed_root / "failure.jsonl"
    logger = logging.getLogger("quantking.tests.exception")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.ERROR)
    handler = JsonLinesHandler(managed_root, "failure.jsonl")
    logger.addHandler(handler)

    try:
        try:
            raise ValueError("token=inner-secret")
        except ValueError as cause:
            raise RuntimeError("password=outer-secret") from cause
    except RuntimeError:
        logger.exception("synthetic failure cookie=session-secret", extra={"event": "job_failed"})
    handler.close()

    payload = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    exception = payload["exception"]
    assert exception["type"] == "RuntimeError"
    assert exception["stack"]
    assert exception["chain"][0]["type"] == "ValueError"
    rendered = json.dumps(payload)
    assert "inner-secret" not in rendered
    assert "outer-secret" not in rendered
    assert "session-secret" not in rendered


@pytest.mark.parametrize("log_id", [".", "../escape.jsonl", "C:/escape.jsonl"])
def test_jsonl_handler_rejects_unmanaged_log_ids(tmp_path: Path, log_id: str) -> None:
    managed_root = tmp_path / "managed-logs"
    managed_root.mkdir()

    with pytest.raises(InvalidPathError):
        JsonLinesHandler(managed_root, log_id)


def test_jsonl_handler_rejects_symlink_escape(tmp_path: Path) -> None:
    managed_root = tmp_path / "managed-logs"
    managed_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = managed_root / "escape"
    _ensure_link(escape, outside)

    with pytest.raises(InvalidPathError):
        JsonLinesHandler(managed_root, "escape/leak.jsonl")
