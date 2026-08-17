from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from tests.helpers import app_settings

from quantking.common.time import TradingDate
from quantking.data.eod.contracts import (
    EOD_CONFIG_SCHEMA,
    EOD_JOB_TYPE,
    EOD_REQUEST_MANIFEST_SCHEMA,
    K4_EOD_CONFIG_SCHEMA,
    EodDatasetConfig,
    EodPublishIntent,
    EodRunRequest,
    canonical_json,
    canonical_request_json,
    request_sha256,
    sha256_text,
)
from quantking.data.eod.fingerprint import eod_input_fingerprint
from quantking.data.eod.frozen import FrozenEodCommandError, parse_frozen_eod_command
from quantking.data.eod.release import S107K4_CODE_VERSION
from quantking.data.eod.service import EodCommandService, EodExecutionConfig
from quantking.data.models import DatasetKind
from quantking.jobs.adapters.persistence.sqlite import (
    SqliteUnitOfWorkFactory,
    create_sqlite_engine,
    initialize_database,
)
from quantking.jobs.models import FailureInfo, JobStatus
from quantking.jobs.services import JobService

_EXCEPTIONS = {
    "600000.SH": (("2026-07-01", "2026-07-03"), ("2026-07-06", "2026-07-06")),
}


def _config(
    settings,
    *,
    exceptions: dict[str, tuple[tuple[str, str], ...]] | None = None,
) -> EodExecutionConfig:
    config = EodExecutionConfig.production(code_version=S107K4_CODE_VERSION, settings=settings)
    return replace(
        config,
        datasets={
            DatasetKind.DAILY_BAR_UNADJUSTED.value: EodDatasetConfig(
                scope=settings.eod_symbols,
                security_exceptions=exceptions or {},
            )
        },
    )


def _manifests(
    config: EodExecutionConfig,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    request = EodRunRequest(
        start_date=date(2026, 6, 11),
        end_date=date(2026, 8, 9),
        datasets=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
        publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
        base_versions={},
    )
    request_json = canonical_request_json(request)
    config_dict = config.to_dict(
        selected=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
        bases={},
    )
    config_json = canonical_json(config_dict)
    version_manifest = {
        "adapter_version": config.adapter_version,
        "baostock_unit_policy_version": config.baostock_unit_policy_version,
        "baostock_version": config.baostock_version,
        "code_version": config.code_version,
        "resolution_rule_version": config.resolution_rule_version,
        "rule_version": config.rule_version,
        "source_route_version": config.source_route_version,
        "schema_versions_sha256": sha256_text(
            canonical_json({DatasetKind.DAILY_BAR_UNADJUSTED.value: "daily_bar_unadjusted.v1"})
        ),
    }
    if config.resolution_rule_version == "source-resolution.daily-bar-unadjusted.k4.v1":
        version_manifest["akshare_unit_policy_version"] = config.akshare_unit_policy_version
    return (
        {
            "schema_version": EOD_REQUEST_MANIFEST_SCHEMA,
            "request_json": request_json,
            "request_sha256": request_sha256(request),
        },
        {
            "schema_version": config_dict["schema_version"],
            "config_json": config_json,
            "config_sha256": sha256_text(config_json),
        },
        version_manifest,
    )


def _parse(config: EodExecutionConfig):
    input_manifest, config_manifest, version_manifest = _manifests(config)
    return parse_frozen_eod_command(
        job_type=EOD_JOB_TYPE,
        input_manifest=input_manifest,
        config_manifest=config_manifest,
        version_manifest=version_manifest,
    )


def test_v4_exceptions_round_trip_through_frozen_command(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    config = _config(settings, exceptions=_EXCEPTIONS)
    assert config.to_dict(selected=(), bases={})["schema_version"] == K4_EOD_CONFIG_SCHEMA
    frozen = _parse(config)
    dataset = frozen.datasets[0]
    assert dataset.security_exceptions == {
        "600000.SH": (
            (TradingDate.from_iso("2026-07-01"), TradingDate.from_iso("2026-07-03")),
            (TradingDate.from_iso("2026-07-06"), TradingDate.from_iso("2026-07-06")),
        )
    }


def test_dataset_config_rejects_invalid_exception_shapes() -> None:
    scope = ("000001.SZ", "600000.SH")
    cases: list[object] = [
        {"600000.SH": (("2026-07-03", "2026-07-01"),)},  # inverted
        {"600000.SH": (("2026-07-01", "2026-07-03"), ("2026-07-01", "2026-07-03"))},  # dup
        {"600000.SH": (("2026-07-01", "2026-07-03"), ("2026-07-02", "2026-07-05"))},  # overlap
        {"300750.SZ": (("2026-07-01", "2026-07-03"),)},  # outside scope
        {"600000.SH": (("2026/07/01", "2026-07-03"),)},  # non-ISO
    ]
    for exceptions in cases:
        with pytest.raises((TypeError, ValueError)):
            EodDatasetConfig(
                scope=scope,
                security_exceptions=exceptions,  # type: ignore[arg-type]
            )


def test_v4_config_requires_security_exceptions_field(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    input_manifest, config_manifest, version_manifest = _manifests(_config(settings, exceptions={}))
    config = json.loads(str(config_manifest["config_json"]))
    raw_dataset = config["datasets"][DatasetKind.DAILY_BAR_UNADJUSTED.value]
    raw_dataset.pop("security_exceptions")
    config_manifest = {
        "schema_version": K4_EOD_CONFIG_SCHEMA,
        "config_json": canonical_json(config),
        "config_sha256": sha256_text(canonical_json(config)),
    }
    with pytest.raises(FrozenEodCommandError, match="security_exceptions"):
        parse_frozen_eod_command(
            job_type=EOD_JOB_TYPE,
            input_manifest=input_manifest,
            config_manifest=config_manifest,
            version_manifest=version_manifest,
        )


def test_exception_change_changes_config_sha_and_k4_fingerprint(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    base = _config(settings, exceptions={})
    changed = _config(settings, exceptions=_EXCEPTIONS)
    dataset_key = DatasetKind.DAILY_BAR_UNADJUSTED.value
    base_dict = base.to_dict(selected=(dataset_key,), bases={})
    assert base_dict["datasets"][dataset_key]["security_exceptions"] == {}
    _, base_manifest, _ = _manifests(base)
    _, changed_manifest, _ = _manifests(changed)
    assert base_manifest["config_sha256"] != changed_manifest["config_sha256"]
    assert eod_input_fingerprint(_parse(base)) != eod_input_fingerprint(_parse(changed))


def test_tampered_frozen_config_is_rejected(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    input_manifest, config_manifest, version_manifest = _manifests(
        _config(settings, exceptions=_EXCEPTIONS)
    )
    tampered = json.loads(str(config_manifest["config_json"]))
    tampered["datasets"][DatasetKind.DAILY_BAR_UNADJUSTED.value]["security_exceptions"][
        "600000.SH"
    ] = [["2026-07-01", "2026-07-04"]]
    config_manifest["config_json"] = canonical_json(tampered)
    with pytest.raises(FrozenEodCommandError, match="config SHA"):
        parse_frozen_eod_command(
            job_type=EOD_JOB_TYPE,
            input_manifest=input_manifest,
            config_manifest=config_manifest,
            version_manifest=version_manifest,
        )


def test_historical_v3_k3_config_parses_without_exceptions(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    config = EodExecutionConfig(
        code_version="s1-07k3.v1",
        adapter_version="baostock-akshare-daily-bar.k3.v1",
        rule_version="quality-rules.daily-bar-first-release.v1",
        resolution_rule_version="source-resolution.daily-bar-unadjusted.k3.v1",
        source_route_version="BAO-EAST-TX-SINA.k3.v1",
        baostock_version="0.9.3",
        baostock_unit_policy_version="baostock-0.9.3-returned-units.v1",
        akshare_unit_policy_version="akshare-1.18.83-returned-units.v1",
        planner_lookback_days=0,
        adapter_context_lookback_days=15,
        max_days_per_chunk=31,
        max_items_per_chunk=1,
        endpoint_invocation_limit=16,
        c_endpoint_invocation_limit=4,
        d_endpoint_invocation_limit=4,
        estimated_http_request_limit=24,
        estimated_baostock_protocol_message_limit=12,
        legacy_import_manifest_sha256=None,
        datasets={
            DatasetKind.DAILY_BAR_UNADJUSTED.value: EodDatasetConfig(scope=settings.eod_symbols)
        },
    )
    input_manifest, config_manifest, version_manifest = _manifests(config)
    assert config_manifest["schema_version"] == EOD_CONFIG_SCHEMA
    frozen = parse_frozen_eod_command(
        job_type=EOD_JOB_TYPE,
        input_manifest=input_manifest,
        config_manifest=config_manifest,
        version_manifest=version_manifest,
    )
    assert frozen.resolution_rule_version == "source-resolution.daily-bar-unadjusted.k3.v1"
    assert frozen.datasets[0].security_exceptions == {}
    assert frozen.akshare_unit_policy_version is None


def test_v3_config_with_exceptions_field_is_rejected(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    config = EodExecutionConfig(
        code_version="s1-07k3.v1",
        adapter_version="baostock-akshare-daily-bar.k3.v1",
        rule_version="quality-rules.daily-bar-first-release.v1",
        resolution_rule_version="source-resolution.daily-bar-unadjusted.k3.v1",
        source_route_version="BAO-EAST-TX-SINA.k3.v1",
        baostock_version="0.9.3",
        baostock_unit_policy_version="baostock-0.9.3-returned-units.v1",
        akshare_unit_policy_version="akshare-1.18.83-returned-units.v1",
        planner_lookback_days=0,
        adapter_context_lookback_days=15,
        max_days_per_chunk=31,
        max_items_per_chunk=1,
        endpoint_invocation_limit=16,
        c_endpoint_invocation_limit=4,
        d_endpoint_invocation_limit=4,
        estimated_http_request_limit=24,
        estimated_baostock_protocol_message_limit=12,
        legacy_import_manifest_sha256=None,
        datasets={
            DatasetKind.DAILY_BAR_UNADJUSTED.value: EodDatasetConfig(scope=settings.eod_symbols)
        },
    )
    input_manifest, config_manifest, version_manifest = _manifests(config)
    payload = json.loads(str(config_manifest["config_json"]))
    payload["datasets"][DatasetKind.DAILY_BAR_UNADJUSTED.value]["security_exceptions"] = {}
    config_manifest["config_json"] = canonical_json(payload)
    config_manifest["config_sha256"] = sha256_text(config_manifest["config_json"])
    with pytest.raises(FrozenEodCommandError, match="security_exceptions"):
        parse_frozen_eod_command(
            job_type=EOD_JOB_TYPE,
            input_manifest=input_manifest,
            config_manifest=config_manifest,
            version_manifest=version_manifest,
        )


def _finish(jobs: JobService, job_id: str, terminal: JobStatus) -> None:
    jobs.transition(job_id, JobStatus.RUNNING, worker_id="test-worker", summary="claimed")
    jobs.transition(
        job_id,
        terminal,
        failure=FailureInfo("TEST_FAILURE", "stable failure", False),
        summary="terminal",
    )


def test_retry_requires_the_same_frozen_exceptions(tmp_path: Path) -> None:
    database = tmp_path / "jobs.db"
    initialize_database(database)
    jobs = JobService(SqliteUnitOfWorkFactory(create_sqlite_engine(database, busy_timeout_ms=250)))
    settings = app_settings(tmp_path)
    command = EodCommandService(jobs, config=_config(settings, exceptions=_EXCEPTIONS))
    request = EodRunRequest(
        start_date=date(2026, 6, 11),
        end_date=date(2026, 8, 9),
        datasets=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
        publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
        base_versions={},
    )
    previous = command.enqueue(request, idempotency_key="k4b-exception-retry", requester="pytest")
    _finish(jobs, previous.job_id, JobStatus.FAILED)

    # Same frozen exceptions: the retry link is created with the exact inputs.
    retry = command.enqueue(
        request,
        idempotency_key="k4b-exception-retry-2",
        requester="pytest",
        retry_of_job_id=previous.job_id,
    )
    assert retry.job_id != previous.job_id
    persisted = jobs.get_job(retry.job_id)
    assert persisted.input_manifest["retry_of_job_id"] == previous.job_id

    # Changed frozen exceptions must not reuse or retry the previous inputs.
    changed_command = EodCommandService(jobs, config=_config(settings, exceptions={}))
    with pytest.raises(ValueError, match="exactly match"):
        changed_command.enqueue(
            request,
            idempotency_key="k4b-exception-retry-3",
            requester="pytest",
            retry_of_job_id=previous.job_id,
        )
