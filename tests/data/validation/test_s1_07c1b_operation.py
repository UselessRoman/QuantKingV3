"""S1-07C1B offline-only formal-entry regressions."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from quantking.common.ids import new_entity_id
from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.models import DatasetKind
from quantking.data.validation import c1b_operation
from quantking.data.validation.authority_bundle import (
    CONTROLLED_AUTHORITY_BUNDLE_MANIFEST_SHA256,
)
from quantking.data.validation.c1b_cli import main
from quantking.data.validation.c1b_operation import (
    C1B_EXECUTION_ACK,
    C1BExecutionAuthorization,
    C1BOperationError,
    execute,
    issue_authorization,
    load_authorization,
    load_plan,
    prepare_plan,
    write_authorization,
    write_plan,
)
from quantking.data.validation.models import ValidationVersions
from quantking.data.validation.real_authorization import (
    ExplicitLocalRealKey,
    RealExecutionAuthorizationAuthority,
    RealRuntimeAuthorizationAuthority,
)
from quantking.data.validation.real_runtime import RealXtQuantValidationRuntime
from quantking.data.validation.samples import load_fixed_sample_v4
from quantking.jobs.adapters.persistence.sqlite import initialize_database


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


def _write_metadata(root: Path, version: str = "1.2.3") -> None:
    metadata = root / f"xtquant-{version}.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(f"Name: xtquant\nVersion: {version}\n", encoding="utf-8")


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


def _prepare_kwargs(root: Path, *, expired: bool = False) -> dict[str, object]:
    _write_verified_authority_bundle(root / "authority")
    site_packages = root / "site-packages"
    _write_metadata(site_packages)
    authorization_private, authorization_public, authorization_id = _keypair(root, "authorization")
    runner_private, runner_public, runner_id = _keypair(root, "runner")
    database = root / "metadata.db"
    initialize_database(database)
    now = utc_now().value
    valid_from = UtcTimestamp(now - timedelta(minutes=2))
    expires_at = (
        UtcTimestamp(now - timedelta(seconds=1))
        if expired
        else UtcTimestamp(now + timedelta(minutes=5))
    )
    versions = _versions()
    return {
        "source": "XTQUANT",
        "dataset": DatasetKind.TRADING_STATUS,
        "case_id": "st_warning_603377_sh",
        "xtquant_site_packages_root": site_packages,
        "reference_bundle_root": root / "authority",
        "reference_bundle_id": "st-603377-authority-v3",
        "reference_bundle_sha256": CONTROLLED_AUTHORITY_BUNDLE_MANIFEST_SHA256,
        "output_root": root / "output",
        "claim_root": root / "claims",
        "metadata_database": database,
        "sqlite_busy_timeout_ms": 250,
        "assessment_id": str(new_entity_id("data_assessment")),
        "session_id": str(new_entity_id("data_session")),
        "invocation_id": str(new_entity_id("data_invocation")),
        "run_id": str(new_entity_id("data_validation")),
        "authorization_id": str(new_entity_id("data_authorization")),
        "authorization_private_key_file": authorization_private,
        "authorization_public_key_file": authorization_public,
        "authorization_allowed_key_ids": (authorization_id,),
        "runner_private_key_file": runner_private,
        "runner_public_key_file": runner_public,
        "runner_allowed_key_ids": (runner_id,),
        "valid_from": valid_from.to_iso(),
        "expires_at": expires_at.to_iso(),
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


def _issued(root: Path, *, expired: bool = False) -> tuple[Path, dict[str, object]]:
    kwargs = _prepare_kwargs(root, expired=expired)
    plan = prepare_plan(**kwargs)  # type: ignore[arg-type]
    authorization = issue_authorization(plan=plan)
    authorization_file = root / "c1b-authorization.json"
    write_authorization(authorization=authorization, authorization_file=authorization_file)
    return authorization_file, kwargs


def _prepare_cli_args(kwargs: dict[str, object], plan_file: Path) -> list[str]:
    names = {
        "source": "--source",
        "dataset": "--dataset",
        "case_id": "--case-id",
        "xtquant_site_packages_root": "--xtquant-site-packages-root",
        "reference_bundle_root": "--reference-bundle-root",
        "reference_bundle_id": "--reference-bundle-id",
        "reference_bundle_sha256": "--reference-bundle-sha256",
        "output_root": "--output-root",
        "claim_root": "--claim-root",
        "metadata_database": "--metadata-database",
        "sqlite_busy_timeout_ms": "--sqlite-busy-timeout-ms",
        "assessment_id": "--assessment-id",
        "session_id": "--session-id",
        "invocation_id": "--invocation-id",
        "run_id": "--run-id",
        "authorization_id": "--authorization-id",
        "authorization_private_key_file": "--authorization-private-key-file",
        "authorization_public_key_file": "--authorization-public-key-file",
        "runner_private_key_file": "--runner-private-key-file",
        "runner_public_key_file": "--runner-public-key-file",
        "valid_from": "--valid-from",
        "expires_at": "--expires-at",
        "claim_nonce": "--claim-nonce",
        "adapter_version": "--adapter-version",
        "package_version": "--package-version",
        "code_version": "--code-version",
        "schema_version": "--schema-version",
        "rule_version": "--rule-version",
        "threshold_version": "--threshold-version",
        "sample_spec_version": "--sample-spec-version",
        "config_version": "--config-version",
    }
    args = ["prepare"]
    for key, flag in names.items():
        value = kwargs[key]
        args.extend((flag, value.value if isinstance(value, DatasetKind) else str(value)))
    for key, flag in (
        ("authorization_allowed_key_ids", "--authorization-allowed-key-id"),
        ("runner_allowed_key_ids", "--runner-allowed-key-id"),
    ):
        args.extend((flag, str(kwargs[key][0])))  # type: ignore[index]
    args.extend(("--plan-file", str(plan_file)))
    return args


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _assert_no_execution_side_effects(root: Path) -> None:
    assert not (root / "claims").exists()
    assert not (root / "output").exists()


def test_help_prepare_issue_and_review_do_not_import_xtquant_or_execute(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = set(sys.modules)
    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    assert help_exit.value.code == 0

    kwargs = _prepare_kwargs(tmp_path)
    plan_file = tmp_path / "reviewed-plan.json"
    assert main(_prepare_cli_args(kwargs, plan_file)) == 0
    assert not (tmp_path / "claims").exists()
    assert not (tmp_path / "output").exists()
    prepare_output = capsys.readouterr().out
    assert "signature" not in prepare_output
    assert "authorization_private_key_file" not in prepare_output

    authorization_file = tmp_path / "c1b-authorization.json"
    assert (
        main(
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
    assert main(["review", "--authorization-file", str(authorization_file)]) == 0
    review_output = capsys.readouterr().out
    assert "signature" not in review_output
    assert "claim_nonce" not in review_output
    assert "xtquant" not in set(sys.modules) - before
    assert not (tmp_path / "claims").exists()
    assert not (tmp_path / "output").exists()


def test_execute_is_file_only_failure_only_and_persists_after_seal(tmp_path: Path) -> None:
    authorization_file, _kwargs = _issued(tmp_path)
    before = set(sys.modules)

    result = execute(
        authorization_file=authorization_file,
        acknowledgement=C1B_EXECUTION_ACK,
    )

    assert result.event_order == (
        "PRECHECK",
        "CLAIM",
        "IMPORT",
        "OUTPUT",
        "VERIFY",
        "SEAL",
        "PERSIST",
    )
    assert result.assessment_status == "BLOCKED_EXTERNAL_VALIDATION"
    assert len(list((tmp_path / "claims").glob("*.json"))) == 1
    assert list((tmp_path / "output" / ".assessment-seals").glob("*.zip"))
    assert "xtquant" not in set(sys.modules) - before
    with pytest.raises(C1BOperationError) as repeated:
        execute(authorization_file=authorization_file, acknowledgement=C1B_EXECUTION_ACK)
    assert repeated.value.code == "AUTHORIZATION_ALREADY_CONSUMED"

    with pytest.raises(TypeError):
        execute(  # type: ignore[call-arg]
            authorization_file=authorization_file,
            acknowledgement=C1B_EXECUTION_ACK,
            output_root=tmp_path / "override",
        )


def test_missing_relative_reparse_and_expired_inputs_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative = _prepare_kwargs(tmp_path / "relative")
    relative["xtquant_site_packages_root"] = Path("relative-site-packages")
    with pytest.raises(C1BOperationError) as invalid:
        prepare_plan(**relative)  # type: ignore[arg-type]
    assert invalid.value.code == "C1B_PARAMETER_INVALID"

    reparse = _prepare_kwargs(tmp_path / "reparse")
    with monkeypatch.context() as patch:
        patch.setattr(c1b_operation, "is_reparse_point", lambda _path: True)
        with pytest.raises(C1BOperationError) as unsafe:
            prepare_plan(**reparse)  # type: ignore[arg-type]
        assert unsafe.value.code == "C1B_PATH_UNSAFE"

    authorization_file, _kwargs = _issued(tmp_path / "expired", expired=True)
    with pytest.raises(C1BOperationError) as expired:
        execute(authorization_file=authorization_file, acknowledgement=C1B_EXECUTION_ACK)
    assert expired.value.code == "AUTHORIZATION_INVALID_OR_EXPIRED"
    assert not (tmp_path / "expired" / "claims").exists()
    assert not (tmp_path / "expired" / "output").exists()


def test_package_and_key_substitution_stop_before_claim(tmp_path: Path) -> None:
    package_file, package_kwargs = _issued(tmp_path / "package")
    metadata = (
        Path(package_kwargs["xtquant_site_packages_root"]) / "xtquant-1.2.3.dist-info" / "METADATA"
    )
    metadata.write_text("Name: xtquant\nVersion: 1.2.4\n", encoding="utf-8")
    with pytest.raises(C1BOperationError) as package:
        execute(authorization_file=package_file, acknowledgement=C1B_EXECUTION_ACK)
    assert package.value.code == "C1B_PLAN_BINDING_MISMATCH"
    assert not (tmp_path / "package" / "claims").exists()
    assert not (tmp_path / "package" / "output").exists()

    key_file, key_kwargs = _issued(tmp_path / "key")
    _unused_private, changed_public, _changed_id = _keypair(tmp_path / "key", "changed")
    Path(key_kwargs["runner_public_key_file"]).write_bytes(changed_public.read_bytes())
    with pytest.raises(C1BOperationError) as key:
        execute(authorization_file=key_file, acknowledgement=C1B_EXECUTION_ACK)
    assert key.value.code == "C1B_KEY_INVALID"
    assert not (tmp_path / "key" / "claims").exists()
    assert not (tmp_path / "key" / "output").exists()


@pytest.mark.parametrize("field", ("identity", "bundle", "profile", "versions", "output"))
def test_signed_inner_drift_is_rejected_before_claim(tmp_path: Path, field: str) -> None:
    authorization_file, _kwargs = _issued(tmp_path / field)
    original = load_authorization(authorization_file=authorization_file)
    envelope = original.c1a_authorization
    if field == "identity":
        changed = RealRuntimeAuthorizationAuthority(
            private_key_file=original.plan.keys.authorization_private_key_file
        ).issue(
            authorization=envelope.authorization,
            identity=replace(envelope.identity, session_id=new_entity_id("data_session")),
            selected_case_ids=envelope.selected_case_ids,
            reference_bundle_id=envelope.reference_bundle_id,
            reference_bundle_sha256=envelope.reference_bundle_sha256,
            source_dataset_contract=envelope.source_dataset_contract,
            promotion_profile=envelope.promotion_profile,
            output_scope=envelope.output_scope,
            frozen_versions=envelope.frozen_versions,
            plan_sha256=envelope.plan_sha256,
            claim_nonce=envelope.claim_nonce,
            runner_key_id=envelope.runner_key_id,
        )
    elif field == "bundle":
        changed_profile = replace(
            envelope.promotion_profile,
            reference_bundle_id="st-603377-alternate-v3",
            reference_bundle_manifest_sha256="a" * 64,
        )
        changed = RealRuntimeAuthorizationAuthority(
            private_key_file=original.plan.keys.authorization_private_key_file
        ).issue(
            authorization=envelope.authorization,
            identity=envelope.identity,
            selected_case_ids=envelope.selected_case_ids,
            reference_bundle_id=changed_profile.reference_bundle_id,
            reference_bundle_sha256=changed_profile.reference_bundle_manifest_sha256,
            source_dataset_contract=envelope.source_dataset_contract,
            promotion_profile=changed_profile,
            output_scope=envelope.output_scope,
            frozen_versions=envelope.frozen_versions,
            plan_sha256=envelope.plan_sha256,
            claim_nonce=envelope.claim_nonce,
            runner_key_id=envelope.runner_key_id,
        )
    elif field == "profile":
        changed_profile = replace(
            envelope.promotion_profile,
            promotion_profile_version="xtquant-trading-status-real-promotion.c1a-r3.v1",
        )
        changed = RealRuntimeAuthorizationAuthority(
            private_key_file=original.plan.keys.authorization_private_key_file
        ).issue(
            authorization=envelope.authorization,
            identity=envelope.identity,
            selected_case_ids=envelope.selected_case_ids,
            reference_bundle_id=changed_profile.reference_bundle_id,
            reference_bundle_sha256=changed_profile.reference_bundle_manifest_sha256,
            source_dataset_contract=envelope.source_dataset_contract,
            promotion_profile=changed_profile,
            output_scope=envelope.output_scope,
            frozen_versions=envelope.frozen_versions,
            plan_sha256=envelope.plan_sha256,
            claim_nonce=envelope.claim_nonce,
            runner_key_id=envelope.runner_key_id,
        )
    elif field == "versions":
        changed = RealRuntimeAuthorizationAuthority(
            private_key_file=original.plan.keys.authorization_private_key_file
        ).issue(
            authorization=envelope.authorization,
            identity=envelope.identity,
            selected_case_ids=envelope.selected_case_ids,
            reference_bundle_id=envelope.reference_bundle_id,
            reference_bundle_sha256=envelope.reference_bundle_sha256,
            source_dataset_contract=envelope.source_dataset_contract,
            promotion_profile=envelope.promotion_profile,
            output_scope=envelope.output_scope,
            frozen_versions={**envelope.frozen_versions, "adapter_version": "drifted.v1"},
            plan_sha256=envelope.plan_sha256,
            claim_nonce=envelope.claim_nonce,
            runner_key_id=envelope.runner_key_id,
        )
    else:
        changed_execution = RealExecutionAuthorizationAuthority(
            private_key_file=original.plan.keys.authorization_private_key_file
        ).issue(
            authorization_id=envelope.authorization.authorization_id,
            source="XTQUANT",
            dataset=DatasetKind.TRADING_STATUS,
            output_root=tmp_path / field / "different-output",
            sample_sha256=envelope.authorization.sample_sha256,
            valid_from=envelope.authorization.valid_from,
            expires_at=envelope.authorization.expires_at,
        )
        changed = RealRuntimeAuthorizationAuthority(
            private_key_file=original.plan.keys.authorization_private_key_file
        ).issue(
            authorization=changed_execution,
            identity=envelope.identity,
            selected_case_ids=envelope.selected_case_ids,
            reference_bundle_id=envelope.reference_bundle_id,
            reference_bundle_sha256=envelope.reference_bundle_sha256,
            source_dataset_contract=envelope.source_dataset_contract,
            promotion_profile=envelope.promotion_profile,
            output_scope=str(tmp_path / field / "different-output"),
            frozen_versions=envelope.frozen_versions,
            plan_sha256=envelope.plan_sha256,
            claim_nonce=envelope.claim_nonce,
            runner_key_id=envelope.runner_key_id,
        )
    signer = ExplicitLocalRealKey(
        private_key_file=original.plan.keys.authorization_private_key_file
    )
    unsigned = C1BExecutionAuthorization(
        plan=original.plan,
        c1a_authorization=changed,
        authorization_key_id=signer.key_id,
        signature="0" * 128,
    )
    drifted = C1BExecutionAuthorization(
        plan=original.plan,
        c1a_authorization=changed,
        authorization_key_id=signer.key_id,
        signature=signer.sign(
            domain="quantking.s1-07c1b.xtquant-trading-status-authorization.v1",
            payload=unsigned.unsigned_payload(),
        ),
    )
    drifted_file = tmp_path / field / "drifted-authorization.json"
    write_authorization(authorization=drifted, authorization_file=drifted_file)
    with pytest.raises(C1BOperationError) as rejected:
        execute(authorization_file=drifted_file, acknowledgement=C1B_EXECUTION_ACK)
    assert rejected.value.code == "C1B_AUTHORIZATION_BINDING_MISMATCH"
    assert not (tmp_path / field / "claims").exists()
    assert not (tmp_path / field / "output").exists()


@pytest.mark.parametrize("failure", ("import", "runner", "output"))
def test_claim_after_failures_keep_immutable_incomplete_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    authorization_file, _kwargs = _issued(tmp_path / failure)
    original = c1b_operation._runtime_for_plan

    def crashing_runtime(plan: object) -> RealXtQuantValidationRuntime:
        assert hasattr(plan, "identity")
        runtime = original(plan)  # type: ignore[arg-type]
        if failure == "import":
            runtime._import_runner = lambda _module: (_ for _ in ()).throw(RuntimeError("boom"))
        elif failure == "runner":
            runtime._import_runner = lambda _module: SimpleNamespace(
                create_runner=lambda: SimpleNamespace(
                    execute=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
                )
            )
        else:
            runtime._import_runner = lambda _module: SimpleNamespace(
                create_runner=lambda: SimpleNamespace(execute=lambda **_kwargs: tmp_path / "wrong")
            )
        return runtime

    monkeypatch.setattr(c1b_operation, "_runtime_for_plan", crashing_runtime)
    with pytest.raises(C1BOperationError) as captured:
        execute(authorization_file=authorization_file, acknowledgement=C1B_EXECUTION_ACK)
    expected = {
        "import": "REAL_RUNNER_IMPORT_FAILED",
        "runner": "REAL_RUNNER_CRASHED",
        "output": "REAL_RUNNER_OUTPUT_INVALID",
    }
    assert captured.value.code == expected[failure]
    assert len(list((tmp_path / failure / "claims").glob("*.json"))) == 1
    incomplete = list((tmp_path / failure / "output").rglob("*.incomplete"))
    assert incomplete and (incomplete[0] / "failure.json").is_file()


def test_noncanonical_old_dto_and_cli_overrides_cannot_execute(tmp_path: Path) -> None:
    authorization_file, _kwargs = _issued(tmp_path)
    raw = json.loads(authorization_file.read_bytes())
    authorization_file.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    with pytest.raises(C1BOperationError) as noncanonical:
        execute(authorization_file=authorization_file, acknowledgement=C1B_EXECUTION_ACK)
    assert noncanonical.value.code == "C1B_AUTHORIZATION_INVALID"
    assert not (tmp_path / "claims").exists()

    with pytest.raises(SystemExit):
        main(
            [
                "execute",
                "--authorization-file",
                str(authorization_file),
                "--ack",
                C1B_EXECUTION_ACK,
                "--runner-key-id",
                "caller-reported",
            ]
        )
    assert not (tmp_path / "claims").exists()

    plan = prepare_plan(**_prepare_kwargs(tmp_path / "legacy"))  # type: ignore[arg-type]
    plan_file = tmp_path / "legacy" / "plan.json"
    write_plan(plan=plan, plan_file=plan_file)
    assert plan_file.is_file()
    with pytest.raises(TypeError):
        issue_authorization(plan=plan, authorization={"legacy": "dto"})  # type: ignore[call-arg]


def test_repository_plan_and_authorization_paths_fail_before_io_or_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs = _prepare_kwargs(tmp_path)
    plan = prepare_plan(**kwargs)  # type: ignore[arg-type]
    authorization = issue_authorization(plan=plan)
    repository = _repository_root()
    ignored_plan = repository / "tmp" / "c1b-r1-plan.json"
    ignored_authorization = repository / "frontend" / "dist" / "c1b-r1-authorization.json"

    def unexpected_open(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("repository path policy must fail before os.open")

    def unexpected_read(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("repository path policy must fail before read_bytes")

    monkeypatch.setattr(c1b_operation.os, "open", unexpected_open)
    with pytest.raises(C1BOperationError) as repository_root_write:
        write_plan(plan=plan, plan_file=repository)
    assert repository_root_write.value.code == "C1B_REPOSITORY_PATH_FORBIDDEN"
    with pytest.raises(C1BOperationError) as plan_write:
        write_plan(plan=plan, plan_file=ignored_plan)
    assert plan_write.value.code == "C1B_REPOSITORY_PATH_FORBIDDEN"
    with pytest.raises(C1BOperationError) as authorization_write:
        write_authorization(authorization=authorization, authorization_file=ignored_authorization)
    assert authorization_write.value.code == "C1B_REPOSITORY_PATH_FORBIDDEN"

    monkeypatch.setattr(Path, "read_bytes", unexpected_read)
    with pytest.raises(C1BOperationError) as repository_root_load:
        load_plan(plan_file=repository)
    assert repository_root_load.value.code == "C1B_REPOSITORY_PATH_FORBIDDEN"
    with pytest.raises(C1BOperationError) as plan_load:
        load_plan(plan_file=ignored_plan)
    assert plan_load.value.code == "C1B_REPOSITORY_PATH_FORBIDDEN"
    with pytest.raises(C1BOperationError) as authorization_load:
        load_authorization(authorization_file=ignored_authorization)
    assert authorization_load.value.code == "C1B_REPOSITORY_PATH_FORBIDDEN"

    assert main(["review", "--authorization-file", str(ignored_authorization)]) == 2
    assert (
        main(
            [
                "execute",
                "--authorization-file",
                str(ignored_authorization),
                "--ack",
                C1B_EXECUTION_ACK,
            ]
        )
        == 2
    )
    assert not ignored_plan.exists()
    assert not ignored_authorization.exists()
    _assert_no_execution_side_effects(tmp_path)


def test_cli_preflights_repository_outputs_before_plan_or_authorization_issuance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs = _prepare_kwargs(tmp_path)
    repository = _repository_root()
    ignored_plan = repository / "tmp" / "c1b-r1-cli-plan.json"
    ignored_authorization = repository / "tmp" / "c1b-r1-cli-authorization.json"

    def unexpected_prepare(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("repository plan target must fail before plan construction")

    monkeypatch.setattr("quantking.data.validation.c1b_cli.prepare_plan", unexpected_prepare)
    assert main(_prepare_cli_args(kwargs, ignored_plan)) == 2
    assert not ignored_plan.exists()

    external_plan = tmp_path / "external-plan.json"
    plan = prepare_plan(**kwargs)  # type: ignore[arg-type]
    write_plan(plan=plan, plan_file=external_plan)

    def unexpected_issue(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("repository authorization target must fail before signing")

    monkeypatch.setattr("quantking.data.validation.c1b_cli.issue_authorization", unexpected_issue)
    assert (
        main(
            [
                "issue",
                "--plan-file",
                str(external_plan),
                "--authorization-file",
                str(ignored_authorization),
            ]
        )
        == 2
    )
    assert not ignored_authorization.exists()
    _assert_no_execution_side_effects(tmp_path)


@pytest.mark.parametrize(
    "private_field",
    ("authorization_private_key_file", "runner_private_key_file"),
)
def test_repository_private_keys_are_rejected_during_prepare_without_side_effects(
    tmp_path: Path, private_field: str
) -> None:
    kwargs = _prepare_kwargs(tmp_path)
    kwargs[private_field] = _repository_root() / "tmp" / f"{private_field}.pem"

    with pytest.raises(C1BOperationError) as rejected:
        prepare_plan(**kwargs)  # type: ignore[arg-type]

    assert rejected.value.code == "C1B_REPOSITORY_PATH_FORBIDDEN"
    assert not list(tmp_path.glob("*.json"))
    _assert_no_execution_side_effects(tmp_path)


def test_casefolded_repository_and_ignored_paths_cannot_bypass_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = prepare_plan(**_prepare_kwargs(tmp_path))  # type: ignore[arg-type]
    casefolded_repository = Path(str(_repository_root()).swapcase())
    ignored_plan = casefolded_repository / "TMP" / "c1b-r1-casefolded-plan.json"

    monkeypatch.chdir(tmp_path)
    with pytest.raises(C1BOperationError) as rejected:
        write_plan(plan=plan, plan_file=ignored_plan)

    assert rejected.value.code == "C1B_REPOSITORY_PATH_FORBIDDEN"
    assert not ignored_plan.exists()
    _assert_no_execution_side_effects(tmp_path)


def test_external_controlled_temporary_paths_remain_usable(tmp_path: Path) -> None:
    plan = prepare_plan(**_prepare_kwargs(tmp_path))  # type: ignore[arg-type]
    plan_file = tmp_path / "reviewed-plan.json"
    write_plan(plan=plan, plan_file=plan_file)
    loaded_plan = load_plan(plan_file=plan_file)
    authorization = issue_authorization(plan=loaded_plan)
    authorization_file = tmp_path / "c1b-authorization.json"
    write_authorization(authorization=authorization, authorization_file=authorization_file)

    assert (
        load_authorization(authorization_file=authorization_file).plan.plan_sha256
        == plan.plan_sha256
    )
    _assert_no_execution_side_effects(tmp_path)
