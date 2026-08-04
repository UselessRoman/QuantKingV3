# ruff: noqa: RUF001
"""S1-07B-FINAL: authoritative sample v4, reference bundle v3, per-case runtime gating.

Covers immutable version chain, time semantics, case-level eligibility,
reference bundle integrity, runtime per-case gating, security attacks,
and Stage 1 status regression.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from quantking.common.time import TradingDate
from quantking.data.models import DatasetKind
from quantking.data.validation.samples import (
    FIXED_SAMPLE_V3_SHA256,
    SampleEligibility,
    SamplePurpose,
)

# ---------------------------------------------------------------------------
# v4 immutable version chain
# ---------------------------------------------------------------------------


def test_v1_v2_v3_bytes_and_shas_are_unchanged() -> None:
    from importlib.resources import files

    from quantking.data.validation.samples import (
        FIXED_SAMPLE_V1_BYTES_SHA256,
        FIXED_SAMPLE_V1_SHA256,
        FIXED_SAMPLE_V2_RELEASE_BYTES_SHA256,
        FIXED_SAMPLE_V2_SHA256,
        FIXED_SAMPLE_V3_RELEASE_BYTES_SHA256,
        load_fixed_sample_v1,
        load_fixed_sample_v2,
        load_fixed_sample_v3,
    )

    v1 = load_fixed_sample_v1()
    v2 = load_fixed_sample_v2()
    v3 = load_fixed_sample_v3()
    assert v1.sample_spec_sha256 == FIXED_SAMPLE_V1_SHA256
    assert v2.sample_spec_sha256 == FIXED_SAMPLE_V2_SHA256
    assert v3.sample_spec_sha256 == FIXED_SAMPLE_V3_SHA256
    v1_bytes = files("quantking.data.validation").joinpath("fixed_sample.v1.json").read_bytes()
    v2_bytes = (
        files("quantking.data.validation").joinpath("fixed_sample.release.v2.json").read_bytes()
    )
    v3_bytes = (
        files("quantking.data.validation").joinpath("fixed_sample.release.v3.json").read_bytes()
    )
    from quantking.data.adapters.warehouse.manifest import compute_sha256

    assert compute_sha256(v1_bytes) == FIXED_SAMPLE_V1_BYTES_SHA256
    assert compute_sha256(v2_bytes) == FIXED_SAMPLE_V2_RELEASE_BYTES_SHA256
    assert compute_sha256(v3_bytes) == FIXED_SAMPLE_V3_RELEASE_BYTES_SHA256


def test_v4_binds_v3_and_has_twelve_cases() -> None:
    from quantking.data.validation.samples import (
        FIXED_SAMPLE_V4_SHA256,
        load_fixed_sample_v4,
    )

    v4 = load_fixed_sample_v4()
    assert v4.sample_spec_version == "s1-07.fixed-sample.v4"
    assert v4.sample_spec_sha256 == FIXED_SAMPLE_V4_SHA256
    assert v4.previous_sample_spec_sha256 == FIXED_SAMPLE_V3_SHA256
    assert v4.eligibility is SampleEligibility.VALIDATION_READY_WITH_BLOCKED_CASES
    assert len(v4.cases) == 12
    case_ids = {case.case_id for case in v4.cases}
    assert "st_warning_603377_sh" in case_ids
    # 11 inherited framework cases preserved
    inherited = case_ids - {"st_warning_603377_sh"}
    assert len(inherited) == 11


def test_v4_st_case_carries_authority_reference() -> None:
    from quantking.data.validation.samples import load_fixed_sample_v4

    v4 = load_fixed_sample_v4()
    st_case = next(c for c in v4.cases if c.case_id == "st_warning_603377_sh")
    assert st_case.dataset is DatasetKind.TRADING_STATUS
    assert st_case.code == "603377.SH"
    assert st_case.exchange == "SH"
    assert st_case.purpose is SamplePurpose.ST
    assert str(st_case.start_date) == "2026-03-23"
    assert str(st_case.end_date) == "2026-07-31"
    assert "is_st" in st_case.fields
    assert st_case.reference.locator.startswith("https://www.cninfo.com.cn/")


def test_v4_missing_purposes_exclude_st_and_required_keeps_all_seventeen() -> None:
    from quantking.data.validation.samples import load_fixed_sample_v4

    v4 = load_fixed_sample_v4()
    all_purposes = {p for p in SamplePurpose}
    assert set(v4.required_authoritative_purposes) == all_purposes
    missing = set(v4.missing_authoritative_purposes)
    assert SamplePurpose.ST.value not in missing
    assert len(missing) == 16
    assert missing.issubset({p.value for p in all_purposes})


def test_v4_release_bytes_match_controlled_sha() -> None:
    from importlib.resources import files

    from quantking.data.adapters.warehouse.manifest import compute_sha256
    from quantking.data.validation.samples import FIXED_SAMPLE_V4_RELEASE_BYTES_SHA256

    raw = files("quantking.data.validation").joinpath("fixed_sample.release.v4.json").read_bytes()
    assert compute_sha256(raw) == FIXED_SAMPLE_V4_RELEASE_BYTES_SHA256


def test_v4_rejects_tampered_release_content() -> None:
    from quantking.data.validation.samples import (
        fixed_sample_v4_from_release,
        packaged_v4_release_payload,
    )

    tampered = packaged_v4_release_payload()
    tampered["eligibility"] = "PRODUCTION_ELIGIBLE"
    with pytest.raises(ValueError, match="controlled v4 release"):
        fixed_sample_v4_from_release(tampered)

    broken = packaged_v4_release_payload()
    broken["previous_sample_spec_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="controlled v4 release"):
        fixed_sample_v4_from_release(broken)


def test_v4_idempotent_loading_produces_same_sha() -> None:
    from quantking.data.validation.samples import (
        load_fixed_sample_v4,
    )

    first = load_fixed_sample_v4()
    second = load_fixed_sample_v4()
    assert first.sample_spec_sha256 == second.sample_spec_sha256
    assert first.to_canonical_json() == second.to_canonical_json()


# ---------------------------------------------------------------------------
# v3 authority reference bundle helpers
# ---------------------------------------------------------------------------

_PDF_SHA = "f3deec88dbd796c44535ecf72965c7f2103aca2ee663125b6499a427545c98e0"
_P2B_MANIFEST_SHA = "0ae4809084bac61526d99d0576081cee5741b6b248e23015a2d9592e1a587df7"
_P2F_MANIFEST_SHA = "15fc9f0b122b68dfdb11d9fa8e15870d9b65d7286e77f9a971fe2912fe8c4ed9"
_CNINFO_LOCATOR = (
    "https://www.cninfo.com.cn/new/disclosure/detail?stockCode=603377"
    "&orgId=9900023616&announcementId=1225449700&announcementTime=2026-07-31"
)


def _build_st_authority_draft():
    """Build the real ST authority bundle draft from the sealed CNINFO evidence."""
    from quantking.data.validation.authority_bundle import (
        AuthorityCaseFactBinding,
        AuthorityReferenceBundleDraft,
        AuthorityReferenceDocument,
        AuthorityReferenceFact,
        FactNature,
        ReviewDecision,
        ReviewDecisionStatus,
        ReviewerRole,
    )
    from quantking.data.validation.samples import load_fixed_sample_v4

    sample = load_fixed_sample_v4()
    document = AuthorityReferenceDocument(
        reference_id="cninfo-603377-1225449700",
        authority="CNINFO primary disclosure",
        canonical_locator=_CNINFO_LOCATOR,
        announcement_id="1225449700",
        announcement_number="临2026-110",
        security_code="603377.SH",
        security_name="ST 东时",
        company_name="东方时尚驾驶学校股份有限公司",
        document_date=TradingDate.from_iso("2026-07-30"),
        publication_date=TradingDate.from_iso("2026-07-31"),
        known_from_date=TradingDate.from_iso("2026-07-31"),
        material_sha256=_PDF_SHA,
        p2b_manifest_sha256=_P2B_MANIFEST_SHA,
        p2f_manifest_sha256=_P2F_MANIFEST_SHA,
    )
    identity_fact = AuthorityReferenceFact(
        fact_id="st-document-identity-001",
        reference_id=document.reference_id,
        dataset=DatasetKind.TRADING_STATUS,
        purpose=SamplePurpose.ST,
        code="603377.SH",
        exchange="SH",
        fact_nature=FactNature.IDENTITY,
        document_date=TradingDate.from_iso("2026-07-30"),
        publication_date=TradingDate.from_iso("2026-07-31"),
        known_from_date=TradingDate.from_iso("2026-07-31"),
        usable_for_decisions_after="2026-07-31 market close",
        material_sha256=_PDF_SHA,
        paragraph_sha256="2e81c9678e4d86b28e683e2f2dddf7474de51c8f2def68b7afe308644ece0d39",
        pages=(1,),
        statement="证券代码：603377 证券简称：ST 东时 公告编号：临 2026-110",
    )
    retro_0323 = AuthorityReferenceFact(
        fact_id="st-status-20260323",
        reference_id=document.reference_id,
        dataset=DatasetKind.TRADING_STATUS,
        purpose=SamplePurpose.ST,
        code="603377.SH",
        exchange="SH",
        fact_nature=FactNature.RETROSPECTIVE_CONFIRMATION,
        document_date=TradingDate.from_iso("2026-07-30"),
        publication_date=TradingDate.from_iso("2026-07-31"),
        known_from_date=TradingDate.from_iso("2026-07-31"),
        usable_for_decisions_after="2026-07-31 market close",
        material_sha256=_PDF_SHA,
        paragraph_sha256="2f1a2b21bd0b7dc1b419fa8964fefe2cb6d375537f5e8780a4bb760617bed3a8",
        pages=(2,),
        statement=(
            "根据《上海证券交易所股票上市规则》第 9.8.1 条第一款第（七）项规定，"
            "公司股票自 2026 年 3 月 23 日起被叠加实施其他风险警示。"
        ),
        effective_date=TradingDate.from_iso("2026-03-23"),
        other_risk_warning=True,
    )
    retro_0418 = AuthorityReferenceFact(
        fact_id="st-status-20260418",
        reference_id=document.reference_id,
        dataset=DatasetKind.TRADING_STATUS,
        purpose=SamplePurpose.ST,
        code="603377.SH",
        exchange="SH",
        fact_nature=FactNature.RETROSPECTIVE_CONFIRMATION,
        document_date=TradingDate.from_iso("2026-07-30"),
        publication_date=TradingDate.from_iso("2026-07-31"),
        known_from_date=TradingDate.from_iso("2026-07-31"),
        usable_for_decisions_after="2026-07-31 market close",
        material_sha256=_PDF_SHA,
        paragraph_sha256="c69d8f9ebbef147c1524c4e8822534cd7a39fd37c3d44985ac3cc2fb7bb53268",
        pages=(1, 2),
        statement=(
            "根据《上海证券交易所股票上市规则》第 9.8.1 条第一款第（六）项规定，"
            "公司股票自 2026 年 4 月 18 日起被叠加实施其他风险警示。"
        ),
        effective_date=TradingDate.from_iso("2026-04-18"),
        other_risk_warning=True,
    )
    continuation = AuthorityReferenceFact(
        fact_id="st-continues-20260730",
        reference_id=document.reference_id,
        dataset=DatasetKind.TRADING_STATUS,
        purpose=SamplePurpose.ST,
        code="603377.SH",
        exchange="SH",
        fact_nature=FactNature.CONTINUATION,
        document_date=TradingDate.from_iso("2026-07-30"),
        publication_date=TradingDate.from_iso("2026-07-31"),
        known_from_date=TradingDate.from_iso("2026-07-31"),
        usable_for_decisions_after="2026-07-31 market close",
        material_sha256=_PDF_SHA,
        paragraph_sha256="27786c8b65e8124e6685a53881daf438c41f4c447b8bd9f6222ab9c92818457f",
        pages=(2,),
        statement=(
            "公司前期被实施其他风险警示的情形尚未全部消除，公司股票继续被实施其他风险警示。"
        ),
        other_risk_warning_continues=True,
    )
    facts = (identity_fact, retro_0323, retro_0418, continuation)
    bindings = tuple(
        AuthorityCaseFactBinding(
            case_id="st_warning_603377_sh",
            fact_id=fact.fact_id,
            purpose=SamplePurpose.ST,
        )
        for fact in facts
    )
    approval_statement = (
        "LOCAL_OWNER approves the ST authority reference bundle for case "
        "st_warning_603377_sh binding CNINFO announcement 1225449700 "
        "(临2026-110) to the v4 fixed sample."
    )
    from quantking.data.adapters.warehouse.manifest import compute_sha256

    def _canonical(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )

    decision = ReviewDecision(
        decision_id="local-owner-approval-st-603377-v1",
        reviewer_role=ReviewerRole.LOCAL_OWNER,
        status=ReviewDecisionStatus.APPROVED,
        case_ids=("st_warning_603377_sh",),
        approval_statement=approval_statement,
        approval_statement_sha256=compute_sha256(
            _canonical({"approval_statement": approval_statement})
        ),
        decided_at=TradingDate.from_iso("2026-07-31"),
    )
    return AuthorityReferenceBundleDraft(
        bundle_id="st-603377-authority-v1",
        sample_spec_version=sample.sample_spec_version,
        sample_spec_sha256=sample.sample_spec_sha256,
        previous_sample_spec_sha256=sample.previous_sample_spec_sha256,
        references=(document,),
        facts=facts,
        bindings=bindings,
        review_decisions=(decision,),
        code_version="s1-07b.authority-bundle.v3",
        schema_version="authority-bundle.v3",
    )


def _write_st_bundle(root: Path) -> Path:
    from quantking.data.validation.authority_bundle import (
        AuthorityReferenceBundleWriter,
    )
    from quantking.data.validation.samples import load_fixed_sample_v4

    return AuthorityReferenceBundleWriter(root=root).write(
        _build_st_authority_draft(),
        sample=load_fixed_sample_v4(),
    )


# ---------------------------------------------------------------------------
# v3 authority reference bundle writer and verifier
# ---------------------------------------------------------------------------


def test_v3_authority_bundle_writer_produces_canonical_files(tmp_path: Path) -> None:
    from quantking.data.validation.authority_bundle import (
        AuthorityReferenceBundleVerifier,
    )
    from quantking.data.validation.samples import load_fixed_sample_v4

    directory = _write_st_bundle(tmp_path)
    sample = load_fixed_sample_v4()
    verified = AuthorityReferenceBundleVerifier(root=tmp_path).verify(
        directory=directory, sample=sample
    )
    assert verified.bundle_id == "st-603377-authority-v1"
    assert verified.sample_spec_version == sample.sample_spec_version
    assert verified.sample_spec_sha256 == sample.sample_spec_sha256
    assert verified.review_decision_status.value == "APPROVED"
    assert "st_warning_603377_sh" in verified.approved_case_ids
    assert (directory / "manifest.json").is_file()
    assert (directory / "manifest.sha256").is_file()
    assert (directory / "COMPLETE").is_file()
    assert (directory / "facts.json").is_file()
    assert (directory / "bindings.json").is_file()
    assert (directory / "review_decisions.json").is_file()
    assert list((directory / "references").glob("*.json"))


def test_v3_authority_bundle_st_case_is_authority_approved(tmp_path: Path) -> None:
    from quantking.data.validation.authority_bundle import (
        AuthorityReferenceBundleVerifier,
    )
    from quantking.data.validation.samples import CaseEligibility, load_fixed_sample_v4

    directory = _write_st_bundle(tmp_path)
    sample = load_fixed_sample_v4()
    verified = AuthorityReferenceBundleVerifier(root=tmp_path).verify(
        directory=directory, sample=sample
    )
    assert verified.case_eligibilities["st_warning_603377_sh"] is CaseEligibility.AUTHORITY_APPROVED


def test_v3_authority_bundle_inherited_cases_are_framework_only(tmp_path: Path) -> None:
    from quantking.data.validation.authority_bundle import (
        AuthorityReferenceBundleVerifier,
    )
    from quantking.data.validation.samples import CaseEligibility, load_fixed_sample_v4

    directory = _write_st_bundle(tmp_path)
    sample = load_fixed_sample_v4()
    verified = AuthorityReferenceBundleVerifier(root=tmp_path).verify(
        directory=directory, sample=sample
    )
    for case_id, eligibility in verified.case_eligibilities.items():
        if case_id == "st_warning_603377_sh":
            continue
        assert eligibility is CaseEligibility.FRAMEWORK_ONLY


def test_v3_authority_bundle_idempotent_republish(tmp_path: Path) -> None:
    from quantking.data.validation.authority_bundle import (
        AuthorityReferenceBundleWriter,
    )
    from quantking.data.validation.samples import load_fixed_sample_v4

    first = _write_st_bundle(tmp_path)
    second = AuthorityReferenceBundleWriter(root=tmp_path).write(
        _build_st_authority_draft(),
        sample=load_fixed_sample_v4(),
    )
    assert first == second


def test_v3_authority_bundle_rejects_content_conflict(tmp_path: Path) -> None:
    from quantking.data.validation.authority_bundle import (
        AuthorityBundleIdempotencyConflictError,
        AuthorityReferenceBundleWriter,
    )
    from quantking.data.validation.samples import load_fixed_sample_v4

    first_draft = _build_st_authority_draft()
    _write_st_bundle(tmp_path)
    conflicting = replace(
        first_draft,
        code_version="s1-07b.authority-bundle.v3.tampered",
    )
    with pytest.raises(AuthorityBundleIdempotencyConflictError):
        AuthorityReferenceBundleWriter(root=tmp_path).write(
            conflicting,
            sample=load_fixed_sample_v4(),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "manifest",
        "complete",
        "sha256",
        "fact",
        "reference",
        "binding",
        "decision",
        "extra_file",
        "extra_directory",
    ],
)
def test_v3_authority_bundle_rejects_tampering(tmp_path: Path, mutation: str) -> None:
    from quantking.data.adapters.warehouse.manifest import compute_sha256
    from quantking.data.validation.authority_bundle import (
        AuthorityBundleVerificationError,
        AuthorityReferenceBundleVerifier,
    )
    from quantking.data.validation.samples import load_fixed_sample_v4

    def _canonical(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )

    directory = _write_st_bundle(tmp_path)
    sample = load_fixed_sample_v4()
    verifier = AuthorityReferenceBundleVerifier(root=tmp_path)

    if mutation == "manifest":
        (directory / "manifest.json").write_bytes(_canonical({"tampered": True}))
    elif mutation == "complete":
        (directory / "COMPLETE").write_bytes(_canonical({"tampered": True}))
    elif mutation == "sha256":
        (directory / "manifest.sha256").write_bytes(b"0" * 64 + b"  manifest.json\n")
    elif mutation == "fact":
        target = directory / "facts.json"
        payload = json.loads(target.read_bytes())
        payload[0]["statement"] = "tampered"
        target.write_bytes(_canonical(payload))
    elif mutation == "reference":
        target = next(iter((directory / "references").glob("*.json")))
        payload = json.loads(target.read_bytes())
        payload["canonical_locator"] = "https://attacker.invalid/tampered"
        target.write_bytes(_canonical(payload))
    elif mutation == "binding":
        target = directory / "bindings.json"
        payload = json.loads(target.read_bytes())
        payload[0]["case_id"] = "attacker-case"
        target.write_bytes(_canonical(payload))
    elif mutation == "decision":
        target = directory / "review_decisions.json"
        payload = json.loads(target.read_bytes())
        payload[0]["status"] = "REJECTED"
        target.write_bytes(_canonical(payload))
    elif mutation == "extra_file":
        (directory / "undeclared.json").write_bytes(_canonical({"extra": True}))
    elif mutation == "extra_directory":
        (directory / "undeclared").mkdir()
        (directory / "undeclared" / "nested.json").write_bytes(_canonical({}))

    # For reference/binding/decision mutations we re-derive the manifest SHA
    # so the file is internally consistent; the verifier must still reject the
    # tampering via structural validation (URL check, case/fact alignment, or
    # review decision coverage). For fact mutation we do NOT re-derive, so the
    # file SHA mismatch is detected.
    if mutation in {"reference", "binding", "decision"}:
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        files = manifest["files"]
        for entry in files:
            path = directory / entry["relative_path"]
            payload = path.read_bytes()
            entry["sha256"] = compute_sha256(payload)
            entry["size"] = len(payload)
        manifest["reference_inventory_sha256"] = compute_sha256(_canonical(files))
        unsigned = dict(manifest)
        unsigned.pop("manifest_sha256")
        manifest_sha = compute_sha256(_canonical(unsigned))
        manifest["manifest_sha256"] = manifest_sha
        manifest_path.write_bytes(_canonical(manifest))
        (directory / "manifest.sha256").write_bytes(f"{manifest_sha}  manifest.json\n".encode())
        (directory / "COMPLETE").write_bytes(_canonical({"manifest_sha256": manifest_sha}))

    with pytest.raises(AuthorityBundleVerificationError):
        verifier.verify(directory=directory, sample=sample)


def test_v3_authority_bundle_rejects_path_traversal(tmp_path: Path) -> None:
    from quantking.data.adapters.warehouse.manifest import compute_sha256
    from quantking.data.validation.authority_bundle import (
        AuthorityBundleVerificationError,
        AuthorityReferenceBundleVerifier,
    )
    from quantking.data.validation.samples import load_fixed_sample_v4

    def _canonical(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )

    directory = _write_st_bundle(tmp_path)
    sample = load_fixed_sample_v4()
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"][0]["relative_path"] = "../escape.json"
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256")
    manifest["manifest_sha256"] = compute_sha256(_canonical(unsigned))
    manifest_path.write_bytes(_canonical(manifest))
    (directory / "COMPLETE").write_bytes(
        _canonical({"manifest_sha256": manifest["manifest_sha256"]})
    )
    (directory / "manifest.sha256").write_bytes(
        f"{manifest['manifest_sha256']}  manifest.json\n".encode()
    )
    with pytest.raises(AuthorityBundleVerificationError):
        AuthorityReferenceBundleVerifier(root=tmp_path).verify(directory=directory, sample=sample)


def test_v3_authority_bundle_rejects_symlink_or_junction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantking.data.validation import authority_bundle as bundle_module
    from quantking.data.validation.authority_bundle import (
        AuthorityBundleVerificationError,
        AuthorityReferenceBundleVerifier,
    )
    from quantking.data.validation.samples import load_fixed_sample_v4

    directory = _write_st_bundle(tmp_path)
    sample = load_fixed_sample_v4()
    target = directory / "facts.json"
    payload = target.read_bytes()
    target.unlink()
    outside = tmp_path / "outside.json"
    outside.write_bytes(payload)
    try:
        os.symlink(outside, target)
    except OSError:
        target.write_bytes(payload)
        original = bundle_module.is_reparse_point
        monkeypatch.setattr(
            bundle_module,
            "is_reparse_point",
            lambda path: path == target or original(path),
        )
    with pytest.raises(AuthorityBundleVerificationError):
        AuthorityReferenceBundleVerifier(root=tmp_path).verify(directory=directory, sample=sample)


def test_v3_local_owner_approval_cannot_be_real_signer(tmp_path: Path) -> None:
    """LOCAL_OWNER approval proves user review; it must never impersonate a REAL signer."""
    from quantking.data.validation.authority_bundle import (
        AuthorityReferenceBundleVerifier,
        ReviewerRole,
    )
    from quantking.data.validation.samples import load_fixed_sample_v4

    directory = _write_st_bundle(tmp_path)
    sample = load_fixed_sample_v4()
    verified = AuthorityReferenceBundleVerifier(root=tmp_path).verify(
        directory=directory, sample=sample
    )
    # The verifier returns LOCAL_OWNER approval but must never claim REAL signer.
    assert ReviewerRole.LOCAL_OWNER.value == "LOCAL_OWNER"
    assert "REAL" not in verified.review_decision_status.value
    assert "SIGNER" not in verified.review_decision_status.value
    # LOCAL_OWNER approval does not promote to PRODUCTION_ELIGIBLE.
    assert sample.eligibility is not SampleEligibility.PRODUCTION_ELIGIBLE
    assert sample.eligibility is SampleEligibility.VALIDATION_READY_WITH_BLOCKED_CASES


# ---------------------------------------------------------------------------
# Time semantics
# ---------------------------------------------------------------------------


def test_retrospective_fact_requires_effective_date_and_other_risk_warning() -> None:
    from quantking.data.validation.authority_bundle import (
        AuthorityReferenceFact,
        FactNature,
    )

    with pytest.raises(ValueError, match="RETROSPECTIVE_CONFIRMATION requires"):
        AuthorityReferenceFact(
            fact_id="st-test-001",
            reference_id="cninfo-test-001",
            dataset=DatasetKind.TRADING_STATUS,
            purpose=SamplePurpose.ST,
            code="603377.SH",
            exchange="SH",
            fact_nature=FactNature.RETROSPECTIVE_CONFIRMATION,
            document_date=TradingDate.from_iso("2026-07-30"),
            publication_date=TradingDate.from_iso("2026-07-31"),
            known_from_date=TradingDate.from_iso("2026-07-31"),
            usable_for_decisions_after="2026-07-31 market close",
            material_sha256=_PDF_SHA,
            paragraph_sha256="a" * 64,
            pages=(1,),
            statement="test",
        )


def test_continuation_fact_requires_other_risk_warning_continues() -> None:
    from quantking.data.validation.authority_bundle import (
        AuthorityReferenceFact,
        FactNature,
    )

    with pytest.raises(ValueError, match="CONTINUATION requires"):
        AuthorityReferenceFact(
            fact_id="st-test-002",
            reference_id="cninfo-test-001",
            dataset=DatasetKind.TRADING_STATUS,
            purpose=SamplePurpose.ST,
            code="603377.SH",
            exchange="SH",
            fact_nature=FactNature.CONTINUATION,
            document_date=TradingDate.from_iso("2026-07-30"),
            publication_date=TradingDate.from_iso("2026-07-31"),
            known_from_date=TradingDate.from_iso("2026-07-31"),
            usable_for_decisions_after="2026-07-31 market close",
            material_sha256=_PDF_SHA,
            paragraph_sha256="b" * 64,
            pages=(1,),
            statement="test",
        )


def test_identity_fact_rejects_risk_warning_flags() -> None:
    from quantking.data.validation.authority_bundle import (
        AuthorityReferenceFact,
        FactNature,
    )

    with pytest.raises(ValueError, match="IDENTITY must not set"):
        AuthorityReferenceFact(
            fact_id="st-test-003",
            reference_id="cninfo-test-001",
            dataset=DatasetKind.TRADING_STATUS,
            purpose=SamplePurpose.ST,
            code="603377.SH",
            exchange="SH",
            fact_nature=FactNature.IDENTITY,
            document_date=TradingDate.from_iso("2026-07-30"),
            publication_date=TradingDate.from_iso("2026-07-31"),
            known_from_date=TradingDate.from_iso("2026-07-31"),
            usable_for_decisions_after="2026-07-31 market close",
            material_sha256=_PDF_SHA,
            paragraph_sha256="c" * 64,
            pages=(1,),
            statement="test",
            other_risk_warning=True,
        )


def test_publication_date_must_not_precede_document_date() -> None:
    from quantking.data.validation.authority_bundle import (
        AuthorityReferenceDocument,
    )

    with pytest.raises(ValueError, match="publication_date must not precede"):
        AuthorityReferenceDocument(
            reference_id="cninfo-test-002",
            authority="test",
            canonical_locator=_CNINFO_LOCATOR,
            announcement_id="1225449700",
            announcement_number="临2026-110",
            security_code="603377.SH",
            security_name="ST 东时",
            company_name="东方时尚驾驶学校股份有限公司",
            document_date=TradingDate.from_iso("2026-07-31"),
            publication_date=TradingDate.from_iso("2026-07-30"),
            known_from_date=TradingDate.from_iso("2026-07-31"),
            material_sha256=_PDF_SHA,
            p2b_manifest_sha256=_P2B_MANIFEST_SHA,
            p2f_manifest_sha256=_P2F_MANIFEST_SHA,
        )


def test_known_from_date_must_not_precede_publication_date() -> None:
    from quantking.data.validation.authority_bundle import (
        AuthorityReferenceDocument,
    )

    with pytest.raises(ValueError, match="known_from_date must not precede"):
        AuthorityReferenceDocument(
            reference_id="cninfo-test-003",
            authority="test",
            canonical_locator=_CNINFO_LOCATOR,
            announcement_id="1225449700",
            announcement_number="临2026-110",
            security_code="603377.SH",
            security_name="ST 东时",
            company_name="东方时尚驾驶学校股份有限公司",
            document_date=TradingDate.from_iso("2026-07-30"),
            publication_date=TradingDate.from_iso("2026-07-31"),
            known_from_date=TradingDate.from_iso("2026-07-30"),
            material_sha256=_PDF_SHA,
            p2b_manifest_sha256=_P2B_MANIFEST_SHA,
            p2f_manifest_sha256=_P2F_MANIFEST_SHA,
        )


def test_retrospective_effective_date_must_not_follow_document_date() -> None:
    from quantking.data.validation.authority_bundle import (
        AuthorityReferenceFact,
        FactNature,
    )

    with pytest.raises(ValueError, match="retrospective effective_date must not follow"):
        AuthorityReferenceFact(
            fact_id="st-test-004",
            reference_id="cninfo-test-001",
            dataset=DatasetKind.TRADING_STATUS,
            purpose=SamplePurpose.ST,
            code="603377.SH",
            exchange="SH",
            fact_nature=FactNature.RETROSPECTIVE_CONFIRMATION,
            document_date=TradingDate.from_iso("2026-07-30"),
            publication_date=TradingDate.from_iso("2026-07-31"),
            known_from_date=TradingDate.from_iso("2026-07-31"),
            usable_for_decisions_after="2026-07-31 market close",
            material_sha256=_PDF_SHA,
            paragraph_sha256="d" * 64,
            pages=(1,),
            statement="test",
            effective_date=TradingDate.from_iso("2026-08-01"),
            other_risk_warning=True,
        )


def test_usable_for_decisions_after_must_match_market_close_pattern() -> None:
    from quantking.data.validation.authority_bundle import (
        AuthorityReferenceFact,
        FactNature,
    )

    with pytest.raises(ValueError, match="usable_for_decisions_after"):
        AuthorityReferenceFact(
            fact_id="st-test-005",
            reference_id="cninfo-test-001",
            dataset=DatasetKind.TRADING_STATUS,
            purpose=SamplePurpose.ST,
            code="603377.SH",
            exchange="SH",
            fact_nature=FactNature.IDENTITY,
            document_date=TradingDate.from_iso("2026-07-30"),
            publication_date=TradingDate.from_iso("2026-07-31"),
            known_from_date=TradingDate.from_iso("2026-07-31"),
            usable_for_decisions_after="2026-07-31 09:30:00",
            material_sha256=_PDF_SHA,
            paragraph_sha256="e" * 64,
            pages=(1,),
            statement="test",
        )


def test_st_authority_facts_carry_correct_time_semantics() -> None:
    """Verify the four approved ST facts carry the exact time semantics from the task."""
    draft = _build_st_authority_draft()
    facts = {fact.fact_id: fact for fact in draft.facts}

    identity = facts["st-document-identity-001"]
    assert identity.fact_nature.value == "IDENTITY"
    assert str(identity.document_date) == "2026-07-30"
    assert str(identity.publication_date) == "2026-07-31"
    assert str(identity.known_from_date) == "2026-07-31"
    assert identity.usable_for_decisions_after == "2026-07-31 market close"
    assert identity.effective_date is None
    assert identity.other_risk_warning is None
    assert identity.other_risk_warning_continues is None

    retro_0323 = facts["st-status-20260323"]
    assert retro_0323.fact_nature.value == "RETROSPECTIVE_CONFIRMATION"
    assert str(retro_0323.effective_date) == "2026-03-23"
    assert retro_0323.other_risk_warning is True
    assert str(retro_0323.document_date) == "2026-07-30"
    assert str(retro_0323.publication_date) == "2026-07-31"
    assert str(retro_0323.known_from_date) == "2026-07-31"

    retro_0418 = facts["st-status-20260418"]
    assert retro_0418.fact_nature.value == "RETROSPECTIVE_CONFIRMATION"
    assert str(retro_0418.effective_date) == "2026-04-18"
    assert retro_0418.other_risk_warning is True

    continuation = facts["st-continues-20260730"]
    assert continuation.fact_nature.value == "CONTINUATION"
    assert continuation.other_risk_warning_continues is True
    assert continuation.effective_date is None
    assert continuation.other_risk_warning is None


# ---------------------------------------------------------------------------
# Runtime per-case gating
# ---------------------------------------------------------------------------


def test_runtime_plan_uses_v4_sample_by_default(tmp_path: Path) -> None:
    from quantking.data.validation.runtime import (
        ControlledRunnerRegistry,
        LocalPackageVersionProbe,
        RealValidationRuntime,
        ValidationSource,
    )

    runtime = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={"xtquant": "unavailable"}),
    )
    plan = runtime.plan(
        source=ValidationSource.XTQUANT,
        dataset=DatasetKind.TRADING_STATUS,
        output_root=tmp_path,
    )
    assert plan.sample_spec_version == "s1-07.fixed-sample.v4"
    # Without a reference bundle, all cases are FRAMEWORK_ONLY.
    assert "CASE_NOT_AUTHORITY_APPROVED" in plan.blocking_reasons


def test_runtime_plan_with_st_case_and_bundle_passes_sample_gate(tmp_path: Path) -> None:
    from quantking.data.validation.runtime import (
        ControlledRunnerRegistry,
        LocalPackageVersionProbe,
        RealValidationRuntime,
        ValidationSource,
    )

    bundle_dir = _write_st_bundle(tmp_path)
    runtime = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={"xtquant": "1.0.0"}),
        reference_bundle_root=tmp_path,
    )
    plan = runtime.plan(
        source=ValidationSource.XTQUANT,
        dataset=DatasetKind.TRADING_STATUS,
        output_root=tmp_path / "output",
        case_ids=("st_warning_603377_sh",),
        reference_bundle_directory=bundle_dir,
    )
    assert "st_warning_603377_sh" in plan.case_ids
    assert "CASE_NOT_AUTHORITY_APPROVED" not in plan.blocking_reasons
    assert plan.reference_bundle_id == "st-603377-authority-v1"
    assert plan.reference_bundle_sha256
    # Package version is available, but signer is still not initialized.
    assert "REAL_SIGNER_NOT_INITIALIZED" in plan.blocking_reasons


def test_runtime_plan_without_bundle_treats_st_case_as_framework_only(
    tmp_path: Path,
) -> None:
    from quantking.data.validation.runtime import (
        ControlledRunnerRegistry,
        LocalPackageVersionProbe,
        RealValidationRuntime,
        ValidationSource,
    )

    runtime = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={"xtquant": "1.0.0"}),
    )
    plan = runtime.plan(
        source=ValidationSource.XTQUANT,
        dataset=DatasetKind.TRADING_STATUS,
        output_root=tmp_path,
        case_ids=("st_warning_603377_sh",),
    )
    assert "CASE_NOT_AUTHORITY_APPROVED" in plan.blocking_reasons
    assert not plan.reference_bundle_id


def test_runtime_plan_with_framework_case_fails(tmp_path: Path) -> None:
    from quantking.data.validation.runtime import (
        ControlledRunnerRegistry,
        LocalPackageVersionProbe,
        RealValidationRuntime,
        ValidationSource,
    )

    bundle_dir = _write_st_bundle(tmp_path)
    runtime = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={"xtquant": "1.0.0"}),
        reference_bundle_root=tmp_path,
    )
    plan = runtime.plan(
        source=ValidationSource.XTQUANT,
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        output_root=tmp_path / "output",
        case_ids=("calendar_sh",),
        reference_bundle_directory=bundle_dir,
    )
    # calendar_sh is FRAMEWORK_ONLY (not bound to the ST authority bundle).
    assert "CASE_NOT_AUTHORITY_APPROVED" in plan.blocking_reasons


def test_runtime_plan_with_mixed_cases_fails(tmp_path: Path) -> None:
    from quantking.data.validation.runtime import (
        ControlledRunnerRegistry,
        LocalPackageVersionProbe,
        RealValidationRuntime,
        ValidationSource,
    )

    bundle_dir = _write_st_bundle(tmp_path)
    runtime = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={"xtquant": "1.0.0"}),
        reference_bundle_root=tmp_path,
    )
    # Mixing AUTHORITY_APPROVED + FRAMEWORK_ONLY must fail the whole plan.
    plan = runtime.plan(
        source=ValidationSource.XTQUANT,
        dataset=DatasetKind.TRADING_STATUS,
        output_root=tmp_path / "output",
        case_ids=("st_warning_603377_sh", "trading_status_normal_sh"),
        reference_bundle_directory=bundle_dir,
    )
    assert "CASE_NOT_AUTHORITY_APPROVED" in plan.blocking_reasons


def test_runtime_plan_rejects_case_id_not_in_sample(tmp_path: Path) -> None:
    from quantking.data.validation.runtime import (
        ControlledRunnerRegistry,
        LocalPackageVersionProbe,
        RealValidationRuntime,
        ValidationSource,
    )

    runtime = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={"xtquant": "1.0.0"}),
    )
    with pytest.raises(ValueError, match="case_ids"):
        runtime.plan(
            source=ValidationSource.XTQUANT,
            dataset=DatasetKind.TRADING_STATUS,
            output_root=tmp_path,
            case_ids=("nonexistent-case",),
        )


def test_runtime_execute_with_approved_case_still_blocks_on_signer(
    tmp_path: Path,
) -> None:
    from quantking.data.validation.runtime import (
        ControlledRunnerRegistry,
        LocalPackageVersionProbe,
        RealRuntimeExecutionError,
        RealValidationRuntime,
        ValidationSource,
    )

    bundle_dir = _write_st_bundle(tmp_path)
    runtime = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={"xtquant": "1.0.0"}),
        reference_bundle_root=tmp_path,
    )
    with pytest.raises(RealRuntimeExecutionError) as captured:
        runtime.execute(
            source=ValidationSource.XTQUANT,
            dataset=DatasetKind.TRADING_STATUS,
            output_root=tmp_path / "output",
            acknowledgement="I_ACKNOWLEDGE_AUTHORIZED_REAL_SOURCE_VALIDATION",
            authorization=None,
            case_ids=("st_warning_603377_sh",),
            reference_bundle_directory=bundle_dir,
        )
    # Sample/reference gate passes, but authorization is required.
    assert captured.value.code == "AUTHORIZATION_REQUIRED"


def test_runtime_execute_with_unapproved_case_fails_before_authorization(
    tmp_path: Path,
) -> None:
    from quantking.data.validation.runtime import (
        ControlledRunnerRegistry,
        LocalPackageVersionProbe,
        RealRuntimeExecutionError,
        RealValidationRuntime,
        ValidationSource,
    )

    runtime = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={"xtquant": "1.0.0"}),
    )
    with pytest.raises(RealRuntimeExecutionError) as captured:
        runtime.execute(
            source=ValidationSource.XTQUANT,
            dataset=DatasetKind.TRADING_STATUS,
            output_root=tmp_path / "output",
            acknowledgement="I_ACKNOWLEDGE_AUTHORIZED_REAL_SOURCE_VALIDATION",
            authorization=None,
            case_ids=("st_warning_603377_sh",),
        )
    assert captured.value.code == "CASE_NOT_AUTHORITY_APPROVED"


def test_runtime_plan_freezes_reference_bundle_id_and_sha(tmp_path: Path) -> None:
    from quantking.data.validation.authority_bundle import (
        AuthorityReferenceBundleVerifier,
    )
    from quantking.data.validation.runtime import (
        ControlledRunnerRegistry,
        LocalPackageVersionProbe,
        RealValidationRuntime,
        ValidationSource,
    )
    from quantking.data.validation.samples import load_fixed_sample_v4

    bundle_dir = _write_st_bundle(tmp_path)
    verified = AuthorityReferenceBundleVerifier(root=tmp_path).verify(
        directory=bundle_dir, sample=load_fixed_sample_v4()
    )
    runtime = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={"xtquant": "1.0.0"}),
        reference_bundle_root=tmp_path,
    )
    plan = runtime.plan(
        source=ValidationSource.XTQUANT,
        dataset=DatasetKind.TRADING_STATUS,
        output_root=tmp_path / "output",
        case_ids=("st_warning_603377_sh",),
        reference_bundle_directory=bundle_dir,
    )
    assert plan.reference_bundle_id == verified.bundle_id
    assert plan.reference_bundle_sha256 == verified.manifest_sha256


def test_runtime_plan_package_unavailable_keeps_blocking(tmp_path: Path) -> None:
    from quantking.data.validation.runtime import (
        ControlledRunnerRegistry,
        LocalPackageVersionProbe,
        RealValidationRuntime,
        ValidationSource,
    )

    bundle_dir = _write_st_bundle(tmp_path)
    runtime = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={"xtquant": "unavailable"}),
        reference_bundle_root=tmp_path,
    )
    plan = runtime.plan(
        source=ValidationSource.XTQUANT,
        dataset=DatasetKind.TRADING_STATUS,
        output_root=tmp_path / "output",
        case_ids=("st_warning_603377_sh",),
        reference_bundle_directory=bundle_dir,
    )
    assert "PROVIDER_PACKAGE_VERSION_UNRESOLVED" in plan.blocking_reasons


# ---------------------------------------------------------------------------
# Stage 1 status regression
# ---------------------------------------------------------------------------


def test_stage1_status_s1_07b_awaiting_acceptance() -> None:
    """S1-07B final implementation is submitted but awaiting main acceptance."""
    from pathlib import Path as _Path

    acceptance = _Path(__file__).resolve().parents[3] / "docs" / "stage1" / "ACCEPTANCE.md"
    text = acceptance.read_text(encoding="utf-8")
    assert "S1-07A" in text and "主验收" in text
    # S1-07B must not claim main acceptance yet.
    assert "等待主验收" in text or "awaiting" in text.lower() or "待主验收" in text
    # S1-07C/D and S1-08 must remain unauthorized.
    assert "S1-07C" in text
    assert "尚未开始" in text or "未获授权" in text or "未开始" in text


def test_formal_bundle_builder_binds_every_fact_to_st_purpose() -> None:
    """The production builder must not select a case by tuple position."""
    from scripts.build_s1_07b_reference_bundle import _build_draft

    draft = _build_draft(
        pdf_sha=_PDF_SHA,
        p2b_sha=_P2B_MANIFEST_SHA,
        p2f_sha=_P2F_MANIFEST_SHA,
    )
    assert draft.bundle_id == "st-603377-authority-v3"
    assert draft.references[0].security_code == "603377.SH"
    assert draft.references[0].security_name == "ST 东时"
    assert draft.references[0].company_name == "东方时尚驾驶学校股份有限公司"
    assert {fact.purpose for fact in draft.facts} == {SamplePurpose.ST}
    assert {binding.purpose for binding in draft.bindings} == {SamplePurpose.ST}
    assert {str(decision.decided_at) for decision in draft.review_decisions} == {"2026-08-04"}
    identity = next(fact for fact in draft.facts if fact.fact_id == "st-document-identity-001")
    assert identity.statement == "证券代码：603377 证券简称：ST 东时 公告编号：临 2026-110"


def test_writer_rejects_cross_purpose_bundle_before_final_publish(tmp_path: Path) -> None:
    from quantking.data.validation.authority_bundle import AuthorityReferenceBundleWriter
    from quantking.data.validation.samples import load_fixed_sample_v4

    draft = _build_st_authority_draft()
    malformed = replace(
        draft,
        facts=tuple(replace(fact, purpose=SamplePurpose.NORMAL) for fact in draft.facts),
        bindings=tuple(
            replace(binding, purpose=SamplePurpose.NORMAL) for binding in draft.bindings
        ),
    )
    with pytest.raises(ValueError, match="binding does not align"):
        AuthorityReferenceBundleWriter(root=tmp_path).write(
            malformed,
            sample=load_fixed_sample_v4(),
        )
    assert not (tmp_path / "reference-bundles" / malformed.bundle_id).exists()


def test_writer_rejects_reference_identity_drift_before_final_publish(tmp_path: Path) -> None:
    from quantking.data.validation.authority_bundle import AuthorityReferenceBundleWriter
    from quantking.data.validation.samples import load_fixed_sample_v4

    draft = _build_st_authority_draft()
    malformed = replace(
        draft,
        references=(replace(draft.references[0], security_code="000001.SZ"),),
    )
    with pytest.raises(ValueError, match="fact lineage does not align"):
        AuthorityReferenceBundleWriter(root=tmp_path).write(
            malformed,
            sample=load_fixed_sample_v4(),
        )
    assert not (tmp_path / "reference-bundles" / malformed.bundle_id).exists()
