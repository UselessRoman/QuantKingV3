from __future__ import annotations

from datetime import date

import pytest

from quantking.data.eod.contracts import (
    EOD_JOB_TYPE,
    EodBaseVersion,
    EodDatasetConfig,
    EodDatasetResult,
    EodPublishIntent,
    EodRunRequest,
    EodRunResult,
    canonical_request_json,
    request_sha256,
)


def _request(*, datasets: tuple[str, ...]) -> EodRunRequest:
    return EodRunRequest(
        start_date=date(2026, 7, 15),
        end_date=date(2026, 7, 15),
        datasets=datasets,
        publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
        base_versions={},
    )


def test_request_is_sorted_and_sha_is_deterministic() -> None:
    first = _request(datasets=("security_master", "exchange_calendar"))
    second = _request(datasets=("exchange_calendar", "security_master"))

    assert first == second
    assert first.datasets == ("exchange_calendar", "security_master")
    assert canonical_request_json(first) == canonical_request_json(second)
    assert request_sha256(first) == request_sha256(second)
    assert EOD_JOB_TYPE == "data.eod.v1"


def test_request_freezes_exact_base_version_mapping() -> None:
    version_id = "data_version:11111111-1111-4111-8111-111111111111"
    request = EodRunRequest(
        start_date=date(2026, 7, 15),
        end_date=date(2026, 7, 15),
        datasets=("exchange_calendar",),
        publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
        base_versions={"exchange_calendar": version_id},
    )

    assert request.to_dict()["base_versions"] == {"exchange_calendar": version_id}


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
            base_versions={},
        )
    with pytest.raises(TypeError, match="date"):
        EodRunRequest(  # type: ignore[arg-type]
            start_date="2026-07-15",
            end_date=date(2026, 7, 15),
            datasets=("exchange_calendar",),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        )


def _dataset_result(dataset: str) -> EodDatasetResult:
    return EodDatasetResult(
        dataset=dataset,
        status="PUBLISHED_NEW",
        batch_id="data_batch:11111111-1111-4111-8111-111111111111",
        version_id="data_version:22222222-2222-4222-8222-222222222222",
        quality_run_id="data_quality:33333333-3333-4333-8333-333333333333",
        content_sha256="a" * 64,
        manifest_sha256="b" * 64,
        parent_version_id=None,
    )


@pytest.mark.parametrize(
    "results",
    [
        (),
        (_dataset_result("exchange_calendar"), _dataset_result("exchange_calendar")),
        (
            _dataset_result("exchange_calendar"),
            _dataset_result("security_master"),
        ),
    ],
)
def test_run_result_rejects_missing_duplicate_or_extra_datasets(
    results: tuple[EodDatasetResult, ...],
) -> None:
    with pytest.raises(ValueError, match="exactly"):
        EodRunResult(
            job_id="job:44444444-4444-4444-8444-444444444444",
            datasets=results,
            expected_datasets=("exchange_calendar",),
        )


@pytest.mark.parametrize(
    "coverage",
    [
        (("SZ", "2026-07-16"),),
        (("SZ", "2026-07-16", "2026-07-15"),),
        (("SZ", "2026/07/15", "2026-07-16"),),
        (("XX", "2026-07-15", "2026-07-16"),),
        (
            ("SZ", "2026-07-15", "2026-07-16"),
            ("SZ", "2026-07-15", "2026-07-16"),
        ),
    ],
)
def test_known_coverage_rejects_invalid_shape_scope_dates_or_duplicates(
    coverage: tuple[tuple[str, ...], ...],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        EodBaseVersion(
            version_id="data_version:11111111-1111-4111-8111-111111111111",
            dataset="exchange_calendar",
            schema_version="exchange_calendar.v1",
            content_sha256="a" * 64,
            manifest_sha256="b" * 64,
            coverage=coverage,  # type: ignore[arg-type]
        )


def test_known_coverage_cannot_be_declared_without_base() -> None:
    with pytest.raises(ValueError, match="base_version"):
        EodDatasetConfig(
            scope=("SZ",),
            known_coverage=(("SZ", "2026-07-15", "2026-07-16"),),
        )
