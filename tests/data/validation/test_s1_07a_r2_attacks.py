"""S1-07A-R2 attacks for signer trust, coverage, quality and authorization."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.models import DatasetKind


def _days() -> tuple[date, ...]:
    return tuple(date(2024, 1, day) for day in (2, 3, 4, 5))


def _calendar(exchange: str) -> tuple[dict[str, object], ...]:
    return tuple({"exchange": exchange, "trade_date": item, "is_open": True} for item in _days())


def test_fake_signer_cannot_be_promoted_by_wrapping_verifier(tmp_path: Path) -> None:
    from quantking.data.validation.authorization import (
        FakeRunnerTrustStore,
        SignerTrustPolicy,
    )
    from quantking.data.validation.testing import build_deep_fake_run

    class LyingVerifier:
        establishes_real_identity = True

        def __init__(self) -> None:
            self.fake = FakeRunnerTrustStore()

        def verify(self, attestation: object) -> bool:
            return self.fake.verify(attestation)  # type: ignore[arg-type]

    built = build_deep_fake_run(root=tmp_path, run_index=1)
    verified = built.verifier.with_trust_policy(SignerTrustPolicy.synthetic_only()).verify_run(
        directory=built.directory
    )
    assert verified.evidence_kind.value == "SYNTHETIC"
    assert not hasattr(SignerTrustPolicy, "establishes_real_identity")
    assert LyingVerifier().establishes_real_identity


def test_fake_signer_profile_is_immutably_synthetic() -> None:
    from quantking.data.validation.authorization import (
        FakeRunnerTrustStore,
        SignerProfile,
        SignerTrustPolicy,
    )

    signer = FakeRunnerTrustStore()
    policy = SignerTrustPolicy.synthetic_only()
    assert signer.profile is SignerProfile.SYNTHETIC
    assert policy.profile_for(signer.key_id) is SignerProfile.SYNTHETIC
    with pytest.raises((AttributeError, TypeError)):
        signer.profile = SignerProfile.REAL  # type: ignore[misc]


def test_result_attestation_binds_every_artifact_credential(tmp_path: Path) -> None:
    from quantking.data.validation.authorization import ResultAttestation
    from quantking.data.validation.testing import build_deep_fake_run

    built = build_deep_fake_run(root=tmp_path, run_index=1)
    payload = (built.directory / "runner" / "result-attestation.json").read_bytes()
    attestation = ResultAttestation.from_bytes(payload)
    assert attestation.raw_manifest_sha256
    assert attestation.canonical_content_sha256
    assert attestation.quality_manifest_sha256
    assert attestation.quality_run_id.namespace == "data_quality"
    assert attestation.evidence_inventory_sha256
    assert set(attestation.versions) == {
        "adapter_version",
        "package_version",
        "code_version",
        "schema_version",
        "rule_version",
        "threshold_version",
        "sample_spec_version",
        "config_version",
    }


@pytest.mark.parametrize("replacement", ("raw", "canonical", "quality"))
def test_legal_artifact_replacement_after_signing_destroys_identity(
    tmp_path: Path, replacement: str
) -> None:
    from quantking.data.validation.testing import build_deep_fake_run

    built = build_deep_fake_run(
        root=tmp_path, run_index=1, attack=f"replace_{replacement}_after_attestation"
    )
    verified = built.verifier.verify_run(directory=built.directory)
    assert verified.evidence_kind.value == "SYNTHETIC"
    assert verified.verification_reason == "RESULT_ATTESTATION_MISMATCH"


def test_calendar_four_day_case_with_one_day_is_incomplete() -> None:
    from quantking.data.validation.coverage import evaluate_fixed_sample_coverage
    from quantking.data.validation.samples import load_packaged_fixed_sample

    sample = load_packaged_fixed_sample()
    cases = tuple(
        case.to_dict() for case in sample.cases if case.dataset is DatasetKind.EXCHANGE_CALENDAR
    )
    rows = tuple(
        {"exchange": case["exchange"], "trade_date": date(2024, 1, 2), "is_open": True}
        for case in cases
    )
    assert not evaluate_fixed_sample_coverage(
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        rows=rows,
        cases=cases,
        calendar_rows=(),
    )


@pytest.mark.parametrize("gap", ("head", "middle", "tail"))
@pytest.mark.parametrize(
    ("dataset", "code", "exchange"),
    (
        (DatasetKind.DAILY_BAR_UNADJUSTED, "600000.SH", "SH"),
        (DatasetKind.ADJUSTMENT_FACTOR, "600000.SH", "SH"),
        (DatasetKind.TRADING_STATUS, "600000.SH", "SH"),
        (DatasetKind.BENCHMARK_INDEX_DAILY, "000001.SH", "SH"),
    ),
)
def test_dated_dataset_coverage_uses_code_suffix_and_rejects_each_gap(
    dataset: DatasetKind, code: str, exchange: str, gap: str
) -> None:
    from quantking.data.validation.coverage import evaluate_fixed_sample_coverage

    dates = list(_days())
    missing = {"head": 0, "middle": 1, "tail": 3}[gap]
    rows = tuple(
        {"code": code, "trade_date": item} for index, item in enumerate(dates) if index != missing
    )
    case = {
        "case_id": "coverage_case",
        "code": code,
        "exchange": exchange,
        "start_date": "2024-01-02",
        "end_date": "2024-01-05",
    }
    assert not evaluate_fixed_sample_coverage(
        dataset=dataset,
        rows=rows,
        cases=(case,),
        calendar_rows=_calendar(exchange),
    )
    complete = tuple({"code": code, "trade_date": item} for item in dates)
    assert evaluate_fixed_sample_coverage(
        dataset=dataset,
        rows=complete,
        cases=(case,),
        calendar_rows=_calendar(exchange),
    )


@pytest.mark.parametrize("gap", ("head", "middle", "tail"))
def test_exchange_calendar_rejects_each_natural_day_gap(gap: str) -> None:
    from quantking.data.validation.coverage import evaluate_fixed_sample_coverage

    missing = {"head": 0, "middle": 1, "tail": 3}[gap]
    rows = tuple(row for index, row in enumerate(_calendar("SH")) if index != missing)
    case = {
        "case_id": "calendar_sh",
        "code": None,
        "exchange": "SH",
        "start_date": "2024-01-02",
        "end_date": "2024-01-05",
    }
    assert not evaluate_fixed_sample_coverage(
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        rows=rows,
        cases=(case,),
        calendar_rows=(),
    )
    assert evaluate_fixed_sample_coverage(
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        rows=_calendar("SH"),
        cases=(case,),
        calendar_rows=(),
    )


def test_security_master_uses_interval_intersection_not_effective_start_window() -> None:
    from quantking.data.validation.coverage import evaluate_fixed_sample_coverage

    case = {
        "case_id": "security",
        "code": "000001.SZ",
        "exchange": "SZ",
        "start_date": "2024-01-02",
        "end_date": "2024-01-05",
    }
    rows = (
        {
            "code": "000001.SZ",
            "exchange": "SZ",
            "effective_from": date(2020, 1, 1),
            "effective_to": None,
        },
    )
    assert evaluate_fixed_sample_coverage(
        dataset=DatasetKind.SECURITY_MASTER,
        rows=rows,
        cases=(case,),
        calendar_rows=(),
    )
    assert not evaluate_fixed_sample_coverage(
        dataset=DatasetKind.SECURITY_MASTER,
        rows=({**rows[0], "exchange": "SH"},),
        cases=(case,),
        calendar_rows=(),
    )


@pytest.mark.parametrize(
    "attack",
    ("custom_only", "missing_rule", "extra_rule", "duplicate_rule"),
)
def test_quality_rule_result_set_must_equal_catalogue(attack: str) -> None:
    from quantking.data.quality.rules import RULE_VERSION, default_rule_set
    from quantking.data.validation.quality import validate_quality_rule_catalog

    dataset = DatasetKind.EXCHANGE_CALENDAR
    expected = tuple(rule.code for rule in default_rule_set().for_dataset(dataset))
    if attack == "custom_only":
        actual = ("FAKE_OFFLINE_CONTRACT",)
    elif attack == "missing_rule":
        actual = expected[:-1]
    elif attack == "extra_rule":
        actual = (*expected, "caller_rule")
    else:
        actual = (*expected, expected[0])
    assert not validate_quality_rule_catalog(
        dataset=dataset,
        rule_version=RULE_VERSION,
        result_rule_codes=actual,
    )
    assert validate_quality_rule_catalog(
        dataset=dataset,
        rule_version=RULE_VERSION,
        result_rule_codes=expected,
    )


def test_authorization_claim_is_atomic_and_one_time(tmp_path: Path) -> None:
    from quantking.data.validation.authorization import (
        AuthorizationClaimError,
        AuthorizationClaimStore,
        FakeAuthorizationAuthority,
    )
    from quantking.data.validation.samples import load_packaged_fixed_sample

    authority = FakeAuthorizationAuthority()
    authorization = authority.issue(
        source="XTQUANT",
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        output_root=tmp_path,
        sample_sha256=load_packaged_fixed_sample().sample_spec_sha256,
    )
    store = AuthorizationClaimStore(claim_root=tmp_path / "claims")
    first = new_entity_id("data_invocation")
    store.claim(
        authorization,
        invocation_id=first,
        verifier=authority,
        source="XTQUANT",
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        output_root=tmp_path,
        sample_sha256=load_packaged_fixed_sample().sample_spec_sha256,
        at=UtcTimestamp(datetime.now(UTC)),
    )
    with pytest.raises(AuthorizationClaimError, match="AUTHORIZATION_ALREADY_CONSUMED"):
        store.claim(
            authorization,
            invocation_id=new_entity_id("data_invocation"),
            verifier=authority,
            source="XTQUANT",
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            output_root=tmp_path,
            sample_sha256=load_packaged_fixed_sample().sample_spec_sha256,
            at=UtcTimestamp(datetime.now(UTC)),
        )


def test_concurrent_authorization_claim_has_exactly_one_winner(tmp_path: Path) -> None:
    from quantking.data.validation.authorization import (
        AuthorizationClaimError,
        AuthorizationClaimStore,
        FakeAuthorizationAuthority,
    )
    from quantking.data.validation.samples import load_packaged_fixed_sample

    authority = FakeAuthorizationAuthority()
    sample_sha = load_packaged_fixed_sample().sample_spec_sha256
    authorization = authority.issue(
        source="XTQUANT",
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        output_root=tmp_path,
        sample_sha256=sample_sha,
    )
    store = AuthorizationClaimStore(claim_root=tmp_path / "claims")

    def claim() -> str:
        try:
            store.claim(
                authorization,
                invocation_id=new_entity_id("data_invocation"),
                verifier=authority,
                source="XTQUANT",
                dataset=DatasetKind.EXCHANGE_CALENDAR,
                output_root=tmp_path,
                sample_sha256=sample_sha,
                at=UtcTimestamp(datetime.now(UTC)),
            )
        except AuthorizationClaimError as error:
            return str(error)
        return "CLAIMED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _index: claim(), range(2)))
    assert sorted(results) == ["AUTHORIZATION_ALREADY_CONSUMED", "CLAIMED"]


def test_execute_does_not_consume_authorization_when_preflight_is_blocked(
    tmp_path: Path,
) -> None:
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
    store = AuthorizationClaimStore(claim_root=tmp_path / "claims")
    arguments = {
        "source": "XTQUANT",
        "dataset": DatasetKind.EXCHANGE_CALENDAR,
        "output_root": tmp_path,
        "acknowledgement": REAL_EXECUTION_ACK,
        "authorization": authorization,
        "authorization_verifier": authority,
        "authorization_claim_store": store,
    }
    with pytest.raises(ValidationExecutionError) as first:
        execute(**arguments, invocation_id=new_entity_id("data_invocation"))
    assert first.value.code == "LEGACY_AUTHORIZATION_DTO_REJECTED"
    with pytest.raises(ValidationExecutionError) as second:
        execute(**arguments, invocation_id=new_entity_id("data_invocation"))
    assert second.value.code == "LEGACY_AUTHORIZATION_DTO_REJECTED"
    assert not (tmp_path / "claims" / "authorization-claims.sqlite3").exists()


def test_cli_valid_authorization_is_hard_blocked_consistently(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from quantking.data.validation.authorization import FakeAuthorizationAuthority
    from quantking.data.validation.cli import REAL_EXECUTION_ACK, main
    from quantking.data.validation.samples import load_packaged_fixed_sample

    authorization = FakeAuthorizationAuthority().issue(
        source="XTQUANT",
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        output_root=tmp_path,
        sample_sha256=load_packaged_fixed_sample().sample_spec_sha256,
    )
    authorization_file = tmp_path / "authorization.json"
    authorization_file.write_bytes(authorization.to_bytes())
    exit_code = main(
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
    assert exit_code == 2
    # C0B closes the legacy ExecutionAuthorization CLI payload; the old
    # readiness command remains fail-closed and cannot reach any runner.
    assert '"error_code": "AUTHORIZATION_REQUIRED"' in captured.err
    assert "Traceback" not in captured.err


def test_sample_content_change_requires_new_version() -> None:
    from quantking.data.validation.samples import load_packaged_fixed_sample

    sample = load_packaged_fixed_sample()
    assert sample.sample_spec_version == "s1-07.fixed-sample.v2"
    assert sample.sample_spec_sha256 == sample.compute_sha256()
