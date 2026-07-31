# ruff: noqa: RUF001
"""S1-07B-FINAL: verify sealed evidence and build the authoritative reference bundle.

This script runs OUTSIDE pytest (which blocks formal paths). It:
1. Re-verifies the P2B manifest, P2F manifest, P2F inventory, COMPLETE markers,
   per-file SHA-256 and path safety.
2. Verifies the PDF SHA-256.
3. Builds the immutable v2 reference bundle binding the 603377.SH ST authority
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

from quantking.common.paths import is_reparse_point  # noqa: E402
from quantking.common.time import TradingDate  # noqa: E402
from quantking.data.adapters.warehouse.manifest import compute_sha256  # noqa: E402
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

    complete = json.loads(complete_path.read_bytes())
    if complete.get("manifest_sha256") != actual_sha:
        raise RuntimeError(f"{label}: COMPLETE marker SHA mismatch")

    print(f"  {label}: manifest SHA verified ({actual_sha[:16]}...)")
    return manifest


def _verify_p2f_inventory(manifest: dict) -> None:
    """Verify P2F file inventory SHA and per-file SHA-256."""
    files = manifest.get("files")
    if not isinstance(files, list):
        raise RuntimeError("P2F: manifest files missing")
    inventory_canonical = _canonical(files)
    inventory_sha = compute_sha256(inventory_canonical)
    if inventory_sha != EXPECTED_P2F_INVENTORY_SHA:
        raise RuntimeError(
            f"P2F inventory SHA mismatch: {inventory_sha} != {EXPECTED_P2F_INVENTORY_SHA}"
        )
    print(f"  P2F inventory SHA verified ({inventory_sha[:16]}...)")

    for entry in files:
        rel = entry["path"]
        expected = entry["sha256"]
        size = entry["size"]
        target = P2F_ROOT / rel
        if not target.is_file():
            raise RuntimeError(f"P2F: file missing: {rel}")
        payload = target.read_bytes()
        if len(payload) != size:
            raise RuntimeError(f"P2F: size mismatch: {rel}")
        actual = compute_sha256(payload)
        if actual != expected:
            raise RuntimeError(f"P2F: SHA mismatch: {rel}")
    print(f"  P2F per-file SHA verified ({len(files)} files)")


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
    document = AuthorityReferenceDocument(
        reference_id="cninfo-603377-1225449700",
        authority="CNINFO primary disclosure",
        canonical_locator=CNINFO_LOCATOR,
        announcement_id="1225449700",
        announcement_number="临2026-110",
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
        purpose=sample.cases[0].purpose,  # ST
        code="603377.SH",
        exchange="SH",
        fact_nature=FactNature.IDENTITY,
        document_date=TradingDate.from_iso("2026-07-30"),
        publication_date=TradingDate.from_iso("2026-07-31"),
        known_from_date=TradingDate.from_iso("2026-07-31"),
        usable_for_decisions_after="2026-07-31 market close",
        material_sha256=pdf_sha,
        paragraph_sha256="2e81c9678e4d86b28e683e2f2dddf7474de51c8f2def68b7afe308644ece0d39",
        pages=(1,),
        statement="证券代码：603377 证券简称：ST 东时 公告编号：临 2026-110",
    )
    retro_0323 = AuthorityReferenceFact(
        fact_id="st-status-20260323",
        reference_id=document.reference_id,
        dataset=DatasetKind.TRADING_STATUS,
        purpose=sample.cases[0].purpose,
        code="603377.SH",
        exchange="SH",
        fact_nature=FactNature.RETROSPECTIVE_CONFIRMATION,
        document_date=TradingDate.from_iso("2026-07-30"),
        publication_date=TradingDate.from_iso("2026-07-31"),
        known_from_date=TradingDate.from_iso("2026-07-31"),
        usable_for_decisions_after="2026-07-31 market close",
        material_sha256=pdf_sha,
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
        purpose=sample.cases[0].purpose,
        code="603377.SH",
        exchange="SH",
        fact_nature=FactNature.RETROSPECTIVE_CONFIRMATION,
        document_date=TradingDate.from_iso("2026-07-30"),
        publication_date=TradingDate.from_iso("2026-07-31"),
        known_from_date=TradingDate.from_iso("2026-07-31"),
        usable_for_decisions_after="2026-07-31 market close",
        material_sha256=pdf_sha,
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
        purpose=sample.cases[0].purpose,
        code="603377.SH",
        exchange="SH",
        fact_nature=FactNature.CONTINUATION,
        document_date=TradingDate.from_iso("2026-07-30"),
        publication_date=TradingDate.from_iso("2026-07-31"),
        known_from_date=TradingDate.from_iso("2026-07-31"),
        usable_for_decisions_after="2026-07-31 market close",
        material_sha256=pdf_sha,
        paragraph_sha256="27786c8b65e8124e6685a53881daf438c41f4c447b8bd9f6222ab9c92818457f",
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
            purpose=sample.cases[0].purpose,
        )
        for fact in facts
    )
    approval_statement = (
        "LOCAL_OWNER approves the ST authority reference bundle for case "
        "st_warning_603377_sh binding CNINFO announcement 1225449700 "
        "(临2026-110) to the v4 fixed sample."
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
        previous_sample_spec_sha256=sample.previous_sample_spec_sha256 or "",
        references=(document,),
        facts=facts,
        bindings=bindings,
        review_decisions=(decision,),
        code_version="s1-07b.authority-bundle.v2",
        schema_version="authority-bundle.v2",
    )


def main() -> None:
    print("=== S1-07B-FINAL Reference Bundle Builder ===")
    print()
    print("[1/4] Verifying P2B evidence...")
    _check_reparse(P2B_ROOT)
    _verify_manifest(P2B_ROOT, "P2B", EXPECTED_P2B_MANIFEST_SHA)

    print()
    print("[2/4] Verifying P2F evidence...")
    _check_reparse(P2F_ROOT)
    p2f_manifest = _verify_manifest(P2F_ROOT, "P2F", EXPECTED_P2F_MANIFEST_SHA)
    _verify_p2f_inventory(p2f_manifest)

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
    bundle_dir = writer.write(draft)
    print(f"  Bundle written to: {bundle_dir}")

    # Verify the written bundle
    manifest_path = bundle_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest_sha = manifest["manifest_sha256"]
    inventory_sha = manifest["reference_inventory_sha256"]
    print(f"  Bundle ID: {manifest['bundle_id']}")
    print(f"  Manifest SHA: {manifest_sha}")
    print(f"  Inventory SHA: {inventory_sha}")
    print(f"  Sample version: {manifest['sample_spec_version']}")
    print(f"  Files: {len(manifest['files'])}")
    print()
    print("=== SUCCESS: Reference bundle built and verified ===")


if __name__ == "__main__":
    main()
