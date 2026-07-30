"""S1-07B authoritative-sample and reference-bundle trust attacks."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from quantking.common.time import TradingDate
from quantking.data.validation.samples import SampleEligibility, SamplePurpose


def test_v1_v2_are_frozen_and_v3_is_framework_only() -> None:
    from quantking.data.validation.samples import (
        FIXED_SAMPLE_V1_SHA256,
        FIXED_SAMPLE_V2_SHA256,
        load_fixed_sample_v1,
        load_fixed_sample_v2,
        load_fixed_sample_v3,
    )

    v1 = load_fixed_sample_v1()
    v2 = load_fixed_sample_v2()
    v3 = load_fixed_sample_v3()
    assert v1.sample_spec_sha256 == FIXED_SAMPLE_V1_SHA256
    assert v2.sample_spec_sha256 == FIXED_SAMPLE_V2_SHA256
    assert v3.sample_spec_version == "s1-07.fixed-sample.v3"
    assert v3.previous_sample_spec_sha256 == v2.sample_spec_sha256
    assert v3.eligibility is SampleEligibility.FRAMEWORK_ONLY
    assert v3.fixed_sample_gate_eligible is False


def test_v3_declared_production_eligibility_cannot_override_verifier() -> None:
    from quantking.data.validation.samples import (
        fixed_sample_v3_from_release,
        packaged_v3_release_payload,
    )

    payload = packaged_v3_release_payload()
    payload["eligibility"] = "PRODUCTION_ELIGIBLE"
    payload["missing_authoritative_purposes"] = []
    with pytest.raises(ValueError, match="controlled v3 release"):
        fixed_sample_v3_from_release(payload)


def test_v3_rejects_broken_v2_chain_and_same_version_content_change() -> None:
    from quantking.data.validation.samples import (
        fixed_sample_v3_from_release,
        packaged_v3_release_payload,
    )

    broken = packaged_v3_release_payload()
    broken["previous_sample_spec_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="controlled v3 release"):
        fixed_sample_v3_from_release(broken)

    changed = packaged_v3_release_payload()
    changed["required_authoritative_purposes"] = [
        *changed["required_authoritative_purposes"],
        "UNRELATED",
    ]
    with pytest.raises(ValueError, match="controlled v3 release"):
        fixed_sample_v3_from_release(changed)


def test_v3_declares_every_required_authoritative_purpose() -> None:
    from quantking.data.validation.samples import load_fixed_sample_v3

    sample = load_fixed_sample_v3()
    required = {
        SamplePurpose.NORMAL,
        SamplePurpose.IPO,
        SamplePurpose.LISTING,
        SamplePurpose.POINT_IN_TIME,
        SamplePurpose.DELISTING,
        SamplePurpose.DELISTING_PERIOD,
        SamplePurpose.ST,
        SamplePurpose.STAR_ST,
        SamplePurpose.SUSPENSION,
        SamplePurpose.RESUMPTION,
        SamplePurpose.LIMIT_UP,
        SamplePurpose.LIMIT_DOWN,
        SamplePurpose.CORPORATE_ACTION,
        SamplePurpose.BENCHMARK,
        SamplePurpose.ADJUSTMENT_FORMULA,
        SamplePurpose.UNIT_SEMANTICS,
        SamplePurpose.MISSING_OR_UNSUPPORTED,
    }
    assert set(sample.required_authoritative_purposes) == required
    assert set(sample.missing_authoritative_purposes) == {item.value for item in required}


def _draft(sample: object) -> object:
    from quantking.data.validation.references import (
        AuthorityTier,
        CaseFactBinding,
        ReferenceBundleDraft,
        ReferenceDocument,
        ReferenceFact,
        ReviewerDecision,
    )

    case = sample.cases[0]  # type: ignore[attr-defined]
    document = ReferenceDocument(
        reference_id="ref-exchange-notice-001",
        authority="Synthetic Exchange Fixture",
        authority_tier=AuthorityTier.PRIMARY,
        canonical_locator="https://authority.invalid/notices/document-001",
        document_id="document-001",
        published_date=TradingDate.from_iso("2024-01-01"),
        retrieved_date=TradingDate.from_iso("2026-07-30"),
        material_sha256="1" * 64,
    )
    fact = ReferenceFact(
        fact_id="fact-exchange-notice-001",
        reference_id=document.reference_id,
        dataset=case.dataset,
        purpose=case.purpose,
        code=case.code,
        exchange=case.exchange,
        start_date=case.start_date,
        end_date=case.end_date,
        statement="Synthetic fixture fact; not an accepted authoritative fact.",
    )
    return ReferenceBundleDraft(
        bundle_id="reference-bundle-test-001",
        sample_spec_version=sample.sample_spec_version,  # type: ignore[attr-defined]
        sample_spec_sha256=sample.sample_spec_sha256,  # type: ignore[attr-defined]
        previous_sample_spec_sha256=sample.previous_sample_spec_sha256,  # type: ignore[attr-defined]
        references=(document,),
        facts=(fact,),
        bindings=(CaseFactBinding(case_id=case.case_id, fact_id=fact.fact_id),),
        code_version="s1-07b.references.v1",
        schema_version="reference-bundle.v1",
        reviewed_at=TradingDate.from_iso("2026-07-30"),
        reviewer_decision=ReviewerDecision.PENDING,
        reviewer_key_id=None,
        reviewer_signature=None,
    )


def test_reference_bundle_is_canonical_strict_and_never_eligible_without_trusted_review(
    tmp_path: Path,
) -> None:
    from quantking.data.validation.references import (
        ReferenceBundleVerifier,
        ReferenceBundleWriter,
        ReviewerTrustPolicy,
    )
    from quantking.data.validation.samples import load_fixed_sample_v3

    sample = load_fixed_sample_v3()
    directory = ReferenceBundleWriter(root=tmp_path).write(_draft(sample))
    verified = ReferenceBundleVerifier(
        root=tmp_path,
        reviewer_trust_policy=ReviewerTrustPolicy.framework_only(),
    ).verify(directory=directory, sample=sample)
    assert verified.eligibility is SampleEligibility.FRAMEWORK_ONLY
    assert verified.complete is False
    assert (directory / "manifest.json").is_file()
    assert (directory / "COMPLETE").is_file()


@pytest.mark.parametrize(
    "mutation",
    (
        "manifest",
        "complete",
        "reference",
        "extra",
        "extra-directory",
    ),
)
def test_reference_bundle_rejects_file_and_lineage_tampering(
    tmp_path: Path,
    mutation: str,
) -> None:
    from quantking.data.validation.references import (
        ReferenceBundleVerificationError,
        ReferenceBundleVerifier,
        ReferenceBundleWriter,
        ReviewerTrustPolicy,
    )
    from quantking.data.validation.samples import load_fixed_sample_v3

    sample = load_fixed_sample_v3()
    directory = ReferenceBundleWriter(root=tmp_path).write(_draft(sample))
    if mutation == "manifest":
        (directory / "manifest.json").write_text("{}", encoding="utf-8")
    elif mutation == "complete":
        (directory / "COMPLETE").write_text("{}", encoding="utf-8")
    elif mutation == "reference":
        target = next((directory / "references").glob("*.json"))
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["canonical_locator"] = "https://authority.invalid/tampered"
        target.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    else:
        if mutation == "extra":
            (directory / "undeclared.json").write_text("{}", encoding="utf-8")
        else:
            (directory / "undeclared" / "nested").mkdir(parents=True)
    with pytest.raises(ReferenceBundleVerificationError):
        ReferenceBundleVerifier(
            root=tmp_path,
            reviewer_trust_policy=ReviewerTrustPolicy.framework_only(),
        ).verify(directory=directory, sample=sample)


def test_reference_contract_rejects_homepage_third_party_and_unrelated_fact() -> None:
    from quantking.data.validation.references import AuthorityTier
    from quantking.data.validation.samples import load_fixed_sample_v3

    sample = load_fixed_sample_v3()
    draft = _draft(sample)
    reference = draft.references[0]
    with pytest.raises(ValueError, match="document-specific"):
        replace(reference, canonical_locator="https://authority.invalid/")
    with pytest.raises(ValueError, match="PRIMARY"):
        replace(reference, authority_tier=AuthorityTier.THIRD_PARTY)
    unrelated = replace(
        draft.facts[0],
        end_date=TradingDate.from_iso("2024-01-06"),
    )
    with pytest.raises(ValueError, match="case fact"):
        replace(draft, facts=(unrelated,)).validate_against(sample)


def test_reference_contract_rejects_duplicate_ids_and_untrusted_approval(tmp_path: Path) -> None:
    from quantking.data.validation.references import (
        ReferenceBundleVerifier,
        ReferenceBundleWriter,
        ReviewerDecision,
        ReviewerTrustPolicy,
    )
    from quantking.data.validation.samples import load_fixed_sample_v3

    sample = load_fixed_sample_v3()
    draft = _draft(sample)
    with pytest.raises(ValueError, match="duplicate"):
        replace(draft, facts=(draft.facts[0], draft.facts[0]))
    with pytest.raises(ValueError, match="every fact"):
        replace(draft, bindings=()).validate_against(sample)

    approved = replace(
        draft,
        bundle_id="reference-bundle-test-approved",
        reviewer_decision=ReviewerDecision.APPROVED,
        reviewer_key_id="test.self-signed",
        reviewer_signature="2" * 64,
    )
    directory = ReferenceBundleWriter(root=tmp_path).write(approved)
    verified = ReferenceBundleVerifier(
        root=tmp_path,
        reviewer_trust_policy=ReviewerTrustPolicy.framework_only(),
    ).verify(directory=directory, sample=sample)
    assert verified.eligibility is SampleEligibility.FRAMEWORK_ONLY

    class CallerPolicy(ReviewerTrustPolicy):
        def accepts(self, **_: object) -> bool:
            return True

    with pytest.raises(ValueError, match="controlled registry"):
        ReferenceBundleVerifier(
            root=tmp_path,
            reviewer_trust_policy=CallerPolicy(frozenset()),
        )


def test_reference_bundle_rejects_path_traversal_and_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantking.data.adapters.warehouse.manifest import compute_sha256
    from quantking.data.validation.references import (
        ReferenceBundleVerificationError,
        ReferenceBundleVerifier,
        ReferenceBundleWriter,
        ReviewerTrustPolicy,
    )
    from quantking.data.validation.samples import load_fixed_sample_v3

    sample = load_fixed_sample_v3()
    directory = ReferenceBundleWriter(root=tmp_path).write(_draft(sample))
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["relative_path"] = "../escape.json"
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256")

    def canonical(value: object) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()

    manifest["manifest_sha256"] = compute_sha256(canonical(unsigned))
    manifest_path.write_bytes(canonical(manifest))
    (directory / "COMPLETE").write_bytes(
        canonical({"manifest_sha256": manifest["manifest_sha256"]})
    )
    verifier = ReferenceBundleVerifier(
        root=tmp_path,
        reviewer_trust_policy=ReviewerTrustPolicy.framework_only(),
    )
    with pytest.raises(ReferenceBundleVerificationError):
        verifier.verify(directory=directory, sample=sample)

    safe = ReferenceBundleWriter(root=tmp_path).write(
        replace(_draft(sample), bundle_id="reference-bundle-symlink-test")
    )
    target = safe / "facts.json"
    payload = target.read_bytes()
    target.unlink()
    outside = tmp_path / "outside.json"
    outside.write_bytes(payload)
    try:
        os.symlink(outside, target)
    except OSError:
        from quantking.data.validation import references as reference_module

        target.write_bytes(payload)
        original = reference_module.is_reparse_point
        monkeypatch.setattr(
            reference_module,
            "is_reparse_point",
            lambda path: path == target or original(path),
        )
    with pytest.raises(ReferenceBundleVerificationError):
        verifier.verify(directory=safe, sample=sample)
