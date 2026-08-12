"""S1-08 release verifier script remains a thin canonical-JSON entry point."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.verify_s1_08_release import _parser, main


def test_script_accepts_only_explicit_version_id_and_outputs_canonical_json(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    data_root = (tmp_path / "absent-data-root").resolve()
    monkeypatch.setenv("QUANTKING_CODE_ROOT", str((tmp_path / "code").resolve()))
    monkeypatch.setenv("QUANTKING_DATA_ROOT", str(data_root))
    monkeypatch.setenv(
        "QUANTKING_OFFSITE_BACKUP_ROOT",
        str((tmp_path / "offsite").resolve()),
    )
    parser = _parser()
    assert {action.dest for action in parser._actions} == {"help", "version_id"}

    result = main(["--version-id", "data_version:00000000-0000-4000-8000-000000000000"])

    assert result == 2
    output = capsys.readouterr().out.strip()
    payload = json.loads(output)
    assert output == json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert payload == {
        "error_code": "S1_08_RELEASE_VERIFICATION_FAILED",
        "message": "S1-08 release verification failed.",
        "status": "FAILED",
    }
    assert str(data_root) not in output
    assert not data_root.exists()


def test_script_configuration_failure_is_canonical_and_does_not_leak_detail(
    monkeypatch,
    capsys,
) -> None:
    secret = r"E:\secret\provider-token"
    monkeypatch.setenv("QUANTKING_UNRECOGNIZED_SECRET", secret)

    result = main(["--version-id", "data_version:00000000-0000-4000-8000-000000000000"])

    assert result == 2
    output = capsys.readouterr().out.strip()
    assert json.loads(output) == {
        "error_code": "S1_08_RELEASE_VERIFICATION_FAILED",
        "message": "S1-08 release verification failed.",
        "status": "FAILED",
    }
    assert secret not in output
    assert "UNRECOGNIZED" not in output
