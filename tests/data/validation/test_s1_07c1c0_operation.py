"""S1-07C1C0-R3 offline-only control-chain and artifact regressions."""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from quantking.common.errors import InvalidIdError
from quantking.common.ids import new_entity_id
from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.models import DatasetKind
from quantking.data.validation import c1b_operation, c1c0_operation
from quantking.data.validation.authority_bundle import (
    CONTROLLED_AUTHORITY_BUNDLE_MANIFEST_SHA256,
)
from quantking.data.validation.c1b_operation import (
    C1BExecutionAuthorization,
    C1BOperationError,
)
from quantking.data.validation.c1b_operation import (
    issue_authorization as issue_c1b_authorization,
)
from quantking.data.validation.c1b_operation import (
    load_authorization as load_c1b_authorization,
)
from quantking.data.validation.c1b_operation import prepare_plan as prepare_c1b_plan
from quantking.data.validation.c1b_operation import (
    write_authorization as write_c1b_authorization,
)
from quantking.data.validation.c1c0_cli import main as c1c0_main
from quantking.data.validation.c1c0_operation import (
    C1C0_EXECUTION_ACK,
    C1C0Authorization,
    C1C0OperationError,
    execute,
    issue_authorization,
    load_authorization,
    load_plan,
    parse_st_intervals,
    prepare_plan,
    review_authorization,
    verify_observation,
    write_authorization,
    write_plan,
)
from quantking.data.validation.models import ValidationVersions
from quantking.data.validation.real_authorization import (
    ExplicitLocalRealKey,
    RealClaimReceiptIssuer,
)
from quantking.data.validation.samples import load_fixed_sample_v4
from quantking.jobs.adapters.persistence.sqlite import initialize_database


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _sha(value: bytes) -> str:
    return sha256(value).hexdigest()


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


def _write_verified_authority_bundle(root: Path) -> Path:
    from scripts.build_s1_07b_reference_bundle import (
        EXPECTED_P2B_MANIFEST_SHA,
        EXPECTED_P2F_MANIFEST_SHA,
        EXPECTED_PDF_SHA,
        _build_draft,
    )

    from quantking.data.validation.authority_bundle import AuthorityReferenceBundleWriter

    return AuthorityReferenceBundleWriter(root=root).write(
        _build_draft(EXPECTED_PDF_SHA, EXPECTED_P2B_MANIFEST_SHA, EXPECTED_P2F_MANIFEST_SHA),
        sample=load_fixed_sample_v4(),
    )


def _ids() -> dict[str, str]:
    return {
        "assessment_id": str(new_entity_id("data_assessment")),
        "authorization_id": str(new_entity_id("data_authorization")),
        "invocation_id": str(new_entity_id("data_invocation")),
        "session_id": str(new_entity_id("data_session")),
        "run_id": str(new_entity_id("data_validation")),
    }


def _versions() -> ValidationVersions:
    sample = load_fixed_sample_v4()
    return ValidationVersions(
        adapter_version="xtquant-readonly.s1-07c1a-r2.v1",
        package_version="1.2.3",
        code_version="s1-07c1a-r2.xtquant.runtime.v1",
        schema_version="trading_status.v1",
        rule_version="quality-rules.v1",
        threshold_version="comparison-thresholds.v1",
        sample_spec_version=sample.sample_spec_version,
        config_version="real-validation-config.c1a-r2.v1",
    )


def _c1b_authorization(
    root: Path, body: str, *, init: str = ""
) -> tuple[C1BExecutionAuthorization, dict[str, object]]:
    site = _fake_xtquant(root, body, init)
    bundle_directory = _write_verified_authority_bundle(root / "authority")
    authorization_private, authorization_public, authorization_id = _keypair(root, "authorization")
    runner_private, runner_public, runner_id = _keypair(root, "runner")
    database = root / "metadata.db"
    initialize_database(database)
    now = utc_now().value
    versions = _versions()
    kwargs: dict[str, object] = {
        "source": "XTQUANT",
        "dataset": DatasetKind.TRADING_STATUS,
        "case_id": "st_warning_603377_sh",
        "xtquant_site_packages_root": site,
        "reference_bundle_root": root / "authority",
        "reference_bundle_id": "st-603377-authority-v3",
        "reference_bundle_sha256": CONTROLLED_AUTHORITY_BUNDLE_MANIFEST_SHA256,
        "output_root": root / "output",
        "claim_root": root / "claims",
        "metadata_database": database,
        "sqlite_busy_timeout_ms": 250,
        **_ids(),
        "authorization_private_key_file": authorization_private,
        "authorization_public_key_file": authorization_public,
        "authorization_allowed_key_ids": (authorization_id,),
        "runner_private_key_file": runner_private,
        "runner_public_key_file": runner_public,
        "runner_allowed_key_ids": (runner_id,),
        "valid_from": UtcTimestamp(now - timedelta(minutes=2)).to_iso(),
        "expires_at": UtcTimestamp(now + timedelta(minutes=5)).to_iso(),
        "claim_nonce": "c" * 64,
        "adapter_version": versions.adapter_version,
        "package_version": versions.package_version,
        "code_version": versions.code_version,
        "schema_version": versions.schema_version,
        "rule_version": versions.rule_version,
        "threshold_version": versions.threshold_version,
        "sample_spec_version": versions.sample_spec_version,
        "config_version": versions.config_version,
    }
    plan = prepare_c1b_plan(**kwargs)  # type: ignore[arg-type]
    return issue_c1b_authorization(plan=plan), {
        "authorization_private": authorization_private,
        "authorization_public": authorization_public,
        "authorization_key_id": authorization_id,
        "runner_private": runner_private,
        "runner_public": runner_public,
        "runner_key_id": runner_id,
        "bundle_directory": bundle_directory,
        "reference_bundle_root": root / "authority",
        "metadata_database": database,
        "kwargs": kwargs,
    }


def _issued(root: Path, body: str, *, init: str = "") -> tuple[Path, dict[str, object]]:
    c1b_authorization, facts = _c1b_authorization(root, body, init=init)
    plan = prepare_plan(c1b_authorization=c1b_authorization)
    authorization = issue_authorization(plan=plan)
    path = root / "authorization.json"
    write_authorization(authorization=authorization, authorization_file=path)
    return path, {**facts, "plan": plan, "c1b_authorization": c1b_authorization}


def _c1b_authorization_file(
    root: Path, body: str, *, init: str = ""
) -> tuple[Path, dict[str, object]]:
    authorization, facts = _c1b_authorization(root, body, init=init)
    path = root / "c1b-authorization.json"
    write_c1b_authorization(authorization=authorization, authorization_file=path)
    return path, {**facts, "c1b_authorization": authorization}


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _clean_xtquant_modules() -> None:
    for name in tuple(sys.modules):
        if name == "xtquant" or name.startswith("xtquant."):
            sys.modules.pop(name, None)


def _resign_manifest(output: Path, runner_private: Path) -> None:
    manifest = json.loads((output / "manifest.json").read_bytes())
    unsigned = dict(manifest)
    unsigned.pop("signature")
    signer = ExplicitLocalRealKey(private_key_file=runner_private)
    manifest["signature"] = signer.sign(domain=c1c0_operation._ARTIFACT_DOMAIN, payload=unsigned)
    (output / "manifest.json").write_bytes(_canonical(manifest))


def test_legal_fake_sdk_embeds_c1a_c1b_context_claims_then_calls_once_and_blocks(
    tmp_path: Path,
) -> None:
    body = (
        "from pathlib import Path\n"
        "def get_his_st_data(code):\n"
        " Path(__file__).with_name('calls.txt').write_text(code)\n"
        " return {'ST': [['20260323', '20260418']]}\n"
    )
    before = set(sys.modules)
    authorization, facts = _issued(tmp_path, body)
    assert not (set(sys.modules) - before) & {"xtquant", "xtquant.xtdata"}
    output = execute(authorization_file=authorization, acknowledgement=C1C0_EXECUTION_ACK)
    assert (tmp_path / "site-packages" / "xtquant" / "calls.txt").read_text() == "603377.SH"
    assert len(list((tmp_path / "claims").glob("*.json"))) == 1
    manifest = verify_observation(directory=output)
    assert manifest["event_order"] == ["PRECHECK", "CLAIM", "IMPORT", "CALL", "OUTPUT"]
    assert manifest["conclusion"] == "BLOCKED/CONTRACT_INCOMPLETE"
    assert manifest["c1b_authorization_sha256"] == _sha(
        facts["c1b_authorization"].to_bytes()  # type: ignore[union-attr]
    )
    assert facts["plan"].output_directory == output  # type: ignore[union-attr]
    assert not output.with_name(output.name + ".incomplete").exists()
    _clean_xtquant_modules()


def test_c1c1_formal_cli_handoff_stays_file_only_until_exact_execute_acknowledgement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = (
        "from pathlib import Path\n"
        "def get_his_st_data(code):\n"
        " Path(__file__).with_name('calls.txt').write_text(code)\n"
        " return {'ST': [['20260323', '20260418']]}\n"
    )
    c1b_file, facts = _c1b_authorization_file(tmp_path, body)
    plan_file = tmp_path / "c1c0-plan.json"
    authorization_file = tmp_path / "c1c0-authorization.json"
    before = set(sys.modules)

    assert (
        c1c0_main(
            [
                "prepare",
                "--c1b-authorization-file",
                str(c1b_file),
                "--plan-file",
                str(plan_file),
            ]
        )
        == 0
    )
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["c1b_authorization_sha256"] == _sha(
        facts["c1b_authorization"].to_bytes()  # type: ignore[union-attr]
    )
    assert plan_file.read_bytes() == _canonical(json.loads(plan_file.read_bytes()))
    assert not (set(sys.modules) - before) & {"xtquant", "xtquant.xtdata"}
    assert not (tmp_path / "claims").exists()
    assert not (tmp_path / "output").exists()

    assert (
        c1c0_main(
            [
                "issue",
                "--plan-file",
                str(plan_file),
                "--authorization-file",
                str(authorization_file),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert c1c0_main(["review", "--authorization-file", str(authorization_file)]) == 0
    review = json.loads(capsys.readouterr().out)
    assert review == review_authorization(authorization_file=authorization_file)
    assert review["authorization_file_sha256"] == _sha(authorization_file.read_bytes())
    assert review["plan_sha256"] == prepared["plan_sha256"]
    assert review["allowed_conclusion"] == "BLOCKED/CONTRACT_INCOMPLETE"
    assert review["allowed_conclusions"] == ["BLOCKED/CONTRACT_INCOMPLETE"]
    assert review["real_execution_authorized"] is False
    assert review["fixed_request"] == {
        "code": "603377.SH",
        "interface": "xtdata.get_his_st_data",
    }
    assert {
        "assessment_id",
        "authorization_id",
        "invocation_id",
        "run_id",
        "session_id",
        "source",
        "dataset",
        "case_id",
        "site_packages_root",
        "output_root",
        "claim_root",
        "xtquant_package_version",
        "valid_from",
        "expires_at",
        "authorization_key_id",
        "runner_key_id",
    } <= set(review)
    rendered = json.dumps(review, sort_keys=True)
    for forbidden in (
        "authorization_private_key_file",
        "runner_private_key_file",
        "claim_nonce",
        '"signature"',
    ):
        assert forbidden not in rendered
    deterministic_review = review_authorization(authorization_file=authorization_file)
    assert review == deterministic_review
    review["fixed_request"]["code"] = "tampered"  # type: ignore[index]
    assert review_authorization(authorization_file=authorization_file) == deterministic_review
    assert not (set(sys.modules) - before) & {"xtquant", "xtquant.xtdata"}
    assert not (tmp_path / "claims").exists()
    assert not (tmp_path / "output").exists()

    assert (
        c1c0_main(
            [
                "execute",
                "--authorization-file",
                str(authorization_file),
                "--ack",
                C1C0_EXECUTION_ACK,
            ]
        )
        == 0
    )
    assert (tmp_path / "site-packages" / "xtquant" / "calls.txt").read_text() == "603377.SH"
    assert len(list((tmp_path / "claims").glob("*.json"))) == 1
    _clean_xtquant_modules()


@pytest.mark.parametrize("target", ("plan", "authorization"))
@pytest.mark.parametrize("mutation", ("noncanonical", "unknown", "wrong_type", "sha_drift"))
def test_c1c1_canonical_material_readers_reject_all_byte_and_schema_drift(
    tmp_path: Path, target: str, mutation: str
) -> None:
    c1b_authorization, _facts = _c1b_authorization(
        tmp_path, "def get_his_st_data(code):\n return {}\n"
    )
    plan = prepare_plan(c1b_authorization=c1b_authorization)
    plan_file = tmp_path / "plan.json"
    write_plan(plan=plan, plan_file=plan_file)
    authorization = issue_authorization(plan=plan)
    authorization_file = tmp_path / "authorization.json"
    write_authorization(authorization=authorization, authorization_file=authorization_file)
    path = plan_file if target == "plan" else authorization_file

    if mutation == "noncanonical":
        path.write_text(json.dumps(json.loads(path.read_bytes()), indent=2), encoding="utf-8")
    else:
        raw = json.loads(path.read_bytes())
        if mutation == "unknown":
            raw["unbound"] = "rejected"
        elif mutation == "wrong_type":
            raw["plan_sha256" if target == "plan" else "authorization_key_id"] = 1
        elif target == "plan":
            raw["plan_sha256"] = "0" * 64
        else:
            raw["plan"]["plan_sha256"] = "0" * 64
        path.write_bytes(_canonical(raw))

    if target == "plan":
        with pytest.raises(C1C0OperationError) as captured:
            load_plan(plan_file=path)
        assert captured.value.code == "C1C0_PLAN_INVALID"
    else:
        with pytest.raises(C1C0OperationError) as captured:
            load_authorization(authorization_file=path)
        assert captured.value.code == "C1C0_AUTHORIZATION_INVALID"
    assert not (tmp_path / "claims").exists()
    assert not (tmp_path / "output").exists()


def test_c1c1_canonical_material_paths_reject_repository_and_reparse_before_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c1b_authorization, _facts = _c1b_authorization(
        tmp_path, "def get_his_st_data(code):\n return {}\n"
    )
    plan = prepare_plan(c1b_authorization=c1b_authorization)
    authorization = issue_authorization(plan=plan)
    repository = _repository_root()
    ignored_plan = repository / "tmp" / "c1c1-p0-plan.json"
    ignored_authorization = repository / "frontend" / "dist" / "c1c1-p0-authorization.json"

    def unexpected_open(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("repository path policy must fail before os.open")

    def unexpected_read(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("repository path policy must fail before read_bytes")

    monkeypatch.setattr(c1c0_operation.os, "open", unexpected_open)
    with pytest.raises(C1C0OperationError) as plan_write:
        write_plan(plan=plan, plan_file=ignored_plan)
    assert plan_write.value.code == "C1C0_PATH_UNSAFE"
    with pytest.raises(C1C0OperationError) as authorization_write:
        write_authorization(authorization=authorization, authorization_file=ignored_authorization)
    assert authorization_write.value.code == "C1C0_PATH_UNSAFE"

    monkeypatch.setattr(Path, "read_bytes", unexpected_read)
    with pytest.raises(C1C0OperationError) as plan_read:
        load_plan(plan_file=ignored_plan)
    assert plan_read.value.code == "C1C0_PATH_UNSAFE"
    with pytest.raises(C1C0OperationError) as authorization_read:
        load_authorization(authorization_file=ignored_authorization)
    assert authorization_read.value.code == "C1C0_PATH_UNSAFE"

    external_plan = tmp_path / "external-plan.json"
    with monkeypatch.context() as patch:
        patch.setattr(c1b_operation, "is_reparse_point", lambda _path: True)
        with pytest.raises(C1C0OperationError) as reparse:
            write_plan(plan=plan, plan_file=external_plan)
    assert reparse.value.code == "C1C0_PATH_UNSAFE"
    external_authorization = tmp_path / "external-authorization.json"
    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_symlink", lambda candidate: candidate == tmp_path)
        with pytest.raises(C1C0OperationError) as symlink:
            write_authorization(
                authorization=authorization, authorization_file=external_authorization
            )
    assert symlink.value.code == "C1C0_PATH_UNSAFE"
    assert not ignored_plan.exists()
    assert not ignored_authorization.exists()
    assert not external_plan.exists()
    assert not external_authorization.exists()
    assert not (tmp_path / "claims").exists()
    assert not (tmp_path / "output").exists()


def test_c1c1_cli_rejects_bad_ack_and_cross_dispatch_without_sensitive_error_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    c1b_file, _facts = _c1b_authorization_file(tmp_path, "def get_his_st_data(code):\n return {}\n")
    plan_file = tmp_path / "plan.json"
    authorization_file = tmp_path / "authorization.json"
    assert (
        c1c0_main(
            [
                "prepare",
                "--c1b-authorization-file",
                str(c1b_file),
                "--plan-file",
                str(plan_file),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        c1c0_main(
            [
                "issue",
                "--plan-file",
                str(plan_file),
                "--authorization-file",
                str(authorization_file),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        c1c0_main(
            [
                "execute",
                "--authorization-file",
                str(authorization_file),
                "--ack",
                "wrong acknowledgement",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error_code": "C1C0_ACKNOWLEDGEMENT_REQUIRED"}\n'
    assert str(authorization_file) not in captured.err
    assert not (tmp_path / "claims").exists()

    assert (
        c1c0_main(
            [
                "execute",
                "--authorization-file",
                str(c1b_file),
                "--ack",
                C1C0_EXECUTION_ACK,
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error_code": "C1C0_AUTHORIZATION_INVALID"}\n'
    with pytest.raises(C1BOperationError) as c1b_rejected:
        load_c1b_authorization(authorization_file=authorization_file)
    assert c1b_rejected.value.code == "C1B_AUTHORIZATION_INVALID"
    assert not (tmp_path / "claims").exists()
    assert not (tmp_path / "output").exists()


def test_c1c1_expired_and_replayed_authorizations_do_not_reimport_or_retry_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (
        "from pathlib import Path\n"
        "def get_his_st_data(code):\n"
        " Path(__file__).with_name('calls.txt').write_text(code)\n"
        " return {}\n"
    )
    authorization_file, facts = _issued(tmp_path, body)
    plan = facts["plan"]  # type: ignore[assignment]
    expired_now = UtcTimestamp(plan.expires_at.value + timedelta(seconds=1))
    monkeypatch.setattr(c1b_operation, "utc_now", lambda: expired_now)
    monkeypatch.setattr(c1c0_operation, "utc_now", lambda: expired_now)
    with pytest.raises(C1C0OperationError) as expired:
        execute(authorization_file=authorization_file, acknowledgement=C1C0_EXECUTION_ACK)
    assert expired.value.code == "C1C0_PRECLAIM_CONTEXT_INVALID"
    assert not (tmp_path / "claims").exists()
    assert not (tmp_path / "site-packages" / "xtquant" / "calls.txt").exists()

    monkeypatch.undo()
    output = execute(authorization_file=authorization_file, acknowledgement=C1C0_EXECUTION_ACK)
    assert output.is_dir()
    with pytest.raises(C1C0OperationError) as replay:
        execute(authorization_file=authorization_file, acknowledgement=C1C0_EXECUTION_ACK)
    assert replay.value.code == "C1C0_PRECLAIM_CONTEXT_INVALID"
    assert len(list((tmp_path / "claims").glob("*.json"))) == 1
    assert (tmp_path / "site-packages" / "xtquant" / "calls.txt").read_text() == "603377.SH"
    _clean_xtquant_modules()


def test_c1b_context_cannot_dispatch_c1c0_without_c1c0_outer_authorization(tmp_path: Path) -> None:
    c1b_authorization, _facts = _c1b_authorization(
        tmp_path, "def get_his_st_data(code):\n return {}\n"
    )
    c1b_file = tmp_path / "c1b-only.json"
    c1b_file.write_bytes(c1b_authorization.to_bytes())
    with pytest.raises(C1C0OperationError) as captured:
        execute(authorization_file=c1b_file, acknowledgement=C1C0_EXECUTION_ACK)
    assert captured.value.code == "C1C0_AUTHORIZATION_INVALID"
    assert not (tmp_path / "claims").exists()


def test_mismatched_claim_private_key_is_rejected_before_claim_or_import(tmp_path: Path) -> None:
    c1b_authorization, facts = _c1b_authorization(
        tmp_path, "def get_his_st_data(code):\n return {}\n"
    )
    foreign_private, _foreign_public, _foreign_id = _keypair(tmp_path, "foreign")
    altered_keys = replace(
        c1b_authorization.plan.keys, authorization_private_key_file=foreign_private
    )
    provisional = replace(
        c1b_authorization.plan,
        keys=altered_keys,
        plan_sha256="0" * 64,
    )
    altered_plan = replace(
        provisional,
        plan_sha256=c1b_operation._sha(c1b_operation._canonical(provisional.unsigned_payload())),
    )
    unsigned_c1b = replace(c1b_authorization, plan=altered_plan, signature="0" * 128)
    authorization_signer = ExplicitLocalRealKey(
        private_key_file=facts["authorization_private"]  # type: ignore[arg-type]
    )
    altered_c1b = replace(
        unsigned_c1b,
        signature=authorization_signer.sign(
            domain=c1b_operation._DOMAIN, payload=unsigned_c1b.unsigned_payload()
        ),
    )
    plan = prepare_plan(c1b_authorization=altered_c1b)
    unsigned_c1c = C1C0Authorization(
        plan=plan,
        authorization_key_id=authorization_signer.key_id,
        signature="0" * 128,
    )
    altered_c1c = replace(
        unsigned_c1c,
        signature=authorization_signer.sign(
            domain=c1c0_operation._SIGN_DOMAIN, payload=unsigned_c1c.unsigned()
        ),
    )
    authorization_file = tmp_path / "mismatched-authorization.json"
    authorization_file.write_bytes(altered_c1c.to_bytes())
    with pytest.raises(C1C0OperationError) as captured:
        execute(authorization_file=authorization_file, acknowledgement=C1C0_EXECUTION_ACK)
    assert captured.value.code == "C1C0_PRECLAIM_CONTEXT_INVALID"
    assert not (tmp_path / "claims").exists()
    assert not (tmp_path / "output").exists()
    _clean_xtquant_modules()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "unbound-schema-drift"),
        ("adapter_version", "unbound-adapter-drift"),
        ("package_version", "9.9.9"),
        ("runner_contract_id", "unbound-runner-contract"),
        ("runner_profile", "unbound-runner-profile"),
        ("runner_version", "unbound-runner-version"),
        ("fixed_request", {"code": "000001.SZ", "interface": "anything"}),
        ("event_order", ["CALL", "OUTPUT"]),
        ("conclusion", "READY"),
    ],
)
def test_signed_manifest_semantic_drift_is_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    authorization, facts = _issued(
        tmp_path, "def get_his_st_data(code):\n return {'ST': [['20260323', '20260418']]}\n"
    )
    output = execute(authorization_file=authorization, acknowledgement=C1C0_EXECUTION_ACK)
    manifest = json.loads((output / "manifest.json").read_bytes())
    manifest[field] = value
    (output / "manifest.json").write_bytes(_canonical(manifest))
    _resign_manifest(output, facts["runner_private"])  # type: ignore[arg-type]
    with pytest.raises(C1C0OperationError) as captured:
        verify_observation(directory=output)
    assert captured.value.code == "C1C0_OBSERVATION_INVALID"
    _clean_xtquant_modules()


@pytest.mark.parametrize(
    "claim_field", ["claim_nonce", "claimed_at", "invocation_id", "plan_sha256"]
)
def test_signed_claim_semantic_drift_is_rejected(tmp_path: Path, claim_field: str) -> None:
    authorization, facts = _issued(
        tmp_path, "def get_his_st_data(code):\n return {'ST': [['20260323', '20260418']]}\n"
    )
    output = execute(authorization_file=authorization, acknowledgement=C1C0_EXECUTION_ACK)
    outer = (output / "authorization.json").read_bytes()
    plan = facts["plan"]
    values: dict[str, object] = {
        "authorization_id": plan.identity.authorization_id,  # type: ignore[union-attr]
        "invocation_id": plan.identity.invocation_id,  # type: ignore[union-attr]
        "run_id": plan.identity.run_id,  # type: ignore[union-attr]
        "claimed_at": utc_now(),
        "claim_nonce": plan.claim_nonce,  # type: ignore[union-attr]
        "authorization_envelope_sha256": _sha(outer),
        "plan_sha256": plan.plan_sha256,  # type: ignore[union-attr]
    }
    if claim_field == "claim_nonce":
        values[claim_field] = "d" * 64
    elif claim_field == "claimed_at":
        values[claim_field] = UtcTimestamp(utc_now().value + timedelta(hours=1))
    elif claim_field == "invocation_id":
        values[claim_field] = new_entity_id("data_invocation")
    else:
        values[claim_field] = "d" * 64
    claim = RealClaimReceiptIssuer(
        private_key_file=facts["authorization_private"]  # type: ignore[arg-type]
    ).issue(**values)  # type: ignore[arg-type]
    claim_bytes = claim.to_bytes()
    (output / "claim-receipt.json").write_bytes(claim_bytes)
    manifest = json.loads((output / "manifest.json").read_bytes())
    manifest["files"]["claim-receipt.json"] = _sha(claim_bytes)
    manifest["claim_receipt_sha256"] = _sha(claim_bytes)
    (output / "manifest.json").write_bytes(_canonical(manifest))
    _resign_manifest(output, facts["runner_private"])  # type: ignore[arg-type]
    with pytest.raises(C1C0OperationError):
        verify_observation(directory=output)
    _clean_xtquant_modules()


@pytest.mark.parametrize(
    "field,value",
    [
        ("fixed_request", {"code": "000001.SZ", "interface": "x"}),
        ("runner_version", "drift"),
        ("conclusion", "READY"),
    ],
)
def test_signed_observation_semantic_drift_is_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    authorization, facts = _issued(
        tmp_path, "def get_his_st_data(code):\n return {'ST': [['20260323', '20260418']]}\n"
    )
    output = execute(authorization_file=authorization, acknowledgement=C1C0_EXECUTION_ACK)
    observation = json.loads((output / "observation.json").read_bytes())
    observation[field] = value
    observation_bytes = _canonical(observation)
    (output / "observation.json").write_bytes(observation_bytes)
    manifest = json.loads((output / "manifest.json").read_bytes())
    manifest["files"]["observation.json"] = _sha(observation_bytes)
    manifest["observation_sha256"] = _sha(observation_bytes)
    (output / "manifest.json").write_bytes(_canonical(manifest))
    _resign_manifest(output, facts["runner_private"])  # type: ignore[arg-type]
    with pytest.raises(C1C0OperationError):
        verify_observation(directory=output)
    _clean_xtquant_modules()


def test_verifier_rejects_moved_artifact_and_works_after_private_keys_removed(
    tmp_path: Path,
) -> None:
    authorization, facts = _issued(
        tmp_path, "def get_his_st_data(code):\n return {'ST': [['20260323', '20260418']]}\n"
    )
    output = execute(authorization_file=authorization, acknowledgement=C1C0_EXECUTION_ACK)
    replay = tmp_path / "replayed-artifact"
    shutil.copytree(output, replay)
    with pytest.raises(C1C0OperationError):
        verify_observation(directory=replay)
    shutil.rmtree(facts["reference_bundle_root"])  # type: ignore[arg-type]
    facts["authorization_private"].unlink()  # type: ignore[union-attr]
    facts["runner_private"].unlink()  # type: ignore[union-attr]
    assert verify_observation(directory=output)["conclusion"] == "BLOCKED/CONTRACT_INCOMPLETE"
    _clean_xtquant_modules()


@pytest.mark.parametrize(
    "field,value", [("authorization_id", "..\\escaped-claim"), ("run_id", "..\\..\\escaped-output")]
)
def test_c1b_structured_ids_reject_escape_before_c1c0_plan_or_output(
    tmp_path: Path, field: str, value: str
) -> None:
    c1b_authorization, facts = _c1b_authorization(
        tmp_path, "def get_his_st_data(code):\n return {}\n"
    )
    kwargs = dict(facts["kwargs"])  # type: ignore[arg-type]
    kwargs[field] = value
    with pytest.raises((C1BOperationError, InvalidIdError)):
        prepare_c1b_plan(**kwargs)  # type: ignore[arg-type]
    assert not (tmp_path / "escaped-claim.json").exists()
    assert not (tmp_path / "escaped-output").exists()
    assert c1b_authorization.plan.identity.authorization_id.value.hex


def test_unknown_outer_plan_field_is_rejected_before_claim(tmp_path: Path) -> None:
    authorization, _facts = _issued(tmp_path, "def get_his_st_data(code):\n return {}\n")
    raw = json.loads(authorization.read_bytes())
    raw["plan"]["unbound"] = "rejected"
    authorization.write_bytes(_canonical(raw))
    with pytest.raises(C1C0OperationError) as captured:
        execute(authorization_file=authorization, acknowledgement=C1C0_EXECUTION_ACK)
    assert captured.value.code == "C1C0_AUTHORIZATION_INVALID"
    assert not (tmp_path / "claims").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "delete_manifest",
        "change_manifest",
        "delete_declared_file",
        "fixed_sample_loader_drift",
        "delete_metadata_database",
        "replace_metadata_database",
    ],
)
def test_live_c1b_c1a_precheck_rejects_drift_before_claim_or_fake_sdk_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    body = (
        "from pathlib import Path\n"
        "def get_his_st_data(code):\n"
        " Path(__file__).with_name('calls.txt').write_text(code)\n"
        " return {}\n"
    )
    authorization, facts = _issued(tmp_path, body)
    bundle = facts["bundle_directory"]  # type: ignore[assignment]
    manifest = bundle / "manifest.json"
    metadata = facts["metadata_database"]  # type: ignore[assignment]
    if mutation == "delete_manifest":
        manifest.unlink()
    elif mutation == "change_manifest":
        manifest.write_bytes(manifest.read_bytes() + b" ")
    elif mutation == "delete_declared_file":
        raw = json.loads(manifest.read_bytes())
        (bundle / raw["files"][0]["relative_path"]).unlink()
    elif mutation == "fixed_sample_loader_drift":
        sample = load_fixed_sample_v4()
        object.__setattr__(sample, "sample_spec_version", "s1-07.fixed-sample.tampered")
        object.__setattr__(sample, "sample_spec_sha256", sample.compute_sha256())
        monkeypatch.setattr(
            "quantking.data.validation.real_runtime.load_fixed_sample_v4", lambda: sample
        )
    elif mutation == "delete_metadata_database":
        metadata.unlink()
    else:
        metadata.write_bytes(b"replaced-temporary-metadata-database")
    with pytest.raises(C1C0OperationError) as captured:
        execute(authorization_file=authorization, acknowledgement=C1C0_EXECUTION_ACK)
    assert captured.value.code in {
        "C1C0_PRECLAIM_CONTEXT_INVALID",
        "C1C0_METADATA_DATABASE_UNAVAILABLE",
    }
    assert not (tmp_path / "site-packages" / "xtquant" / "calls.txt").exists()
    assert not (tmp_path / "claims").exists()
    assert not (tmp_path / "output").exists()
    _clean_xtquant_modules()


def test_package_path_redirection_and_preloaded_external_modules_are_rejected(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external" / "xtquant"
    external.mkdir(parents=True)
    (external / "xtdata.py").write_text(
        "def get_his_st_data(code):\n return {'ST': [['19990101', '19990102']]}\n",
        encoding="utf-8",
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


@pytest.mark.parametrize("mutation", ["extra", "missing", "response", "parsed"])
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
    elif mutation == "response":
        (output / "response.canonical.json").write_text("{}", encoding="utf-8")
    else:
        raw = json.loads((output / "observation.json").read_bytes())
        raw["parsed"]["intervals"] = []
        (output / "observation.json").write_bytes(_canonical(raw))
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
