"""Single strict parser for persisted and evidenced EOD commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from quantking.common.ids import parse_entity_id
from quantking.common.time import TradingDate
from quantking.data.eod.contracts import (
    EOD_CONFIG_SCHEMA,
    EOD_JOB_TYPE,
    EOD_REQUEST_MANIFEST_SCHEMA,
    K4_EOD_CONFIG_SCHEMA,
    LEGACY_EOD_CONFIG_SCHEMA,
    R2_EOD_CONFIG_SCHEMA,
    EodBaseVersion,
    EodDatasetConfig,
    EodPublishIntent,
    EodReferenceVersion,
    EodRunRequest,
    canonical_json,
    load_canonical_json,
    sha256_text,
)
from quantking.data.eod.release_manifest import RELEASE_SAFETY_CAPS
from quantking.data.incremental import CoverageWindow
from quantking.data.models import DatasetKind
from quantking.data.resolution.models import (
    K2_RESOLUTION_RULE_VERSION,
    K3_RESOLUTION_RULE_VERSION,
    K4_RESOLUTION_RULE_VERSION,
)
from quantking.data.schemas import descriptor_for


class FrozenEodCommandError(ValueError):
    """A persisted command is not exact, canonical, or internally bound."""


@dataclass(frozen=True, slots=True)
class FrozenEodDataset:
    dataset: DatasetKind
    schema_version: str
    scope: tuple[str, ...]
    known_coverage: tuple[CoverageWindow, ...]
    base_version: EodBaseVersion | None
    references: Mapping[str, EodReferenceVersion]
    security_exceptions: Mapping[str, tuple[tuple[TradingDate, TradingDate], ...]] = (
        MappingProxyType({})
    )


@dataclass(frozen=True, slots=True)
class FrozenEodCommand:
    request: EodRunRequest
    request_sha256: str
    config_sha256: str
    code_version: str
    adapter_version: str
    rule_version: str
    resolution_rule_version: str
    source_route_version: str
    baostock_version: str | None
    baostock_unit_policy_version: str | None
    akshare_unit_policy_version: str | None
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
    datasets: tuple[FrozenEodDataset, ...]
    retry_of_job_id: str | None = None
    release_manifest_sha256: str | None = None
    a_baostock_endpoint_limit: int = 4
    b_tencent_endpoint_limit: int = 4


def _integer(value: object, *, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise FrozenEodCommandError(f"{field} is invalid")
    return value


def parse_frozen_eod_command(
    *,
    job_type: str,
    input_manifest: Mapping[str, object],
    config_manifest: Mapping[str, object],
    version_manifest: Mapping[str, object],
) -> FrozenEodCommand:
    """Reconstruct one command from the exact three persisted manifests."""
    try:
        if job_type != EOD_JOB_TYPE:
            raise FrozenEodCommandError("job type is invalid")
        input_fields = frozenset(input_manifest)
        if (
            input_fields
            not in {
                frozenset({"schema_version", "request_json", "request_sha256"}),
                frozenset(
                    {
                        "schema_version",
                        "request_json",
                        "request_sha256",
                        "retry_of_job_id",
                    }
                ),
            }
            or input_manifest.get("schema_version") != EOD_REQUEST_MANIFEST_SCHEMA
        ):
            raise FrozenEodCommandError("request manifest fields are invalid")
        request_json = input_manifest["request_json"]
        request_sha = input_manifest["request_sha256"]
        if not isinstance(request_json, str) or not isinstance(request_sha, str):
            raise FrozenEodCommandError("request manifest values are invalid")
        if sha256_text(request_json) != request_sha:
            raise FrozenEodCommandError("request SHA does not match")
        request = EodRunRequest.from_dict(load_canonical_json(request_json))
        retry_of_job_id = input_manifest.get("retry_of_job_id")
        if retry_of_job_id is not None:
            if not isinstance(retry_of_job_id, str):
                raise FrozenEodCommandError("retry_of_job_id is invalid")
            parsed_retry = parse_entity_id(retry_of_job_id)
            if parsed_retry.namespace != "job" or str(parsed_retry) != retry_of_job_id:
                raise FrozenEodCommandError("retry_of_job_id is invalid")
        if request.publish_intent is not EodPublishIntent.PUBLISH_AFTER_QUALITY:
            raise FrozenEodCommandError("publish intent is invalid")

        if set(config_manifest) != {
            "schema_version",
            "config_json",
            "config_sha256",
        } or config_manifest.get("schema_version") not in {
            EOD_CONFIG_SCHEMA,
            K4_EOD_CONFIG_SCHEMA,
            R2_EOD_CONFIG_SCHEMA,
            LEGACY_EOD_CONFIG_SCHEMA,
        }:
            raise FrozenEodCommandError("config manifest fields are invalid")
        config_json = config_manifest["config_json"]
        config_sha = config_manifest["config_sha256"]
        if not isinstance(config_json, str) or not isinstance(config_sha, str):
            raise FrozenEodCommandError("config manifest values are invalid")
        if sha256_text(config_json) != config_sha:
            raise FrozenEodCommandError("config SHA does not match")
        config = load_canonical_json(config_json)
        schema_version = config.get("schema_version")
        legacy = schema_version == LEGACY_EOD_CONFIG_SCHEMA
        k4 = schema_version == K4_EOD_CONFIG_SCHEMA
        r2 = schema_version == R2_EOD_CONFIG_SCHEMA
        expected_config_fields = {
            "schema_version",
            "code_version",
            "adapter_version",
            "rule_version",
            "planner_lookback_days",
            "adapter_context_lookback_days",
            "max_days_per_chunk",
            "max_items_per_chunk",
            "endpoint_invocation_limit",
            "c_endpoint_invocation_limit",
            "estimated_http_request_limit",
            "datasets",
            "schema_versions",
        }
        if not legacy:
            expected_config_fields.update(
                {
                    "resolution_rule_version",
                    "source_route_version",
                    "baostock_version",
                    "baostock_unit_policy_version",
                    "d_endpoint_invocation_limit",
                    "estimated_baostock_protocol_message_limit",
                    "legacy_import_manifest_sha256",
                }
            )
        config_resolution_rule = None if legacy else config.get("resolution_rule_version")
        if k4 or r2:
            expected_config_fields.add("akshare_unit_policy_version")
        if r2:
            expected_config_fields.update(
                {
                    "release_manifest_sha256",
                    "a_baostock_endpoint_limit",
                    "b_tencent_endpoint_limit",
                }
            )
        if set(config) != expected_config_fields or schema_version != config_manifest.get(
            "schema_version"
        ):
            raise FrozenEodCommandError("config fields are invalid")
        if (
            not legacy
            and schema_version == EOD_CONFIG_SCHEMA
            and (config_resolution_rule != K3_RESOLUTION_RULE_VERSION)
        ):
            raise FrozenEodCommandError("v3 config schema is bound to the K3 resolution rule")
        if k4 and config_resolution_rule != K4_RESOLUTION_RULE_VERSION:
            raise FrozenEodCommandError("v4 config schema is bound to the K4 resolution rule")
        if r2 and config_resolution_rule != K4_RESOLUTION_RULE_VERSION:
            raise FrozenEodCommandError("R2 config schema is bound to the K4 resolution rule")
        raw_datasets = config["datasets"]
        raw_schemas = config["schema_versions"]
        if not isinstance(raw_datasets, Mapping) or not isinstance(raw_schemas, Mapping):
            raise FrozenEodCommandError("dataset config is invalid")
        if set(raw_datasets) != set(request.datasets) or set(raw_schemas) != set(request.datasets):
            raise FrozenEodCommandError("dataset config does not match request")
        plans: list[FrozenEodDataset] = []
        frozen_ids: dict[str, str] = {}
        for name in request.datasets:
            raw = raw_datasets[name]
            if not isinstance(raw, Mapping):
                raise FrozenEodCommandError("dataset plan is invalid")
            if k4:
                if "security_exceptions" not in raw:
                    raise FrozenEodCommandError("v4 dataset config requires security_exceptions")
            elif r2:
                if "security_exceptions" not in raw:
                    raise FrozenEodCommandError("R2 dataset config requires security_exceptions")
            elif "security_exceptions" in raw:
                raise FrozenEodCommandError(
                    "historical dataset configs must not carry security_exceptions"
                )
            dataset_config = EodDatasetConfig.from_dict(raw)
            expected_schema = descriptor_for(name).schema_version
            if raw_schemas[name] != expected_schema:
                raise FrozenEodCommandError("schema version is invalid")
            if dataset_config.base_version is not None:
                if dataset_config.base_version.dataset != name:
                    raise FrozenEodCommandError("base dataset does not match")
                frozen_ids[name] = dataset_config.base_version.version_id
            plans.append(
                FrozenEodDataset(
                    dataset=DatasetKind(name),
                    schema_version=expected_schema,
                    scope=dataset_config.scope,
                    known_coverage=tuple(
                        CoverageWindow(
                            scope=scope,
                            start=TradingDate.from_iso(start),
                            end=TradingDate.from_iso(end),
                        )
                        for scope, start, end in dataset_config.known_coverage
                    ),
                    base_version=dataset_config.base_version,
                    references=dataset_config.references,
                    security_exceptions={
                        code: tuple(
                            (TradingDate.from_iso(start), TradingDate.from_iso(end))
                            for start, end in ranges
                        )
                        for code, ranges in dataset_config.security_exceptions.items()
                    },
                )
            )
        if frozen_ids != dict(request.base_versions):
            raise FrozenEodCommandError("frozen bases do not match public request")
        expected_versions = {
            "adapter_version": config["adapter_version"],
            "code_version": config["code_version"],
            "rule_version": config["rule_version"],
            "schema_versions_sha256": sha256_text(canonical_json(dict(raw_schemas))),
        }
        if not legacy:
            expected_versions.update(
                {
                    "baostock_unit_policy_version": config["baostock_unit_policy_version"],
                    "baostock_version": config["baostock_version"],
                    "resolution_rule_version": config["resolution_rule_version"],
                    "source_route_version": config["source_route_version"],
                }
            )
            if k4:
                expected_versions["akshare_unit_policy_version"] = config[
                    "akshare_unit_policy_version"
                ]
            if r2:
                expected_versions["akshare_unit_policy_version"] = config[
                    "akshare_unit_policy_version"
                ]
                if config["release_manifest_sha256"] is not None:
                    expected_versions["release_manifest_sha256"] = config["release_manifest_sha256"]
        if dict(version_manifest) != expected_versions or not all(
            isinstance(item, str) and item for item in version_manifest.values()
        ):
            raise FrozenEodCommandError("version manifest does not match config")
        planner_lookback = _integer(
            config["planner_lookback_days"], field="planner_lookback_days", minimum=0
        )
        adapter_context_lookback = _integer(
            config["adapter_context_lookback_days"],
            field="adapter_context_lookback_days",
            minimum=0,
        )
        max_days = _integer(config["max_days_per_chunk"], field="max_days_per_chunk", minimum=1)
        max_items = _integer(config["max_items_per_chunk"], field="max_items_per_chunk", minimum=1)
        endpoint_limit = _integer(
            config["endpoint_invocation_limit"],
            field="endpoint_invocation_limit",
            minimum=1,
        )
        c_endpoint_limit = _integer(
            config["c_endpoint_invocation_limit"],
            field="c_endpoint_invocation_limit",
            minimum=1,
        )
        d_endpoint_limit = (
            0
            if legacy
            else _integer(
                config["d_endpoint_invocation_limit"],
                field="d_endpoint_invocation_limit",
                minimum=0,
            )
        )
        http_limit = _integer(
            config["estimated_http_request_limit"],
            field="estimated_http_request_limit",
            minimum=1,
        )
        baostock_protocol_limit = (
            0
            if legacy
            else _integer(
                config["estimated_baostock_protocol_message_limit"],
                field="estimated_baostock_protocol_message_limit",
                minimum=0,
            )
        )
        if legacy and (
            planner_lookback != 0
            or adapter_context_lookback != 15
            or max_days != 31
            or max_items != 1
            or endpoint_limit != 12
            or c_endpoint_limit != 4
            or http_limit != 24
        ):
            raise FrozenEodCommandError("S1-08 frozen execution policy is invalid")
        resolution_rule = None if legacy else config["resolution_rule_version"]
        if r2:
            caps = RELEASE_SAFETY_CAPS
            release_manifest_sha = config["release_manifest_sha256"]
            if release_manifest_sha is not None and (
                not isinstance(release_manifest_sha, str) or len(release_manifest_sha) != 64
            ):
                raise FrozenEodCommandError("R2 release manifest SHA is invalid")
            a_limit = _integer(
                config["a_baostock_endpoint_limit"],
                field="a_baostock_endpoint_limit",
                minimum=1,
            )
            b_limit = _integer(
                config["b_tencent_endpoint_limit"],
                field="b_tencent_endpoint_limit",
                minimum=1,
            )
            r2_invalid = (
                planner_lookback != 0
                or adapter_context_lookback != 15
                or max_days != 31
                or max_items != 1
                or resolution_rule != K4_RESOLUTION_RULE_VERSION
                or d_endpoint_limit != 0
                or a_limit > caps["endpoint_invocations_per_role"]
                or b_limit > caps["endpoint_invocations_per_role"]
                or c_endpoint_limit > caps["endpoint_invocations_per_role"]
                or endpoint_limit != a_limit + b_limit + c_endpoint_limit
                or endpoint_limit > caps["endpoint_invocations_total"]
                or http_limit != b_limit * 3 + c_endpoint_limit * 2
                or http_limit > caps["akshare_http_requests"]
                or baostock_protocol_limit != a_limit * 3
                or baostock_protocol_limit > caps["baostock_protocol_messages"]
                or config["legacy_import_manifest_sha256"] is not None
            )
            if r2_invalid:
                raise FrozenEodCommandError("R2 frozen execution policy is invalid")
        if not legacy and not r2:
            common_invalid = (
                planner_lookback != 0
                or adapter_context_lookback != 15
                or max_days != 31
                or max_items != 1
                or c_endpoint_limit != 4
            )
            k2_invalid = resolution_rule == K2_RESOLUTION_RULE_VERSION and (
                endpoint_limit != 12
                or d_endpoint_limit != 0
                or http_limit != 24
                or baostock_protocol_limit != 0
            )
            k3_invalid = resolution_rule == K3_RESOLUTION_RULE_VERSION and (
                endpoint_limit != 16
                or d_endpoint_limit != 4
                or http_limit != 24
                or baostock_protocol_limit != 12
            )
            k4_invalid = resolution_rule == K4_RESOLUTION_RULE_VERSION and (
                endpoint_limit != 12
                or d_endpoint_limit != 0
                or http_limit != 20
                or baostock_protocol_limit != 12
            )
            unsupported = resolution_rule not in {
                K2_RESOLUTION_RULE_VERSION,
                K3_RESOLUTION_RULE_VERSION,
                K4_RESOLUTION_RULE_VERSION,
            }
            if common_invalid or unsupported or k2_invalid or k3_invalid or k4_invalid:
                raise FrozenEodCommandError("frozen execution policy is invalid")
        version_fields = ["code_version", "adapter_version", "rule_version"]
        if not legacy:
            version_fields.extend(
                [
                    "resolution_rule_version",
                    "source_route_version",
                    "baostock_version",
                    "baostock_unit_policy_version",
                ]
            )
            if k4:
                version_fields.append("akshare_unit_policy_version")
            if r2:
                version_fields.append("akshare_unit_policy_version")
        for field in version_fields:
            if not isinstance(config[field], str) or not config[field]:
                raise FrozenEodCommandError(f"{field} is invalid")
        legacy_import_manifest_sha = None if legacy else config["legacy_import_manifest_sha256"]
        if legacy_import_manifest_sha is not None and (
            not isinstance(legacy_import_manifest_sha, str)
            or len(legacy_import_manifest_sha) != 64
            or any(character not in "0123456789abcdef" for character in legacy_import_manifest_sha)
        ):
            raise FrozenEodCommandError("legacy import manifest SHA is invalid")
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, FrozenEodCommandError):
            raise
        raise FrozenEodCommandError("frozen EOD command is invalid") from error

    return FrozenEodCommand(
        request=request,
        request_sha256=request_sha,
        config_sha256=config_sha,
        code_version=config["code_version"],  # type: ignore[arg-type]
        adapter_version=config["adapter_version"],  # type: ignore[arg-type]
        rule_version=config["rule_version"],  # type: ignore[arg-type]
        resolution_rule_version=(
            K2_RESOLUTION_RULE_VERSION if legacy else cast(str, config["resolution_rule_version"])
        ),
        source_route_version=(
            "EAST-TX-SINA.k2.v1" if legacy else cast(str, config["source_route_version"])
        ),
        baostock_version=(None if legacy else cast(str, config["baostock_version"])),
        baostock_unit_policy_version=(
            None if legacy else cast(str, config["baostock_unit_policy_version"])
        ),
        akshare_unit_policy_version=(
            None if not k4 else cast(str, config["akshare_unit_policy_version"])
        ),
        planner_lookback_days=planner_lookback,
        adapter_context_lookback_days=adapter_context_lookback,
        max_days_per_chunk=max_days,
        max_items_per_chunk=max_items,
        endpoint_invocation_limit=endpoint_limit,
        c_endpoint_invocation_limit=c_endpoint_limit,
        d_endpoint_invocation_limit=d_endpoint_limit,
        estimated_http_request_limit=http_limit,
        estimated_baostock_protocol_message_limit=baostock_protocol_limit,
        legacy_import_manifest_sha256=legacy_import_manifest_sha,
        datasets=tuple(plans),
        retry_of_job_id=retry_of_job_id,
        release_manifest_sha256=(
            None if not r2 else cast("str | None", config["release_manifest_sha256"])
        ),
        a_baostock_endpoint_limit=(4 if not r2 else a_limit),
        b_tencent_endpoint_limit=(4 if not r2 else b_limit),
    )


__all__ = [
    "FrozenEodCommand",
    "FrozenEodCommandError",
    "FrozenEodDataset",
    "parse_frozen_eod_command",
]
