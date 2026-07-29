"""Canonical fixed-sample request manifests and run fingerprints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from quantking.data.models import DatasetKind
from quantking.data.validation.samples import FixedSampleSpec


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class ValidationRequestManifest:
    source: str
    dataset: DatasetKind
    sample_spec_version: str
    sample_spec_sha256: str
    cases: tuple[dict[str, object], ...]

    @classmethod
    def for_controlled_sample(
        cls, *, source: str, dataset: DatasetKind, sample: FixedSampleSpec
    ) -> ValidationRequestManifest:
        cases = tuple(
            case.to_dict()
            for case in sample.cases
            if case.dataset is dataset and source in case.applicable_sources
        )
        if not cases:
            raise ValueError("controlled sample has no case for source x dataset")
        return cls(
            source=source,
            dataset=dataset,
            sample_spec_version=sample.sample_spec_version,
            sample_spec_sha256=sample.sample_spec_sha256,
            cases=cases,
        )

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "cases": list(self.cases),
            "dataset": self.dataset.value,
            "request_schema_version": "source-validation-request.v1",
            "sample_spec_sha256": self.sample_spec_sha256,
            "sample_spec_version": self.sample_spec_version,
            "source": self.source,
        }

    @property
    def request_sha256(self) -> str:
        return sha256_json(self.unsigned_payload())

    def signed_payload(self) -> dict[str, object]:
        return {**self.unsigned_payload(), "request_sha256": self.request_sha256}

    def to_bytes(self) -> bytes:
        return canonical_json(self.signed_payload())


def verify_request_manifest(
    payload: bytes,
    *,
    source: str,
    dataset: DatasetKind,
    controlled_sample: FixedSampleSpec,
) -> ValidationRequestManifest:
    try:
        parsed: Any = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError("request manifest is invalid JSON") from error
    if not isinstance(parsed, dict) or payload != canonical_json(parsed):
        raise ValueError("request manifest must be canonical JSON")
    required = {
        "cases",
        "dataset",
        "request_schema_version",
        "request_sha256",
        "sample_spec_sha256",
        "sample_spec_version",
        "source",
    }
    if set(parsed) != required:
        raise ValueError("request manifest fields do not match the schema")
    declared_sha = parsed.pop("request_sha256")
    if declared_sha != sha256_json(parsed):
        raise ValueError("request manifest SHA-256 mismatch")
    expected = ValidationRequestManifest.for_controlled_sample(
        source=source,
        dataset=dataset,
        sample=controlled_sample,
    )
    if parsed != expected.unsigned_payload():
        raise ValueError("request manifest does not exactly cover the controlled fixed sample")
    return expected


def compute_run_fingerprint(
    *,
    source: str,
    dataset: DatasetKind,
    request_sha256: str,
    sample_sha256: str,
    versions: dict[str, str],
) -> str:
    expected_version_fields = {
        "adapter_version",
        "code_version",
        "config_version",
        "package_version",
        "rule_version",
        "sample_spec_version",
        "schema_version",
        "threshold_version",
    }
    if set(versions) != expected_version_fields:
        raise ValueError("run fingerprint versions do not match the frozen contract")
    return sha256_json(
        {
            "dataset": dataset.value,
            "request_sha256": request_sha256,
            "sample_sha256": sample_sha256,
            "source": source,
            "versions": dict(sorted(versions.items())),
        }
    )


__all__ = [
    "ValidationRequestManifest",
    "canonical_json",
    "compute_run_fingerprint",
    "sha256_json",
    "verify_request_manifest",
]
