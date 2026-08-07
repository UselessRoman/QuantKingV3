"""Fixed R1 XTQuant validation runner with no SDK or test-builder import."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from time import sleep
from typing import Protocol

from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.adapters.sources.xtquant_real_validation import (
    XtQuantTradingStatusValidationAdapter,
    XtQuantValidationAdapterResult,
)
from quantking.data.validation.contracts_v3 import AttemptOutcomeV3
from quantking.data.validation.real_artifacts import RealValidationEvidenceWriter
from quantking.data.validation.real_runtime import RealXtQuantRunPlan


class _FixedCaseAdapter(Protocol):
    def validate_fixed_case(self) -> XtQuantValidationAdapterResult: ...


class XtQuantRealProviderRunner:
    """Write only the default capability-blocked REAL evidence after claim."""

    def __init__(self, *, clock: Callable[[], UtcTimestamp] = utc_now) -> None:
        self._clock = clock
        self._adapter: _FixedCaseAdapter = XtQuantTradingStatusValidationAdapter()

    def execute(self, *, plan: RealXtQuantRunPlan, output_root: Path) -> Path:
        if output_root != plan.output_root:
            raise ValueError("REAL runner output root does not match the authorized plan")
        started_at = self._clock()
        result = self._adapter.validate_fixed_case()
        ended_at = self._clock()
        for _attempt in range(20):
            if ended_at.value > started_at.value:
                break
            sleep(0.001)
            ended_at = self._clock()
        if ended_at.value <= started_at.value:
            raise ValueError("REAL runner clock did not advance during execution")
        if result.outcome is not AttemptOutcomeV3.FAILED or result.failure is None:
            raise ValueError("fixed trading_status runner must remain capability-blocked")
        return RealValidationEvidenceWriter(validation_root=output_root).write_failure(
            inputs=plan.evidence_inputs(),
            failure=result.failure,
            started_at=started_at,
            ended_at=ended_at,
        )


def create_runner() -> XtQuantRealProviderRunner:
    """Formal factory: no adapter or success path is injectable here."""
    return XtQuantRealProviderRunner()


__all__ = ["XtQuantRealProviderRunner", "create_runner"]
