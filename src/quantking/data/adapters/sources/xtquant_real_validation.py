"""XTQuant C1A validation adapter boundary.

The runner never imports ``xtquant``.  Any future SDK load belongs behind this
adapter and must still use an explicit site-packages root.  For the fixed
``trading_status`` unit the evidence-backed capability remains unavailable, so
this adapter returns a signed-failure-safe result without loading the SDK or
guessing historical ST status from current fields, names, or volume.
"""

from __future__ import annotations

from dataclasses import dataclass

from quantking.data.adapters.sources.capabilities import capability_for
from quantking.data.models import DatasetKind
from quantking.data.validation.contracts_v3 import AttemptOutcomeV3
from quantking.data.validation.models import ValidationFailure


@dataclass(frozen=True, slots=True)
class XtQuantValidationAdapterResult:
    """A runner-owned outcome that contains no caller-controlled gate state."""

    outcome: AttemptOutcomeV3
    failure: ValidationFailure | None

    def __post_init__(self) -> None:
        if self.outcome is AttemptOutcomeV3.SUCCEEDED and self.failure is not None:
            raise ValueError("successful adapter result cannot carry a failure")
        if self.outcome is AttemptOutcomeV3.FAILED and self.failure is None:
            raise ValueError("failed adapter result requires a failure")

    @classmethod
    def unsupported(cls) -> XtQuantValidationAdapterResult:
        return cls(
            outcome=AttemptOutcomeV3.FAILED,
            failure=ValidationFailure("CAPABILITY_UNSUPPORTED", retryable=False),
        )


class XtQuantTradingStatusValidationAdapter:
    """The fixed C1A adapter; it preserves the current capability boundary."""

    def validate_fixed_case(self) -> XtQuantValidationAdapterResult:
        capability = capability_for(DatasetKind.TRADING_STATUS)
        if not capability.available:
            return XtQuantValidationAdapterResult.unsupported()
        # This branch is intentionally not implemented by C1A.  A later,
        # separately authorized task must introduce a point-in-time provider
        # contract before it can load the SDK through an explicit adapter-only
        # mechanism.  It must not infer history from current metadata.
        return XtQuantValidationAdapterResult.unsupported()


__all__ = ["XtQuantTradingStatusValidationAdapter", "XtQuantValidationAdapterResult"]
