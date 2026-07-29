"""Provider-neutral, versioned fixed-sample contract."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from types import MappingProxyType

from quantking.common.time import TradingDate
from quantking.data.adapters.warehouse.manifest import compute_sha256
from quantking.data.models import DatasetKind
from quantking.data.schemas import descriptor_for

_CASE_ID = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
_CODE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_SOURCE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class SamplePurpose(StrEnum):
    NORMAL = "NORMAL"
    IPO = "IPO"
    LISTING = "LISTING"
    DELISTING = "DELISTING"
    ST = "ST"
    SUSPENSION = "SUSPENSION"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    BENCHMARK = "BENCHMARK"


class SampleEligibility(StrEnum):
    FRAMEWORK_ONLY = "FRAMEWORK_ONLY"
    PRODUCTION_ELIGIBLE = "PRODUCTION_ELIGIBLE"


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
        object.__setattr__(self, "missing_authoritative_purposes", missing)
        if self.compute_sha256() != self.sample_spec_sha256:
            raise ValueError("sample_spec_sha256 does not bind the canonical sample content")

    def _unsigned_payload(self) -> dict[str, object]:
        return {
            "cases": [case.to_dict() for case in self.cases],
            "eligibility": self.eligibility.value,
            "missing_authoritative_purposes": list(self.missing_authoritative_purposes),
            "sample_spec_version": self.sample_spec_version,
        }

    def compute_sha256(self) -> str:
        return compute_sha256(_canonical_json(self._unsigned_payload()).encode("utf-8"))

    def to_canonical_json(self) -> str:
        payload = self._unsigned_payload()
        payload["sample_spec_sha256"] = self.sample_spec_sha256
        return _canonical_json(payload)

    @property
    def fixed_sample_gate_eligible(self) -> bool:
        return self.eligibility is SampleEligibility.PRODUCTION_ELIGIBLE


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
    return FixedSampleSpec(
        sample_spec_version=str(payload["sample_spec_version"]),
        cases=tuple(cases),
        eligibility=SampleEligibility(str(payload["eligibility"])),
        missing_authoritative_purposes=tuple(str(item) for item in missing),
        sample_spec_sha256=str(payload["sample_spec_sha256"]),
    )


def load_packaged_fixed_sample() -> FixedSampleSpec:
    resource = files("quantking.data.validation").joinpath("fixed_sample.v1.json")
    payload = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("packaged fixed sample must be a JSON object")
    spec = fixed_sample_from_dict(payload)
    if spec.to_canonical_json() != resource.read_text(encoding="utf-8").strip():
        raise ValueError("packaged fixed sample JSON must use canonical bytes")
    return spec


__all__ = [
    "FixedSampleCase",
    "FixedSampleSpec",
    "SampleEligibility",
    "SamplePurpose",
    "SampleReference",
    "fixed_sample_from_dict",
    "load_packaged_fixed_sample",
]
