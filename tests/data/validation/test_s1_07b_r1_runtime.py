"""S1-07B-R1 pre-claim runtime gates and authorization sequencing attacks."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantking.common.ids import new_entity_id
from quantking.data.models import DatasetKind
from quantking.data.validation.authorization import (
    AuthorizationClaimStore,
    FakeAuthorizationAuthority,
)
from quantking.data.validation.runtime import (
    ControlledRunnerRegistry,
    RealRuntimeExecutionError,
    RealValidationRuntime,
    RuntimeAuthorizationEnvelope,
    RuntimeValidationPlan,
    ValidationSource,
)
from quantking.data.validation.samples import load_fixed_sample_v3, load_fixed_sample_v4

ACK = "I_ACKNOWLEDGE_AUTHORIZED_REAL_SOURCE_VALIDATION"
VERSION_FIELDS = (
    "adapter_version",
    "package_version",
    "code_version",
    "schema_version",
    "rule_version",
    "threshold_version",
    "sample_spec_version",
    "config_version",
)


class StaticProbe:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def exact_version(self, distribution_name: str) -> str | None:
        assert distribution_name == "xtquant"
        return self.value


class SequenceProbe:
    def __init__(self, values: list[str | None]) -> None:
        self.values = values

    def exact_version(self, distribution_name: str) -> str | None:
        assert distribution_name == "xtquant"
        return self.values.pop(0)


class AcceptRuntimeAuthorization:
    def verify(
        self,
        envelope: RuntimeAuthorizationEnvelope,
        *,
        plan: RuntimeValidationPlan,
    ) -> bool:
        return envelope.plan_sha256 == plan.plan_sha256


class SpyClaimStore:
    def __init__(self) -> None:
        self.calls = 0

    def claim(self, *_args: object, **_kwargs: object) -> None:
        self.calls += 1


def _eligible_sample(*, empty: bool = False) -> SimpleNamespace:
    sample = load_fixed_sample_v4()
    return SimpleNamespace(
        cases=() if empty else sample.cases,
        sample_spec_version=sample.sample_spec_version,
        sample_spec_sha256=sample.sample_spec_sha256,
        fixed_sample_gate_eligible=True,
    )


def _patch_cases_approved(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch the verifier so all selected cases pass the case-level gate.

    This is a test-only bypass for runtime mechanics tests (authorization
    consumption, version drift). It does NOT exist in the production runtime
    API; callers cannot forge case eligibility.
    """
    from quantking.data.validation.authority_bundle import (
        ReviewDecisionStatus,
        VerifiedAuthorityReferenceBundle,
    )
    from quantking.data.validation.samples import CaseEligibility

    sample = load_fixed_sample_v4()
    case_eligibilities = {case.case_id: CaseEligibility.AUTHORITY_APPROVED for case in sample.cases}
    fake = VerifiedAuthorityReferenceBundle(
        bundle_id="test-mechanics-bundle",
        manifest_sha256="a" * 64,
        sample_spec_version=sample.sample_spec_version,
        sample_spec_sha256=sample.sample_spec_sha256,
        previous_sample_spec_sha256=sample.previous_sample_spec_sha256 or "",
        reference_inventory_sha256="b" * 64,
        approved_case_ids=frozenset(case_eligibilities),
        review_decision_status=ReviewDecisionStatus.APPROVED,
        case_eligibilities=case_eligibilities,
    )
    monkeypatch.setattr(
        "quantking.data.validation.runtime.AuthorityReferenceBundleVerifier.verify",
        lambda self, *, directory, sample: fake,
    )
    return Path("fake-bundle")


def _runtime(
    *,
    package: str | None = "1.2.3",
    claim_store: object | None = None,
    authority: FakeAuthorizationAuthority | None = None,
    invocation: bool = True,
    reference_bundle_root: Path | None = None,
) -> RealValidationRuntime:
    return RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=StaticProbe(package),
        claim_store=claim_store,  # type: ignore[arg-type]
        authorization_verifier=authority,
        runtime_authorization_verifier=AcceptRuntimeAuthorization(),
        invocation_id=new_entity_id("data_invocation") if invocation else None,
        reference_bundle_root=reference_bundle_root,
    )


def _versions(plan: RuntimeValidationPlan) -> dict[str, str]:
    return {field: str(getattr(plan, field)) for field in VERSION_FIELDS}


def _envelope(
    plan: RuntimeValidationPlan,
    *,
    authority: FakeAuthorizationAuthority,
    output_root: Path,
    versions: dict[str, str] | None = None,
) -> RuntimeAuthorizationEnvelope:
    return RuntimeAuthorizationEnvelope(
        authorization=authority.issue(
            source=plan.source.value,
            dataset=plan.dataset,
            output_root=output_root,
            sample_sha256=plan.sample_spec_sha256,
        ),
        plan_sha256=plan.plan_sha256,
        frozen_versions=versions or _versions(plan),
        signature="1" * 64,
    )


def _execute(
    runtime: RealValidationRuntime,
    *,
    output_root: Path,
    authorization: RuntimeAuthorizationEnvelope | None,
    case_ids: tuple[str, ...] | None = None,
    reference_bundle_directory: Path | None = None,
    dataset: DatasetKind = DatasetKind.EXCHANGE_CALENDAR,
) -> None:
    runtime.execute(
        source=ValidationSource.XTQUANT,
        dataset=dataset,
        output_root=output_root,
        acknowledgement=ACK,
        authorization=authorization,
        case_ids=case_ids,
        reference_bundle_directory=reference_bundle_directory,
    )


@pytest.mark.parametrize("value", [None, "", "unavailable", "unknown", "latest"])
def test_unresolved_package_version_fails_before_claim_or_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    monkeypatch.setattr("quantking.data.validation.runtime.load_fixed_sample_v4", _eligible_sample)
    imports: list[str] = []
    monkeypatch.setattr(
        "quantking.data.validation.runtime._import_runner",
        lambda name: imports.append(name),
    )
    claims = SpyClaimStore()
    runtime = _runtime(package=value, claim_store=claims)

    with pytest.raises(RealRuntimeExecutionError) as captured:
        _execute(runtime, output_root=tmp_path, authorization=None)

    assert captured.value.code == "PROVIDER_PACKAGE_VERSION_UNRESOLVED"
    assert claims.calls == 0
    assert imports == []


@pytest.mark.parametrize(
    "value",
    [">=1.0", "1.*", "1.0,<2", "~=1.2", "1.2.3rc1", "1.2.3+local", "word"],
)
def test_non_exact_or_unstable_package_version_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setattr("quantking.data.validation.runtime.load_fixed_sample_v4", _eligible_sample)
    claims = SpyClaimStore()
    runtime = _runtime(package=value, claim_store=claims)

    with pytest.raises(RealRuntimeExecutionError) as captured:
        _execute(runtime, output_root=tmp_path, authorization=None)

    assert captured.value.code == "PROVIDER_PACKAGE_VERSION_INVALID"
    assert claims.calls == 0


def test_package_version_drift_between_plan_and_execute_does_not_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_dir = _patch_cases_approved(monkeypatch)
    authority = FakeAuthorizationAuthority()
    claims = SpyClaimStore()
    runtime = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=SequenceProbe(["1.2.3", "1.2.4"]),
        claim_store=claims,  # type: ignore[arg-type]
        authorization_verifier=authority,
        runtime_authorization_verifier=AcceptRuntimeAuthorization(),
        invocation_id=new_entity_id("data_invocation"),
        reference_bundle_root=tmp_path,
    )
    original = runtime.plan(
        source=ValidationSource.XTQUANT,
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        output_root=tmp_path,
        case_ids=("calendar_sh",),
        reference_bundle_directory=bundle_dir,
    )
    envelope = _envelope(original, authority=authority, output_root=tmp_path)

    with pytest.raises(RealRuntimeExecutionError) as captured:
        _execute(
            runtime,
            output_root=tmp_path,
            authorization=envelope,
            case_ids=("calendar_sh",),
            reference_bundle_directory=bundle_dir,
        )

    assert captured.value.code == "RUNTIME_VERSION_MISMATCH"
    assert claims.calls == 0


@pytest.mark.parametrize(
    "field",
    [
        "adapter_version",
        "code_version",
        "schema_version",
        "rule_version",
        "threshold_version",
        "sample_spec_version",
        "config_version",
    ],
)
def test_each_non_package_runtime_version_drift_fails_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    bundle_dir = _patch_cases_approved(monkeypatch)
    authority = FakeAuthorizationAuthority()
    claims = SpyClaimStore()
    runtime = _runtime(
        package="1.2.3",
        claim_store=claims,
        authority=authority,
        reference_bundle_root=tmp_path,
    )
    plan = runtime.plan(
        source=ValidationSource.XTQUANT,
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        output_root=tmp_path,
        case_ids=("calendar_sh",),
        reference_bundle_directory=bundle_dir,
    )
    versions = _versions(plan)
    versions[field] = f"{versions[field]}.drift"

    with pytest.raises(RealRuntimeExecutionError) as captured:
        _execute(
            runtime,
            output_root=tmp_path,
            authorization=_envelope(
                plan, authority=authority, output_root=tmp_path, versions=versions
            ),
            case_ids=("calendar_sh",),
            reference_bundle_directory=bundle_dir,
        )

    assert captured.value.code == "RUNTIME_VERSION_MISMATCH"
    assert claims.calls == 0


def test_empty_source_dataset_scope_fails_before_claim_or_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "quantking.data.validation.runtime.load_fixed_sample_v4",
        lambda: _eligible_sample(empty=True),
    )
    imports: list[str] = []
    monkeypatch.setattr(
        "quantking.data.validation.runtime._import_runner",
        lambda name: imports.append(name),
    )
    claims = SpyClaimStore()
    runtime = _runtime(package="1.2.3", claim_store=claims)

    with pytest.raises(RealRuntimeExecutionError) as captured:
        _execute(runtime, output_root=tmp_path, authorization=None)

    assert captured.value.code == "SAMPLE_SCOPE_EMPTY"
    assert claims.calls == 0
    assert imports == []


def test_reparse_output_root_fails_before_claim_or_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("quantking.data.validation.runtime.load_fixed_sample_v4", _eligible_sample)
    unsafe = tmp_path / "linked"
    unsafe.mkdir()
    monkeypatch.setattr(
        "quantking.data.validation.runtime.is_reparse_point",
        lambda path: path == unsafe,
    )
    imports: list[str] = []
    monkeypatch.setattr(
        "quantking.data.validation.runtime._import_runner",
        lambda name: imports.append(name),
    )
    claims = SpyClaimStore()
    runtime = _runtime(package="1.2.3", claim_store=claims)

    with pytest.raises(RealRuntimeExecutionError) as captured:
        _execute(runtime, output_root=unsafe, authorization=None)

    assert captured.value.code == "OUTPUT_ROOT_UNSAFE"
    assert claims.calls == 0
    assert imports == []


def test_lexical_output_root_escape_fails_before_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("quantking.data.validation.runtime.load_fixed_sample_v4", _eligible_sample)
    claims = SpyClaimStore()
    runtime = _runtime(package="1.2.3", claim_store=claims)
    unsafe = tmp_path / "managed" / ".." / "escaped"

    with pytest.raises(RealRuntimeExecutionError) as captured:
        _execute(runtime, output_root=unsafe, authorization=None)

    assert captured.value.code == "OUTPUT_ROOT_UNSAFE"
    assert claims.calls == 0


def test_relative_output_root_returns_stable_error_before_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("quantking.data.validation.runtime.load_fixed_sample_v4", _eligible_sample)
    claims = SpyClaimStore()
    runtime = _runtime(package="1.2.3", claim_store=claims)

    with pytest.raises(RealRuntimeExecutionError) as captured:
        _execute(runtime, output_root=Path("relative-output"), authorization=None)

    assert captured.value.code == "OUTPUT_ROOT_UNSAFE"
    assert claims.calls == 0


def test_preflight_passes_then_missing_signer_consumes_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_dir = _patch_cases_approved(monkeypatch)
    imports: list[str] = []
    monkeypatch.setattr(
        "quantking.data.validation.runtime._import_runner",
        lambda name: imports.append(name),
    )
    authority = FakeAuthorizationAuthority()
    claims = SpyClaimStore()
    runtime = _runtime(
        package="1.2.3",
        claim_store=claims,
        authority=authority,
        reference_bundle_root=tmp_path,
    )
    plan = runtime.plan(
        source=ValidationSource.XTQUANT,
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        output_root=tmp_path,
        case_ids=("calendar_sh",),
        reference_bundle_directory=bundle_dir,
    )

    with pytest.raises(RealRuntimeExecutionError) as captured:
        _execute(
            runtime,
            output_root=tmp_path,
            authorization=_envelope(plan, authority=authority, output_root=tmp_path),
            case_ids=("calendar_sh",),
            reference_bundle_directory=bundle_dir,
        )

    assert captured.value.code == "REAL_SIGNER_UNAVAILABLE"
    assert claims.calls == 1
    assert imports == []


def test_concurrent_execute_consumes_one_authorization_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_dir = _patch_cases_approved(monkeypatch)
    authority = FakeAuthorizationAuthority()
    first = _runtime(
        package="1.2.3",
        claim_store=AuthorizationClaimStore(claim_root=tmp_path / "claims"),
        authority=authority,
        reference_bundle_root=tmp_path,
    )
    plan = first.plan(
        source=ValidationSource.XTQUANT,
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        output_root=tmp_path / "output",
        case_ids=("calendar_sh",),
        reference_bundle_directory=bundle_dir,
    )
    envelope = _envelope(plan, authority=authority, output_root=tmp_path / "output")
    second = _runtime(
        package="1.2.3",
        claim_store=AuthorizationClaimStore(claim_root=tmp_path / "claims"),
        authority=authority,
        reference_bundle_root=tmp_path,
    )

    def invoke(runtime: RealValidationRuntime) -> str:
        try:
            _execute(
                runtime,
                output_root=tmp_path / "output",
                authorization=envelope,
                case_ids=("calendar_sh",),
                reference_bundle_directory=bundle_dir,
            )
        except RealRuntimeExecutionError as error:
            return error.code
        raise AssertionError("offline runtime must remain blocked")

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(invoke, (first, second)))

    assert codes == ["AUTHORIZATION_ALREADY_CONSUMED", "REAL_SIGNER_UNAVAILABLE"]
    assert len(tuple((tmp_path / "claims").glob("*.json"))) == 1


def test_production_v3_remains_framework_only() -> None:
    sample = load_fixed_sample_v3()
    assert sample.fixed_sample_gate_eligible is False
    assert sample.eligibility.value == "FRAMEWORK_ONLY"
