"""Single strict parser for persisted and evidenced EOD commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from quantking.common.time import TradingDate
from quantking.data.eod.contracts import (
    EOD_CONFIG_SCHEMA,
    EOD_JOB_TYPE,
    EOD_REQUEST_MANIFEST_SCHEMA,
    EodBaseVersion,
    EodDatasetConfig,
    EodPublishIntent,
    EodReferenceVersion,
    EodRunRequest,
    canonical_json,
    load_canonical_json,
    sha256_text,
)
from quantking.data.incremental import CoverageWindow
from quantking.data.models import DatasetKind
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


@dataclass(frozen=True, slots=True)
class FrozenEodCommand:
    request: EodRunRequest
    request_sha256: str
    config_sha256: str
    code_version: str
    adapter_version: str
    rule_version: str
    lookback_days: int
    max_days_per_chunk: int
    max_items_per_chunk: int
    datasets: tuple[FrozenEodDataset, ...]
    evidence_request: Mapping[str, object]


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
        if (
            set(input_manifest)
            != {
                "schema_version",
                "request_json",
                "request_sha256",
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
        if request.publish_intent is not EodPublishIntent.PUBLISH_AFTER_QUALITY:
            raise FrozenEodCommandError("publish intent is invalid")

        if (
            set(config_manifest)
            != {
                "schema_version",
                "config_json",
                "config_sha256",
            }
            or config_manifest.get("schema_version") != EOD_CONFIG_SCHEMA
        ):
            raise FrozenEodCommandError("config manifest fields are invalid")
        config_json = config_manifest["config_json"]
        config_sha = config_manifest["config_sha256"]
        if not isinstance(config_json, str) or not isinstance(config_sha, str):
            raise FrozenEodCommandError("config manifest values are invalid")
        if sha256_text(config_json) != config_sha:
            raise FrozenEodCommandError("config SHA does not match")
        config = load_canonical_json(config_json)
        if (
            set(config)
            != {
                "schema_version",
                "code_version",
                "adapter_version",
                "rule_version",
                "lookback_days",
                "max_days_per_chunk",
                "max_items_per_chunk",
                "datasets",
                "schema_versions",
            }
            or config["schema_version"] != EOD_CONFIG_SCHEMA
        ):
            raise FrozenEodCommandError("config fields are invalid")
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
        if dict(version_manifest) != expected_versions or not all(
            isinstance(item, str) and item for item in version_manifest.values()
        ):
            raise FrozenEodCommandError("version manifest does not match config")
        lookback = _integer(config["lookback_days"], field="lookback_days", minimum=0)
        max_days = _integer(config["max_days_per_chunk"], field="max_days_per_chunk", minimum=1)
        max_items = _integer(config["max_items_per_chunk"], field="max_items_per_chunk", minimum=1)
        for field in ("code_version", "adapter_version", "rule_version"):
            if not isinstance(config[field], str) or not config[field]:
                raise FrozenEodCommandError(f"{field} is invalid")
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, FrozenEodCommandError):
            raise
        raise FrozenEodCommandError("frozen EOD command is invalid") from error

    evidence_request = {
        "schema_version": "eod-request-evidence.v2",
        "request": request.to_dict(),
        "request_sha256": request_sha,
        "config": dict(config),
        "config_sha256": config_sha,
        "version_manifest": dict(version_manifest),
    }
    return FrozenEodCommand(
        request=request,
        request_sha256=request_sha,
        config_sha256=config_sha,
        code_version=config["code_version"],  # type: ignore[arg-type]
        adapter_version=config["adapter_version"],  # type: ignore[arg-type]
        rule_version=config["rule_version"],  # type: ignore[arg-type]
        lookback_days=lookback,
        max_days_per_chunk=max_days,
        max_items_per_chunk=max_items,
        datasets=tuple(plans),
        evidence_request=evidence_request,
    )


def parse_evidence_command(value: Mapping[str, object]) -> FrozenEodCommand:
    """Rebuild the persisted manifest triplet from strict request evidence."""
    if (
        set(value)
        != {
            "schema_version",
            "request",
            "request_sha256",
            "config",
            "config_sha256",
            "version_manifest",
        }
        or value.get("schema_version") != "eod-request-evidence.v2"
    ):
        raise FrozenEodCommandError("request evidence fields are invalid")
    request = value["request"]
    config = value["config"]
    versions = value["version_manifest"]
    if (
        not isinstance(request, Mapping)
        or not isinstance(config, Mapping)
        or not isinstance(versions, Mapping)
    ):
        raise FrozenEodCommandError("request evidence values are invalid")
    return parse_frozen_eod_command(
        job_type=EOD_JOB_TYPE,
        input_manifest={
            "schema_version": EOD_REQUEST_MANIFEST_SCHEMA,
            "request_json": canonical_json(request),
            "request_sha256": value["request_sha256"],
        },
        config_manifest={
            "schema_version": EOD_CONFIG_SCHEMA,
            "config_json": canonical_json(config),
            "config_sha256": value["config_sha256"],
        },
        version_manifest=versions,
    )


__all__ = [
    "FrozenEodCommand",
    "FrozenEodCommandError",
    "FrozenEodDataset",
    "parse_evidence_command",
    "parse_frozen_eod_command",
]
