# ruff: noqa: RUF001
"""S1-07B-FINAL: verify sealed evidence and build the authoritative reference bundle.

This script runs OUTSIDE pytest (which blocks formal paths). It:
1. Re-verifies the P2B manifest, P2F manifest, P2F inventory, COMPLETE markers,
   per-file SHA-256 and path safety.
2. Verifies the PDF SHA-256.
3. Builds the immutable v3 reference bundle binding the 603377.SH ST authority
   facts to the v4 fixed sample.
4. Writes the bundle to the managed reference-bundles root.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure project source is importable
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "src"))

from quantking.common.paths import (  # noqa: E402
    is_reparse_point,
    resolve_under,
)
from quantking.common.time import TradingDate  # noqa: E402
from quantking.data.adapters.warehouse.manifest import (  # noqa: E402
    compute_sha256,
    validate_managed_relative,
)
from quantking.data.models import DatasetKind  # noqa: E402
from quantking.data.validation.authority_bundle import (  # noqa: E402
    AuthorityCaseFactBinding,
    AuthorityReferenceBundleDraft,
    AuthorityReferenceBundleWriter,
    AuthorityReferenceDocument,
    AuthorityReferenceFact,
    FactNature,
    ReviewDecision,
    ReviewDecisionStatus,
    ReviewerRole,
)
from quantking.data.validation.samples import (  # noqa: E402
    load_fixed_sample_v4,
)

# Authorized external paths
P2B_ROOT = Path(
    r"E:\QuantKingV3Data\validation\s1-07b\authority-discovery"
    r"\2607c2ae-a2b3-4870-b9e9-4b0aed05176d\evidence"
)
P2F_ROOT = Path(
    r"E:\QuantKingV3Data\validation\s1-07b\authority-material-import"
    r"\1c0bd1fd-c511-465a-a1c0-35d787da4938\evidence"
)
BUNDLE_ROOT = Path(r"E:\QuantKingV3Data\validation\s1-07b")

# Known credentials from task authorization
EXPECTED_P2B_MANIFEST_SHA = "0ae4809084bac61526d99d0576081cee5741b6b248e23015a2d9592e1a587df7"
EXPECTED_P2F_MANIFEST_SHA = "15fc9f0b122b68dfdb11d9fa8e15870d9b65d7286e77f9a971fe2912fe8c4ed9"
EXPECTED_P2F_INVENTORY_SHA = "c61db67b098936d4308b53985ca23780ac42b0957faed207b59cf3c1168ecdd6"
EXPECTED_PDF_SHA = "f3deec88dbd796c44535ecf72965c7f2103aca2ee663125b6499a427545c98e0"

# CNINFO document-specific locator
CNINFO_LOCATOR = (
    "https://www.cninfo.com.cn/new/disclosure/detail?stockCode=603377"
    "&orgId=9900023616&announcementId=1225449700&announcementTime=2026-07-31"
)
IDENTITY_PARAGRAPH_SHA = "2e81c9678e4d86b28e683e2f2dddf7474de51c8f2def68b7afe308644ece0d39"
STATUS_0323_PARAGRAPH_SHA = "2f1a2b21bd0b7dc1b419fa8964fefe2cb6d375537f5e8780a4bb760617bed3a8"
STATUS_0418_PARAGRAPH_SHA = "c69d8f9ebbef147c1524c4e8822534cd7a39fd37c3d44985ac3cc2fb7bb53268"
CONTINUATION_PARAGRAPH_SHA = "27786c8b65e8124e6685a53881daf438c41f4c447b8bd9f6222ab9c92818457f"


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _check_reparse(path: Path) -> None:
    for item in (path, *path.rglob("*")):
        if item.is_symlink() or is_reparse_point(item):
            raise RuntimeError(f"reparse point detected: {item}")


def _verify_manifest(root: Path, label: str, expected_sha: str) -> dict:
    """Verify manifest.json, manifest.sha256 and COMPLETE marker."""
    manifest_path = root / "manifest.json"
    sha_path = root / "manifest.sha256"
    complete_path = root / "COMPLETE"

    if not manifest_path.is_file():
        raise RuntimeError(f"{label}: manifest.json missing")
    if not sha_path.is_file():
        raise RuntimeError(f"{label}: manifest.sha256 missing")
    if not complete_path.is_file():
        raise RuntimeError(f"{label}: COMPLETE marker missing")

    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest_bytes != _canonical(manifest):
        raise RuntimeError(f"{label}: manifest.json is not canonical")

    actual_sha = compute_sha256(manifest_bytes)
    if actual_sha != expected_sha:
        raise RuntimeError(f"{label}: manifest SHA mismatch: {actual_sha} != {expected_sha}")

    sha_line = sha_path.read_bytes()
    expected_line = f"{actual_sha}  manifest.json\n".encode()
    if sha_line != expected_line:
        raise RuntimeError(f"{label}: manifest.sha256 line mismatch")

    complete_bytes = complete_path.read_bytes()
    complete = json.loads(complete_bytes)
    if (
        complete_bytes != _canonical(complete)
        or not isinstance(complete, dict)
        or set(complete)
        != {
            "authorization_id",
            "completed_at",
            "inventory_sha256",
            "manifest_sha256",
            "status",
        }
        or complete.get("authorization_id") != manifest.get("authorization_id")
        or complete.get("inventory_sha256") != manifest.get("inventory_sha256")
        or complete.get("manifest_sha256") != actual_sha
        or not str(complete.get("status", "")).startswith("COMPLETE")
    ):
        raise RuntimeError(f"{label}: COMPLETE marker SHA mismatch")

    print(f"  {label}: manifest SHA verified ({actual_sha[:16]}...)")
    return manifest


def _verify_evidence_inventory(
    root: Path,
    manifest: dict,
    *,
    label: str,
    expected_inventory_sha: str | None = None,
) -> None:
    """Verify an evidence inventory, exact tree and every declared file."""
    files = manifest.get("files")
    if not isinstance(files, list):
        raise RuntimeError(f"{label}: manifest files missing")
    if any(
        not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size"} for entry in files
    ):
        raise RuntimeError(f"{label}: inventory entry schema mismatch")
    inventory_canonical = _canonical(files)
    inventory_sha = compute_sha256(inventory_canonical)
    if inventory_sha != manifest.get("inventory_sha256"):
        raise RuntimeError(f"{label}: embedded inventory SHA mismatch")
    if expected_inventory_sha is not None and inventory_sha != expected_inventory_sha:
        raise RuntimeError(f"{label}: authorized inventory SHA mismatch")
    print(f"  {label} inventory SHA verified ({inventory_sha[:16]}...)")

    declared: set[str] = set()
    for entry in files:
        rel = entry["path"]
        validate_managed_relative(rel, field=f"{label} evidence file")
        if rel in declared:
            raise RuntimeError(f"{label}: duplicate inventory path")
        declared.add(rel)
        expected = entry["sha256"]
        size = entry["size"]
        target = resolve_under(root, rel)
        if target.is_symlink() or is_reparse_point(target) or not target.is_file():
            raise RuntimeError(f"{label}: declared file is unsafe or missing")
        payload = target.read_bytes()
        if len(payload) != size:
            raise RuntimeError(f"{label}: declared file size mismatch")
        actual = compute_sha256(payload)
        if actual != expected:
            raise RuntimeError(f"{label}: declared file SHA mismatch")

    reserved = manifest.get("reserved_files")
    if reserved != ["manifest.json", "manifest.sha256", "COMPLETE"]:
        raise RuntimeError(f"{label}: reserved file contract mismatch")
    expected_files = declared | set(reserved)
    actual_files = {item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_file()}
    expected_directories = {
        parent.as_posix()
        for relative in expected_files
        for parent in Path(relative).parents
        if parent != Path(".")
    }
    actual_directories = {
        item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_dir()
    }
    if actual_files != expected_files or actual_directories != expected_directories:
        raise RuntimeError(f"{label}: exact evidence tree mismatch")
    print(f"  {label} per-file SHA and exact tree verified ({len(files)} files)")


def _verify_p2f_fact_inputs() -> None:
    """Cross-bind the four approved facts to the sealed P2F extraction."""
    payload = (P2F_ROOT / "candidate_facts.json").read_bytes()
    parsed = json.loads(payload)
    if payload != _canonical(parsed) or not isinstance(parsed, dict):
        raise RuntimeError("P2F: candidate facts are not canonical")
    raw_facts = parsed.get("facts")
    if not isinstance(raw_facts, list) or any(not isinstance(item, dict) for item in raw_facts):
        raise RuntimeError("P2F: candidate fact collection is invalid")
    facts = {str(item.get("fact_id")): item for item in raw_facts}
    if set(facts) != {
        "ST-DOCUMENT-IDENTITY-001",
        "ST-STATUS-20260323",
        "ST-STATUS-20260418",
        "ST-CONTINUES-20260730",
    }:
        raise RuntimeError("P2F: candidate fact identity mismatch")

    for item in facts.values():
        if (
            item.get("purpose") != "ST"
            or item.get("dataset") != "trading_status"
            or item.get("security_code") != "603377"
            or item.get("security_name") != "ST 东时"
            or item.get("company_name") != "东方时尚驾驶学校股份有限公司"
            or item.get("announcement_number") != "临2026-110"
            or item.get("document_date") != "2026-07-30"
            or item.get("locator_publication_date") != "2026-07-31"
            or item.get("document_specific_locator") != CNINFO_LOCATOR
            or item.get("material_sha256") != EXPECTED_PDF_SHA
            or item.get("reviewer_decision") != "NEEDS_MORE_EVIDENCE"
        ):
            raise RuntimeError("P2F: candidate fact common lineage mismatch")

    identity = facts["ST-DOCUMENT-IDENTITY-001"]
    if identity.get("candidate_fact") != {
        "announcement_number": "临2026-110",
        "company_name": "东方时尚驾驶学校股份有限公司",
        "displayed_security_name": "ST 东时",
        "document_date": "2026-07-30",
        "security_code": "603377",
    } or _locator_projection(identity, "identity") != {
        "exact_paragraph_sha256": IDENTITY_PARAGRAPH_SHA,
        "pages": [1],
    }:
        raise RuntimeError("P2F: document identity binding mismatch")

    expected_status = {
        "ST-STATUS-20260323": (
            "2026-03-23",
            "《上海证券交易所股票上市规则》第9.8.1条第一款第（七）项",
            STATUS_0323_PARAGRAPH_SHA,
            [2],
        ),
        "ST-STATUS-20260418": (
            "2026-04-18",
            "《上海证券交易所股票上市规则》第9.8.1条第一款第（六）项",
            STATUS_0418_PARAGRAPH_SHA,
            [1, 2],
        ),
    }
    for fact_id, (effective_date, basis, paragraph_sha, pages) in expected_status.items():
        item = facts[fact_id]
        if (
            item.get("effective_date") != effective_date
            or item.get("candidate_fact") != {"basis": basis, "other_risk_warning": True}
            or _locator_projection(item)
            != {
                "exact_paragraph_sha256": paragraph_sha,
                "pages": pages,
            }
        ):
            raise RuntimeError("P2F: retrospective status binding mismatch")

    continuation = facts["ST-CONTINUES-20260730"]
    if continuation.get("candidate_fact") != {
        "displayed_security_name": "ST 东时",
        "other_risk_warning_continues": True,
    } or _locator_projection(continuation, "continuation") != {
        "exact_paragraph_sha256": CONTINUATION_PARAGRAPH_SHA,
        "pages": [2],
    }:
        raise RuntimeError("P2F: continuation binding mismatch")
    print("  P2F four approved fact inputs and paragraph locators verified")


def _locator_projection(item: dict, nested: str | None = None) -> dict[str, object]:
    locator = item.get("locator")
    if not isinstance(locator, dict):
        raise RuntimeError("P2F: fact locator is invalid")
    if nested is not None:
        locator = locator.get(nested)
        if not isinstance(locator, dict):
            raise RuntimeError("P2F: nested fact locator is invalid")
    return {
        "exact_paragraph_sha256": locator.get("exact_paragraph_sha256"),
        "pages": locator.get("pages"),
    }


def _verify_pdf() -> str:
    """Verify the PDF SHA-256 and return it."""
    pdf_path = P2F_ROOT / "materials" / "MATERIAL-ST-001" / "original.pdf"
    if not pdf_path.is_file():
        raise RuntimeError(f"PDF missing: {pdf_path}")
    payload = pdf_path.read_bytes()
    actual = compute_sha256(payload)
    if actual != EXPECTED_PDF_SHA:
        raise RuntimeError(f"PDF SHA mismatch: {actual} != {EXPECTED_PDF_SHA}")
    print(f"  PDF SHA verified ({actual[:16]}...)")
    return actual


def _build_draft(pdf_sha: str, p2b_sha: str, p2f_sha: str) -> AuthorityReferenceBundleDraft:
    sample = load_fixed_sample_v4()
    st_case = next(case for case in sample.cases if case.case_id == "st_warning_603377_sh")
    if st_case.purpose.value != "ST":
        raise RuntimeError("controlled ST case purpose mismatch")
    document = AuthorityReferenceDocument(
        reference_id="cninfo-603377-1225449700",
        authority="CNINFO primary disclosure",
        canonical_locator=CNINFO_LOCATOR,
        announcement_id="1225449700",
        announcement_number="临2026-110",
        security_code="603377.SH",
        security_name="ST 东时",
        company_name="东方时尚驾驶学校股份有限公司",
        document_date=TradingDate.from_iso("2026-07-30"),
        publication_date=TradingDate.from_iso("2026-07-31"),
        known_from_date=TradingDate.from_iso("2026-07-31"),
        material_sha256=pdf_sha,
        p2b_manifest_sha256=p2b_sha,
        p2f_manifest_sha256=p2f_sha,
    )
    identity = AuthorityReferenceFact(
        fact_id="st-document-identity-001",
        reference_id=document.reference_id,
        dataset=DatasetKind.TRADING_STATUS,
        purpose=st_case.purpose,
        code="603377.SH",
        exchange="SH",
        fact_nature=FactNature.IDENTITY,
        document_date=TradingDate.from_iso("2026-07-30"),
        publication_date=TradingDate.from_iso("2026-07-31"),
        known_from_date=TradingDate.from_iso("2026-07-31"),
        usable_for_decisions_after="2026-07-31 market close",
        material_sha256=pdf_sha,
        paragraph_sha256=IDENTITY_PARAGRAPH_SHA,
        pages=(1,),
        statement="证券代码：603377 证券简称：ST 东时 公告编号：临 2026-110",
    )
    retro_0323 = AuthorityReferenceFact(
        fact_id="st-status-20260323",
        reference_id=document.reference_id,
        dataset=DatasetKind.TRADING_STATUS,
        purpose=st_case.purpose,
        code="603377.SH",
        exchange="SH",
        fact_nature=FactNature.RETROSPECTIVE_CONFIRMATION,
        document_date=TradingDate.from_iso("2026-07-30"),
        publication_date=TradingDate.from_iso("2026-07-31"),
        known_from_date=TradingDate.from_iso("2026-07-31"),
        usable_for_decisions_after="2026-07-31 market close",
        material_sha256=pdf_sha,
        paragraph_sha256=STATUS_0323_PARAGRAPH_SHA,
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
        purpose=st_case.purpose,
        code="603377.SH",
        exchange="SH",
        fact_nature=FactNature.RETROSPECTIVE_CONFIRMATION,
        document_date=TradingDate.from_iso("2026-07-30"),
        publication_date=TradingDate.from_iso("2026-07-31"),
        known_from_date=TradingDate.from_iso("2026-07-31"),
        usable_for_decisions_after="2026-07-31 market close",
        material_sha256=pdf_sha,
        paragraph_sha256=STATUS_0418_PARAGRAPH_SHA,
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
        purpose=st_case.purpose,
        code="603377.SH",
        exchange="SH",
        fact_nature=FactNature.CONTINUATION,
        document_date=TradingDate.from_iso("2026-07-30"),
        publication_date=TradingDate.from_iso("2026-07-31"),
        known_from_date=TradingDate.from_iso("2026-07-31"),
        usable_for_decisions_after="2026-07-31 market close",
        material_sha256=pdf_sha,
        paragraph_sha256=CONTINUATION_PARAGRAPH_SHA,
        pages=(2,),
        statement=(
            "公司前期被实施其他风险警示的情形尚未全部消除，公司股票继续被实施其他风险警示。"
        ),
        other_risk_warning_continues=True,
    )
    facts = (identity, retro_0323, retro_0418, continuation)
    bindings = tuple(
        AuthorityCaseFactBinding(
            case_id="st_warning_603377_sh",
            fact_id=fact.fact_id,
            purpose=st_case.purpose,
        )
        for fact in facts
    )
    approval_statement = (
        "LOCAL_OWNER approves only the document identity, retrospective other-risk-warning "
        "confirmations effective 2026-03-23 and 2026-04-18, and continuation at publication "
        "for case st_warning_603377_sh; this approval is not REAL provider identity."
    )
    decision = ReviewDecision(
        decision_id="local-owner-approval-st-603377-v3",
        reviewer_role=ReviewerRole.LOCAL_OWNER,
        status=ReviewDecisionStatus.APPROVED,
        case_ids=("st_warning_603377_sh",),
        approval_statement=approval_statement,
        approval_statement_sha256=compute_sha256(
            _canonical({"approval_statement": approval_statement})
        ),
        decided_at=TradingDate.from_iso("2026-08-04"),
    )
    return AuthorityReferenceBundleDraft(
        bundle_id="st-603377-authority-v3",
        sample_spec_version=sample.sample_spec_version,
        sample_spec_sha256=sample.sample_spec_sha256,
        previous_sample_spec_sha256=sample.previous_sample_spec_sha256 or "",
        references=(document,),
        facts=facts,
        bindings=bindings,
        review_decisions=(decision,),
        code_version="s1-07b.authority-bundle.v3",
        schema_version="authority-bundle.v3",
    )


def main() -> None:
    print("=== S1-07B-FINAL Reference Bundle Builder ===")
    print()
    print("[1/4] Verifying P2B evidence...")
    _check_reparse(P2B_ROOT)
    p2b_manifest = _verify_manifest(P2B_ROOT, "P2B", EXPECTED_P2B_MANIFEST_SHA)
    _verify_evidence_inventory(P2B_ROOT, p2b_manifest, label="P2B")

    print()
    print("[2/4] Verifying P2F evidence...")
    _check_reparse(P2F_ROOT)
    p2f_manifest = _verify_manifest(P2F_ROOT, "P2F", EXPECTED_P2F_MANIFEST_SHA)
    _verify_evidence_inventory(
        P2F_ROOT,
        p2f_manifest,
        label="P2F",
        expected_inventory_sha=EXPECTED_P2F_INVENTORY_SHA,
    )
    _verify_p2f_fact_inputs()

    print()
    print("[3/4] Verifying PDF material...")
    pdf_sha = _verify_pdf()

    print()
    print("[4/4] Building reference bundle...")
    BUNDLE_ROOT.mkdir(parents=True, exist_ok=True)
    _check_reparse(BUNDLE_ROOT)
    draft = _build_draft(
        pdf_sha=pdf_sha,
        p2b_sha=EXPECTED_P2B_MANIFEST_SHA,
        p2f_sha=EXPECTED_P2F_MANIFEST_SHA,
    )
    writer = AuthorityReferenceBundleWriter(root=BUNDLE_ROOT)
    sample = load_fixed_sample_v4()
    bundle_dir = writer.write(draft, sample=sample)
    print(f"  Bundle written to: {bundle_dir}")

    # Deeply verify the written bundle. Reading its self-reported manifest is
    # not sufficient to establish case-level authority eligibility.
    from quantking.data.validation.authority_bundle import (
        AuthorityReferenceBundleVerifier,
    )

    verified = AuthorityReferenceBundleVerifier(root=BUNDLE_ROOT).verify(
        directory=bundle_dir,
        sample=sample,
    )
    manifest_path = bundle_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest_sha = manifest["manifest_sha256"]
    inventory_sha = manifest["reference_inventory_sha256"]
    print(f"  Bundle ID: {manifest['bundle_id']}")
    print(f"  Manifest SHA: {manifest_sha}")
    print(f"  Inventory SHA: {inventory_sha}")
    print(f"  Sample version: {manifest['sample_spec_version']}")
    print(f"  Files: {len(manifest['files'])}")
    print(f"  Approved cases: {sorted(verified.approved_case_ids)}")
    print()
    print("=== SUCCESS: Reference bundle built and verified ===")


if __name__ == "__main__":
    main()
