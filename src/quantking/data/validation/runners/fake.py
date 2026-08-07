"""Deterministic C0B Fake provider with no supplier SDK dependency."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from quantking.data.validation.contracts_v3 import (
    AttemptOutcomeV3,
    ValidationAttemptIdentityV3,
)
from quantking.data.validation.models import ValidationFailure, ValidationVersions
from quantking.data.validation.testing_v3 import build_v3_run


class FakeRunnerPlan(Protocol):
    context: object
    identity: ValidationAttemptIdentityV3
    selected_case_ids: tuple[str, ...]
    versions: ValidationVersions


class FakeProviderRunner:
    """Writes one immutable SYNTHETIC v3 attempt after the runtime claim."""

    def execute(self, *, plan: FakeRunnerPlan, output_root: Path) -> Path:
        built = build_v3_run(
            root=output_root,
            # Identity is supplied explicitly; keep the fixture timestamp in
            # its valid deterministic range rather than deriving seconds from
            # an arbitrary UUID integer.
            run_index=1,
            selected_case_ids=plan.selected_case_ids,
            context=plan.context,  # type: ignore[arg-type]
            identity_override=plan.identity,
            versions_override=plan.versions,
        )
        return built.directory

    def fail(self, *, plan: FakeRunnerPlan, output_root: Path, category: str) -> Path:
        built = build_v3_run(
            root=output_root,
            run_index=1,
            outcome=AttemptOutcomeV3.FAILED,
            failure=ValidationFailure(category, retryable=False),
            selected_case_ids=plan.selected_case_ids,
            context=plan.context,  # type: ignore[arg-type]
            identity_override=plan.identity,
            versions_override=plan.versions,
        )
        return built.directory


def create_runner() -> FakeProviderRunner:
    """Fixed factory resolved only after a successful authorization claim."""
    return FakeProviderRunner()
