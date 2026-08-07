"""S1-07B-FINAL authoritative reference bundle with time semantics.

This module implements the v3 authority bundle contract that binds the
603377.SH ST announcement to the v4 fixed sample. It is distinct from the
v1 ``references.py`` synthetic test bundle: v3 carries document/publication/
known-from dates, retrospective confirmation semantics, LOCAL_OWNER review
decisions, and per-case eligibility. It never produces a sample-level
``PRODUCTION_ELIGIBLE`` verdict and never impersonates a REAL provider signer.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit
from uuid import uuid4

from quantking.common.paths import is_reparse_point, resolve_under
from quantking.common.time import TradingDate
from quantking.data.adapters.warehouse.manifest import (
    compute_sha256,
    validate_managed_relative,
)
from quantking.data.models import DatasetKind
from quantking.data.validation.samples import (
    CaseEligibility,
    FixedSampleSpec,
    SamplePurpose,
)

_ID = re.compile(r"^[a-z][a-z0-9-]{2,95}$")
_CASE_ID = re.compile(r"^[a-z][a-z0-9_-]{2,95}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PAGE = re.compile(r"^[1-9][0-9]*$")
_SCHEMA = "authority-bundle.manifest.v3"
_SECURITY_CODE = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")
_USABLE_BOUNDARY = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2} market close$")
AUTHORITY_TRUST_RELEASE_BYTES_SHA256 = (
    "c2ec1e5c0c600dd6bbef35db893b952dec3ba541343c306523907ff75b72a05b"
)
CONTROLLED_AUTHORITY_BUNDLE_ID = "st-603377-authority-v3"
CONTROLLED_AUTHORITY_BUNDLE_MANIFEST_SHA256 = (
    "94ac8da21586a13803058b80344ff1b8dd63633c091e90b8cba832b5ef6a0bea"
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _load_controlled_authority_release() -> dict[str, object]:
    """Load the sole Git-controlled LOCAL_OWNER trust anchor for S1-07B."""
    payload = (
        files("quantking.data.validation").joinpath("authority_bundle.release.v1.json").read_bytes()
    )
    if compute_sha256(payload) != AUTHORITY_TRUST_RELEASE_BYTES_SHA256:
        raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_TRUST_RELEASE_INVALID")
    try:
        release = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_TRUST_RELEASE_INVALID") from None
    if not isinstance(release, dict) or set(release) != {
        "approval",
        "bundle_id",
        "bundle_manifest_sha256",
        "facts",
        "reference",
        "reference_inventory_sha256",
        "sample",
        "schema_version",
    }:
        raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_TRUST_RELEASE_INVALID")
    return release


class FactNature(StrEnum):
    """How a fact relates to the material that reports it."""

    IDENTITY = "IDENTITY"
    RETROSPECTIVE_CONFIRMATION = "RETROSPECTIVE_CONFIRMATION"
    CONTINUATION = "CONTINUATION"


class ReviewerRole(StrEnum):
    """Who approved the bundle. LOCAL_OWNER is the only S1-07B role.

    LOCAL_OWNER approval proves the user reviewed the fixed-sample facts; it
    is independent from REAL provider identity and never impersonates a signer.
    """

    LOCAL_OWNER = "LOCAL_OWNER"


class ReviewDecisionStatus(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class AuthorityReferenceDocument:
    """CNINFO document-specific reference binding P2B/P2F manifest SHAs."""

    reference_id: str
    authority: str
    canonical_locator: str
    announcement_id: str
    announcement_number: str
    security_code: str
    security_name: str
    company_name: str
    document_date: TradingDate
    publication_date: TradingDate
    known_from_date: TradingDate
    material_sha256: str
    p2b_manifest_sha256: str
    p2f_manifest_sha256: str

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.reference_id):
            raise ValueError("reference_id must be a stable identifier")
        if not self.authority.strip():
            raise ValueError("authority must be non-empty")
        locator = urlsplit(self.canonical_locator)
        if (
            locator.scheme != "https"
            or not locator.netloc
            or locator.path in {"", "/"}
            or "announcementId=" not in (locator.query or "")
        ):
            raise ValueError("canonical locator must be HTTPS and announcement-specific")
        if not self.announcement_id.strip() or not self.announcement_number.strip():
            raise ValueError("announcement_id and announcement_number are required")
        if not _SECURITY_CODE.fullmatch(self.security_code):
            raise ValueError("security_code must be a controlled exchange-qualified code")
        if not self.security_name.strip() or not self.company_name.strip():
            raise ValueError("security_name and company_name are required")
        for label, value in (
            ("document_date", self.document_date),
            ("publication_date", self.publication_date),
            ("known_from_date", self.known_from_date),
        ):
            if not isinstance(value, TradingDate):
                raise TypeError(f"{label} must be a TradingDate")
        if self.publication_date.value < self.document_date.value:
            raise ValueError("publication_date must not precede document_date")
        if self.known_from_date.value < self.publication_date.value:
            raise ValueError("known_from_date must not precede publication_date")
        for sha_label, sha_value in (
            ("material_sha256", self.material_sha256),
            ("p2b_manifest_sha256", self.p2b_manifest_sha256),
            ("p2f_manifest_sha256", self.p2f_manifest_sha256),
        ):
            if not _HEX64.fullmatch(sha_value):
                raise ValueError(f"{sha_label} must be a lowercase SHA-256")

    def to_dict(self) -> dict[str, str]:
        return {
            "announcement_id": self.announcement_id,
            "announcement_number": self.announcement_number,
            "authority": self.authority,
            "canonical_locator": self.canonical_locator,
            "company_name": self.company_name,
            "document_date": str(self.document_date),
            "known_from_date": str(self.known_from_date),
            "material_sha256": self.material_sha256,
            "p2b_manifest_sha256": self.p2b_manifest_sha256,
            "p2f_manifest_sha256": self.p2f_manifest_sha256,
            "publication_date": str(self.publication_date),
            "reference_id": self.reference_id,
            "security_code": self.security_code,
            "security_name": self.security_name,
        }


@dataclass(frozen=True, slots=True)
class AuthorityReferenceFact:
    """Time-semantic fact bound to an authority document and a sample case."""

    fact_id: str
    reference_id: str
    dataset: DatasetKind
    purpose: SamplePurpose
    code: str
    exchange: str
    fact_nature: FactNature
    document_date: TradingDate
    publication_date: TradingDate
    known_from_date: TradingDate
    usable_for_decisions_after: str
    material_sha256: str
    paragraph_sha256: str
    pages: tuple[int, ...]
    statement: str
    effective_date: TradingDate | None = None
    other_risk_warning: bool | None = None
    other_risk_warning_continues: bool | None = None

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.fact_id) or not _ID.fullmatch(self.reference_id):
            raise ValueError("fact and reference IDs must be stable identifiers")
        if not isinstance(self.dataset, DatasetKind) or not isinstance(self.purpose, SamplePurpose):
            raise TypeError("fact dataset and purpose must use controlled enums")
        if not isinstance(self.fact_nature, FactNature):
            raise TypeError("fact_nature must be a FactNature")
        if self.exchange not in {"SH", "SZ", "BJ"}:
            raise ValueError("fact exchange must be SH, SZ or BJ")
        if not self.code.endswith(f".{self.exchange}"):
            raise ValueError("fact code suffix must match exchange")
        for label, value in (
            ("document_date", self.document_date),
            ("publication_date", self.publication_date),
            ("known_from_date", self.known_from_date),
        ):
            if not isinstance(value, TradingDate):
                raise TypeError(f"{label} must be a TradingDate")
        if self.publication_date.value < self.document_date.value:
            raise ValueError("publication_date must not precede document_date")
        if self.known_from_date.value < self.publication_date.value:
            raise ValueError("known_from_date must not precede publication_date")
        if self.effective_date is not None:
            if not isinstance(self.effective_date, TradingDate):
                raise TypeError("effective_date must be a TradingDate or None")
            if self.effective_date.value > self.document_date.value:
                raise ValueError("retrospective effective_date must not follow document_date")
        if not _USABLE_BOUNDARY.fullmatch(self.usable_for_decisions_after):
            raise ValueError("usable_for_decisions_after must be '<date> market close'")
        usable_date = TradingDate.from_iso(self.usable_for_decisions_after.split(" ", 1)[0])
        if usable_date.value < self.known_from_date.value:
            raise ValueError("usable_for_decisions_after must not precede known_from_date")
        for sha_label, sha_value in (
            ("material_sha256", self.material_sha256),
            ("paragraph_sha256", self.paragraph_sha256),
        ):
            if not _HEX64.fullmatch(sha_value):
                raise ValueError(f"{sha_label} must be a lowercase SHA-256")
        pages = tuple(sorted(set(self.pages)))
        if not pages or any(not _PAGE.fullmatch(str(page)) for page in pages):
            raise ValueError("pages must be a non-empty set of positive integers")
        object.__setattr__(self, "pages", pages)
        if not self.statement.strip() or len(self.statement) > 800:
            raise ValueError("statement must be non-empty and bounded")
        if self.fact_nature is FactNature.RETROSPECTIVE_CONFIRMATION:
            if self.effective_date is None or self.other_risk_warning is None:
                raise ValueError(
                    "RETROSPECTIVE_CONFIRMATION requires effective_date and other_risk_warning"
                )
            if self.other_risk_warning_continues is not None:
                raise ValueError(
                    "RETROSPECTIVE_CONFIRMATION must not set other_risk_warning_continues"
                )
        elif self.fact_nature is FactNature.CONTINUATION:
            if self.other_risk_warning_continues is None:
                raise ValueError("CONTINUATION requires other_risk_warning_continues")
            if self.effective_date is not None or self.other_risk_warning is not None:
                raise ValueError("CONTINUATION must not set effective_date or other_risk_warning")
        elif self.fact_nature is FactNature.IDENTITY:
            if (
                self.effective_date is not None
                or self.other_risk_warning is not None
                or self.other_risk_warning_continues is not None
            ):
                raise ValueError("IDENTITY must not set effective_date or risk-warning flags")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "dataset": self.dataset.value,
            "document_date": str(self.document_date),
            "exchange": self.exchange,
            "fact_id": self.fact_id,
            "fact_nature": self.fact_nature.value,
            "known_from_date": str(self.known_from_date),
            "material_sha256": self.material_sha256,
            "pages": list(self.pages),
            "paragraph_sha256": self.paragraph_sha256,
            "publication_date": str(self.publication_date),
            "purpose": self.purpose.value,
            "reference_id": self.reference_id,
            "statement": self.statement,
            "usable_for_decisions_after": self.usable_for_decisions_after,
        }
        if self.effective_date is not None:
            payload["effective_date"] = str(self.effective_date)
        if self.other_risk_warning is not None:
            payload["other_risk_warning"] = self.other_risk_warning
        if self.other_risk_warning_continues is not None:
            payload["other_risk_warning_continues"] = self.other_risk_warning_continues
        return payload


@dataclass(frozen=True, slots=True)
class AuthorityCaseFactBinding:
    case_id: str
    fact_id: str
    purpose: SamplePurpose

    def __post_init__(self) -> None:
        if not _CASE_ID.fullmatch(self.case_id) or not _ID.fullmatch(self.fact_id):
            raise ValueError("binding IDs must be stable identifiers")
        if not isinstance(self.purpose, SamplePurpose):
            raise TypeError("binding purpose must be a SamplePurpose")

    def to_dict(self) -> dict[str, str]:
        return {
            "case_id": self.case_id,
            "fact_id": self.fact_id,
            "purpose": self.purpose.value,
        }


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """LOCAL_OWNER approval record. Never a REAL provider signer."""

    decision_id: str
    reviewer_role: ReviewerRole
    status: ReviewDecisionStatus
    case_ids: tuple[str, ...]
    approval_statement: str
    approval_statement_sha256: str
    decided_at: TradingDate

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.decision_id):
            raise ValueError("decision_id must be a stable identifier")
        if not isinstance(self.reviewer_role, ReviewerRole):
            raise TypeError("reviewer_role must be a ReviewerRole")
        if not isinstance(self.status, ReviewDecisionStatus):
            raise TypeError("status must be a ReviewDecisionStatus")
        case_ids = tuple(sorted(set(self.case_ids)))
        if not case_ids or any(not _CASE_ID.fullmatch(case_id) for case_id in case_ids):
            raise ValueError("case_ids must be a non-empty set of stable identifiers")
        object.__setattr__(self, "case_ids", case_ids)
        if not self.approval_statement.strip() or len(self.approval_statement) > 800:
            raise ValueError("approval_statement must be non-empty and bounded")
        if not _HEX64.fullmatch(self.approval_statement_sha256):
            raise ValueError("approval_statement_sha256 must be a lowercase SHA-256")
        if (
            compute_sha256(_canonical({"approval_statement": self.approval_statement}))
            != self.approval_statement_sha256
        ):
            raise ValueError("approval_statement_sha256 must bind the canonical approval statement")
        if not isinstance(self.decided_at, TradingDate):
            raise TypeError("decided_at must be a TradingDate")

    def to_dict(self) -> dict[str, object]:
        return {
            "approval_statement": self.approval_statement,
            "approval_statement_sha256": self.approval_statement_sha256,
            "case_ids": list(self.case_ids),
            "decided_at": str(self.decided_at),
            "decision_id": self.decision_id,
            "reviewer_role": self.reviewer_role.value,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class AuthorityReferenceBundleDraft:
    bundle_id: str
    sample_spec_version: str
    sample_spec_sha256: str
    previous_sample_spec_sha256: str
    references: tuple[AuthorityReferenceDocument, ...]
    facts: tuple[AuthorityReferenceFact, ...]
    bindings: tuple[AuthorityCaseFactBinding, ...]
    review_decisions: tuple[ReviewDecision, ...]
    code_version: str
    schema_version: str

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.bundle_id):
            raise ValueError("bundle_id must be a stable identifier")
        if not _VERSION.fullmatch(self.sample_spec_version):
            raise ValueError("sample_spec_version is invalid")
        for digest in (self.sample_spec_sha256, self.previous_sample_spec_sha256):
            if not _HEX64.fullmatch(digest):
                raise ValueError("sample lineage SHA is invalid")
        for field_name in ("code_version", "schema_version"):
            if not _VERSION.fullmatch(getattr(self, field_name)):
                raise ValueError(f"{field_name} is invalid")
        for values, label, attribute in (
            (self.references, "reference", "reference_id"),
            (self.facts, "fact", "fact_id"),
            (self.review_decisions, "decision", "decision_id"),
        ):
            identifiers = [getattr(item, attribute) for item in values]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"duplicate {label} ID")
        binding_pairs = [(item.case_id, item.fact_id) for item in self.bindings]
        if len(binding_pairs) != len(set(binding_pairs)):
            raise ValueError("duplicate case/fact binding")
        if not self.references or not self.facts or not self.bindings:
            raise ValueError("authority bundle must declare references, facts and bindings")
        if not self.review_decisions:
            raise ValueError("authority bundle must declare at least one review decision")

    def validate_against(self, sample: FixedSampleSpec) -> None:
        if (
            self.sample_spec_version != sample.sample_spec_version
            or self.sample_spec_sha256 != sample.sample_spec_sha256
            or self.previous_sample_spec_sha256 != sample.previous_sample_spec_sha256
        ):
            raise ValueError("authority bundle does not bind the controlled sample")
        references = {item.reference_id: item for item in self.references}
        facts = {item.fact_id: item for item in self.facts}
        cases = {item.case_id: item for item in sample.cases}
        if any(item.reference_id not in references for item in self.facts):
            raise ValueError("fact references an undeclared authority document")
        if {item.reference_id for item in self.facts} != set(references):
            raise ValueError("every authority document must support a declared fact")
        for reference_fact in self.facts:
            reference = references[reference_fact.reference_id]
            if (
                reference_fact.code != reference.security_code
                or reference_fact.material_sha256 != reference.material_sha256
                or reference_fact.document_date != reference.document_date
                or reference_fact.publication_date != reference.publication_date
                or reference_fact.known_from_date != reference.known_from_date
            ):
                raise ValueError("fact lineage does not align with its authority document")
        for binding in self.bindings:
            case = cases.get(binding.case_id)
            fact = facts.get(binding.fact_id)
            if case is None or fact is None:
                raise ValueError("binding references an undeclared case or fact")
            if (
                fact.dataset is not case.dataset
                or fact.purpose is not case.purpose
                or fact.code != case.code
                or fact.exchange != case.exchange
                or binding.purpose is not case.purpose
            ):
                raise ValueError("binding does not align with the fixed sample case")
            if fact.effective_date is not None and not (
                case.start_date.value <= fact.effective_date.value <= case.end_date.value
            ):
                raise ValueError("fact effective_date is outside the case date range")
        approved_scope: set[str] = set()
        for decision in self.review_decisions:
            if decision.status is ReviewDecisionStatus.APPROVED:
                approved_scope.update(decision.case_ids)
        bound_cases = {item.case_id for item in self.bindings}
        if not bound_cases.issubset(approved_scope):
            raise ValueError("every bound case must be covered by an APPROVED review decision")

    def unsigned_manifest(self) -> dict[str, object]:
        inventory = [
            {
                "relative_path": relative,
                "sha256": compute_sha256(payload),
                "size": len(payload),
            }
            for relative, payload in sorted(self._payload_map().items())
        ]
        return {
            "bundle_id": self.bundle_id,
            "code_version": self.code_version,
            "files": inventory,
            "manifest_schema_version": _SCHEMA,
            "previous_sample_spec_sha256": self.previous_sample_spec_sha256,
            "reference_inventory_sha256": compute_sha256(_canonical(inventory)),
            "review_decisions": [
                decision.to_dict()
                for decision in sorted(self.review_decisions, key=lambda d: d.decision_id)
            ],
            "sample_spec_sha256": self.sample_spec_sha256,
            "sample_spec_version": self.sample_spec_version,
            "schema_version": self.schema_version,
        }

    def _payload_map(self) -> dict[str, bytes]:
        files: dict[str, bytes] = {
            "bindings.json": _canonical(
                [
                    item.to_dict()
                    for item in sorted(self.bindings, key=lambda item: (item.case_id, item.fact_id))
                ]
            ),
            "facts.json": _canonical(
                [item.to_dict() for item in sorted(self.facts, key=lambda item: item.fact_id)]
            ),
            "review_decisions.json": _canonical(
                [
                    item.to_dict()
                    for item in sorted(self.review_decisions, key=lambda item: item.decision_id)
                ]
            ),
        }
        for reference in self.references:
            files[f"references/{reference.reference_id}.json"] = _canonical(reference.to_dict())
        return files


@dataclass(frozen=True, slots=True)
class VerifiedAuthorityReferenceBundle:
    bundle_id: str
    manifest_sha256: str
    sample_spec_version: str
    sample_spec_sha256: str
    previous_sample_spec_sha256: str
    reference_inventory_sha256: str
    approved_case_ids: frozenset[str]
    review_decision_status: ReviewDecisionStatus
    case_eligibilities: Mapping[str, CaseEligibility] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _StructurallyVerifiedAuthorityBundle:
    draft: AuthorityReferenceBundleDraft
    manifest_sha256: str
    reference_inventory_sha256: str


class AuthorityBundleVerificationError(Exception):
    """Stable, sanitized authority-bundle verification failure."""


class AuthorityBundleIdempotencyConflictError(Exception):
    """Same bundle_id already exists with different content."""


class AuthorityReferenceBundleWriter:
    def __init__(self, *, root: Path) -> None:
        if not root.is_absolute():
            raise ValueError("reference root must be absolute")
        self._root = root

    def write(
        self,
        draft: AuthorityReferenceBundleDraft,
        *,
        sample: FixedSampleSpec,
    ) -> Path:
        parent = resolve_under(self._root, "reference-bundles")
        parent.mkdir(parents=True, exist_ok=True)
        if is_reparse_point(parent):
            raise ValueError("reference root is unsafe")
        attempt_id = uuid4().hex
        final = parent / draft.bundle_id
        incomplete = parent / f".{draft.bundle_id}.{attempt_id}.incomplete"
        try:
            draft.validate_against(sample)
            self._write_draft(incomplete, draft)
            AuthorityReferenceBundleVerifier(root=self._root).verify_structure(
                directory=incomplete,
                sample=sample,
            )
            if final.exists():
                self._verify_idempotent(final, draft)
                AuthorityReferenceBundleVerifier(root=self._root).verify_structure(
                    directory=final,
                    sample=sample,
                )
                _remove_success_attempt(incomplete)
                return final
            try:
                os.rename(incomplete, final)
            except OSError:
                if not final.exists():
                    raise
                self._verify_idempotent(final, draft)
                AuthorityReferenceBundleVerifier(root=self._root).verify_structure(
                    directory=final,
                    sample=sample,
                )
                _remove_success_attempt(incomplete)
        except Exception as error:
            _preserve_failed_attempt(
                incomplete,
                draft=draft,
                attempt_id=attempt_id,
                error=error,
            )
            raise
        return final

    def _write_draft(self, incomplete: Path, draft: AuthorityReferenceBundleDraft) -> None:
        (incomplete / "references").mkdir(parents=True)
        payload_map = draft._payload_map()
        for relative, payload in payload_map.items():
            target = incomplete / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        unsigned = draft.unsigned_manifest()
        manifest_sha = compute_sha256(_canonical(unsigned))
        manifest_payload = {**unsigned, "manifest_sha256": manifest_sha}
        (incomplete / "manifest.json").write_bytes(_canonical(manifest_payload))
        (incomplete / "manifest.sha256").write_bytes(f"{manifest_sha}  manifest.json\n".encode())
        (incomplete / "COMPLETE").write_bytes(_canonical({"manifest_sha256": manifest_sha}))

    def _verify_idempotent(self, existing: Path, draft: AuthorityReferenceBundleDraft) -> None:
        try:
            manifest_bytes = (existing / "manifest.json").read_bytes()
            manifest = json.loads(manifest_bytes)
            if (
                not isinstance(manifest, dict)
                or manifest.get("bundle_id") != draft.bundle_id
                or manifest.get("sample_spec_sha256") != draft.sample_spec_sha256
                or manifest.get("sample_spec_version") != draft.sample_spec_version
                or manifest.get("previous_sample_spec_sha256") != draft.previous_sample_spec_sha256
            ):
                raise ValueError("bundle identity mismatch")
            existing_unsigned = dict(manifest)
            existing_unsigned.pop("manifest_sha256", None)
            expected_unsigned = draft.unsigned_manifest()
            if existing_unsigned != expected_unsigned:
                raise ValueError("bundle content mismatch")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise AuthorityBundleIdempotencyConflictError(
                "authority bundle ID is immutable and content conflicts"
            ) from error


def _remove_success_attempt(incomplete: Path) -> None:
    if not incomplete.exists():
        return
    for item in incomplete.rglob("*"):
        if item.is_file():
            item.unlink(missing_ok=True)
    for item in sorted((path for path in incomplete.rglob("*") if path.is_dir()), reverse=True):
        item.rmdir()
    incomplete.rmdir()


def _preserve_failed_attempt(
    incomplete: Path,
    *,
    draft: AuthorityReferenceBundleDraft,
    attempt_id: str,
    error: Exception,
) -> None:
    """Preserve a bounded, sanitized, non-overwriting failed publication attempt."""
    incomplete.mkdir(parents=True, exist_ok=True)
    input_inventory = {
        "bundle_id": draft.bundle_id,
        "files": draft.unsigned_manifest()["files"],
        "sample_sha256": draft.sample_spec_sha256,
        "schema_version": "authority-bundle-failed-input.v1",
    }
    inventory_target = incomplete / "input_inventory.json"
    if not inventory_target.exists():
        inventory_descriptor = os.open(
            inventory_target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            os.write(inventory_descriptor, _canonical(input_inventory))
            os.fsync(inventory_descriptor)
        finally:
            os.close(inventory_descriptor)
    code = str(error)
    if not code.startswith("AUTHORITY_BUNDLE_"):
        code = "AUTHORITY_BUNDLE_PUBLICATION_FAILED"
    payload = {
        "attempt_id": attempt_id,
        "bundle_id": draft.bundle_id,
        "error_code": code,
        "failed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "sample_sha256": draft.sample_spec_sha256,
        "schema_version": "authority-bundle-failure.v1",
    }
    target = incomplete / "failure.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, _canonical(payload))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class AuthorityReferenceBundleVerifier:
    """Verifies v3 authority bundles and returns per-case eligibility.

    The verifier never promotes a sample to ``PRODUCTION_ELIGIBLE`` and never
    treats LOCAL_OWNER approval as a REAL provider signer. It only certifies
    that bound cases carry complete, locally approved authority evidence.
    """

    def __init__(self, *, root: Path, record_failures: bool = True) -> None:
        if not root.is_absolute():
            raise ValueError("reference root must be absolute")
        if not isinstance(record_failures, bool):
            raise TypeError("record_failures must be a bool")
        self._root = root
        self._record_failures = record_failures

    def verify(
        self,
        *,
        directory: Path,
        sample: FixedSampleSpec,
    ) -> VerifiedAuthorityReferenceBundle:
        try:
            structural = self.verify_structure(directory=directory, sample=sample)
            self._verify_controlled_trust(structural)
            draft = structural.draft
            approved_case_ids = {
                case_id
                for decision in draft.review_decisions
                if decision.status is ReviewDecisionStatus.APPROVED
                for case_id in decision.case_ids
            }
            bound_cases = {item.case_id for item in draft.bindings}
            case_eligibilities = {
                case.case_id: (
                    CaseEligibility.AUTHORITY_APPROVED
                    if case.case_id in bound_cases and case.case_id in approved_case_ids
                    else CaseEligibility.BLOCKED_REFERENCE
                    if case.case_id in bound_cases
                    else CaseEligibility.FRAMEWORK_ONLY
                )
                for case in sample.cases
            }
            return VerifiedAuthorityReferenceBundle(
                bundle_id=draft.bundle_id,
                manifest_sha256=structural.manifest_sha256,
                sample_spec_version=draft.sample_spec_version,
                sample_spec_sha256=draft.sample_spec_sha256,
                previous_sample_spec_sha256=draft.previous_sample_spec_sha256,
                reference_inventory_sha256=structural.reference_inventory_sha256,
                approved_case_ids=frozenset(approved_case_ids & bound_cases),
                review_decision_status=ReviewDecisionStatus.APPROVED,
                case_eligibilities=case_eligibilities,
            )
        except AuthorityBundleVerificationError as error:
            if self._record_failures:
                self._record_failure(str(error))
            raise
        except Exception:
            if self._record_failures:
                self._record_failure("AUTHORITY_BUNDLE_INVALID")
            raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_INVALID") from None

    def verify_structure(
        self, *, directory: Path, sample: FixedSampleSpec
    ) -> _StructurallyVerifiedAuthorityBundle:
        try:
            relative = directory.relative_to(self._root)
            resolved = resolve_under(self._root, relative)
        except Exception:
            raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_OUTSIDE_ROOT") from None
        if resolved != directory.resolve(strict=False) or not directory.is_dir():
            raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_UNSAFE")
        for item in (directory, *directory.rglob("*")):
            if item.is_symlink() or is_reparse_point(item):
                raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_UNSAFE")
        manifest_bytes = (directory / "manifest.json").read_bytes()
        complete_bytes = (directory / "COMPLETE").read_bytes()
        manifest_sha_file = (directory / "manifest.sha256").read_bytes()
        manifest = json.loads(manifest_bytes)
        complete = json.loads(complete_bytes)
        if (
            not isinstance(manifest, dict)
            or manifest_bytes != _canonical(manifest)
            or not isinstance(complete, dict)
            or complete_bytes != _canonical(complete)
        ):
            raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_NOT_CANONICAL")
        unsigned = dict(manifest)
        embedded = unsigned.pop("manifest_sha256", None)
        required = {
            "bundle_id",
            "code_version",
            "files",
            "manifest_schema_version",
            "manifest_sha256",
            "previous_sample_spec_sha256",
            "reference_inventory_sha256",
            "review_decisions",
            "sample_spec_sha256",
            "sample_spec_version",
            "schema_version",
        }
        if (
            set(manifest) != required
            or manifest["manifest_schema_version"] != _SCHEMA
            or not isinstance(embedded, str)
            or compute_sha256(_canonical(unsigned)) != embedded
            or complete != {"manifest_sha256": embedded}
            or manifest_sha_file != f"{embedded}  manifest.json\n".encode()
        ):
            raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_MANIFEST_MISMATCH")
        if (
            manifest["sample_spec_version"] != sample.sample_spec_version
            or manifest["sample_spec_sha256"] != sample.sample_spec_sha256
            or manifest["previous_sample_spec_sha256"] != sample.previous_sample_spec_sha256
        ):
            raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_SAMPLE_MISMATCH")
        raw_files = manifest["files"]
        if not isinstance(raw_files, list):
            raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_INVENTORY_INVALID")
        payloads: dict[str, bytes] = {}
        for entry in raw_files:
            if not isinstance(entry, dict) or set(entry) != {
                "relative_path",
                "sha256",
                "size",
            }:
                raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_INVENTORY_INVALID")
            relative_path = str(entry["relative_path"])
            try:
                validate_managed_relative(relative_path, field="authority bundle file")
                target = resolve_under(directory, relative_path)
            except Exception:
                raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_UNSAFE") from None
            if relative_path in payloads or target.is_symlink() or not target.is_file():
                raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_FILE_MISMATCH")
            payload = target.read_bytes()
            if len(payload) != entry["size"] or compute_sha256(payload) != entry["sha256"]:
                raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_FILE_MISMATCH")
            payloads[relative_path] = payload
        expected = {*payloads, "manifest.json", "manifest.sha256", "COMPLETE"}
        actual = {
            item.relative_to(directory).as_posix()
            for item in directory.rglob("*")
            if item.is_file()
        }
        actual_directories = {
            item.relative_to(directory).as_posix() for item in directory.rglob("*") if item.is_dir()
        }
        if (
            actual != expected
            or actual_directories != {"references"}
            or manifest["reference_inventory_sha256"] != compute_sha256(_canonical(raw_files))
        ):
            raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_FILE_SET_MISMATCH")
        draft = _draft_from_verified_payloads(manifest, payloads)
        try:
            draft.validate_against(sample)
        except ValueError:
            raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_FACT_MISMATCH") from None
        return _StructurallyVerifiedAuthorityBundle(
            draft=draft,
            manifest_sha256=embedded,
            reference_inventory_sha256=str(manifest["reference_inventory_sha256"]),
        )

    def _verify_controlled_trust(self, verified: _StructurallyVerifiedAuthorityBundle) -> None:
        try:
            release = _load_controlled_authority_release()
            release_facts = release["facts"]
            if not isinstance(release_facts, list) or any(
                not isinstance(item, dict) for item in release_facts
            ):
                raise ValueError
            release["facts"] = sorted(
                cast(list[dict[str, object]], release_facts),
                key=lambda item: str(item["fact_id"]),
            )
            draft = verified.draft
            reference = draft.references[0]
            decision = draft.review_decisions[0]
            fact_projection = sorted(
                (
                    {
                        "document_date": str(fact.document_date),
                        "effective_date": (
                            str(fact.effective_date) if fact.effective_date is not None else None
                        ),
                        "fact_id": fact.fact_id,
                        "fact_nature": fact.fact_nature.value,
                        "known_from_date": str(fact.known_from_date),
                        "pages": list(fact.pages),
                        "paragraph_sha256": fact.paragraph_sha256,
                        "publication_date": str(fact.publication_date),
                        "usable_for_decisions_after": fact.usable_for_decisions_after,
                    }
                    for fact in draft.facts
                ),
                key=lambda item: str(item["fact_id"]),
            )
            actual = {
                "approval": {
                    "approval_statement_sha256": decision.approval_statement_sha256,
                    "case_ids": list(decision.case_ids),
                    "decided_at": str(decision.decided_at),
                    "decision_id": decision.decision_id,
                    "reviewer_role": decision.reviewer_role.value,
                    "status": decision.status.value,
                },
                "bundle_id": draft.bundle_id,
                "bundle_manifest_sha256": verified.manifest_sha256,
                "facts": fact_projection,
                "reference": {
                    "announcement_id": reference.announcement_id,
                    "announcement_number": reference.announcement_number,
                    "canonical_locator": reference.canonical_locator,
                    "material_sha256": reference.material_sha256,
                    "p2b_manifest_sha256": reference.p2b_manifest_sha256,
                    "p2f_manifest_sha256": reference.p2f_manifest_sha256,
                    "reference_id": reference.reference_id,
                },
                "reference_inventory_sha256": verified.reference_inventory_sha256,
                "sample": {
                    "previous_sample_spec_sha256": draft.previous_sample_spec_sha256,
                    "sample_spec_sha256": draft.sample_spec_sha256,
                    "sample_spec_version": draft.sample_spec_version,
                },
                "schema_version": "authority-trust-release.v1",
            }
            if actual != release:
                raise ValueError
        except (IndexError, KeyError, TypeError, ValueError):
            raise AuthorityBundleVerificationError("AUTHORITY_BUNDLE_TRUST_MISMATCH") from None

    def _record_failure(self, code: str) -> None:
        root = self._root / "authority-failures"
        try:
            root.mkdir(parents=True, exist_ok=True)
            target = root / f"{uuid4().hex}.json"
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(
                    descriptor,
                    _canonical(
                        {
                            "error_code": (
                                code
                                if code.startswith("AUTHORITY_BUNDLE_")
                                else "AUTHORITY_BUNDLE_INVALID"
                            ),
                            "schema_version": "authority-failure.v1",
                        }
                    ),
                )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            return


def _draft_from_verified_payloads(
    manifest: dict[str, object],
    payloads: dict[str, bytes],
) -> AuthorityReferenceBundleDraft:
    references: list[AuthorityReferenceDocument] = []
    for relative, payload in sorted(payloads.items()):
        if not relative.startswith("references/"):
            continue
        raw = json.loads(payload)
        if payload != _canonical(raw) or set(raw) != {
            "announcement_id",
            "announcement_number",
            "authority",
            "canonical_locator",
            "company_name",
            "document_date",
            "known_from_date",
            "material_sha256",
            "p2b_manifest_sha256",
            "p2f_manifest_sha256",
            "publication_date",
            "reference_id",
            "security_code",
            "security_name",
        }:
            raise ValueError("authority reference document schema mismatch")
        references.append(
            AuthorityReferenceDocument(
                reference_id=str(raw["reference_id"]),
                authority=str(raw["authority"]),
                canonical_locator=str(raw["canonical_locator"]),
                announcement_id=str(raw["announcement_id"]),
                announcement_number=str(raw["announcement_number"]),
                security_code=str(raw["security_code"]),
                security_name=str(raw["security_name"]),
                company_name=str(raw["company_name"]),
                document_date=TradingDate.from_iso(str(raw["document_date"])),
                publication_date=TradingDate.from_iso(str(raw["publication_date"])),
                known_from_date=TradingDate.from_iso(str(raw["known_from_date"])),
                material_sha256=str(raw["material_sha256"]),
                p2b_manifest_sha256=str(raw["p2b_manifest_sha256"]),
                p2f_manifest_sha256=str(raw["p2f_manifest_sha256"]),
            )
        )
    raw_facts = json.loads(payloads["facts.json"])
    if payloads["facts.json"] != _canonical(raw_facts) or not isinstance(raw_facts, list):
        raise ValueError("authority facts must be a canonical array")
    facts = tuple(_fact_from_raw(raw) for raw in raw_facts if isinstance(raw, dict))
    raw_bindings = json.loads(payloads["bindings.json"])
    if payloads["bindings.json"] != _canonical(raw_bindings) or not isinstance(raw_bindings, list):
        raise ValueError("case bindings must be a canonical array")
    if any(
        not isinstance(raw, dict) or set(raw) != {"case_id", "fact_id", "purpose"}
        for raw in raw_bindings
    ):
        raise ValueError("authority case binding schema mismatch")
    bindings = tuple(
        AuthorityCaseFactBinding(
            case_id=str(raw["case_id"]),
            fact_id=str(raw["fact_id"]),
            purpose=SamplePurpose(str(raw["purpose"])),
        )
        for raw in raw_bindings
    )
    raw_decisions = json.loads(payloads["review_decisions.json"])
    if payloads["review_decisions.json"] != _canonical(raw_decisions) or not isinstance(
        raw_decisions, list
    ):
        raise ValueError("review decisions must be a canonical array")
    if any(
        not isinstance(raw, dict)
        or set(raw)
        != {
            "approval_statement",
            "approval_statement_sha256",
            "case_ids",
            "decided_at",
            "decision_id",
            "reviewer_role",
            "status",
        }
        for raw in raw_decisions
    ):
        raise ValueError("review decision schema mismatch")
    review_decisions = tuple(
        ReviewDecision(
            decision_id=str(raw["decision_id"]),
            reviewer_role=ReviewerRole(str(raw["reviewer_role"])),
            status=ReviewDecisionStatus(str(raw["status"])),
            case_ids=tuple(str(item) for item in raw["case_ids"]),
            approval_statement=str(raw["approval_statement"]),
            approval_statement_sha256=str(raw["approval_statement_sha256"]),
            decided_at=TradingDate.from_iso(str(raw["decided_at"])),
        )
        for raw in raw_decisions
    )
    return AuthorityReferenceBundleDraft(
        bundle_id=str(manifest["bundle_id"]),
        sample_spec_version=str(manifest["sample_spec_version"]),
        sample_spec_sha256=str(manifest["sample_spec_sha256"]),
        previous_sample_spec_sha256=str(manifest["previous_sample_spec_sha256"]),
        references=tuple(references),
        facts=facts,
        bindings=bindings,
        review_decisions=review_decisions,
        code_version=str(manifest["code_version"]),
        schema_version=str(manifest["schema_version"]),
    )


def _fact_from_raw(raw: dict[str, object]) -> AuthorityReferenceFact:
    raw_pages = raw["pages"]
    if not isinstance(raw_pages, list):
        raise ValueError("authority fact pages must be a JSON array")
    pages = tuple(int(item) for item in raw_pages)
    expected_optional = {
        "effective_date",
        "other_risk_warning",
        "other_risk_warning_continues",
    }
    expected_required = {
        "code",
        "dataset",
        "document_date",
        "exchange",
        "fact_id",
        "fact_nature",
        "known_from_date",
        "material_sha256",
        "pages",
        "paragraph_sha256",
        "publication_date",
        "purpose",
        "reference_id",
        "statement",
        "usable_for_decisions_after",
    }
    if set(raw) - expected_required - expected_optional:
        raise ValueError("authority fact schema mismatch")
    if not expected_required.issubset(raw):
        raise ValueError("authority fact schema mismatch")
    return AuthorityReferenceFact(
        fact_id=str(raw["fact_id"]),
        reference_id=str(raw["reference_id"]),
        dataset=DatasetKind(str(raw["dataset"])),
        purpose=SamplePurpose(str(raw["purpose"])),
        code=str(raw["code"]),
        exchange=str(raw["exchange"]),
        fact_nature=FactNature(str(raw["fact_nature"])),
        document_date=TradingDate.from_iso(str(raw["document_date"])),
        publication_date=TradingDate.from_iso(str(raw["publication_date"])),
        known_from_date=TradingDate.from_iso(str(raw["known_from_date"])),
        usable_for_decisions_after=str(raw["usable_for_decisions_after"]),
        material_sha256=str(raw["material_sha256"]),
        paragraph_sha256=str(raw["paragraph_sha256"]),
        pages=pages,
        statement=str(raw["statement"]),
        effective_date=(
            TradingDate.from_iso(str(raw["effective_date"]))
            if raw.get("effective_date") is not None
            else None
        ),
        other_risk_warning=(
            bool(raw["other_risk_warning"]) if raw.get("other_risk_warning") is not None else None
        ),
        other_risk_warning_continues=(
            bool(raw["other_risk_warning_continues"])
            if raw.get("other_risk_warning_continues") is not None
            else None
        ),
    )


__all__ = [
    "AUTHORITY_TRUST_RELEASE_BYTES_SHA256",
    "CONTROLLED_AUTHORITY_BUNDLE_ID",
    "CONTROLLED_AUTHORITY_BUNDLE_MANIFEST_SHA256",
    "AuthorityBundleIdempotencyConflictError",
    "AuthorityBundleVerificationError",
    "AuthorityCaseFactBinding",
    "AuthorityReferenceBundleDraft",
    "AuthorityReferenceBundleVerifier",
    "AuthorityReferenceBundleWriter",
    "AuthorityReferenceDocument",
    "AuthorityReferenceFact",
    "FactNature",
    "ReviewDecision",
    "ReviewDecisionStatus",
    "ReviewerRole",
    "VerifiedAuthorityReferenceBundle",
]
