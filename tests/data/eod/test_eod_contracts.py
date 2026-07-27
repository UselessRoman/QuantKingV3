from __future__ import annotations

from datetime import date

import pytest

from quantking.data.eod.contracts import (
    EOD_JOB_TYPE,
    EodPublishIntent,
    EodRunRequest,
    canonical_request_json,
    request_sha256,
)


def _request(*, datasets: tuple[str, ...]) -> EodRunRequest:
    return EodRunRequest(
        start_date=date(2026, 7, 15),
        end_date=date(2026, 7, 15),
        datasets=datasets,
        publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
    )


def test_request_is_sorted_and_sha_is_deterministic() -> None:
    first = _request(datasets=("security_master", "exchange_calendar"))
    second = _request(datasets=("exchange_calendar", "security_master"))

    assert first == second
    assert first.datasets == ("exchange_calendar", "security_master")
    assert canonical_request_json(first) == canonical_request_json(second)
    assert request_sha256(first) == request_sha256(second)
    assert EOD_JOB_TYPE == "data.eod.v1"


@pytest.mark.parametrize(
    "datasets",
    [
        (),
        ("exchange_calendar", "exchange_calendar"),
        ("unknown",),
        ("../exchange_calendar",),
        ("C:\\secret",),
        ("latest",),
    ],
)
def test_request_rejects_empty_duplicate_unknown_or_unsafe_datasets(
    datasets: tuple[str, ...],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        _request(datasets=datasets)


def test_request_rejects_inverted_or_non_date_range() -> None:
    with pytest.raises(ValueError, match="end_date"):
        EodRunRequest(
            start_date=date(2026, 7, 16),
            end_date=date(2026, 7, 15),
            datasets=("exchange_calendar",),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
        )
    with pytest.raises(TypeError, match="date"):
        EodRunRequest(  # type: ignore[arg-type]
            start_date="2026-07-15",
            end_date=date(2026, 7, 15),
            datasets=("exchange_calendar",),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
        )
