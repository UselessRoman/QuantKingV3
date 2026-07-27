"""Strong, framework-free EOD command contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from types import MappingProxyType

from quantking.data.models import DatasetKind

EOD_JOB_TYPE = "data.eod.v1"
EOD_REQUEST_SCHEMA = "eod-request.v1"
EOD_REQUEST_MANIFEST_SCHEMA = "eod-request-manifest.v1"
EOD_CONFIG_SCHEMA = "eod-execution-config.v1"

_DATASET_ORDER = (
    DatasetKind.EXCHANGE_CALENDAR,
    DatasetKind.SECURITY_MASTER,
    DatasetKind.DAILY_BAR_UNADJUSTED,
    DatasetKind.ADJUSTMENT_FACTOR,
    DatasetKind.TRADING_STATUS,
    DatasetKind.BENCHMARK_INDEX_DAILY,
)
_DATASET_RANK = {item.value: index for index, item in enumerate(_DATASET_ORDER)}
_SAFE_SCOPE_PATTERN = re.compile(r"(?:SZ|SH|BJ|\d{6}\.(?:SZ|SH|BJ))\Z")


class EodPublishIntent(StrEnum):
    """The only Stage 1 EOD terminal action."""

    PUBLISH_AFTER_QUALITY = "PUBLISH_AFTER_QUALITY"


def canonical_json(value: Mapping[str, object]) -> str:
    """Serialize one exact JSON object for hashing and persistence."""
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EodRunRequest:
    """Caller-controlled EOD inputs; no paths or executable expressions."""

    start_date: date
    end_date: date
    datasets: tuple[str, ...]
    publish_intent: EodPublishIntent

    def __post_init__(self) -> None:
        if (
            not isinstance(self.start_date, date)
            or isinstance(self.start_date, type)
            or not isinstance(self.end_date, date)
            or isinstance(self.end_date, type)
        ):
            raise TypeError("start_date and end_date must be date values")
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        if not isinstance(self.datasets, tuple):
            raise TypeError("datasets must be a tuple")
        if not self.datasets:
            raise ValueError("datasets must not be empty")
        if any(not isinstance(item, str) for item in self.datasets):
            raise TypeError("datasets must contain text values")
        if len(set(self.datasets)) != len(self.datasets):
            raise ValueError("datasets must not contain duplicates")
        unknown = set(self.datasets) - set(_DATASET_RANK)
        if unknown:
            raise ValueError("datasets contain an unknown value")
        object.__setattr__(
            self,
            "datasets",
            tuple(sorted(self.datasets, key=_DATASET_RANK.__getitem__)),
        )
        if not isinstance(self.publish_intent, EodPublishIntent):
            raise TypeError("publish_intent must be an EodPublishIntent")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": EOD_REQUEST_SCHEMA,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "datasets": list(self.datasets),
            "publish_intent": self.publish_intent.value,
            "source": "XTQUANT",
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> EodRunRequest:
        expected = {
            "schema_version",
            "start_date",
            "end_date",
            "datasets",
            "publish_intent",
            "source",
        }
        if set(value) != expected:
            raise ValueError("EOD request fields do not match the frozen schema")
        if value.get("schema_version") != EOD_REQUEST_SCHEMA:
            raise ValueError("EOD request schema_version is unsupported")
        if value.get("source") != "XTQUANT":
            raise ValueError("EOD source must be XTQUANT")
        raw_datasets = value.get("datasets")
        if not isinstance(raw_datasets, list):
            raise TypeError("datasets must be a JSON array")
        try:
            start = date.fromisoformat(str(value["start_date"]))
            end = date.fromisoformat(str(value["end_date"]))
            intent = EodPublishIntent(str(value["publish_intent"]))
        except (KeyError, ValueError) as error:
            raise ValueError("EOD request contains an invalid date or publish intent") from error
        request = cls(
            start_date=start,
            end_date=end,
            datasets=tuple(raw_datasets),
            publish_intent=intent,
        )
        if request.to_dict() != dict(value):
            raise ValueError("EOD request is not in canonical form")
        return request


def canonical_request_json(request: EodRunRequest) -> str:
    if not isinstance(request, EodRunRequest):
        raise TypeError("request must be an EodRunRequest")
    return canonical_json(request.to_dict())


def request_sha256(request: EodRunRequest) -> str:
    return sha256_text(canonical_request_json(request))


@dataclass(frozen=True, slots=True)
class EodReferenceVersion:
    """One explicit, immutable reference input for quality rules."""

    dataset: str
    version_id: str
    content_sha256: str

    def __post_init__(self) -> None:
        if self.dataset not in _DATASET_RANK:
            raise ValueError("reference dataset is unknown")
        if not self.version_id.startswith("data_version:"):
            raise ValueError("reference version_id is invalid")
        if len(self.content_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.content_sha256
        ):
            raise ValueError("reference content_sha256 must be lowercase SHA-256")

    def to_dict(self) -> dict[str, str]:
        return {
            "dataset": self.dataset,
            "version_id": self.version_id,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class EodDatasetResult:
    dataset: str
    status: str
    batch_id: str
    version_id: str
    quality_run_id: str
    content_sha256: str

    def __post_init__(self) -> None:
        if self.dataset not in _DATASET_RANK:
            raise ValueError("result dataset is unknown")
        if self.status != "PUBLISHED":
            raise ValueError("completed EOD dataset result must be PUBLISHED")
        for field, prefix in (
            ("batch_id", "data_batch:"),
            ("version_id", "data_version:"),
            ("quality_run_id", "data_quality:"),
        ):
            if not getattr(self, field).startswith(prefix):
                raise ValueError(f"{field} uses an invalid namespace")
        if _SHA256_PATTERN.fullmatch(self.content_sha256) is None:
            raise ValueError("content_sha256 must be lowercase SHA-256")

    def to_dict(self) -> dict[str, str]:
        return {
            "dataset": self.dataset,
            "status": self.status,
            "batch_id": self.batch_id,
            "version_id": self.version_id,
            "quality_run_id": self.quality_run_id,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class EodRunResult:
    job_id: str
    datasets: tuple[EodDatasetResult, ...]

    def __post_init__(self) -> None:
        if not self.job_id.startswith("job:"):
            raise ValueError("job_id uses an invalid namespace")
        object.__setattr__(self, "datasets", tuple(self.datasets))
        names = tuple(item.dataset for item in self.datasets)
        if tuple(sorted(names, key=_DATASET_RANK.__getitem__)) != names:
            raise ValueError("dataset results must use deterministic order")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "eod-result.v1",
            "job_id": self.job_id,
            "status": "SUCCEEDED",
            "datasets": [item.to_dict() for item in self.datasets],
        }


_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class EodDatasetConfig:
    """Frozen planning and quality inputs for one allowlisted dataset."""

    scope: tuple[str, ...]
    known_coverage: tuple[tuple[str, str, str], ...] = ()
    references: Mapping[str, EodReferenceVersion] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isinstance(self.scope, tuple):
            raise TypeError("scope must be a tuple")
        if any(not isinstance(item, str) or not item for item in self.scope):
            raise ValueError("scope must contain non-empty text")
        if tuple(sorted(set(self.scope))) != self.scope:
            raise ValueError("scope must be sorted and unique")
        if any(_SAFE_SCOPE_PATTERN.fullmatch(item) is None for item in self.scope):
            raise ValueError("scope must contain canonical exchanges or security codes")
        coverage = tuple(sorted(self.known_coverage))
        if coverage != self.known_coverage:
            raise ValueError("known_coverage must be deterministically sorted")
        normalized: dict[str, EodReferenceVersion] = {}
        for key, reference in self.references.items():
            if not isinstance(key, str) or not key:
                raise ValueError("reference keys must be non-empty text")
            if not isinstance(reference, EodReferenceVersion):
                raise TypeError("references must contain EodReferenceVersion values")
            normalized[key] = reference
        object.__setattr__(self, "references", MappingProxyType(dict(sorted(normalized.items()))))

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": list(self.scope),
            "known_coverage": [
                {"scope": scope, "start": start, "end": end}
                for scope, start, end in self.known_coverage
            ],
            "references": {key: reference.to_dict() for key, reference in self.references.items()},
        }


def load_canonical_json(value: str) -> Mapping[str, object]:
    try:
        raw = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("manifest JSON is invalid") from error
    if not isinstance(raw, dict) or canonical_json(raw) != value:
        raise ValueError("manifest JSON is not canonical")
    return raw


__all__ = [
    "EOD_CONFIG_SCHEMA",
    "EOD_JOB_TYPE",
    "EOD_REQUEST_MANIFEST_SCHEMA",
    "EOD_REQUEST_SCHEMA",
    "EodDatasetConfig",
    "EodDatasetResult",
    "EodPublishIntent",
    "EodReferenceVersion",
    "EodRunRequest",
    "EodRunResult",
    "canonical_json",
    "canonical_request_json",
    "load_canonical_json",
    "request_sha256",
    "sha256_text",
]
