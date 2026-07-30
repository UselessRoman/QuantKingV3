"""S1-07B REAL runtime readiness and pre-import blocking attacks."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from quantking.data.models import DatasetKind


def test_runtime_plan_freezes_scope_versions_and_does_not_import_sdk(tmp_path: Path) -> None:
    from quantking.data.validation.runtime import (
        ControlledRunnerRegistry,
        LocalPackageVersionProbe,
        RealValidationRuntime,
        ValidationSource,
    )

    before = set(sys.modules)
    runtime = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={"xtquant": "unavailable"}),
    )
    plan = runtime.plan(
        source=ValidationSource.XTQUANT,
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        output_root=tmp_path,
    )
    assert plan.sample_spec_version == "s1-07.fixed-sample.v3"
    assert plan.adapter_version
    assert plan.package_version == "unavailable"
    assert plan.code_version
    assert plan.schema_version
    assert plan.rule_version
    assert plan.threshold_version
    assert plan.config_version
    assert plan.network_required is True
    assert plan.qmt_required is True
    assert plan.blocking_reasons
    assert plan.sdk_loaded is False
    assert not ({"xtquant", "baostock", "akshare", "tushare"} & (set(sys.modules) - before))


def test_registry_rejects_arbitrary_module_and_sdk_path_injection() -> None:
    from quantking.data.validation.runtime import ControlledRunnerRegistry, RunnerDescriptor

    registry = ControlledRunnerRegistry.s1_07b()
    with pytest.raises(ValueError, match="controlled registry"):
        registry.with_descriptor(
            RunnerDescriptor(
                source="XTQUANT",
                module_name="attacker.module",
                factory_name="factory",
                distribution_name="xtquant",
                adapter_version="attacker",
                network_required=True,
                qmt_required=True,
            )
        )
    assert not hasattr(registry, "sdk_path")


def test_runtime_blocks_before_runner_import_without_eligible_sample_and_real_signer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantking.data.validation.runtime import (
        ControlledRunnerRegistry,
        LocalPackageVersionProbe,
        RealRuntimeExecutionError,
        RealValidationRuntime,
        ValidationSource,
    )

    imports: list[str] = []

    def forbidden_import(name: str) -> object:
        imports.append(name)
        raise AssertionError("runner import must not occur")

    monkeypatch.setattr("quantking.data.validation.runtime._import_runner", forbidden_import)
    runtime = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={"xtquant": "1.0.0"}),
    )
    with pytest.raises(RealRuntimeExecutionError) as captured:
        runtime.execute(
            source=ValidationSource.XTQUANT,
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            output_root=tmp_path,
            acknowledgement="I_ACKNOWLEDGE_AUTHORIZED_REAL_SOURCE_VALIDATION",
            authorization=None,
        )
    assert captured.value.code == "SAMPLE_NOT_PRODUCTION_ELIGIBLE"
    assert imports == []


def test_runtime_source_and_dataset_are_enum_only(tmp_path: Path) -> None:
    from quantking.data.validation.runtime import (
        ControlledRunnerRegistry,
        LocalPackageVersionProbe,
        RealValidationRuntime,
    )

    runtime = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={}),
    )
    with pytest.raises(TypeError, match="ValidationSource"):
        runtime.plan(  # type: ignore[arg-type]
            source="attacker.module",
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            output_root=tmp_path,
        )
    with pytest.raises(TypeError, match="DatasetKind"):
        runtime.plan(  # type: ignore[arg-type]
            source=next(iter(runtime.registry.sources)),
            dataset="SELECT * FROM secrets",
            output_root=tmp_path,
        )


def test_package_version_drift_changes_frozen_plan_and_incomplete_authorization_fails(
    tmp_path: Path,
) -> None:
    from quantking.data.validation.authorization import FakeAuthorizationAuthority
    from quantking.data.validation.runtime import (
        ControlledRunnerRegistry,
        LocalPackageVersionProbe,
        RealValidationRuntime,
        RuntimeAuthorizationEnvelope,
        ValidationSource,
    )

    first = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={"xtquant": "1.0.0"}),
    ).plan(
        source=ValidationSource.XTQUANT,
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        output_root=tmp_path,
    )
    second = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={"xtquant": "1.0.1"}),
    ).plan(
        source=ValidationSource.XTQUANT,
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        output_root=tmp_path,
    )
    assert first.plan_sha256 != second.plan_sha256
    assert first.package_version != second.package_version
    with pytest.raises(ValueError, match="versions are incomplete"):
        RuntimeAuthorizationEnvelope(
            authorization=FakeAuthorizationAuthority().issue(
                source="XTQUANT",
                dataset=DatasetKind.EXCHANGE_CALENDAR,
                output_root=tmp_path,
                sample_sha256=first.sample_spec_sha256,
            ),
            plan_sha256=first.plan_sha256,
            frozen_versions={"package_version": "1.0.0"},
            signature="1" * 64,
        )


def test_help_status_verify_and_plan_leave_vendor_modules_unloaded(
    tmp_path: Path,
) -> None:
    from quantking.data.validation.cli import build_plan, main, status

    before = set(sys.modules)
    with pytest.raises(SystemExit) as captured:
        main(["--help"])
    assert captured.value.code == 0
    build_plan(
        source="XTQUANT",
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        output_root=tmp_path,
    )
    status()
    assert not ({"xtquant", "baostock", "akshare", "tushare"} & (set(sys.modules) - before))
