"""Replay-safe EOD command application service."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Protocol

from quantking.common.config import AppSettings
from quantking.data.adapters.sources.baostock import BAOSTOCK_UNIT_POLICY_VERSION
from quantking.data.adapters.sources.baostock_worker import BAOSTOCK_DISTRIBUTION_VERSION
from quantking.data.adapters.sources.units import AKSHARE_UNIT_POLICY_VERSION
from quantking.data.eod.contracts import (
    EOD_CONFIG_SCHEMA,
    EOD_JOB_TYPE,
    EOD_REQUEST_MANIFEST_SCHEMA,
    K4_EOD_CONFIG_SCHEMA,
    R2_EOD_CONFIG_SCHEMA,
    EodBaseVersion,
    EodDatasetConfig,
    EodRunRequest,
    canonical_json,
    canonical_request_json,
    request_sha256,
    sha256_text,
)
from quantking.data.eod.frozen import FrozenEodCommandError, parse_frozen_eod_command
from quantking.data.eod.release_manifest import (
    RELEASE_SAFETY_CAPS,
    ReleaseManifest,
)
from quantking.data.models import DatasetKind
from quantking.data.quality.models import K4_DAILY_RULE_VERSION
from quantking.data.resolution.models import (
    K2_RESOLUTION_RULE_VERSION,
    K3_RESOLUTION_RULE_VERSION,
    K4_RESOLUTION_RULE_VERSION,
)
from quantking.data.schemas import descriptor_for
from quantking.jobs.models import JobStatus
from quantking.jobs.services import CreateJob, JobService

_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SAFE_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class EodExecutionConfig:
    """Operator-owned versions and incremental planning policy frozen at enqueue."""

    code_version: str
    adapter_version: str
    rule_version: str
    resolution_rule_version: str
    source_route_version: str
    baostock_version: str
    baostock_unit_policy_version: str
    akshare_unit_policy_version: str
    planner_lookback_days: int
    adapter_context_lookback_days: int
    max_days_per_chunk: int
    max_items_per_chunk: int
    endpoint_invocation_limit: int
    c_endpoint_invocation_limit: int
    d_endpoint_invocation_limit: int
    estimated_http_request_limit: int
    estimated_baostock_protocol_message_limit: int
    legacy_import_manifest_sha256: str | None
    datasets: Mapping[str, EodDatasetConfig]
    release_manifest_sha256: str | None = None
    a_baostock_endpoint_limit: int | None = None
    b_tencent_endpoint_limit: int | None = None

    def __post_init__(self) -> None:
        r2 = (
            self.release_manifest_sha256 is not None
            or self.a_baostock_endpoint_limit is not None
            or self.b_tencent_endpoint_limit is not None
        )
        if r2 and (self.a_baostock_endpoint_limit is None or self.b_tencent_endpoint_limit is None):
            raise ValueError("R2 execution config requires both per-role endpoint limits")
        for field in (
            "code_version",
            "adapter_version",
            "rule_version",
            "resolution_rule_version",
            "source_route_version",
            "baostock_version",
            "baostock_unit_policy_version",
            "akshare_unit_policy_version",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or _SAFE_VERSION.fullmatch(value) is None:
                raise ValueError(f"{field} must be a safe stable identifier")
        for field in (
            "planner_lookback_days",
            "adapter_context_lookback_days",
            "max_days_per_chunk",
            "max_items_per_chunk",
            "endpoint_invocation_limit",
            "c_endpoint_invocation_limit",
            "d_endpoint_invocation_limit",
            "estimated_http_request_limit",
            "estimated_baostock_protocol_message_limit",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field} must be an integer")
        if self.planner_lookback_days != 0:
            raise ValueError("planner_lookback_days must remain zero")
        if self.adapter_context_lookback_days != 15:
            raise ValueError("adapter_context_lookback_days must equal 15")
        if self.max_days_per_chunk <= 0 or self.max_items_per_chunk <= 0:
            raise ValueError("chunk sizes must be positive")
        if self.max_days_per_chunk != 31 or self.max_items_per_chunk != 1:
            raise ValueError("S1-08 chunk sizes must equal 31 days and one symbol")
        if self.release_manifest_sha256 is not None and (
            not isinstance(self.release_manifest_sha256, str)
            or _SHA256.fullmatch(self.release_manifest_sha256) is None
        ):
            raise ValueError("release_manifest_sha256 must be lowercase SHA-256 or None")
        if r2:
            caps = RELEASE_SAFETY_CAPS
            if self.resolution_rule_version != K4_RESOLUTION_RULE_VERSION:
                raise ValueError("R2 execution config is bound to the K4 resolution rule")
            if self.d_endpoint_invocation_limit != 0:
                raise ValueError("R2 execution config keeps the D role retired")
            if self.legacy_import_manifest_sha256 is not None:
                raise ValueError("R2 execution config keeps legacy import manifest SHA null")
            assert self.a_baostock_endpoint_limit is not None
            assert self.b_tencent_endpoint_limit is not None
            for name, value in (
                ("a_baostock_endpoint_limit", self.a_baostock_endpoint_limit),
                ("b_tencent_endpoint_limit", self.b_tencent_endpoint_limit),
                ("c_endpoint_invocation_limit", self.c_endpoint_invocation_limit),
            ):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise TypeError(f"{name} must be an integer")
                if not 1 <= value <= caps["endpoint_invocations_per_role"]:
                    raise ValueError(f"{name} must stay within the safety cap")
            if self.endpoint_invocation_limit != (
                self.a_baostock_endpoint_limit
                + self.b_tencent_endpoint_limit
                + self.c_endpoint_invocation_limit
            ):
                raise ValueError("R2 endpoint total must equal the sum of the role limits")
            if self.endpoint_invocation_limit > caps["endpoint_invocations_total"]:
                raise ValueError("R2 endpoint total exceeds the safety cap")
            if self.estimated_http_request_limit != (
                self.b_tencent_endpoint_limit * 3 + self.c_endpoint_invocation_limit * 2
            ):
                raise ValueError("R2 HTTP budget must be derived from the role limits")
            if self.estimated_http_request_limit > caps["akshare_http_requests"]:
                raise ValueError("R2 HTTP budget exceeds the safety cap")
            if self.estimated_baostock_protocol_message_limit != (
                self.a_baostock_endpoint_limit * 3
            ):
                raise ValueError("R2 protocol budget must be derived from the A role limit")
            if self.estimated_baostock_protocol_message_limit > caps["baostock_protocol_messages"]:
                raise ValueError("R2 protocol budget exceeds the safety cap")
        elif self.resolution_rule_version == K2_RESOLUTION_RULE_VERSION:
            if (
                self.endpoint_invocation_limit != 12
                or self.c_endpoint_invocation_limit != 4
                or self.d_endpoint_invocation_limit != 0
                or self.estimated_http_request_limit != 24
                or self.estimated_baostock_protocol_message_limit != 0
            ):
                raise ValueError("K2 compatibility budget must remain 12/4/0/24/0")
        elif self.resolution_rule_version == K3_RESOLUTION_RULE_VERSION:
            if (
                self.endpoint_invocation_limit != 16
                or self.c_endpoint_invocation_limit != 4
                or self.d_endpoint_invocation_limit != 4
                or self.estimated_http_request_limit != 24
                or self.estimated_baostock_protocol_message_limit != 12
            ):
                raise ValueError("S1-07K3 network budget must remain 16/4/4/24/12")
        elif self.resolution_rule_version == K4_RESOLUTION_RULE_VERSION:
            if (
                self.endpoint_invocation_limit != 12
                or self.c_endpoint_invocation_limit != 4
                or self.d_endpoint_invocation_limit != 0
                or self.estimated_http_request_limit != 20
                or self.estimated_baostock_protocol_message_limit != 12
            ):
                raise ValueError("S1-07K4 network budget must remain 12/4/0/20/12")
        else:
            raise ValueError("resolution_rule_version is unsupported")
        if self.legacy_import_manifest_sha256 is not None and (
            not isinstance(self.legacy_import_manifest_sha256, str)
            or _SHA256.fullmatch(self.legacy_import_manifest_sha256) is None
        ):
            raise ValueError("legacy_import_manifest_sha256 must be lowercase SHA-256 or None")
        normalized: dict[str, EodDatasetConfig] = {}
        for key, config in self.datasets.items():
            DatasetKind(key)
            if not isinstance(config, EodDatasetConfig):
                raise TypeError("dataset config must be EodDatasetConfig")
            if config.base_version is not None:
                raise ValueError("runtime dataset config must not preselect a base_version")
            normalized[key] = config
        object.__setattr__(self, "datasets", MappingProxyType(dict(sorted(normalized.items()))))

    @classmethod
    def production_k4(cls, *, code_version: str, settings: AppSettings) -> EodExecutionConfig:
        """Freeze the historical K4-route config (frozen 12/4/0/20/12 budget).

        Retained for the frozen K4 evidence/probe route and its regression
        tests; new releases use the R2 manifest-driven configs
        (``production`` / ``from_release_manifest``).
        """
        if not isinstance(settings, AppSettings):
            raise TypeError("settings must be AppSettings")
        return cls(
            code_version=code_version,
            adapter_version="baostock-akshare-daily-bar.k4.v1",
            rule_version=K4_DAILY_RULE_VERSION,
            resolution_rule_version=K4_RESOLUTION_RULE_VERSION,
            source_route_version="BAO-TX-SINA.k4.v1",
            baostock_version=BAOSTOCK_DISTRIBUTION_VERSION,
            baostock_unit_policy_version=BAOSTOCK_UNIT_POLICY_VERSION,
            akshare_unit_policy_version=AKSHARE_UNIT_POLICY_VERSION,
            planner_lookback_days=0,
            adapter_context_lookback_days=15,
            max_days_per_chunk=31,
            max_items_per_chunk=1,
            endpoint_invocation_limit=12,
            c_endpoint_invocation_limit=4,
            d_endpoint_invocation_limit=0,
            estimated_http_request_limit=20,
            estimated_baostock_protocol_message_limit=12,
            legacy_import_manifest_sha256=None,
            datasets={
                DatasetKind.DAILY_BAR_UNADJUSTED.value: EodDatasetConfig(
                    scope=settings.eod_symbols
                ),
            },
        )

    @classmethod
    def production(cls, *, code_version: str, settings: AppSettings) -> EodExecutionConfig:
        """Freeze the R2 production scope from validated application settings.

        The production config no longer hardcodes the historical first-release
        budgets: per-role budgets are bounded by the independent R2 safety hard
        caps, and the worker preflight freezes the actual planned counts from
        the deterministic chunk inventory before the first provider call. A
        release manifest (``from_release_manifest``) tightens these bounds to
        the frozen per-release plan.
        """
        if not isinstance(settings, AppSettings):
            raise TypeError("settings must be AppSettings")
        caps = RELEASE_SAFETY_CAPS
        role_cap = caps["endpoint_invocations_per_role"]
        return cls(
            code_version=code_version,
            adapter_version="baostock-akshare-daily-bar.k4.v1",
            rule_version=K4_DAILY_RULE_VERSION,
            resolution_rule_version=K4_RESOLUTION_RULE_VERSION,
            source_route_version="BAO-TX-SINA.k4.v1",
            baostock_version=BAOSTOCK_DISTRIBUTION_VERSION,
            baostock_unit_policy_version=BAOSTOCK_UNIT_POLICY_VERSION,
            akshare_unit_policy_version=AKSHARE_UNIT_POLICY_VERSION,
            planner_lookback_days=0,
            adapter_context_lookback_days=15,
            max_days_per_chunk=31,
            max_items_per_chunk=1,
            endpoint_invocation_limit=role_cap * 3,
            c_endpoint_invocation_limit=role_cap,
            d_endpoint_invocation_limit=0,
            estimated_http_request_limit=role_cap * 3 + role_cap * 2,
            estimated_baostock_protocol_message_limit=role_cap * 3,
            legacy_import_manifest_sha256=None,
            datasets={
                DatasetKind.DAILY_BAR_UNADJUSTED.value: EodDatasetConfig(
                    scope=settings.eod_symbols
                ),
            },
            release_manifest_sha256=None,
            a_baostock_endpoint_limit=role_cap,
            b_tencent_endpoint_limit=role_cap,
        )

    @classmethod
    def from_release_manifest(
        cls,
        manifest: ReleaseManifest,
        *,
        code_version: str | None = None,
    ) -> EodExecutionConfig:
        """Freeze the execution config from a verified release manifest.

        Budgets bind exactly to the manifest's planned chunk inventory; the
        worker preflight re-derives the planned counts and fails closed when
        they do not match. The manifest SHA travels in every frozen command.
        """
        if not isinstance(manifest, ReleaseManifest):
            raise TypeError("manifest must be a ReleaseManifest")
        budgets = manifest.budgets
        return cls(
            code_version=code_version or manifest.code_version,
            adapter_version=manifest.adapter_version,
            rule_version=manifest.quality_rule_version,
            resolution_rule_version=manifest.resolution_rule_version,
            source_route_version=manifest.source_route_version,
            baostock_version=BAOSTOCK_DISTRIBUTION_VERSION,
            baostock_unit_policy_version=manifest.unit_policy_versions.get(
                "baostock", BAOSTOCK_UNIT_POLICY_VERSION
            ),
            akshare_unit_policy_version=manifest.unit_policy_versions.get(
                "akshare", AKSHARE_UNIT_POLICY_VERSION
            ),
            planner_lookback_days=0,
            adapter_context_lookback_days=15,
            max_days_per_chunk=manifest.max_days_per_chunk,
            max_items_per_chunk=1,
            endpoint_invocation_limit=budgets["endpoint_total"],
            c_endpoint_invocation_limit=budgets["c_sina_max_endpoints"],
            d_endpoint_invocation_limit=0,
            estimated_http_request_limit=budgets["akshare_http_max"],
            estimated_baostock_protocol_message_limit=budgets["baostock_protocol_max"],
            legacy_import_manifest_sha256=None,
            datasets={
                DatasetKind.DAILY_BAR_UNADJUSTED.value: EodDatasetConfig(scope=manifest.symbols),
            },
            release_manifest_sha256=manifest.manifest_sha256(),
            a_baostock_endpoint_limit=budgets["a_baostock_endpoints"],
            b_tencent_endpoint_limit=budgets["b_tencent_endpoints"],
        )

    @property
    def is_r2(self) -> bool:
        """Whether this config uses the R2 manifest-driven schema."""
        return (
            self.release_manifest_sha256 is not None
            or self.a_baostock_endpoint_limit is not None
            or self.b_tencent_endpoint_limit is not None
        )

    def to_dict(
        self,
        *,
        selected: tuple[str, ...],
        bases: Mapping[str, EodBaseVersion],
    ) -> dict[str, object]:
        missing = set(selected) - set(self.datasets)
        if missing:
            raise ValueError("selected dataset has no frozen execution config")
        if self.is_r2:
            config_schema = R2_EOD_CONFIG_SCHEMA
        else:
            config_schema = (
                K4_EOD_CONFIG_SCHEMA
                if self.resolution_rule_version == K4_RESOLUTION_RULE_VERSION
                else EOD_CONFIG_SCHEMA
            )
        datasets: dict[str, dict[str, object]] = {}
        for key in selected:
            dataset = replace(
                self.datasets[key],
                known_coverage=(),
                base_version=bases.get(key),
            ).to_dict()
            if config_schema not in {K4_EOD_CONFIG_SCHEMA, R2_EOD_CONFIG_SCHEMA}:
                # Historical v2/v3 configs never carried security_exceptions.
                dataset.pop("security_exceptions", None)
            datasets[key] = dataset
        payload: dict[str, object] = {
            "schema_version": config_schema,
            "code_version": self.code_version,
            "adapter_version": self.adapter_version,
            "rule_version": self.rule_version,
            "resolution_rule_version": self.resolution_rule_version,
            "source_route_version": self.source_route_version,
            "baostock_version": self.baostock_version,
            "baostock_unit_policy_version": self.baostock_unit_policy_version,
            "planner_lookback_days": self.planner_lookback_days,
            "adapter_context_lookback_days": self.adapter_context_lookback_days,
            "max_days_per_chunk": self.max_days_per_chunk,
            "max_items_per_chunk": self.max_items_per_chunk,
            "endpoint_invocation_limit": self.endpoint_invocation_limit,
            "c_endpoint_invocation_limit": self.c_endpoint_invocation_limit,
            "d_endpoint_invocation_limit": self.d_endpoint_invocation_limit,
            "estimated_http_request_limit": self.estimated_http_request_limit,
            "estimated_baostock_protocol_message_limit": (
                self.estimated_baostock_protocol_message_limit
            ),
            "legacy_import_manifest_sha256": self.legacy_import_manifest_sha256,
            "datasets": datasets,
            "schema_versions": {key: descriptor_for(key).schema_version for key in selected},
        }
        if config_schema is K4_EOD_CONFIG_SCHEMA or config_schema is R2_EOD_CONFIG_SCHEMA:
            payload["akshare_unit_policy_version"] = self.akshare_unit_policy_version
        if config_schema is R2_EOD_CONFIG_SCHEMA:
            payload["release_manifest_sha256"] = self.release_manifest_sha256
            payload["a_baostock_endpoint_limit"] = self.a_baostock_endpoint_limit
            payload["b_tencent_endpoint_limit"] = self.b_tencent_endpoint_limit
        return payload


@dataclass(frozen=True, slots=True)
class EodEnqueueResult:
    job_id: str
    status: JobStatus
    row_version: int
    created: bool


class EodBaseVersionResolver(Protocol):
    """Resolve and verify one exact published base; never select a latest alias."""

    def resolve(
        self,
        *,
        dataset: str,
        version_id: str,
        scope: tuple[str, ...],
    ) -> EodBaseVersion: ...


class EodCommandService:
    """The single enqueue port shared by CLI and Web."""

    def __init__(
        self,
        jobs: JobService,
        *,
        config: EodExecutionConfig,
        base_resolver: EodBaseVersionResolver | None = None,
    ) -> None:
        if not isinstance(jobs, JobService):
            raise TypeError("jobs must be a JobService")
        if not isinstance(config, EodExecutionConfig):
            raise TypeError("config must be an EodExecutionConfig")
        self._jobs = jobs
        self._config = config
        self._base_resolver = base_resolver

    def enqueue(
        self,
        request: EodRunRequest,
        *,
        idempotency_key: str,
        requester: str,
        retry_of_job_id: str | None = None,
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
        previous = None
        if retry_of_job_id is not None:
            previous = self._jobs.get_job(retry_of_job_id)
            if previous.job_type != EOD_JOB_TYPE or previous.status not in {
                JobStatus.FAILED,
                JobStatus.INTERRUPTED,
            }:
                raise ValueError("retry_of_job_id must reference a failed or interrupted EOD Job")
        request_json = canonical_request_json(request)
        request_sha = request_sha256(request)
        resolved_bases: dict[str, EodBaseVersion] = {}
        if request.base_versions:
            if self._base_resolver is None:
                raise ValueError("base version resolver is unavailable")
            for dataset in request.datasets:
                resolved_bases[dataset] = self._base_resolver.resolve(
                    dataset=dataset,
                    version_id=request.base_versions[dataset],
                    scope=self._config.datasets[dataset].scope,
                )
        config_json = canonical_json(
            self._config.to_dict(selected=request.datasets, bases=resolved_bases)
        )
        config_sha = sha256_text(config_json)
        version_manifest = {
            "adapter_version": self._config.adapter_version,
            "baostock_unit_policy_version": self._config.baostock_unit_policy_version,
            "baostock_version": self._config.baostock_version,
            "code_version": self._config.code_version,
            "resolution_rule_version": self._config.resolution_rule_version,
            "rule_version": self._config.rule_version,
            "source_route_version": self._config.source_route_version,
            "schema_versions_sha256": sha256_text(
                canonical_json(
                    {key: descriptor_for(key).schema_version for key in request.datasets}
                )
            ),
        }
        if self._config.resolution_rule_version == K4_RESOLUTION_RULE_VERSION:
            version_manifest["akshare_unit_policy_version"] = (
                self._config.akshare_unit_policy_version
            )
        if self._config.release_manifest_sha256 is not None:
            version_manifest["release_manifest_sha256"] = self._config.release_manifest_sha256
        if previous is not None:
            try:
                frozen_previous = parse_frozen_eod_command(
                    job_type=previous.job_type,
                    input_manifest=previous.input_manifest,
                    config_manifest=previous.config_manifest,
                    version_manifest=previous.version_manifest,
                )
            except FrozenEodCommandError as error:
                raise ValueError("retry source EOD Job has invalid frozen inputs") from error
            if (
                frozen_previous.request_sha256 != request_sha
                or frozen_previous.config_sha256 != config_sha
                or dict(previous.version_manifest) != version_manifest
            ):
                raise ValueError("retry inputs must exactly match the source EOD Job")
        log_key = hashlib.sha256(f"{EOD_JOB_TYPE}\0{idempotency_key}".encode()).hexdigest()
        input_manifest = {
            "schema_version": EOD_REQUEST_MANIFEST_SCHEMA,
            "request_json": request_json,
            "request_sha256": request_sha,
        }
        if retry_of_job_id is not None:
            input_manifest["retry_of_job_id"] = retry_of_job_id
        if self._config.is_r2:
            config_manifest_schema = R2_EOD_CONFIG_SCHEMA
        else:
            config_manifest_schema = (
                K4_EOD_CONFIG_SCHEMA
                if self._config.resolution_rule_version == K4_RESOLUTION_RULE_VERSION
                else EOD_CONFIG_SCHEMA
            )
        outcome = self._jobs.create_job_result(
            CreateJob(
                job_type=EOD_JOB_TYPE,
                idempotency_key=idempotency_key,
                input_manifest=input_manifest,
                config_manifest={
                    "schema_version": config_manifest_schema,
                    "config_json": config_json,
                    "config_sha256": config_sha,
                },
                version_manifest=version_manifest,
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


__all__ = [
    "EodBaseVersionResolver",
    "EodCommandService",
    "EodEnqueueResult",
    "EodExecutionConfig",
]
