"""Provider-neutral, versioned fixed-sample contract."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from types import MappingProxyType
from typing import cast

from quantking.common.time import TradingDate
from quantking.data.adapters.warehouse.manifest import compute_sha256
from quantking.data.models import DatasetKind
from quantking.data.schemas import descriptor_for

_CASE_ID = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
_CODE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_SOURCE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

FIXED_SAMPLE_V1_SHA256 = "bc40271a632e0f8b4bae493806bf3d316897857a1102ed01b70f2bb46c632bee"
FIXED_SAMPLE_V2_SHA256 = "03d3d6c1e898b0746b7c5ab3911917a600fa3975f802c1d414fd0b6196c80dfb"
FIXED_SAMPLE_V3_SHA256 = "5b5bf202e17181dfcbd9159f261dc9b4c636fb7641a3786523b42f952c6f3200"
FIXED_SAMPLE_V4_SHA256 = "859726293e8ed2e77663b6a4168b29a2cfd48070236ec5b8815ad2ee3b5904d7"
FIXED_SAMPLE_V1_BYTES_SHA256 = "aafb03db4a3845aa710848dc1f12542350afbb57d6edae466cf0978b5c0bc249"
FIXED_SAMPLE_V2_RELEASE_BYTES_SHA256 = (
    "2b692c631018c1e600680af329d3936dd98267d9d82ffdbcbe854b3ca8574cb8"
)
FIXED_SAMPLE_V3_RELEASE_BYTES_SHA256 = (
    "52bc879ed5f42ea6a8fe0d6b489d4d9c1153e3568379d614539b65ca207e72eb"
)
FIXED_SAMPLE_V4_RELEASE_BYTES_SHA256 = (
    "48b054df659423c93b198aa63660892ba221db9c2d3afd008715ed8bca3c6034"
)


class SamplePurpose(StrEnum):
    ADJUSTMENT_FORMULA = "ADJUSTMENT_FORMULA"
    NORMAL = "NORMAL"
    IPO = "IPO"
    LISTING = "LISTING"
    DELISTING = "DELISTING"
    DELISTING_PERIOD = "DELISTING_PERIOD"
    ST = "ST"
    STAR_ST = "STAR_ST"
    SUSPENSION = "SUSPENSION"
    RESUMPTION = "RESUMPTION"
    LIMIT_UP = "LIMIT_UP"
    LIMIT_DOWN = "LIMIT_DOWN"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    BENCHMARK = "BENCHMARK"
    POINT_IN_TIME = "POINT_IN_TIME"
    UNIT_SEMANTICS = "UNIT_SEMANTICS"
    MISSING_OR_UNSUPPORTED = "MISSING_OR_UNSUPPORTED"


class SampleEligibility(StrEnum):
    """Sample-wide eligibility contract.

    S1-07B-FINAL introduced ``VALIDATION_READY_WITH_BLOCKED_CASES`` to replace
    the previous all-or-nothing ``PRODUCTION_ELIGIBLE`` gate. A sample at this
    tier may drive runtime preflight for the cases it explicitly approves, but
    never claims all 17 authoritative purposes are production-ready.
    """

    FRAMEWORK_ONLY = "FRAMEWORK_ONLY"
    PRODUCTION_ELIGIBLE = "PRODUCTION_ELIGIBLE"
    VALIDATION_READY_WITH_BLOCKED_CASES = "VALIDATION_READY_WITH_BLOCKED_CASES"


class CaseEligibility(StrEnum):
    """Per-case authority eligibility.

    ``FRAMEWORK_ONLY`` cases only validate request/schema/unit contracts; they
    carry no authoritative fact and cannot enter S1-07C runtime.
    ``AUTHORITY_APPROVED`` cases are bound to a verified, locally approved
    reference bundle and may pass runtime preflight (still subject to provider
    package, REAL signer and authorization gates).
    ``VALIDATION_READY`` is reserved for future cases that have complete
    reference evidence but await REAL provider response.
    ``BLOCKED_REFERENCE`` marks a case whose reference evidence is incomplete,
    conflicting or has been revoked; it cannot pass runtime preflight.
    """

    FRAMEWORK_ONLY = "FRAMEWORK_ONLY"
    AUTHORITY_APPROVED = "AUTHORITY_APPROVED"
    VALIDATION_READY = "VALIDATION_READY"
    BLOCKED_REFERENCE = "BLOCKED_REFERENCE"


@dataclass(frozen=True, slots=True)
class SampleReference:
    authority: str
    locator: str
    reference_version: str
    reviewed_at: TradingDate

    def __post_init__(self) -> None:
        for field in ("authority", "locator", "reference_version"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be non-empty text")
        if not isinstance(self.reviewed_at, TradingDate):
            raise TypeError("reviewed_at must be a TradingDate")

    def to_dict(self) -> dict[str, str]:
        return {
            "authority": self.authority,
            "locator": self.locator,
            "reference_version": self.reference_version,
            "reviewed_at": str(self.reviewed_at),
        }


@dataclass(frozen=True, slots=True)
class FixedSampleCase:
    case_id: str
    dataset: DatasetKind
    code: str | None
    exchange: str
    start_date: TradingDate
    end_date: TradingDate
    fields: tuple[str, ...]
    expected_units: Mapping[str, str]
    purpose: SamplePurpose
    applicable_sources: tuple[str, ...]
    reference: SampleReference

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not _CASE_ID.fullmatch(self.case_id):
            raise ValueError("case_id must be a stable lowercase identifier")
        if not isinstance(self.dataset, DatasetKind):
            raise TypeError("dataset must be a DatasetKind")
        if self.dataset is DatasetKind.EXCHANGE_CALENDAR:
            if self.code is not None:
                raise ValueError("exchange_calendar case must not carry a security code")
            if self.exchange not in {"SH", "SZ", "BJ"}:
                raise ValueError("exchange must be SH, SZ or BJ")
        else:
            if not isinstance(self.code, str) or not _CODE.fullmatch(self.code):
                raise ValueError("code must use canonical 000001.SZ form")
            suffix = self.code.rsplit(".", 1)[1]
            if self.exchange != suffix:
                raise ValueError("exchange must match the canonical code suffix")
        if self.start_date.value > self.end_date.value:
            raise ValueError("start_date must not follow end_date")
        descriptor = descriptor_for(self.dataset.value)
        normalized_fields = tuple(self.fields)
        if not normalized_fields or len(set(normalized_fields)) != len(normalized_fields):
            raise ValueError("fields must be non-empty and unique")
        if not set(normalized_fields).issubset(descriptor.field_names):
            raise ValueError("fields must belong to the canonical dataset")
        units = dict(sorted(self.expected_units.items()))
        if set(units) != set(normalized_fields):
            raise ValueError("expected_units must cover the requested fields exactly")
        if any(not isinstance(value, str) or not value.strip() for value in units.values()):
            raise ValueError("expected units must be non-empty text")
        sources = tuple(sorted(self.applicable_sources))
        if not sources or any(not _SOURCE.fullmatch(item) for item in sources):
            raise ValueError("applicable_sources must contain stable source identifiers")
        if len(set(sources)) != len(sources):
            raise ValueError("applicable_sources must not contain duplicates")
        if not isinstance(self.purpose, SamplePurpose):
            raise TypeError("purpose must be a SamplePurpose")
        if not isinstance(self.reference, SampleReference):
            raise TypeError("reference must be a SampleReference")
        object.__setattr__(self, "fields", normalized_fields)
        object.__setattr__(self, "expected_units", MappingProxyType(units))
        object.__setattr__(self, "applicable_sources", sources)

    def to_dict(self) -> dict[str, object]:
        return {
            "applicable_sources": list(self.applicable_sources),
            "case_id": self.case_id,
            "code": self.code,
            "dataset": self.dataset.value,
            "end_date": str(self.end_date),
            "exchange": self.exchange,
            "expected_units": dict(self.expected_units),
            "fields": list(self.fields),
            "purpose": self.purpose.value,
            "reference": self.reference.to_dict(),
            "start_date": str(self.start_date),
        }


@dataclass(frozen=True, slots=True)
class FixedSampleSpec:
    sample_spec_version: str
    cases: tuple[FixedSampleCase, ...]
    eligibility: SampleEligibility
    missing_authoritative_purposes: tuple[str, ...]
    sample_spec_sha256: str
    previous_sample_spec_sha256: str | None = None
    required_authoritative_purposes: tuple[SamplePurpose, ...] = ()

    def __post_init__(self) -> None:
        if not self.sample_spec_version.strip():
            raise ValueError("sample_spec_version must be non-empty")
        cases = tuple(sorted(self.cases, key=lambda item: item.case_id))
        if not cases or len({item.case_id for item in cases}) != len(cases):
            raise ValueError("sample cases must be non-empty and unique by case_id")
        object.__setattr__(self, "cases", cases)
        if not isinstance(self.eligibility, SampleEligibility):
            raise TypeError("eligibility must be a SampleEligibility")
        missing = tuple(sorted(self.missing_authoritative_purposes))
        if len(set(missing)) != len(missing):
            raise ValueError("missing_authoritative_purposes must be unique")
        allowed = {item.value for item in SamplePurpose}
        if not set(missing).issubset(allowed):
            raise ValueError("missing_authoritative_purposes contains an unknown purpose")
        if self.eligibility is SampleEligibility.PRODUCTION_ELIGIBLE and missing:
            raise ValueError("production-eligible sample cannot have missing purposes")
        if self.eligibility is SampleEligibility.FRAMEWORK_ONLY and not missing:
            raise ValueError("framework-only sample must declare missing authoritative purposes")
        if (
            self.eligibility is SampleEligibility.VALIDATION_READY_WITH_BLOCKED_CASES
            and not missing
        ):
            raise ValueError(
                "validation-ready-with-blocked-cases sample must declare missing purposes"
            )
        object.__setattr__(self, "missing_authoritative_purposes", missing)
        required = tuple(
            sorted(
                (
                    item if isinstance(item, SamplePurpose) else SamplePurpose(str(item))
                    for item in self.required_authoritative_purposes
                ),
                key=lambda item: item.value,
            )
        )
        if len(set(required)) != len(required):
            raise ValueError("required_authoritative_purposes must be unique")
        object.__setattr__(self, "required_authoritative_purposes", required)
        if self.previous_sample_spec_sha256 is not None and not _HEX64.fullmatch(
            self.previous_sample_spec_sha256
        ):
            raise ValueError("previous_sample_spec_sha256 must be a lowercase SHA-256")
        if required and not set(missing).issubset({item.value for item in required}):
            raise ValueError("missing purposes must be a subset of required authoritative purposes")
        if self.compute_sha256() != self.sample_spec_sha256:
            raise ValueError("sample_spec_sha256 does not bind the canonical sample content")

    def _unsigned_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "cases": [case.to_dict() for case in self.cases],
            "eligibility": self.eligibility.value,
            "missing_authoritative_purposes": list(self.missing_authoritative_purposes),
            "sample_spec_version": self.sample_spec_version,
        }
        if self.previous_sample_spec_sha256 is not None:
            payload["previous_sample_spec_sha256"] = self.previous_sample_spec_sha256
        if self.required_authoritative_purposes:
            payload["required_authoritative_purposes"] = [
                item.value for item in self.required_authoritative_purposes
            ]
        return payload

    def compute_sha256(self) -> str:
        return compute_sha256(_canonical_json(self._unsigned_payload()).encode("utf-8"))

    def to_canonical_json(self) -> str:
        payload = self._unsigned_payload()
        payload["sample_spec_sha256"] = self.sample_spec_sha256
        return _canonical_json(payload)

    @property
    def fixed_sample_gate_eligible(self) -> bool:
        return self.eligibility in (
            SampleEligibility.PRODUCTION_ELIGIBLE,
            SampleEligibility.VALIDATION_READY_WITH_BLOCKED_CASES,
        )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def fixed_sample_from_dict(payload: Mapping[str, object]) -> FixedSampleSpec:
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("cases must be a JSON array")
    cases: list[FixedSampleCase] = []
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise ValueError("each case must be a JSON object")
        reference = raw.get("reference")
        if not isinstance(reference, dict):
            raise ValueError("reference must be a JSON object")
        units = raw.get("expected_units")
        if not isinstance(units, dict):
            raise ValueError("expected_units must be a JSON object")
        cases.append(
            FixedSampleCase(
                case_id=str(raw["case_id"]),
                dataset=DatasetKind(str(raw["dataset"])),
                code=str(raw["code"]) if raw["code"] is not None else None,
                exchange=str(raw["exchange"]),
                start_date=TradingDate.from_iso(str(raw["start_date"])),
                end_date=TradingDate.from_iso(str(raw["end_date"])),
                fields=tuple(str(item) for item in raw["fields"]),
                expected_units={str(key): str(value) for key, value in units.items()},
                purpose=SamplePurpose(str(raw["purpose"])),
                applicable_sources=tuple(str(item) for item in raw["applicable_sources"]),
                reference=SampleReference(
                    authority=str(reference["authority"]),
                    locator=str(reference["locator"]),
                    reference_version=str(reference["reference_version"]),
                    reviewed_at=TradingDate.from_iso(str(reference["reviewed_at"])),
                ),
            )
        )
    missing = payload.get("missing_authoritative_purposes")
    if not isinstance(missing, list):
        raise ValueError("missing_authoritative_purposes must be a JSON array")
    required = payload.get("required_authoritative_purposes", [])
    if not isinstance(required, list):
        raise ValueError("required_authoritative_purposes must be a JSON array")
    return FixedSampleSpec(
        sample_spec_version=str(payload["sample_spec_version"]),
        cases=tuple(cases),
        eligibility=SampleEligibility(str(payload["eligibility"])),
        missing_authoritative_purposes=tuple(str(item) for item in missing),
        sample_spec_sha256=str(payload["sample_spec_sha256"]),
        previous_sample_spec_sha256=(
            str(payload["previous_sample_spec_sha256"])
            if payload.get("previous_sample_spec_sha256") is not None
            else None
        ),
        required_authoritative_purposes=tuple(SamplePurpose(str(item)) for item in required),
    )


def _base_payload_and_spec() -> tuple[dict[str, object], FixedSampleSpec]:
    resource = files("quantking.data.validation").joinpath("fixed_sample.v1.json")
    raw = resource.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("packaged fixed sample must be a JSON object")
    base = fixed_sample_from_dict(payload)
    if (
        base.to_canonical_json().encode("utf-8") != raw.rstrip(b"\r\n")
        or base.sample_spec_sha256 != FIXED_SAMPLE_V1_SHA256
        or compute_sha256(raw) != FIXED_SAMPLE_V1_BYTES_SHA256
    ):
        raise ValueError("packaged fixed sample JSON must use canonical bytes")
    return payload, base


def load_fixed_sample_v1() -> FixedSampleSpec:
    return _base_payload_and_spec()[1]


def load_fixed_sample_v2() -> FixedSampleSpec:
    payload, base = _base_payload_and_spec()
    release_resource = files("quantking.data.validation").joinpath("fixed_sample.release.v2.json")
    release_bytes = release_resource.read_bytes()
    release = json.loads(release_bytes)
    if (
        not isinstance(release, dict)
        or release.get("base_sample_spec_sha256") != base.sample_spec_sha256
        or release.get("sample_spec_sha256") != FIXED_SAMPLE_V2_SHA256
        or compute_sha256(release_bytes) != FIXED_SAMPLE_V2_RELEASE_BYTES_SHA256
        or set(release)
        != {
            "base_sample_spec_sha256",
            "sample_spec_sha256",
            "sample_spec_version",
        }
    ):
        raise ValueError("fixed sample release does not bind the immutable base")
    promoted = dict(payload)
    promoted["sample_spec_version"] = release["sample_spec_version"]
    promoted["sample_spec_sha256"] = release["sample_spec_sha256"]
    spec = fixed_sample_from_dict(promoted)
    return spec


def packaged_v3_release_payload() -> dict[str, object]:
    resource = files("quantking.data.validation").joinpath("fixed_sample.release.v3.json")
    raw = resource.read_bytes()
    payload = json.loads(raw)
    if (
        not isinstance(payload, dict)
        or raw.rstrip(b"\r\n") != _canonical_json(payload).encode("utf-8")
        or compute_sha256(raw) != FIXED_SAMPLE_V3_RELEASE_BYTES_SHA256
    ):
        raise ValueError("controlled v3 release bytes are invalid")
    return dict(payload)


def fixed_sample_v3_from_release(release: Mapping[str, object]) -> FixedSampleSpec:
    controlled = packaged_v3_release_payload()
    if dict(release) != controlled:
        raise ValueError("controlled v3 release content mismatch")
    v2 = load_fixed_sample_v2()
    expected_fields = {
        "eligibility",
        "missing_authoritative_purposes",
        "previous_sample_spec_sha256",
        "required_authoritative_purposes",
        "sample_spec_sha256",
        "sample_spec_version",
    }
    if (
        set(release) != expected_fields
        or release["sample_spec_version"] != "s1-07.fixed-sample.v3"
        or release["sample_spec_sha256"] != FIXED_SAMPLE_V3_SHA256
        or release["previous_sample_spec_sha256"] != v2.sample_spec_sha256
        or release["eligibility"] != SampleEligibility.FRAMEWORK_ONLY.value
    ):
        raise ValueError("controlled v3 release lineage mismatch")
    payload, _base = _base_payload_and_spec()
    promoted = dict(payload)
    promoted.update(release)
    return fixed_sample_from_dict(promoted)


def load_fixed_sample_v3() -> FixedSampleSpec:
    return fixed_sample_v3_from_release(packaged_v3_release_payload())


def packaged_v4_release_payload() -> dict[str, object]:
    resource = files("quantking.data.validation").joinpath("fixed_sample.release.v4.json")
    raw = resource.read_bytes()
    payload = json.loads(raw)
    if (
        not isinstance(payload, dict)
        or raw.rstrip(b"\r\n") != _canonical_json(payload).encode("utf-8")
        or compute_sha256(raw) != FIXED_SAMPLE_V4_RELEASE_BYTES_SHA256
    ):
        raise ValueError("controlled v4 release bytes are invalid")
    return dict(payload)


def fixed_sample_v4_from_release(release: Mapping[str, object]) -> FixedSampleSpec:
    controlled = packaged_v4_release_payload()
    if dict(release) != controlled:
        raise ValueError("controlled v4 release content mismatch")
    v3 = load_fixed_sample_v3()
    expected_fields = {
        "additional_cases",
        "eligibility",
        "missing_authoritative_purposes",
        "previous_sample_spec_sha256",
        "required_authoritative_purposes",
        "sample_spec_sha256",
        "sample_spec_version",
    }
    if (
        set(release) != expected_fields
        or release["sample_spec_version"] != "s1-07.fixed-sample.v4"
        or release["sample_spec_sha256"] != FIXED_SAMPLE_V4_SHA256
        or release["previous_sample_spec_sha256"] != v3.sample_spec_sha256
        or release["eligibility"] != SampleEligibility.VALIDATION_READY_WITH_BLOCKED_CASES.value
    ):
        raise ValueError("controlled v4 release lineage mismatch")
    payload, _base = _base_payload_and_spec()
    promoted = dict(payload)
    # Combine v1 base cases with the additional authority cases declared by v4.
    additional_cases = cast("list[dict[str, object]]", release["additional_cases"])
    promoted["cases"] = cast("list[dict[str, object]]", payload["cases"]) + additional_cases
    # Promote v4 metadata; fixed_sample_from_dict reads these top-level keys.
    promoted["eligibility"] = release["eligibility"]
    promoted["missing_authoritative_purposes"] = release["missing_authoritative_purposes"]
    promoted["previous_sample_spec_sha256"] = release["previous_sample_spec_sha256"]
    promoted["required_authoritative_purposes"] = release["required_authoritative_purposes"]
    promoted["sample_spec_version"] = release["sample_spec_version"]
    promoted["sample_spec_sha256"] = release["sample_spec_sha256"]
    return fixed_sample_from_dict(promoted)


def load_fixed_sample_v4() -> FixedSampleSpec:
    return fixed_sample_v4_from_release(packaged_v4_release_payload())


def load_packaged_fixed_sample() -> FixedSampleSpec:
    """Load the S1-07A v2 sample used by already-accepted evidence contracts."""
    return load_fixed_sample_v2()


__all__ = [
    "FIXED_SAMPLE_V1_BYTES_SHA256",
    "FIXED_SAMPLE_V1_SHA256",
    "FIXED_SAMPLE_V2_RELEASE_BYTES_SHA256",
    "FIXED_SAMPLE_V2_SHA256",
    "FIXED_SAMPLE_V3_RELEASE_BYTES_SHA256",
    "FIXED_SAMPLE_V3_SHA256",
    "FIXED_SAMPLE_V4_RELEASE_BYTES_SHA256",
    "FIXED_SAMPLE_V4_SHA256",
    "CaseEligibility",
    "FixedSampleCase",
    "FixedSampleSpec",
    "SampleEligibility",
    "SamplePurpose",
    "SampleReference",
    "fixed_sample_from_dict",
    "fixed_sample_v3_from_release",
    "fixed_sample_v4_from_release",
    "load_fixed_sample_v1",
    "load_fixed_sample_v2",
    "load_fixed_sample_v3",
    "load_fixed_sample_v4",
    "load_packaged_fixed_sample",
    "packaged_v3_release_payload",
    "packaged_v4_release_payload",
]
