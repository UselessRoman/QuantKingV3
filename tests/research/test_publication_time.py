"""Independent date-precision and source-revision boundaries for S2-01."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from quantking.common.time import UtcTimestamp
from quantking.research import contracts as c


def publication(day: date = date(2023, 6, 7)) -> c.PublicationDateEvidence:
    return c.PublicationDateEvidence(
        document_id="announcement-2023-026",
        document_revision="original",
        document_sha256="a" * 64,
        published_on=day,
        publication_record_sha256="b" * 64,
    )


def fact(day: date = date(2023, 6, 7)) -> c.SourceFact:
    return c.SourceFact.from_publication_date(
        source="synthetic-official-record",
        revision="original",
        evidence_sha256="a" * 64,
        observed_at=UtcTimestamp.from_iso("2026-01-01T00:00:00Z"),
        publication=publication(day),
    )


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2023, 6, 7), "2023-06-07T16:00:00Z"),
        (date(2023, 6, 9), "2023-06-09T16:00:00Z"),  # Friday -> Saturday, not Monday
        (date(2023, 12, 31), "2023-12-31T16:00:00Z"),
        (date(2024, 2, 29), "2024-02-29T16:00:00Z"),
    ],
)
def test_date_precision_uses_next_shanghai_natural_day(day: date, expected: str) -> None:
    source = fact(day)
    assert source.precision is c.KnowledgeTimePrecision.DATE
    assert source.available_at.to_iso() == expected
    assert source.publication == publication(day)


def test_date_fact_rejects_same_day_signal_and_accepts_exact_safe_boundary() -> None:
    source = fact()
    row = c.SecurityStatusDaily("000001.SZ", date(2023, 6, 7), c.RiskStatus.NORMAL, False, source)
    for cutoff in ("2023-06-07T07:00:00Z", "2023-06-07T15:59:59Z"):
        with pytest.raises(c.ResearchContractError, match="FUTURE_KNOWLEDGE"):
            c.validate_reference_rows((row,), as_of=UtcTimestamp.from_iso(cutoff))
    c.validate_reference_rows((row,), as_of=UtcTimestamp.from_iso("2023-06-07T16:00:00Z"))
    with pytest.raises(c.ResearchContractError, match="DATE_AVAILABILITY_MISMATCH"):
        replace(source, available_at=UtcTimestamp.from_iso("2023-06-07T00:00:00Z"))


def test_new_revision_cannot_reuse_old_content_publication_binding() -> None:
    old = fact()
    with pytest.raises(c.ResearchContractError, match="PUBLICATION_REVISION_MISMATCH"):
        replace(old, revision="corrected")
    with pytest.raises(c.ResearchContractError, match="PUBLICATION_CONTENT_MISMATCH"):
        replace(old, evidence_sha256="c" * 64)
    revised_evidence = replace(
        publication(),
        document_revision="corrected",
        document_sha256="c" * 64,
        published_on=date(2023, 6, 12),
        publication_record_sha256="d" * 64,
    )
    revised = c.SourceFact.from_publication_date(
        source=old.source,
        revision="corrected",
        evidence_sha256="c" * 64,
        observed_at=old.observed_at,
        publication=revised_evidence,
    )
    assert revised.available_at.to_iso() == "2023-06-12T16:00:00Z"
    assert old.available_at.to_iso() == "2023-06-07T16:00:00Z"


def test_missing_record_mixed_precision_and_early_observation_fail() -> None:
    source = fact()
    with pytest.raises(c.ResearchContractError, match="PUBLICATION_REQUIRED"):
        replace(source, publication=None)
    with pytest.raises(c.ResearchContractError, match="UNEXPECTED_PUBLICATION_DATE"):
        replace(source, precision=c.KnowledgeTimePrecision.EXACT)
    with pytest.raises(c.ResearchContractError, match="INVALID_SHA256"):
        replace(publication(), publication_record_sha256="")
    with pytest.raises(c.ResearchContractError, match="INVALID_OBSERVATION_TIME"):
        replace(source, observed_at=UtcTimestamp.from_iso("2023-06-07T07:00:00Z"))


def test_date_only_action_does_not_invent_a_disclosure_timestamp() -> None:
    action = c.CorporateAction(
        event_id="synthetic-cash",
        code="000001.SZ",
        kind=c.CorporateActionType.CASH_DIVIDEND,
        disclosed_at=None,
        disclosed_on=date(2023, 6, 7),
        record_date=date(2023, 6, 13),
        ex_date=date(2023, 6, 14),
        cash_per_share=Decimal("0.285"),
        shares_per_share=None,
        cash_pay_date=date(2023, 6, 14),
        shares_available_date=None,
        reference_price=None,
        tax_policy=None,
        source=fact(),
    )
    assert action.disclosed_at is None
    assert action.execution_gaps() == ("reference_price", "tax_policy")
    with pytest.raises(c.ResearchContractError, match="DISCLOSURE_PRECISION_CONFLICT"):
        replace(action, disclosed_at=action.source.available_at)
    with pytest.raises(c.ResearchContractError, match="DISCLOSURE_DATE_MISMATCH"):
        replace(action, disclosed_on=date(2023, 6, 6))
    with pytest.raises(c.ResearchContractError, match="DISCLOSURE_DATE_MISMATCH"):
        replace(action, disclosed_on=None)


def test_exact_source_and_action_keep_existing_behavior() -> None:
    stamp = UtcTimestamp.from_iso("2023-06-07T06:00:00Z")
    source = c.SourceFact("synthetic", "original", "a" * 64, stamp, stamp)
    assert source.precision is c.KnowledgeTimePrecision.EXACT
    action = c.CorporateAction(
        "cash",
        "000001.SZ",
        c.CorporateActionType.CASH_DIVIDEND,
        stamp,
        date(2023, 6, 13),
        date(2023, 6, 14),
        Decimal("0.285"),
        None,
        None,
        None,
        None,
        None,
        source,
    )
    assert action.disclosed_on is None
    with pytest.raises(c.ResearchContractError, match="INVALID_DISCLOSURE_TIME"):
        replace(action, disclosed_at=None)
    with pytest.raises(c.ResearchContractError, match="DISCLOSURE_PRECISION_CONFLICT"):
        replace(action, disclosed_on=date(2023, 6, 7))
