"""Red tests for SourceOverride creation, approval and application (S1-04C).

The override service must:

* Create PENDING overrides only after difference-report / batch /
  source / dataset / scope validation.
* Approve/reject through an audited transaction; no auto-approval.
* Apply only APPROVED overrides to a new direct-child DatasetVersion,
  re-run the quality gate, atomic-publish, and mark APPLIED in the
  same audited transaction.
* Reject out-of-scope fields, batch substitution, primary-key
  replacement, or application of non-APPROVED overrides.
* Keep the parent PUBLISHED version immutable.
"""

from __future__ import annotations

from datetime import date

import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate

# ---------------------------------------------------------------------------
# Override creation contract
# ---------------------------------------------------------------------------


class TestSourceOverrideCreationContract:
    def test_pending_override_requires_non_empty_reason(self) -> None:
        from quantking.data.override.service import (
            CreateOverrideRequest,
        )

        # Empty reason must be rejected.
        with pytest.raises((TypeError, ValueError)):
            CreateOverrideRequest(  # type: ignore[call-arg]
                difference_report_id=new_entity_id("data_diff"),
                primary_batch_id=new_entity_id("data_batch"),
                fallback_batch_id=new_entity_id("data_batch"),
                primary_source="XTQUANT",
                fallback_source="BAOSTOCK",
                scope_codes=("000001.SZ",),
                scope_dates=(TradingDate(date(2026, 7, 15)),),
                fields=("close",),
                reason="",  # invalid
            )

    def test_pending_override_rejects_empty_scope(self) -> None:
        from quantking.data.override.service import CreateOverrideRequest

        with pytest.raises((TypeError, ValueError)):
            CreateOverrideRequest(
                difference_report_id=new_entity_id("data_diff"),
                primary_batch_id=new_entity_id("data_batch"),
                fallback_batch_id=new_entity_id("data_batch"),
                primary_source="XTQUANT",
                fallback_source="BAOSTOCK",
                scope_codes=(),  # invalid: empty
                scope_dates=(TradingDate(date(2026, 7, 15)),),
                fields=("close",),
                reason="primary gap",
            )

    def test_pending_override_rejects_duplicate_scope(self) -> None:
        from quantking.data.override.service import CreateOverrideRequest

        with pytest.raises((TypeError, ValueError)):
            CreateOverrideRequest(
                difference_report_id=new_entity_id("data_diff"),
                primary_batch_id=new_entity_id("data_batch"),
                fallback_batch_id=new_entity_id("data_batch"),
                primary_source="XTQUANT",
                fallback_source="BAOSTOCK",
                scope_codes=("000001.SZ", "000001.SZ"),  # duplicate
                scope_dates=(TradingDate(date(2026, 7, 15)),),
                fields=("close",),
                reason="primary gap",
            )

    def test_pending_override_rejects_unknown_field(self) -> None:
        from quantking.data.override.service import CreateOverrideRequest

        with pytest.raises((TypeError, ValueError)):
            CreateOverrideRequest(
                difference_report_id=new_entity_id("data_diff"),
                primary_batch_id=new_entity_id("data_batch"),
                fallback_batch_id=new_entity_id("data_batch"),
                primary_source="XTQUANT",
                fallback_source="BAOSTOCK",
                scope_codes=("000001.SZ",),
                scope_dates=(TradingDate(date(2026, 7, 15)),),
                fields=("not_a_field",),  # unknown
                reason="primary gap",
            )

    def test_pending_override_rejects_primary_key_field_replacement(self) -> None:
        from quantking.data.override.service import CreateOverrideRequest

        with pytest.raises((TypeError, ValueError)):
            CreateOverrideRequest(
                difference_report_id=new_entity_id("data_diff"),
                primary_batch_id=new_entity_id("data_batch"),
                fallback_batch_id=new_entity_id("data_batch"),
                primary_source="XTQUANT",
                fallback_source="BAOSTOCK",
                scope_codes=("000001.SZ",),
                scope_dates=(TradingDate(date(2026, 7, 15)),),
                fields=("code",),  # primary key, cannot be replaced
                reason="primary gap",
            )


__all__ = ["TestSourceOverrideCreationContract"]
