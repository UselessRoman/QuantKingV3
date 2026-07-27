"""Replay-safe EOD command application service."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from quantking.data.eod.contracts import (
    EOD_CONFIG_SCHEMA,
    EOD_JOB_TYPE,
    EOD_REQUEST_MANIFEST_SCHEMA,
    EodDatasetConfig,
    EodRunRequest,
    canonical_json,
    canonical_request_json,
    request_sha256,
    sha256_text,
)
from quantking.data.models import DatasetKind
from quantking.data.schemas import descriptor_for
from quantking.jobs.models import JobStatus
from quantking.jobs.services import CreateJob, JobService

_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SAFE_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class EodExecutionConfig:
    """Operator-owned versions and incremental planning policy frozen at enqueue."""

    code_version: str
    adapter_version: str
    rule_version: str
    lookback_days: int
    max_days_per_chunk: int
    max_items_per_chunk: int
    datasets: Mapping[str, EodDatasetConfig]

    def __post_init__(self) -> None:
        for field in ("code_version", "adapter_version", "rule_version"):
            value = getattr(self, field)
            if not isinstance(value, str) or _SAFE_VERSION.fullmatch(value) is None:
                raise ValueError(f"{field} must be a safe stable identifier")
        if self.lookback_days < 0:
            raise ValueError("lookback_days must be non-negative")
        if self.max_days_per_chunk <= 0 or self.max_items_per_chunk <= 0:
            raise ValueError("chunk sizes must be positive")
        normalized: dict[str, EodDatasetConfig] = {}
        for key, config in self.datasets.items():
            DatasetKind(key)
            if not isinstance(config, EodDatasetConfig):
                raise TypeError("dataset config must be EodDatasetConfig")
            normalized[key] = config
        object.__setattr__(self, "datasets", MappingProxyType(dict(sorted(normalized.items()))))

    @classmethod
    def production(cls, *, code_version: str) -> EodExecutionConfig:
        """Conservative offline production composition; only calendar has a usable scope."""
        return cls(
            code_version=code_version,
            adapter_version="xtquant-readonly.v1",
            rule_version="quality-rules.v1",
            lookback_days=0,
            max_days_per_chunk=31,
            max_items_per_chunk=500,
            datasets={
                DatasetKind.EXCHANGE_CALENDAR.value: EodDatasetConfig(scope=("BJ", "SH", "SZ")),
                **{
                    dataset.value: EodDatasetConfig(scope=())
                    for dataset in DatasetKind
                    if dataset is not DatasetKind.EXCHANGE_CALENDAR
                },
            },
        )

    def to_dict(self, *, selected: tuple[str, ...]) -> dict[str, object]:
        missing = set(selected) - set(self.datasets)
        if missing:
            raise ValueError("selected dataset has no frozen execution config")
        return {
            "schema_version": EOD_CONFIG_SCHEMA,
            "code_version": self.code_version,
            "adapter_version": self.adapter_version,
            "rule_version": self.rule_version,
            "lookback_days": self.lookback_days,
            "max_days_per_chunk": self.max_days_per_chunk,
            "max_items_per_chunk": self.max_items_per_chunk,
            "datasets": {key: self.datasets[key].to_dict() for key in selected},
            "schema_versions": {key: descriptor_for(key).schema_version for key in selected},
        }


@dataclass(frozen=True, slots=True)
class EodEnqueueResult:
    job_id: str
    status: JobStatus
    row_version: int
    created: bool


class EodCommandService:
    """The single enqueue port shared by CLI and Web."""

    def __init__(self, jobs: JobService, *, config: EodExecutionConfig) -> None:
        if not isinstance(jobs, JobService):
            raise TypeError("jobs must be a JobService")
        if not isinstance(config, EodExecutionConfig):
            raise TypeError("config must be an EodExecutionConfig")
        self._jobs = jobs
        self._config = config

    def enqueue(
        self,
        request: EodRunRequest,
        *,
        idempotency_key: str,
        requester: str,
    ) -> EodEnqueueResult:
        if not isinstance(request, EodRunRequest):
            raise TypeError("request must be an EodRunRequest")
        if (
            not isinstance(idempotency_key, str)
            or _SAFE_IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None
            or idempotency_key.casefold() == "latest"
        ):
            raise ValueError("idempotency_key must be a safe opaque identifier")
        if not isinstance(requester, str) or not requester.strip():
            raise ValueError("requester must be non-empty text")
        request_json = canonical_request_json(request)
        request_sha = request_sha256(request)
        config_json = canonical_json(self._config.to_dict(selected=request.datasets))
        config_sha = sha256_text(config_json)
        log_key = hashlib.sha256(f"{EOD_JOB_TYPE}\0{idempotency_key}".encode()).hexdigest()
        outcome = self._jobs.create_job_result(
            CreateJob(
                job_type=EOD_JOB_TYPE,
                idempotency_key=idempotency_key,
                input_manifest={
                    "schema_version": EOD_REQUEST_MANIFEST_SCHEMA,
                    "request_json": request_json,
                    "request_sha256": request_sha,
                },
                config_manifest={
                    "schema_version": EOD_CONFIG_SCHEMA,
                    "config_json": config_json,
                    "config_sha256": config_sha,
                },
                version_manifest={
                    "adapter_version": self._config.adapter_version,
                    "code_version": self._config.code_version,
                    "rule_version": self._config.rule_version,
                    "schema_versions_sha256": sha256_text(
                        canonical_json(
                            {key: descriptor_for(key).schema_version for key in request.datasets}
                        )
                    ),
                },
                requester=requester,
                log_id=f"logs/jobs/eod-{log_key}.jsonl",
            )
        )
        return EodEnqueueResult(
            job_id=outcome.job.job_id,
            status=outcome.job.status,
            row_version=outcome.job.row_version,
            created=outcome.created,
        )


__all__ = ["EodCommandService", "EodEnqueueResult", "EodExecutionConfig"]
