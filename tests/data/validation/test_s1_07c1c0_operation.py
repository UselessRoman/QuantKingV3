"""S1-07C1C0 pure-offline runner and provider-observation contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from quantking.data.validation.c1c0_operation import (
    C1C0OperationError,
    execute,
    issue_authorization,
    parse_st_intervals,
    prepare_plan,
    verify_observation,
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


def _fake_xtquant(root: Path, body: str) -> Path:
    package = root / "site-packages" / "xtquant"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "from pathlib import Path\nPath(__file__).with_name('imports.txt').write_text('xtquant')\n",
        encoding="utf-8",
    )
    (package / "xtdata.py").write_text(body, encoding="utf-8")
    metadata = root / "site-packages" / "xtquant-1.2.3.dist-info" / "METADATA"
    metadata.parent.mkdir()
    metadata.write_text("Name: xtquant\nVersion: 1.2.3\n", encoding="utf-8")
    return root / "site-packages"


def _issued(root: Path, body: str) -> tuple[Path, dict[str, object]]:
    site = _fake_xtquant(root, body)
    authorization_private, authorization_public, authorization_id = _keypair(root, "authorization")
    runner_private, runner_public, runner_id = _keypair(root, "runner")
    plan = prepare_plan(
        authorization_id="authorization-1",
        invocation_id="invocation-1",
        run_id="run-1",
        site_packages_root=site,
        package_version="1.2.3",
        output_root=root / "output",
        claim_root=root / "claims",
        authorization_public_key_file=authorization_public,
        authorization_allowed_key_ids=(authorization_id,),
        runner_private_key_file=runner_private,
        runner_public_key_file=runner_public,
        runner_allowed_key_ids=(runner_id,),
    )
    authorization = issue_authorization(
        plan=plan, authorization_private_key_file=authorization_private
    )
    path = root / "authorization.json"
    path.write_bytes(authorization.to_bytes())
    return path, {
        "authorization_public": authorization_public,
        "authorization_id": authorization_id,
        "runner_public": runner_public,
        "runner_id": runner_id,
    }


def test_fake_sdk_is_delayed_and_makes_exactly_one_fixed_read(tmp_path: Path) -> None:
    fake_body = (
        "from pathlib import Path\n"
        "def get_his_st_data(code):\n"
        " Path(__file__).with_name('calls.txt').write_text(code)\n"
        " return {'ST': [('20260323', '20260418')]}\n"
    )
    authorization, keys = _issued(
        tmp_path,
        fake_body,
    )
    assert not (tmp_path / "site-packages" / "xtquant" / "imports.txt").exists()
    output = execute(authorization_file=authorization)
    package = tmp_path / "site-packages" / "xtquant"
    assert (package / "imports.txt").read_text() == "xtquant"
    assert (package / "calls.txt").read_text() == "603377.SH"
    assert not (package / "xttrader.txt").exists()
    manifest = verify_observation(
        directory=output,
        authorization_public_key_file=keys["authorization_public"],
        authorization_allowed_key_ids=frozenset({keys["authorization_id"]}),
        runner_public_key_file=keys["runner_public"],
        runner_allowed_key_ids=frozenset({keys["runner_id"]}),
    )
    assert manifest["conclusion"] == "BLOCKED/CONTRACT_INCOMPLETE"
    assert (output / "observation.json").read_text(encoding="utf-8").count("2026-03-23") == 1


def test_empty_mapping_is_not_construed_as_never_st() -> None:
    assert parse_st_intervals({}) == {"intervals": [], "observation_state": "NO_DETERMINATE_DATA"}
    with pytest.raises(ValueError):
        parse_st_intervals({"ST": [("not-a-date", "20260418")]})


@pytest.mark.parametrize("drift", ["package", "key", "contract"])
def test_preclaim_drift_rejects_without_import_or_claim(tmp_path: Path, drift: str) -> None:
    authorization, keys = _issued(tmp_path, "def get_his_st_data(code):\n return {}\n")
    if drift == "package":
        metadata = tmp_path / "site-packages" / "xtquant-1.2.3.dist-info" / "METADATA"
        metadata.write_text("Name: xtquant\nVersion: 9.9.9\n", encoding="utf-8")
    elif drift == "key":
        _private, replacement, _id = _keypair(tmp_path, "replacement")
        keys["authorization_public"].write_bytes(replacement.read_bytes())
    else:
        raw = authorization.read_bytes().replace(
            b"xtquant.xtdata.get_his_st_data.603377-sh.v1",
            b"xtquant.xtdata.get_his_st_data.603377-sh.v9",
        )
        authorization.write_bytes(raw)
    with pytest.raises(C1C0OperationError):
        execute(authorization_file=authorization)
    assert not (tmp_path / "claims").exists()
    assert not (tmp_path / "site-packages" / "xtquant" / "imports.txt").exists()


@pytest.mark.parametrize(
    "body,code",
    [
        ("raise RuntimeError('load failure')\n", "C1C0_IMPORT_OR_CALL_FAILED"),
        ("def get_his_st_data(code):\n return {'UNKNOWN': []}\n", "C1C0_RESPONSE_PARSE_FAILED"),
    ],
)
def test_postclaim_import_call_and_parse_failures_keep_immutable_evidence(
    tmp_path: Path, body: str, code: str
) -> None:
    authorization, _keys = _issued(tmp_path, body)
    with pytest.raises(C1C0OperationError) as captured:
        execute(authorization_file=authorization)
    assert captured.value.code == code
    assert len(list((tmp_path / "claims").glob("*.json"))) == 1
    assert len(list((tmp_path / "output").rglob("*.incomplete/failure.json"))) == 1
