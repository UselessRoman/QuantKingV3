"""S1-07A-R1 attacks against REAL identity, lineage, stability and authorization."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from quantking.data.models import DatasetKind


def test_packaged_sample_is_framework_only_until_authoritative_events_exist() -> None:
    from quantking.data.validation.samples import SampleEligibility, load_packaged_fixed_sample

    sample = load_packaged_fixed_sample()
    assert sample.eligibility is SampleEligibility.FRAMEWORK_ONLY
    assert {
        "IPO",
        "DELISTING",
        "ST",
        "SUSPENSION",
        "CORPORATE_ACTION",
    }.issubset(set(sample.missing_authoritative_purposes))
    assert not sample.fixed_sample_gate_eligible


def test_direct_assessment_boolean_api_cannot_certify_production() -> None:
    from quantking.data.validation.models import AssessmentGates
    from quantking.data.validation.service import assess_verified_runs

    with pytest.raises(TypeError):
        assess_verified_runs(  # type: ignore[call-arg]
            source="XTQUANT",
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            gates=AssessmentGates.all_passed(),
            verified_run_ids=(),
            assessment_config_version="validation.v1",
            sample_spec_version="s1-07.fixed-sample.v1",
            rule_version="quality-rules.v1",
        )


def test_direct_real_run_construction_cannot_create_verified_identity() -> None:
    from quantking.data.validation.models import RealSourceValidationRun

    with pytest.raises((TypeError, ValueError)):
        RealSourceValidationRun()  # type: ignore[call-arg]


def test_writer_manifest_does_not_persist_caller_declared_gates_or_real_kind(
    tmp_path: Path,
) -> None:
    from quantking.data.validation.testing import build_deep_fake_run

    built = build_deep_fake_run(root=tmp_path, run_index=1)
    manifest = json.loads((built.directory / "manifest.json").read_text(encoding="utf-8"))
    assert "gates" not in manifest
    assert "evidence_kind" not in manifest


def test_deep_verifier_rejects_fake_quality_catalogue_and_ineligible_sample(
    tmp_path: Path,
) -> None:
    from quantking.data.validation.testing import build_deep_fake_run

    built = build_deep_fake_run(root=tmp_path, run_index=1)
    verified = built.verifier.verify_run(directory=built.directory)
    assert verified.evidence_kind.value == "SYNTHETIC"
    assert verified.gates.semantic_contract_verified
    assert verified.gates.schema_unit_verified
    assert verified.gates.coverage_adequate
    assert not verified.gates.quality_passed
    assert not verified.gates.fixed_sample_differences_adequate
    assert verified.status.value == "BLOCKED_EXTERNAL_VALIDATION"


@pytest.mark.parametrize(
    "attack",
    (
        "arbitrary_payloads",
        "shallow_quality",
        "sample_sha_drift",
        "missing_request",
        "request_missing_case",
        "difference_claim_without_report",
    ),
)
def test_forged_real_inputs_cannot_verify_or_persist(tmp_path: Path, attack: str) -> None:
    from quantking.data.validation.testing import build_deep_fake_run

    built = build_deep_fake_run(root=tmp_path, run_index=1, attack=attack)
    result = built.verifier.verify_run(directory=built.directory)
    assert result.evidence_kind.value == "SYNTHETIC"
    assert not result.gates.passed
    assert result.status.value == "BLOCKED_EXTERNAL_VALIDATION"


def test_manifest_self_declared_real_and_gates_are_rejected(tmp_path: Path) -> None:
    from quantking.data.adapters.warehouse.manifest import compute_sha256
    from quantking.data.validation.artifacts import (
        ValidationEvidenceVerificationError,
    )
    from quantking.data.validation.request import canonical_json
    from quantking.data.validation.testing import build_deep_fake_run

    built = build_deep_fake_run(root=tmp_path, run_index=1)
    path = built.directory / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest.pop("manifest_sha256")
    manifest["evidence_kind"] = "REAL"
    manifest["gates"] = {
        "coverage_adequate": True,
        "fixed_sample_differences_adequate": True,
        "quality_passed": True,
        "schema_unit_verified": True,
        "semantic_contract_verified": True,
    }
    digest = compute_sha256(canonical_json(manifest))
    path.write_bytes(canonical_json({**manifest, "manifest_sha256": digest}))
    (built.directory / "COMPLETE").write_bytes(canonical_json({"manifest_sha256": digest}))
    with pytest.raises(ValidationEvidenceVerificationError, match="fields"):
        built.verifier.verify_run(directory=built.directory)


@pytest.mark.parametrize(
    "drift",
    (
        "adapter_version",
        "package_version",
        "code_version",
        "schema_version",
        "rule_version",
        "threshold_version",
        "sample_spec_version",
        "config_version",
        "request",
    ),
)
def test_three_runs_with_any_frozen_credential_drift_remain_blocked(
    tmp_path: Path, drift: str
) -> None:
    from quantking.data.validation.testing import build_deep_fake_assessment

    verified = build_deep_fake_assessment(root=tmp_path, drift=drift)
    assert verified.assessment.status.value == "BLOCKED_EXTERNAL_VALIDATION"
    assert verified.assessment.stability_runs < 3
    assert "fingerprint" in (verified.assessment.blocking_reason or "")


@pytest.mark.parametrize("attack", ("same_invocation", "same_session", "same_timestamps"))
def test_different_run_ids_without_independent_invocations_do_not_count(
    tmp_path: Path, attack: str
) -> None:
    from quantking.data.validation.testing import build_deep_fake_assessment

    verified = build_deep_fake_assessment(root=tmp_path, independence_attack=attack)
    assert verified.assessment.status.value == "BLOCKED_EXTERNAL_VALIDATION"
    assert verified.assessment.stability_runs < 3


def test_acknowledgement_is_not_authorization_and_fails_before_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantking.data.validation.cli import (
        REAL_EXECUTION_ACK,
        ValidationExecutionError,
        execute,
    )

    imports: list[str] = []

    def forbidden_import(name: str) -> object:
        imports.append(name)
        raise AssertionError("runner import must not happen without authorization")

    monkeypatch.setattr("quantking.data.validation.cli.importlib.import_module", forbidden_import)
    with pytest.raises(ValidationExecutionError) as captured:
        execute(
            source="XTQUANT",
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            output_root=tmp_path,
            acknowledgement=REAL_EXECUTION_ACK,
            authorization=None,
            authorization_verifier=None,
        )
    assert captured.value.code == "AUTHORIZATION_REQUIRED"
    assert imports == []


def test_missing_runner_is_stable_sanitized_and_does_not_load_sdk(tmp_path: Path) -> None:
    from quantking.common.ids import new_entity_id
    from quantking.data.validation.authorization import (
        AuthorizationClaimStore,
        FakeAuthorizationAuthority,
    )
    from quantking.data.validation.cli import (
        REAL_EXECUTION_ACK,
        ValidationExecutionError,
        execute,
    )
    from quantking.data.validation.samples import load_packaged_fixed_sample

    authority = FakeAuthorizationAuthority()
    authorization = authority.issue(
        source="XTQUANT",
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        output_root=tmp_path,
        sample_sha256=load_packaged_fixed_sample().sample_spec_sha256,
    )
    before = set(sys.modules)
    with pytest.raises(ValidationExecutionError) as captured:
        execute(
            source="XTQUANT",
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            output_root=tmp_path,
            acknowledgement=REAL_EXECUTION_ACK,
            authorization=authorization,
            authorization_verifier=authority,
            invocation_id=new_entity_id("data_invocation"),
            authorization_claim_store=AuthorizationClaimStore(),
        )
    assert captured.value.code == "RUNNER_UNAVAILABLE"
    assert str(captured.value) == "provider runner is not installed; assessment remains blocked"
    assert "Traceback" not in str(captured.value)
    assert not ({"xtquant", "baostock", "akshare", "tushare"} & (set(sys.modules) - before))


def test_execute_cli_without_installed_authority_returns_sanitized_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from quantking.data.validation.cli import REAL_EXECUTION_ACK, main

    authorization_file = tmp_path / "authorization.json"
    authorization_file.write_text("{}", encoding="utf-8")
    result = main(
        [
            "execute",
            "--source",
            "XTQUANT",
            "--dataset",
            "exchange_calendar",
            "--output-root",
            str(tmp_path),
            "--ack",
            REAL_EXECUTION_ACK,
            "--authorization-file",
            str(authorization_file),
        ]
    )
    captured = capsys.readouterr()
    assert result == 2
    assert '"error_code": "AUTHORIZATION_REQUIRED"' in captured.err
    assert "Traceback" not in captured.err
