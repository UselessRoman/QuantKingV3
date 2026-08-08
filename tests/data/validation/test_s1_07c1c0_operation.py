"""S1-07C1C0-R1 offline-only control-chain attack regressions."""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from quantking.common.ids import new_entity_id
from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.validation.c1c0_operation import (
    C1C0_EXECUTION_ACK,
    C1C0OperationError,
    execute,
    issue_authorization,
    parse_st_intervals,
    prepare_plan,
    verify_observation,
    write_authorization,
)
from quantking.data.validation.real_authorization import ExplicitLocalRealKey


def _keypair(root: Path, name: str) -> tuple[Path, Path, str]:
    private = Ed25519PrivateKey.generate()
    private_file = root / "keys" / f"{name}-private.pem"
    public_file = root / "keys" / f"{name}-public.pem"
    private_file.parent.mkdir(parents=True, exist_ok=True)
    private_file.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_file.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return private_file, public_file, ExplicitLocalRealKey(private_key_file=private_file).key_id


def _fake_xtquant(root: Path, body: str, init: str = "") -> Path:
    package = root / "site-packages" / "xtquant"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(init + "\n", encoding="utf-8")
    (package / "xtdata.py").write_text(body, encoding="utf-8")
    metadata = root / "site-packages" / "xtquant-1.2.3.dist-info" / "METADATA"
    metadata.parent.mkdir()
    metadata.write_text("Name: xtquant\nVersion: 1.2.3\n", encoding="utf-8")
    return root / "site-packages"


def _ids() -> dict[str, str]:
    return {
        "assessment_id": str(new_entity_id("data_assessment")),
        "authorization_id": str(new_entity_id("data_authorization")),
        "invocation_id": str(new_entity_id("data_invocation")),
        "session_id": str(new_entity_id("data_session")),
        "run_id": str(new_entity_id("data_validation")),
    }


def _issued(root: Path, body: str, *, init: str = "") -> tuple[Path, dict[str, object]]:
    site = _fake_xtquant(root, body, init)
    authorization_private, authorization_public, authorization_id = _keypair(root, "authorization")
    runner_private, runner_public, runner_id = _keypair(root, "runner")
    now = utc_now().value
    plan = prepare_plan(
        **_ids(),
        site_packages_root=site,
        package_version="1.2.3",
        output_root=root / "output",
        claim_root=root / "claims",
        authorization_private_key_file=authorization_private,
        authorization_public_key_file=authorization_public,
        authorization_allowed_key_ids=(authorization_id,),
        runner_private_key_file=runner_private,
        runner_public_key_file=runner_public,
        runner_allowed_key_ids=(runner_id,),
        valid_from=UtcTimestamp(now - timedelta(minutes=1)).to_iso(),
        expires_at=UtcTimestamp(now + timedelta(minutes=5)).to_iso(),
        claim_nonce="a" * 64,
    )
    authorization = issue_authorization(
        plan=plan, authorization_private_key_file=authorization_private
    )
    path = root / "authorization.json"
    write_authorization(authorization=authorization, authorization_file=path)
    return path, {
        "plan": plan,
        "authorization_public": authorization_public,
        "runner_public": runner_public,
    }


def _clean_xtquant_modules() -> None:
    for name in tuple(sys.modules):
        if name == "xtquant" or name.startswith("xtquant."):
            sys.modules.pop(name, None)


def test_legal_fake_sdk_has_claim_then_one_fixed_call_and_blocked_output(tmp_path: Path) -> None:
    body = (
        "from pathlib import Path\n"
        "def get_his_st_data(code):\n"
        " Path(__file__).with_name('calls.txt').write_text(code)\n"
        " return {'ST': [['20260323', '20260418']]}\n"
    )
    authorization, facts = _issued(tmp_path, body)
    output = execute(authorization_file=authorization, acknowledgement=C1C0_EXECUTION_ACK)
    assert (tmp_path / "site-packages" / "xtquant" / "calls.txt").read_text() == "603377.SH"
    assert len(list((tmp_path / "claims").glob("*.json"))) == 1
    manifest = verify_observation(directory=output)
    assert manifest["event_order"] == ["PRECHECK", "CLAIM", "IMPORT", "CALL", "OUTPUT"]
    assert manifest["conclusion"] == "BLOCKED/CONTRACT_INCOMPLETE"
    assert not output.with_name(output.name + ".incomplete").exists()
    assert facts["plan"].output_directory == output
    _clean_xtquant_modules()


@pytest.mark.parametrize(
    "field,value", [("authorization_id", "..\\escaped-claim"), ("run_id", "..\\..\\escaped-output")]
)
def test_escaped_ids_are_rejected_during_prepare_without_side_effects(
    tmp_path: Path, field: str, value: str
) -> None:
    site = _fake_xtquant(tmp_path, "def get_his_st_data(code):\n return {}\n")
    authorization_private, authorization_public, authorization_id = _keypair(
        tmp_path, "authorization"
    )
    runner_private, runner_public, runner_id = _keypair(tmp_path, "runner")
    values = _ids()
    values[field] = value
    now = utc_now().value
    with pytest.raises(C1C0OperationError):
        prepare_plan(
            **values,
            site_packages_root=site,
            package_version="1.2.3",
            output_root=tmp_path / "output",
            claim_root=tmp_path / "claims",
            authorization_private_key_file=authorization_private,
            authorization_public_key_file=authorization_public,
            authorization_allowed_key_ids=(authorization_id,),
            runner_private_key_file=runner_private,
            runner_public_key_file=runner_public,
            runner_allowed_key_ids=(runner_id,),
            valid_from=UtcTimestamp(now - timedelta(minutes=1)).to_iso(),
            expires_at=UtcTimestamp(now + timedelta(minutes=1)).to_iso(),
            claim_nonce="a" * 64,
        )
    assert not (tmp_path / "escaped-claim.json").exists()
    assert not (tmp_path / "escaped-output").exists()
    del authorization_private


def test_unknown_signed_plan_field_is_rejected_before_claim(tmp_path: Path) -> None:
    authorization, _facts = _issued(tmp_path, "def get_his_st_data(code):\n return {}\n")
    raw = json.loads(authorization.read_bytes())
    raw["plan"]["unbound"] = "rejected"
    authorization.write_bytes(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(C1C0OperationError) as captured:
        execute(authorization_file=authorization, acknowledgement=C1C0_EXECUTION_ACK)
    assert captured.value.code == "C1C0_AUTHORIZATION_INVALID"
    assert not (tmp_path / "claims").exists()


def test_package_path_redirection_and_preloaded_external_modules_are_rejected(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external" / "xtquant"
    external.mkdir(parents=True)
    (external / "xtdata.py").write_text(
        "def get_his_st_data(code):\n return {'ST': [['19990101', '19990102']]}\n", encoding="utf-8"
    )
    redirected = f"from pathlib import Path\n__path__.append(r'{external.as_posix()}')"
    authorization, _facts = _issued(
        tmp_path / "redirect", "# deliberately absent here\n", init=redirected
    )
    with pytest.raises(C1C0OperationError) as captured:
        execute(authorization_file=authorization, acknowledgement=C1C0_EXECUTION_ACK)
    assert captured.value.code == "C1C0_IMPORT_OR_CALL_FAILED"
    assert len(list((tmp_path / "redirect" / "claims").glob("*.json"))) == 1
    _clean_xtquant_modules()

    authorization, _facts = _issued(
        tmp_path / "preloaded", "def get_his_st_data(code):\n return {}\n"
    )
    package = ModuleType("xtquant")
    package.__file__ = str(external / "__init__.py")
    data = ModuleType("xtquant.xtdata")
    data.__file__ = str(external / "xtdata.py")
    sys.modules["xtquant"] = package
    sys.modules["xtquant.xtdata"] = data
    with pytest.raises(C1C0OperationError):
        execute(authorization_file=authorization, acknowledgement=C1C0_EXECUTION_ACK)
    assert not (tmp_path / "preloaded" / "site-packages" / "xtquant" / "calls.txt").exists()
    _clean_xtquant_modules()


@pytest.mark.parametrize(
    "response",
    [
        {"ST": [["20260323", "20260418"], ["20260323", "20260418"]]},
        {"ST": [["20260323", "20260418"], ["20260401", "20260501"]]},
        {"ST": [["20260418", "20260323"]]},
        {"ST": [["20260323", "20260323"]]},
        {"ST": [["not-date", "20260418"]]},
        {"UNKNOWN": []},
    ],
)
def test_strict_interval_parser_rejects_semantic_ambiguity(response: object) -> None:
    with pytest.raises(ValueError):
        parse_st_intervals(response)
    assert parse_st_intervals({})["observation_state"] == "INDETERMINATE"


@pytest.mark.parametrize("mutation", ["extra", "missing", "manifest", "response", "parsed"])
def test_observation_verifier_rejects_file_set_and_content_drift(
    tmp_path: Path, mutation: str
) -> None:
    authorization, _facts = _issued(
        tmp_path, "def get_his_st_data(code):\n return {'ST': [['20260323', '20260418']]}\n"
    )
    output = execute(authorization_file=authorization, acknowledgement=C1C0_EXECUTION_ACK)
    if mutation == "extra":
        (output / "extra.json").write_text("{}", encoding="utf-8")
    elif mutation == "missing":
        (output / "response.canonical.json").unlink()
    elif mutation == "manifest":
        raw = json.loads((output / "manifest.json").read_bytes())
        raw["package_version"] = "9.9.9"
        (output / "manifest.json").write_bytes(
            json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        )
    elif mutation == "response":
        (output / "response.canonical.json").write_text("{}", encoding="utf-8")
    else:
        raw = json.loads((output / "observation.json").read_bytes())
        raw["parsed"]["intervals"] = []
        (output / "observation.json").write_bytes(
            json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        )
    with pytest.raises(C1C0OperationError):
        verify_observation(directory=output)
    _clean_xtquant_modules()


def test_postclaim_failures_preserve_signed_claim_and_incomplete_staging(tmp_path: Path) -> None:
    authorization, _facts = _issued(tmp_path, "raise RuntimeError('fail import')\n")
    with pytest.raises(C1C0OperationError):
        execute(authorization_file=authorization, acknowledgement=C1C0_EXECUTION_ACK)
    assert len(list((tmp_path / "claims").glob("*.json"))) == 1
    assert len(list((tmp_path / "output").rglob("*.incomplete/failure.json"))) == 1
    _clean_xtquant_modules()
