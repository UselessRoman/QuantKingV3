"""Immutable, provider-neutral authority-reference bundle contracts."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
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
    FixedSampleSpec,
    SampleEligibility,
    SamplePurpose,
)

_ID = re.compile(r"^[a-z][a-z0-9-]{2,95}$")
_CASE_ID = re.compile(r"^[a-z][a-z0-9_-]{2,95}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA = "reference-bundle.manifest.v1"


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


class AuthorityTier(StrEnum):
    PRIMARY = "PRIMARY"
    THIRD_PARTY = "THIRD_PARTY"


class ReviewerDecision(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class ReferenceDocument:
    reference_id: str
    authority: str
    authority_tier: AuthorityTier
    canonical_locator: str
    document_id: str
    published_date: TradingDate
    retrieved_date: TradingDate
    material_sha256: str

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.reference_id):
            raise ValueError("reference_id must be a stable identifier")
        if not self.authority.strip():
            raise ValueError("authority must be non-empty")
        if self.authority_tier is not AuthorityTier.PRIMARY:
            raise ValueError("authoritative sample references must be PRIMARY")
        locator = urlsplit(self.canonical_locator)
        if (
            locator.scheme != "https"
            or not locator.netloc
            or locator.path in {"", "/"}
            or not self.document_id.strip()
        ):
            raise ValueError("canonical locator must be HTTPS and document-specific")
        if not isinstance(self.published_date, TradingDate) or not isinstance(
            self.retrieved_date, TradingDate
        ):
            raise TypeError("reference dates must be TradingDate values")
        if self.retrieved_date.value < self.published_date.value:
            raise ValueError("retrieved_date must not precede published_date")
        if not _HEX64.fullmatch(self.material_sha256):
            raise ValueError("material_sha256 must be a lowercase SHA-256")

    def to_dict(self) -> dict[str, str]:
        return {
            "authority": self.authority,
            "authority_tier": self.authority_tier.value,
            "canonical_locator": self.canonical_locator,
            "document_id": self.document_id,
            "material_sha256": self.material_sha256,
            "published_date": str(self.published_date),
            "reference_id": self.reference_id,
            "retrieved_date": str(self.retrieved_date),
        }


@dataclass(frozen=True, slots=True)
class ReferenceFact:
    fact_id: str
    reference_id: str
    dataset: DatasetKind
    purpose: SamplePurpose
    code: str | None
    exchange: str
    start_date: TradingDate
    end_date: TradingDate
    statement: str

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.fact_id) or not _ID.fullmatch(self.reference_id):
            raise ValueError("fact and reference IDs must be stable identifiers")
        if not isinstance(self.dataset, DatasetKind) or not isinstance(self.purpose, SamplePurpose):
            raise TypeError("fact dataset and purpose must use controlled enums")
        if self.exchange not in {"SH", "SZ", "BJ"}:
            raise ValueError("fact exchange must be SH, SZ or BJ")
        if self.code is not None and not self.code.endswith(f".{self.exchange}"):
            raise ValueError("fact code suffix must match exchange")
        if self.start_date.value > self.end_date.value:
            raise ValueError("fact date range is invalid")
        if not self.statement.strip() or len(self.statement) > 500:
            raise ValueError("fact statement must be short, non-empty structured evidence")

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "dataset": self.dataset.value,
            "end_date": str(self.end_date),
            "exchange": self.exchange,
            "fact_id": self.fact_id,
            "purpose": self.purpose.value,
            "reference_id": self.reference_id,
            "start_date": str(self.start_date),
            "statement": self.statement,
        }


@dataclass(frozen=True, slots=True)
class CaseFactBinding:
    case_id: str
    fact_id: str

    def __post_init__(self) -> None:
        if not _CASE_ID.fullmatch(self.case_id) or not _ID.fullmatch(self.fact_id):
            raise ValueError("case/fact binding IDs must be stable identifiers")

    def to_dict(self) -> dict[str, str]:
        return {"case_id": self.case_id, "fact_id": self.fact_id}


@dataclass(frozen=True, slots=True)
class ReferenceBundleDraft:
    bundle_id: str
    sample_spec_version: str
    sample_spec_sha256: str
    previous_sample_spec_sha256: str
    references: tuple[ReferenceDocument, ...]
    facts: tuple[ReferenceFact, ...]
    bindings: tuple[CaseFactBinding, ...]
    code_version: str
    schema_version: str
    reviewed_at: TradingDate
    reviewer_decision: ReviewerDecision
    reviewer_key_id: str | None
    reviewer_signature: str | None

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.bundle_id):
            raise ValueError("bundle_id must be a stable identifier")
        if not _VERSION.fullmatch(self.sample_spec_version):
            raise ValueError("sample_spec_version is invalid")
        for digest in (self.sample_spec_sha256, self.previous_sample_spec_sha256):
            if not _HEX64.fullmatch(digest):
                raise ValueError("sample lineage SHA is invalid")
        for field in ("code_version", "schema_version"):
            if not _VERSION.fullmatch(getattr(self, field)):
                raise ValueError(f"{field} is invalid")
        if not isinstance(self.reviewed_at, TradingDate):
            raise TypeError("reviewed_at must be a TradingDate")
        for values, label, attribute in (
            (self.references, "reference", "reference_id"),
            (self.facts, "fact", "fact_id"),
        ):
            identifiers = [getattr(item, attribute) for item in values]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"duplicate {label} ID")
        binding_pairs = [(item.case_id, item.fact_id) for item in self.bindings]
        if len(binding_pairs) != len(set(binding_pairs)):
            raise ValueError("duplicate case/fact binding")
        if (self.reviewer_key_id is None) != (self.reviewer_signature is None):
            raise ValueError("reviewer key ID and signature must be supplied together")
        if self.reviewer_signature is not None and not _HEX64.fullmatch(self.reviewer_signature):
            raise ValueError("reviewer signature must be a lowercase SHA-256")

    def validate_against(self, sample: FixedSampleSpec) -> None:
        if (
            self.sample_spec_version != sample.sample_spec_version
            or self.sample_spec_sha256 != sample.sample_spec_sha256
            or self.previous_sample_spec_sha256 != sample.previous_sample_spec_sha256
        ):
            raise ValueError("reference bundle does not bind the controlled sample")
        references = {item.reference_id: item for item in self.references}
        facts = {item.fact_id: item for item in self.facts}
        cases = {item.case_id: item for item in sample.cases}
        if any(item.reference_id not in references for item in self.facts):
            raise ValueError("fact references an undeclared authority document")
        if {item.reference_id for item in self.facts} != set(references):
            raise ValueError("every authority document must support a declared fact")
        bound_fact_ids = {item.fact_id for item in self.bindings}
        if bound_fact_ids != set(facts):
            raise ValueError("every fact must bind an exact fixed-sample case")
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
                or fact.start_date != case.start_date
                or fact.end_date != case.end_date
            ):
                raise ValueError("case fact is unrelated to the fixed sample range")

    def reviewer_payload(self) -> dict[str, object]:
        return {
            "bindings": [
                item.to_dict()
                for item in sorted(self.bindings, key=lambda item: (item.case_id, item.fact_id))
            ],
            "bundle_id": self.bundle_id,
            "code_version": self.code_version,
            "facts": [item.to_dict() for item in sorted(self.facts, key=lambda item: item.fact_id)],
            "previous_sample_spec_sha256": self.previous_sample_spec_sha256,
            "references": [
                item.to_dict()
                for item in sorted(self.references, key=lambda item: item.reference_id)
            ],
            "reviewed_at": str(self.reviewed_at),
            "reviewer_decision": self.reviewer_decision.value,
            "sample_spec_sha256": self.sample_spec_sha256,
            "sample_spec_version": self.sample_spec_version,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ReviewerTrustPolicy:
    _trusted_key_ids: frozenset[str]

    def __post_init__(self) -> None:
        if self._trusted_key_ids:
            raise ValueError("S1-07B has no production reviewer key allowlist")

    @classmethod
    def framework_only(cls) -> ReviewerTrustPolicy:
        return cls(frozenset())

    def accepts(self, *, key_id: str | None, signature: str | None, payload: bytes) -> bool:
        del signature, payload
        return key_id in self._trusted_key_ids


@dataclass(frozen=True, slots=True)
class VerifiedReferenceBundle:
    bundle_id: str
    manifest_sha256: str
    sample_spec_version: str
    sample_spec_sha256: str
    previous_sample_spec_sha256: str
    reference_inventory_sha256: str
    complete: bool
    reviewer_decision: ReviewerDecision
    reviewer_trusted: bool
    eligibility: SampleEligibility


class ReferenceBundleVerificationError(Exception):
    """Stable, sanitized reference-bundle verification failure."""


class ReferenceBundleWriter:
    def __init__(self, *, root: Path) -> None:
        if not root.is_absolute():
            raise ValueError("reference root must be absolute")
        self._root = root

    def write(self, draft: ReferenceBundleDraft) -> Path:
        parent = resolve_under(self._root, "reference-bundles")
        parent.mkdir(parents=True, exist_ok=True)
        if is_reparse_point(parent):
            raise ValueError("reference root is unsafe")
        final = parent / draft.bundle_id
        incomplete = parent / f".{draft.bundle_id}.{uuid4().hex}.incomplete"
        if final.exists():
            raise ValueError("reference bundle ID is immutable")
        (incomplete / "references").mkdir(parents=True)
        files: dict[str, bytes] = {
            "bindings.json": _canonical(
                [
                    item.to_dict()
                    for item in sorted(
                        draft.bindings, key=lambda item: (item.case_id, item.fact_id)
                    )
                ]
            ),
            "facts.json": _canonical(
                [item.to_dict() for item in sorted(draft.facts, key=lambda item: item.fact_id)]
            ),
        }
        for reference in draft.references:
            files[f"references/{reference.reference_id}.json"] = _canonical(reference.to_dict())
        for relative, payload in files.items():
            target = incomplete / relative
            target.write_bytes(payload)
        inventory = [
            {
                "relative_path": relative,
                "sha256": compute_sha256(payload),
                "size": len(payload),
            }
            for relative, payload in sorted(files.items())
        ]
        unsigned = {
            "bundle_id": draft.bundle_id,
            "code_version": draft.code_version,
            "files": inventory,
            "manifest_schema_version": _SCHEMA,
            "previous_sample_spec_sha256": draft.previous_sample_spec_sha256,
            "reference_inventory_sha256": compute_sha256(_canonical(inventory)),
            "reviewed_at": str(draft.reviewed_at),
            "reviewer_decision": draft.reviewer_decision.value,
            "reviewer_key_id": draft.reviewer_key_id,
            "reviewer_signature": draft.reviewer_signature,
            "sample_spec_sha256": draft.sample_spec_sha256,
            "sample_spec_version": draft.sample_spec_version,
            "schema_version": draft.schema_version,
        }
        manifest_sha = compute_sha256(_canonical(unsigned))
        (incomplete / "manifest.json").write_bytes(
            _canonical({**unsigned, "manifest_sha256": manifest_sha})
        )
        (incomplete / "COMPLETE").write_bytes(_canonical({"manifest_sha256": manifest_sha}))
        os.replace(incomplete, final)
        return final


class ReferenceBundleVerifier:
    def __init__(
        self,
        *,
        root: Path,
        reviewer_trust_policy: ReviewerTrustPolicy,
    ) -> None:
        if not root.is_absolute():
            raise ValueError("reference root must be absolute")
        if type(reviewer_trust_policy) is not ReviewerTrustPolicy:
            raise ValueError("reviewer trust policy must come from the controlled registry")
        self._root = root
        self._policy = reviewer_trust_policy

    def verify(self, *, directory: Path, sample: FixedSampleSpec) -> VerifiedReferenceBundle:
        try:
            return self._verify(directory=directory, sample=sample)
        except ReferenceBundleVerificationError as error:
            self._record_failure(str(error))
            raise
        except Exception:
            self._record_failure("REFERENCE_BUNDLE_INVALID")
            raise ReferenceBundleVerificationError("REFERENCE_BUNDLE_INVALID") from None

    def _verify(self, *, directory: Path, sample: FixedSampleSpec) -> VerifiedReferenceBundle:
        try:
            relative = directory.relative_to(self._root)
            resolved = resolve_under(self._root, relative)
        except Exception:
            raise ReferenceBundleVerificationError("REFERENCE_BUNDLE_OUTSIDE_ROOT") from None
        if resolved != directory.resolve(strict=False) or not directory.is_dir():
            raise ReferenceBundleVerificationError("REFERENCE_BUNDLE_UNSAFE")
        for item in (directory, *directory.rglob("*")):
            if item.is_symlink() or is_reparse_point(item):
                raise ReferenceBundleVerificationError("REFERENCE_BUNDLE_UNSAFE")
        manifest_bytes = (directory / "manifest.json").read_bytes()
        complete_bytes = (directory / "COMPLETE").read_bytes()
        manifest = json.loads(manifest_bytes)
        complete = json.loads(complete_bytes)
        if (
            not isinstance(manifest, dict)
            or manifest_bytes != _canonical(manifest)
            or not isinstance(complete, dict)
            or complete_bytes != _canonical(complete)
        ):
            raise ReferenceBundleVerificationError("REFERENCE_BUNDLE_NOT_CANONICAL")
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
            "reviewed_at",
            "reviewer_decision",
            "reviewer_key_id",
            "reviewer_signature",
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
        ):
            raise ReferenceBundleVerificationError("REFERENCE_BUNDLE_MANIFEST_MISMATCH")
        if (
            manifest["sample_spec_version"] != sample.sample_spec_version
            or manifest["sample_spec_sha256"] != sample.sample_spec_sha256
            or manifest["previous_sample_spec_sha256"] != sample.previous_sample_spec_sha256
        ):
            raise ReferenceBundleVerificationError("REFERENCE_BUNDLE_SAMPLE_MISMATCH")
        raw_files = manifest["files"]
        if not isinstance(raw_files, list):
            raise ReferenceBundleVerificationError("REFERENCE_BUNDLE_INVENTORY_INVALID")
        payloads: dict[str, bytes] = {}
        for entry in raw_files:
            if not isinstance(entry, dict) or set(entry) != {
                "relative_path",
                "sha256",
                "size",
            }:
                raise ReferenceBundleVerificationError("REFERENCE_BUNDLE_INVENTORY_INVALID")
            relative_path = str(entry["relative_path"])
            try:
                validate_managed_relative(relative_path, field="reference bundle file")
                target = resolve_under(directory, relative_path)
            except Exception:
                raise ReferenceBundleVerificationError("REFERENCE_BUNDLE_UNSAFE") from None
            if relative_path in payloads or target.is_symlink() or not target.is_file():
                raise ReferenceBundleVerificationError("REFERENCE_BUNDLE_FILE_MISMATCH")
            payload = target.read_bytes()
            if len(payload) != entry["size"] or compute_sha256(payload) != entry["sha256"]:
                raise ReferenceBundleVerificationError("REFERENCE_BUNDLE_FILE_MISMATCH")
            payloads[relative_path] = payload
        expected = {*payloads, "manifest.json", "COMPLETE"}
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
            raise ReferenceBundleVerificationError("REFERENCE_BUNDLE_FILE_SET_MISMATCH")
        draft = _draft_from_verified_payloads(manifest, payloads)
        try:
            draft.validate_against(sample)
        except ValueError:
            raise ReferenceBundleVerificationError("REFERENCE_BUNDLE_FACT_MISMATCH") from None
        bound_cases = {item.case_id for item in draft.bindings}
        supported_purposes = {
            fact.purpose
            for fact in draft.facts
            if fact.fact_id in {binding.fact_id for binding in draft.bindings}
        }
        complete_authority = bound_cases == {case.case_id for case in sample.cases} and set(
            sample.required_authoritative_purposes
        ).issubset(supported_purposes)
        reviewer_payload = _canonical(draft.reviewer_payload())
        trusted = self._policy.accepts(
            key_id=draft.reviewer_key_id,
            signature=draft.reviewer_signature,
            payload=reviewer_payload,
        )
        eligible = (
            complete_authority and draft.reviewer_decision is ReviewerDecision.APPROVED and trusted
        )
        return VerifiedReferenceBundle(
            bundle_id=draft.bundle_id,
            manifest_sha256=embedded,
            sample_spec_version=draft.sample_spec_version,
            sample_spec_sha256=draft.sample_spec_sha256,
            previous_sample_spec_sha256=draft.previous_sample_spec_sha256,
            reference_inventory_sha256=str(manifest["reference_inventory_sha256"]),
            complete=complete_authority,
            reviewer_decision=draft.reviewer_decision,
            reviewer_trusted=trusted,
            eligibility=(
                SampleEligibility.PRODUCTION_ELIGIBLE
                if eligible
                else SampleEligibility.FRAMEWORK_ONLY
            ),
        )

    def _record_failure(self, code: str) -> None:
        root = self._root / "reference-failures"
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
                                if code.startswith("REFERENCE_BUNDLE_")
                                else "REFERENCE_BUNDLE_INVALID"
                            ),
                            "schema_version": "reference-failure.v1",
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
) -> ReferenceBundleDraft:
    references: list[ReferenceDocument] = []
    for relative, payload in sorted(payloads.items()):
        if not relative.startswith("references/"):
            continue
        raw = json.loads(payload)
        if payload != _canonical(raw) or set(raw) != {
            "authority",
            "authority_tier",
            "canonical_locator",
            "document_id",
            "material_sha256",
            "published_date",
            "reference_id",
            "retrieved_date",
        }:
            raise ValueError("reference document schema mismatch")
        references.append(
            ReferenceDocument(
                reference_id=str(raw["reference_id"]),
                authority=str(raw["authority"]),
                authority_tier=AuthorityTier(str(raw["authority_tier"])),
                canonical_locator=str(raw["canonical_locator"]),
                document_id=str(raw["document_id"]),
                published_date=TradingDate.from_iso(str(raw["published_date"])),
                retrieved_date=TradingDate.from_iso(str(raw["retrieved_date"])),
                material_sha256=str(raw["material_sha256"]),
            )
        )
    raw_facts = json.loads(payloads["facts.json"])
    if payloads["facts.json"] != _canonical(raw_facts) or not isinstance(raw_facts, list):
        raise ValueError("reference facts must be a canonical array")
    fact_fields = {
        "code",
        "dataset",
        "end_date",
        "exchange",
        "fact_id",
        "purpose",
        "reference_id",
        "start_date",
        "statement",
    }
    if any(not isinstance(raw, dict) or set(raw) != fact_fields for raw in raw_facts):
        raise ValueError("reference fact schema mismatch")
    facts = tuple(
        ReferenceFact(
            fact_id=str(raw["fact_id"]),
            reference_id=str(raw["reference_id"]),
            dataset=DatasetKind(str(raw["dataset"])),
            purpose=SamplePurpose(str(raw["purpose"])),
            code=str(raw["code"]) if raw["code"] is not None else None,
            exchange=str(raw["exchange"]),
            start_date=TradingDate.from_iso(str(raw["start_date"])),
            end_date=TradingDate.from_iso(str(raw["end_date"])),
            statement=str(raw["statement"]),
        )
        for raw in raw_facts
    )
    raw_bindings = json.loads(payloads["bindings.json"])
    if payloads["bindings.json"] != _canonical(raw_bindings) or not isinstance(raw_bindings, list):
        raise ValueError("case bindings must be a canonical array")
    if any(not isinstance(raw, dict) or set(raw) != {"case_id", "fact_id"} for raw in raw_bindings):
        raise ValueError("case binding schema mismatch")
    bindings = tuple(
        CaseFactBinding(case_id=str(raw["case_id"]), fact_id=str(raw["fact_id"]))
        for raw in raw_bindings
    )
    return ReferenceBundleDraft(
        bundle_id=str(manifest["bundle_id"]),
        sample_spec_version=str(manifest["sample_spec_version"]),
        sample_spec_sha256=str(manifest["sample_spec_sha256"]),
        previous_sample_spec_sha256=str(manifest["previous_sample_spec_sha256"]),
        references=tuple(references),
        facts=facts,
        bindings=bindings,
        code_version=str(manifest["code_version"]),
        schema_version=str(manifest["schema_version"]),
        reviewed_at=TradingDate.from_iso(str(manifest["reviewed_at"])),
        reviewer_decision=ReviewerDecision(str(manifest["reviewer_decision"])),
        reviewer_key_id=(
            str(manifest["reviewer_key_id"]) if manifest["reviewer_key_id"] is not None else None
        ),
        reviewer_signature=(
            str(manifest["reviewer_signature"])
            if manifest["reviewer_signature"] is not None
            else None
        ),
    )


__all__ = [
    "AuthorityTier",
    "CaseFactBinding",
    "ReferenceBundleDraft",
    "ReferenceBundleVerificationError",
    "ReferenceBundleVerifier",
    "ReferenceBundleWriter",
    "ReferenceDocument",
    "ReferenceFact",
    "ReviewerDecision",
    "ReviewerTrustPolicy",
    "VerifiedReferenceBundle",
]
