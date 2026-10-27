"""Strong, framework-free EOD command contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from itertools import pairwise
from types import MappingProxyType

from quantking.common.ids import parse_entity_id
from quantking.data.models import DatasetKind

EOD_JOB_TYPE = "data.eod.v1"
EOD_REQUEST_SCHEMA = "eod-request.v1"
EOD_REQUEST_MANIFEST_SCHEMA = "eod-request-manifest.v1"
LEGACY_EOD_CONFIG_SCHEMA = "eod-execution-config.v2"
EOD_CONFIG_SCHEMA = "eod-execution-config.v3"
K4_EOD_CONFIG_SCHEMA = "eod-execution-config.v4"
R2_EOD_CONFIG_SCHEMA = "eod-execution-config.r2.v5"
# Domain names for current code. Serialized values and historical aliases
# remain frozen for existing Job manifests.
THREE_SOURCE_EOD_CONFIG_SCHEMA = K4_EOD_CONFIG_SCHEMA
MANIFEST_EOD_CONFIG_SCHEMA = R2_EOD_CONFIG_SCHEMA

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
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


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
    base_versions: Mapping[str, str]

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
        if not isinstance(self.base_versions, Mapping):
            raise TypeError("base_versions must be a mapping")
        normalized_bases: dict[str, str] = {}
        for dataset, version_id in self.base_versions.items():
            if not isinstance(dataset, str) or not isinstance(version_id, str):
                raise TypeError("base_versions must map strings to strings")
            if dataset not in _DATASET_RANK:
                raise ValueError("base_versions contains an unknown dataset")
            _entity_id(version_id, field="base version_id", namespace="data_version")
            if version_id.casefold() == "latest":
                raise ValueError("base version_id must be explicit")
            normalized_bases[dataset] = version_id
        if normalized_bases and set(normalized_bases) != set(self.datasets):
            raise ValueError("base_versions must be empty or exactly match datasets")
        object.__setattr__(
            self,
            "base_versions",
            MappingProxyType(
                {
                    dataset: normalized_bases[dataset]
                    for dataset in self.datasets
                    if dataset in normalized_bases
                }
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": EOD_REQUEST_SCHEMA,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "datasets": list(self.datasets),
            "base_versions": dict(self.base_versions),
            "publish_intent": self.publish_intent.value,
            "source": "AKSHARE",
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> EodRunRequest:
        if not isinstance(value, Mapping):
            raise TypeError("EOD request must be an object")
        expected = {
            "schema_version",
            "start_date",
            "end_date",
            "datasets",
            "base_versions",
            "publish_intent",
            "source",
        }
        if set(value) != expected:
            raise ValueError("EOD request fields do not match the frozen schema")
        if value.get("schema_version") != EOD_REQUEST_SCHEMA:
            raise ValueError("EOD request schema_version is unsupported")
        if value.get("source") != "AKSHARE":
            raise ValueError("EOD source must be AKSHARE")
        raw_datasets = value.get("datasets")
        raw_bases = value.get("base_versions")
        if not isinstance(raw_datasets, list):
            raise TypeError("datasets must be a JSON array")
        if not all(isinstance(item, str) for item in raw_datasets):
            raise TypeError("datasets must contain strings")
        if not isinstance(raw_bases, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in raw_bases.items()
        ):
            raise TypeError("base_versions must be a JSON object of strings")
        raw_start = value.get("start_date")
        raw_end = value.get("end_date")
        raw_intent = value.get("publish_intent")
        if not isinstance(raw_start, str) or not isinstance(raw_end, str):
            raise TypeError("EOD request dates must be ISO strings")
        if not isinstance(raw_intent, str):
            raise TypeError("publish_intent must be a string")
        try:
            start = date.fromisoformat(raw_start)
            end = date.fromisoformat(raw_end)
            intent = EodPublishIntent(raw_intent)
        except (KeyError, ValueError) as error:
            raise ValueError("EOD request contains an invalid date or publish intent") from error
        request = cls(
            start_date=start,
            end_date=end,
            datasets=tuple(raw_datasets),
            publish_intent=intent,
            base_versions=raw_bases,
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

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> EodReferenceVersion:
        if set(value) != {"dataset", "version_id", "content_sha256"}:
            raise ValueError("reference fields are invalid")
        if not all(isinstance(value[field], str) for field in value):
            raise TypeError("reference values must be strings")
        result = cls(
            dataset=value["dataset"],  # type: ignore[arg-type]
            version_id=value["version_id"],  # type: ignore[arg-type]
            content_sha256=value["content_sha256"],  # type: ignore[arg-type]
        )
        if result.to_dict() != dict(value):
            raise ValueError("reference is not canonical")
        return result


def _entity_id(value: object, *, field: str, namespace: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    parsed = parse_entity_id(value)
    if parsed.namespace != namespace:
        raise ValueError(f"{field} uses an invalid namespace")
    return str(parsed)


def _sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be lowercase SHA-256")
    return value


def _coverage(
    value: object,
    *,
    scope: tuple[str, ...] | None = None,
) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(value, tuple):
        raise TypeError("known_coverage must be a tuple")
    normalized: list[tuple[str, str, str]] = []
    for item in value:
        if not isinstance(item, tuple) or len(item) != 3:
            raise TypeError("known_coverage entries must be three-item tuples")
        raw_scope, raw_start, raw_end = item
        if not all(isinstance(part, str) for part in item):
            raise TypeError("known_coverage values must be strings")
        if _SAFE_SCOPE_PATTERN.fullmatch(raw_scope) is None:
            raise ValueError("known_coverage scope is invalid")
        if scope is not None and raw_scope not in scope:
            raise ValueError("known_coverage scope must be configured")
        try:
            start = date.fromisoformat(raw_start)
            end = date.fromisoformat(raw_end)
        except ValueError as error:
            raise ValueError("known_coverage dates must be canonical ISO dates") from error
        if start > end:
            raise ValueError("known_coverage start must not follow end")
        if raw_start != start.isoformat() or raw_end != end.isoformat():
            raise ValueError("known_coverage dates must be canonical ISO dates")
        normalized.append((raw_scope, raw_start, raw_end))
    ordered = tuple(sorted(normalized))
    if tuple(normalized) != ordered or len(set(normalized)) != len(normalized):
        raise ValueError("known_coverage must be sorted and unique")
    return ordered


@dataclass(frozen=True, slots=True)
class EodBaseVersion:
    """Frozen credentials for one explicit full-snapshot incremental base."""

    version_id: str
    dataset: str
    schema_version: str
    content_sha256: str
    manifest_sha256: str
    coverage: tuple[tuple[str, str, str], ...]

    def __post_init__(self) -> None:
        _entity_id(self.version_id, field="version_id", namespace="data_version")
        if self.dataset not in _DATASET_RANK:
            raise ValueError("base dataset is unknown")
        if not isinstance(self.schema_version, str) or not self.schema_version:
            raise ValueError("base schema_version must be non-empty text")
        _sha256(self.content_sha256, field="content_sha256")
        _sha256(self.manifest_sha256, field="manifest_sha256")
        object.__setattr__(self, "coverage", _coverage(self.coverage))

    def to_dict(self) -> dict[str, object]:
        return {
            "version_id": self.version_id,
            "dataset": self.dataset,
            "schema_version": self.schema_version,
            "content_sha256": self.content_sha256,
            "manifest_sha256": self.manifest_sha256,
            "coverage": [
                {"scope": scope, "start": start, "end": end} for scope, start, end in self.coverage
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> EodBaseVersion:
        expected = {
            "version_id",
            "dataset",
            "schema_version",
            "content_sha256",
            "manifest_sha256",
            "coverage",
        }
        if set(value) != expected:
            raise ValueError("base_version fields are invalid")
        raw_coverage = value["coverage"]
        if not isinstance(raw_coverage, list):
            raise TypeError("base_version coverage must be an array")
        coverage: list[tuple[str, str, str]] = []
        for item in raw_coverage:
            if not isinstance(item, dict) or set(item) != {"scope", "start", "end"}:
                raise ValueError("base_version coverage fields are invalid")
            parts = (item["scope"], item["start"], item["end"])
            if not all(isinstance(part, str) for part in parts):
                raise TypeError("base_version coverage values must be strings")
            coverage.append(parts)
        result = cls(
            version_id=value["version_id"],  # type: ignore[arg-type]
            dataset=value["dataset"],  # type: ignore[arg-type]
            schema_version=value["schema_version"],  # type: ignore[arg-type]
            content_sha256=value["content_sha256"],  # type: ignore[arg-type]
            manifest_sha256=value["manifest_sha256"],  # type: ignore[arg-type]
            coverage=tuple(coverage),
        )
        if result.to_dict() != dict(value):
            raise ValueError("base_version is not canonical")
        return result


@dataclass(frozen=True, slots=True)
class EodDatasetConfig:
    """Frozen planning and quality inputs for one allowlisted dataset."""

    scope: tuple[str, ...]
    known_coverage: tuple[tuple[str, str, str], ...] = ()
    base_version: EodBaseVersion | None = None
    references: Mapping[str, EodReferenceVersion] = MappingProxyType({})
    security_exceptions: Mapping[str, tuple[tuple[str, str], ...]] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isinstance(self.scope, tuple):
            raise TypeError("scope must be a tuple")
        if any(not isinstance(item, str) or not item for item in self.scope):
            raise ValueError("scope must contain non-empty text")
        if tuple(sorted(set(self.scope))) != self.scope:
            raise ValueError("scope must be sorted and unique")
        if any(_SAFE_SCOPE_PATTERN.fullmatch(item) is None for item in self.scope):
            raise ValueError("scope must contain canonical exchanges or security codes")
        coverage = _coverage(self.known_coverage, scope=self.scope)
        if self.base_version is None:
            if coverage:
                raise ValueError("known_coverage requires a verified explicit base_version")
        else:
            if not isinstance(self.base_version, EodBaseVersion):
                raise TypeError("base_version must be an EodBaseVersion")
            if self.base_version.dataset not in _DATASET_RANK:
                raise ValueError("base_version dataset is invalid")
            if not coverage:
                coverage = self.base_version.coverage
            if coverage != self.base_version.coverage:
                raise ValueError("known_coverage must be derived from base_version coverage")
            if any(item[0] not in self.scope for item in coverage):
                raise ValueError("base_version coverage scope must be configured")
        object.__setattr__(self, "known_coverage", coverage)
        normalized: dict[str, EodReferenceVersion] = {}
        for key, reference in self.references.items():
            if not isinstance(key, str) or not key:
                raise ValueError("reference keys must be non-empty text")
            if not isinstance(reference, EodReferenceVersion):
                raise TypeError("references must contain EodReferenceVersion values")
            normalized[key] = reference
        object.__setattr__(self, "references", MappingProxyType(dict(sorted(normalized.items()))))
        object.__setattr__(
            self, "security_exceptions", _security_exceptions(self.security_exceptions, self.scope)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": list(self.scope),
            "known_coverage": [
                {"scope": scope, "start": start, "end": end}
                for scope, start, end in self.known_coverage
            ],
            "base_version": (None if self.base_version is None else self.base_version.to_dict()),
            "references": {key: reference.to_dict() for key, reference in self.references.items()},
            "security_exceptions": {
                code: [[start, end] for start, end in ranges]
                for code, ranges in self.security_exceptions.items()
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> EodDatasetConfig:
        if set(value) == {"scope", "known_coverage", "base_version", "references"}:
            has_exceptions = False
            raw_exceptions: Mapping[str, object] = {}
        elif set(value) == {
            "scope",
            "known_coverage",
            "base_version",
            "references",
            "security_exceptions",
        }:
            has_exceptions = True
            candidate = value["security_exceptions"]
            if not isinstance(candidate, Mapping):
                raise TypeError("security_exceptions must be an object")
            raw_exceptions = candidate
        else:
            raise ValueError("dataset config fields are invalid")
        scope = value["scope"]
        coverage = value["known_coverage"]
        references = value["references"]
        base = value["base_version"]
        if not isinstance(scope, list) or not all(isinstance(item, str) for item in scope):
            raise TypeError("dataset scope must be an array of strings")
        if not isinstance(coverage, list):
            raise TypeError("known_coverage must be an array")
        coverage_values: list[tuple[str, str, str]] = []
        for item in coverage:
            if not isinstance(item, Mapping) or set(item) != {"scope", "start", "end"}:
                raise ValueError("known_coverage fields are invalid")
            parts = (item["scope"], item["start"], item["end"])
            if not all(isinstance(part, str) for part in parts):
                raise TypeError("known_coverage values must be strings")
            coverage_values.append((str(parts[0]), str(parts[1]), str(parts[2])))
        if base is not None and not isinstance(base, Mapping):
            raise TypeError("base_version must be an object or null")
        if not isinstance(references, Mapping):
            raise TypeError("references must be an object")
        parsed_references: dict[str, EodReferenceVersion] = {}
        for key, item in references.items():
            if not isinstance(key, str) or not isinstance(item, Mapping):
                raise TypeError("reference entries are invalid")
            parsed_references[key] = EodReferenceVersion.from_dict(item)
        parsed_exceptions: dict[str, tuple[tuple[str, str], ...]] = {}
        for code, item in raw_exceptions.items():
            if not isinstance(code, str) or not isinstance(item, list):
                raise TypeError("security exception entries are invalid")
            parsed_exceptions[code] = tuple(_exception_range(pair, code=code) for pair in item)
        result = cls(
            scope=tuple(scope),
            known_coverage=tuple(coverage_values),
            base_version=None if base is None else EodBaseVersion.from_dict(base),
            references=parsed_references,
            security_exceptions=parsed_exceptions,
        )
        expected = dict(result.to_dict())
        if not has_exceptions:
            # Historical v2/v3 configs keep their four-field shape canonical.
            expected.pop("security_exceptions", None)
        if expected != dict(value):
            raise ValueError("dataset config is not canonical")
        return result


def _exception_range(value: object, *, code: str) -> tuple[str, str]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(item, str) for item in value)
    ):
        raise TypeError("security exception range must be [start, end] ISO dates")
    start, end = value[0], value[1]
    try:
        parsed_start = date.fromisoformat(start)
        parsed_end = date.fromisoformat(end)
    except ValueError as error:
        raise ValueError(f"security exception dates for {code} must be ISO dates") from error
    if parsed_end < parsed_start:
        raise ValueError("security exception end must not precede start")
    if start != parsed_start.isoformat() or end != parsed_end.isoformat():
        raise ValueError("security exception dates must be canonical ISO text")
    return start, end


def _security_exceptions(
    value: Mapping[str, tuple[tuple[str, str], ...]], scope: tuple[str, ...]
) -> Mapping[str, tuple[tuple[str, str], ...]]:
    if not isinstance(value, Mapping):
        raise TypeError("security_exceptions must be a mapping")
    normalized: dict[str, tuple[tuple[str, str], ...]] = {}
    for code, ranges in value.items():
        if not isinstance(code, str) or not code.strip():
            raise ValueError("security_exceptions key must be non-empty text")
        if code not in scope:
            raise ValueError("security_exceptions reference securities outside the scope")
        if not isinstance(ranges, tuple):
            raise TypeError("security_exceptions value must be a tuple of date ranges")
        parsed: list[tuple[str, str]] = []
        for pair in ranges:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise TypeError("security exception range must be a (start, end) pair")
            parsed.append(_exception_range(list(pair), code=code))
        parsed.sort()
        if len(set(parsed)) != len(parsed):
            raise ValueError("security exception ranges must be unique")
        for previous, current in pairwise(parsed):
            if current[0] <= previous[1]:
                raise ValueError("security exception ranges must not overlap")
        normalized[code] = tuple(parsed)
    return MappingProxyType(dict(sorted(normalized.items())))


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
    "K4_EOD_CONFIG_SCHEMA",
    "LEGACY_EOD_CONFIG_SCHEMA",
    "MANIFEST_EOD_CONFIG_SCHEMA",
    "R2_EOD_CONFIG_SCHEMA",
    "THREE_SOURCE_EOD_CONFIG_SCHEMA",
    "EodBaseVersion",
    "EodDatasetConfig",
    "EodPublishIntent",
    "EodReferenceVersion",
    "EodRunRequest",
    "canonical_json",
    "canonical_request_json",
    "load_canonical_json",
    "request_sha256",
    "sha256_text",
]
