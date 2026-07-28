"""S1-06-R1 regression tests for strict read-side data-center contracts."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.assessment.models import (
    AssessmentStatus,
    EvidenceKind,
    SourceCandidateAssessment,
)
from quantking.data.center import (
    DataCenterQueryService,
    DataCenterReadIntegrityError,
    InvalidDataCenterQueryError,
)
from quantking.data.models import (
    BatchFailureInfo,
    BatchFailureKind,
    DatasetKind,
    DatasetVersionStatus,
    DataSource,
    IngestionBatch,
    SourceOverrideStatus,
)


class _Uow:
    def __init__(self, repository: object) -> None:
        self.data = repository

    def __enter__(self) -> _Uow:
        return self

    def __exit__(self, *args: object) -> None:
        del args


class _Jobs:
    def list_jobs_page(self, **filters: object) -> list[object]:
        del filters
        return []


class _Preview:
    def query_by_id(self, **request: object) -> object:
        del request
        return SimpleNamespace(
            to_pylist=lambda: [
                {
                    "code": "000001.SZ",
                    "trade_date": datetime(2026, 7, 1).date(),
                    "close": 10.0,
                    "volume": 100,
                },
                {
                    "code": "000001.SZ",
                    "trade_date": datetime(2026, 7, 2).date(),
                    "close": 11.0,
                    "volume": 101,
                },
            ]
        )


class _Verifier:
    def __init__(self, verified: object | None = None) -> None:
        self.verified = verified or SimpleNamespace(
            report_sha256="a" * 64,
            row_differences=[{"field": "close"}],
            created_at=UtcTimestamp(datetime(2026, 7, 1, tzinfo=UTC)),
        )

    def verify(self, **request: object) -> object:
        del request
        return self.verified


def _service(repository: object, *, verifier: object | None = None) -> DataCenterQueryService:
    return DataCenterQueryService(
        data_uow_factory=lambda: _Uow(repository),
        preview=_Preview(),
        difference_verifier=verifier or _Verifier(),
        jobs=_Jobs(),
    )


def _batch(index: int) -> IngestionBatch:
    return IngestionBatch.running(
        batch_id=new_entity_id("data_batch"),
        source=DataSource.XTQUANT,
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        adapter_version="s1-06-r1-test",
        request_params={"index": str(index)},
        started_at=UtcTimestamp(datetime(2026, 7, index, tzinfo=UTC)),
    )


def _opaque_cursor(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_batch_cursor_cannot_be_reused_across_dataset_filters() -> None:
    rows = [_batch(3), _batch(2)]
    repository = SimpleNamespace(list_batches_page=lambda **filters: rows)
    service = _service(repository)

    first = service.list_batches(dataset="exchange_calendar", limit=1)
    assert first.next_cursor

    with pytest.raises(InvalidDataCenterQueryError, match="cursor"):
        service.list_batches(
            dataset="daily_bar_unadjusted",
            cursor=first.next_cursor,
            limit=1,
        )


def test_preview_cursor_cannot_be_reused_across_field_projection() -> None:
    version_id = str(new_entity_id("data_version"))
    version = SimpleNamespace(
        version_id=new_entity_id("data_version"),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        created_at=UtcTimestamp(datetime(2026, 7, 1, tzinfo=UTC)),
    )
    repository = SimpleNamespace(get_version=lambda requested: version)
    service = _service(repository)

    first = service.preview(
        version_id=version_id,
        fields=("close",),
        codes=("000001.SZ",),
        start_date=None,
        end_date=None,
        cursor=None,
        limit=1,
    )
    assert first.next_cursor

    with pytest.raises(InvalidDataCenterQueryError, match="cursor"):
        service.preview(
            version_id=version_id,
            fields=("volume",),
            codes=("000001.SZ",),
            start_date=None,
            end_date=None,
            cursor=first.next_cursor,
            limit=1,
        )


def test_difference_rows_non_numeric_offset_is_a_stable_cursor_error() -> None:
    report_id = str(new_entity_id("data_diff"))
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "filters": {"report_id": report_id},
                "resource": "difference-rows",
                "sort_version": "created-desc-id-desc.v1",
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    cursor = _opaque_cursor(
        {
            "v": 2,
            "k": f"difference-rows:{report_id}",
            "q": fingerprint,
            "t": "2026-07-01T00:00:00Z",
            "i": report_id,
            "s": [],
            "o": "not-an-integer",
        }
    )
    service = _service(SimpleNamespace())

    with pytest.raises(InvalidDataCenterQueryError, match="cursor"):
        service.difference_rows(report_id=report_id, cursor=cursor, limit=50)


def test_source_matrix_does_not_invent_real_evidence_without_assessment() -> None:
    repository = SimpleNamespace(find_latest_source_candidate_assessment=lambda **filters: None)
    exchange_calendar = next(
        item
        for item in _service(repository).sources()
        if item["source"] == "XTQUANT" and item["dataset"] == "exchange_calendar"
    )

    assert exchange_calendar["status"] == "PRODUCTION_READY"
    assert exchange_calendar["evidence_kind"] is None
    assert exchange_calendar["assessment_status"] is None


def test_synthetic_assessment_is_reported_without_promoting_matrix_status() -> None:
    assessment = SourceCandidateAssessment(
        assessment_id=new_entity_id("data_assessment"),
        source="BAOSTOCK",
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        evidence_kind=EvidenceKind.SYNTHETIC,
        semantic_contract_verified=True,
        schema_unit_verified=True,
        coverage_adequate=True,
        quality_passed=True,
        fixed_sample_differences_adequate=True,
        stability_runs=10,
        status=AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION,
        blocking_reason="real external validation is still required",
        assessment_config_version="assessment.v1",
        sample_spec_version="sample.v1",
        rule_version="rules.v1",
        evidence_relative_path="assessments/synthetic.json",
        evidence_sha256="a" * 64,
        created_at=UtcTimestamp(datetime(2026, 7, 1, tzinfo=UTC)),
    )
    repository = SimpleNamespace(
        find_latest_source_candidate_assessment=lambda **filters: (
            assessment
            if filters["source"] == "BAOSTOCK"
            and filters["dataset"] is DatasetKind.DAILY_BAR_UNADJUSTED
            else None
        )
    )

    row = next(
        item
        for item in _service(repository).sources()
        if item["source"] == "BAOSTOCK" and item["dataset"] == "daily_bar_unadjusted"
    )

    assert row["matrix_status"] == "BLOCKED_EXTERNAL_VALIDATION"
    assert row["assessment_status"] == "BLOCKED_EXTERNAL_VALIDATION"
    assert row["evidence_kind"] == "SYNTHETIC"
    assert row["auto_switch"] is False


def test_same_report_sha_cannot_mask_different_artifact_lineage() -> None:
    report_id = new_entity_id("data_diff")
    primary_version_id = new_entity_id("data_version")
    fallback_version_id = new_entity_id("data_version")
    primary_batch_id = new_entity_id("data_batch")
    fallback_batch_id = new_entity_id("data_batch")
    primary_quality_id = new_entity_id("data_quality")
    fallback_quality_id = new_entity_id("data_quality")
    created_at = UtcTimestamp(datetime(2026, 7, 1, tzinfo=UTC))
    report = SimpleNamespace(
        report_id=report_id,
        version_id=primary_version_id,
        primary_source=DataSource.XTQUANT,
        fallback_source=DataSource.BAOSTOCK,
        created_at=created_at,
        field_differences={"close": "1"},
        total_compared=1,
        total_differences=1,
        primary_batch_id=primary_batch_id,
        fallback_batch_id=fallback_batch_id,
        primary_version_id=primary_version_id,
        fallback_version_id=fallback_version_id,
        primary_content_sha256="1" * 64,
        fallback_content_sha256="2" * 64,
        primary_quality_run_id=primary_quality_id,
        fallback_quality_run_id=fallback_quality_id,
        sample_spec_sha256="3" * 64,
        threshold_rule_version="threshold.v1",
        report_sha256="a" * 64,
        report_relative_path=f"data-diff/{report_id}",
    )
    verified = SimpleNamespace(
        report_id=report_id,
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
        primary_source=DataSource.XTQUANT.value,
        fallback_source=DataSource.BAOSTOCK.value,
        created_at=created_at,
        field_differences={"close": "1"},
        total_compared=1,
        total_differences=1,
        primary_batch_id=new_entity_id("data_batch"),
        fallback_batch_id=fallback_batch_id,
        primary_version_id=new_entity_id("data_version"),
        fallback_version_id=fallback_version_id,
        primary_content_sha256="1" * 64,
        fallback_content_sha256="2" * 64,
        primary_quality_run_id=new_entity_id("data_quality"),
        fallback_quality_run_id=fallback_quality_id,
        sample_spec_sha256="3" * 64,
        threshold_rule_version="threshold.v1",
        report_sha256="a" * 64,
        report_relative_path=f"data-diff/{report_id}",
        row_differences=[],
    )
    repository = SimpleNamespace(get_difference_report=lambda requested: report)
    service = _service(repository, verifier=_Verifier(verified))

    with pytest.raises(InvalidDataCenterQueryError, match=r"credentials|lineage"):
        service.get_difference_report(str(report_id))


def _override_fixture() -> tuple[object, object, object]:
    override_id = new_entity_id("data_override")
    report_id = new_entity_id("data_diff")
    primary_batch_id = new_entity_id("data_batch")
    fallback_batch_id = new_entity_id("data_batch")
    primary_version_id = new_entity_id("data_version")
    child_version_id = new_entity_id("data_version")
    now = UtcTimestamp(datetime(2026, 7, 1, tzinfo=UTC))
    override = SimpleNamespace(
        override_id=override_id,
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        status=SourceOverrideStatus.APPROVED,
        difference_report_id=report_id,
        primary_source=DataSource.XTQUANT,
        fallback_source=DataSource.BAOSTOCK,
        primary_batch_id=primary_batch_id,
        fallback_batch_id=fallback_batch_id,
        field_replacements=({"code": "000001.SZ", "trade_date": "2026-07-01", "field": "close"},),
        fallback_only_keys=(),
        exact_scope_sha256="4" * 64,
        contribution_scope_sha256="5" * 64,
        reason="test",
        created_at=now,
        approved_at=now,
        approved_by="tester",
        applied_at=None,
        applied_version_id=None,
    )
    report = SimpleNamespace(
        report_id=report_id,
        primary_source=DataSource.XTQUANT,
        fallback_source=DataSource.BAOSTOCK,
        primary_batch_id=primary_batch_id,
        fallback_batch_id=fallback_batch_id,
        primary_version_id=primary_version_id,
        fallback_version_id=new_entity_id("data_version"),
        primary_content_sha256="1" * 64,
        fallback_content_sha256="2" * 64,
        report_sha256="3" * 64,
    )
    child = SimpleNamespace(
        version_id=child_version_id,
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        status=DatasetVersionStatus.FAILED,
        schema_version="daily-bar.v1",
        code_version="s1-06-r1-test",
        rule_version="quality.v1",
        parent_version_id=primary_version_id,
        input_batch_ids=(primary_batch_id, fallback_batch_id),
        created_at=now,
        published_at=None,
        row_count=None,
        security_count=None,
        coverage_start=None,
        coverage_end=None,
        coverage_summary={},
        quality_run_id=None,
        manifest_sha256=None,
        content_sha256=None,
        validated_at=None,
        failure=BatchFailureInfo(
            error_type=BatchFailureKind.INTERNAL,
            message="sanitized failure",
            retryable=False,
        ),
    )
    return override, report, child


def _binding(override: object, child: object, *, fingerprint: str, status: str) -> object:
    now = UtcTimestamp(datetime(2026, 7, 1, tzinfo=UTC))
    return SimpleNamespace(
        application_id=new_entity_id("data_override_application"),
        override_id=override.override_id,
        child_version_id=child.version_id,
        input_fingerprint=fingerprint,
        status=status,
        report_sha256=None,
        contribution_scope_sha256=None,
        claimed_by="tester",
        claimed_at=now,
        updated_at=now,
        failure_message="sanitized failure" if status == "FAILED" else None,
    )


def _attempt(override: object, child: object, *, fingerprint: str, status: str) -> object:
    now = UtcTimestamp(datetime(2026, 7, 1, tzinfo=UTC))
    return SimpleNamespace(
        attempt_id=new_entity_id("data_override_attempt"),
        override_id=override.override_id,
        attempt_no=1,
        child_version_id=child.version_id,
        input_fingerprint=fingerprint,
        status=status,
        claimed_by="tester",
        started_at=now,
        ended_at=now,
        failure_message="sanitized failure" if status == "FAILED" else None,
    )


def test_approved_override_rejects_application_attempt_fingerprint_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override, report, child = _override_fixture()
    binding = _binding(override, child, fingerprint="a" * 64, status="CLAIMED")
    attempt = _attempt(override, child, fingerprint="b" * 64, status="CLAIMED")
    repository = SimpleNamespace(
        get_override=lambda requested: override,
        find_latest_override_application_binding=lambda requested: binding,
        find_active_override_application_binding=lambda requested: binding,
        find_last_override_application_attempt=lambda requested: attempt,
        list_override_audits=lambda requested, limit: [],
    )
    service = _service(repository)
    monkeypatch.setattr(
        service,
        "_load_verified_difference",
        lambda requested: (
            report,
            SimpleNamespace(dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value),
        ),
    )

    with pytest.raises(DataCenterReadIntegrityError, match="attempt lineage"):
        service.get_source_override(str(override.override_id))


def test_failed_override_attempt_is_returned_without_an_active_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override, report, child = _override_fixture()
    binding = _binding(override, child, fingerprint="a" * 64, status="FAILED")
    attempt = _attempt(override, child, fingerprint="a" * 64, status="FAILED")
    repository = SimpleNamespace(
        get_override=lambda requested: override,
        find_latest_override_application_binding=lambda requested: binding,
        find_active_override_application_binding=lambda requested: None,
        find_last_override_application_attempt=lambda requested: attempt,
        list_override_audits=lambda requested, limit: [],
        get_version=lambda requested: child,
    )
    service = _service(repository)
    monkeypatch.setattr(
        service,
        "_load_verified_difference",
        lambda requested: (
            report,
            SimpleNamespace(dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value),
        ),
    )

    result = service.get_source_override(str(override.override_id))

    assert result["application"] is None
    assert result["last_application"]["status"] == "FAILED"
    assert result["attempt"]["status"] == "FAILED"
    assert result["child"]["version_id"] == str(child.version_id)


def test_applied_override_rejects_a_different_persisted_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override, report, child = _override_fixture()
    override.status = SourceOverrideStatus.APPLIED
    override.applied_version_id = new_entity_id("data_version")
    binding = _binding(override, child, fingerprint="a" * 64, status="APPLIED")
    attempt = _attempt(override, child, fingerprint="a" * 64, status="APPLIED")
    repository = SimpleNamespace(
        get_override=lambda requested: override,
        find_latest_override_application_binding=lambda requested: binding,
        find_active_override_application_binding=lambda requested: binding,
        find_last_override_application_attempt=lambda requested: attempt,
        list_override_audits=lambda requested, limit: [],
    )
    service = _service(repository)
    monkeypatch.setattr(
        service,
        "_load_verified_difference",
        lambda requested: (
            report,
            SimpleNamespace(dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value),
        ),
    )

    with pytest.raises(DataCenterReadIntegrityError, match="APPLIED"):
        service.get_source_override(str(override.override_id))
