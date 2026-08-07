"""Fixed C1A XTQuant validation runner.

This module deliberately imports no vendor SDK.  It delegates the fixed
capability decision to ``data.adapters`` after the runtime has consumed the
one-time authorization claim.  The default adapter can currently emit only
``CAPABILITY_UNSUPPORTED`` for historical trading status.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from quantking.data.adapters.sources.xtquant_real_validation import (
    XtQuantTradingStatusValidationAdapter,
    XtQuantValidationAdapterResult,
)
from quantking.data.validation.contracts_v3 import AttemptOutcomeV3, V3RunnerTrustPolicy
from quantking.data.validation.testing_v3 import build_v3_run


class RealXtQuantRunnerPlan(Protocol):
    context: object
    identity: object
    selected_case_ids: tuple[str, ...]
    versions: object
    runner_signer: object
    runner_trust_policy: V3RunnerTrustPolicy


class _FixedCaseAdapter(Protocol):
    def validate_fixed_case(self) -> XtQuantValidationAdapterResult: ...


class XtQuantRealProviderRunner:
    """Write the fixed attempt's immutable v3 output after a successful claim."""

    def __init__(self, *, adapter: _FixedCaseAdapter | None = None) -> None:
        self._adapter = adapter or XtQuantTradingStatusValidationAdapter()

    def execute(self, *, plan: RealXtQuantRunnerPlan, output_root: Path) -> Path:
        outcome = self._adapter.validate_fixed_case()
        if outcome.outcome is AttemptOutcomeV3.FAILED:
            assert outcome.failure is not None
            return build_v3_run(
                root=output_root,
                run_index=1,
                outcome=AttemptOutcomeV3.FAILED,
                failure=outcome.failure,
                selected_case_ids=plan.selected_case_ids,
                context=plan.context,  # type: ignore[arg-type]
                identity_override=plan.identity,  # type: ignore[arg-type]
                versions_override=plan.versions,  # type: ignore[arg-type]
                signer_override=plan.runner_signer,  # type: ignore[arg-type]
                runner_trust_policy=plan.runner_trust_policy,
            ).directory
        # This path exists solely so the offline C1A control chain can test a
        # completely isolated Fake adapter.  The shipped adapter cannot reach
        # it while the historical trading-status capability is false; it is
        # not evidence that XTQuant has been queried or that a REAL marker ran.
        return build_v3_run(
            root=output_root,
            run_index=1,
            outcome=AttemptOutcomeV3.SUCCEEDED,
            selected_case_ids=plan.selected_case_ids,
            context=plan.context,  # type: ignore[arg-type]
            identity_override=plan.identity,  # type: ignore[arg-type]
            versions_override=plan.versions,  # type: ignore[arg-type]
            signer_override=plan.runner_signer,  # type: ignore[arg-type]
            runner_trust_policy=plan.runner_trust_policy,
        ).directory


def create_runner() -> XtQuantRealProviderRunner:
    """The runtime resolves this fixed factory only after authorization claim."""
    return XtQuantRealProviderRunner()


__all__ = ["XtQuantRealProviderRunner", "create_runner"]
