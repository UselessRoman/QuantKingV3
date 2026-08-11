from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from quantking.common.ids import parse_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.center import (
    DataCenterQueryService,
    DataCenterReadIntegrityError,
    InvalidDataCenterQueryError,
)
from quantking.data.comparison.thresholds import THRESHOLD_RULE_VERSION
from quantking.data.models import DatasetKind
from quantking.data.resolution.artifacts import (
    ResolutionArtifactVerificationError,
    VerifiedResolutionArtifact,
)
from quantking.data.resolution.models import RESOLUTION_RULE_VERSION, ResolutionRunRecord


def _id(namespace: str, suffix: int) -> object:
    return parse_entity_id(f"{namespace}:00000000-0000-4000-8000-{suffix:012d}")


def _record(*, suffix: int = 1, manifest_sha256: str = "a" * 64) -> ResolutionRunRecord:
    return ResolutionRunRecord(
        run_id=_id("data_resolution", suffix),  # type: ignore[arg-type]
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        created_job_id=_id("job", suffix),  # type: ignore[arg-type]
        candidate_version_id=None,
        created_at=UtcTimestamp.from_iso("2026-08-10T01:02:03Z"),
        rule_version=RESOLUTION_RULE_VERSION,
        threshold_rule_version=THRESHOLD_RULE_VERSION,
        input_fingerprint="f" * 64,
        artifact_relative_path=f"data-resolution/run-{suffix}",
        manifest_sha256=manifest_sha256,
        total_decisions=61,
        default_decision_count=0,
        exception_count=0,
        conflict_count=61,
        blocker_count=0,
    )


class _Decision:
    def __init__(self, index: int) -> None:
        self.index = index

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": f"{self.index:06d}.SZ",
            "trade_date": "2026-08-07",
            "reason_code": "NO_TWO_SOURCE_CONSENSUS",
        }


class _Verifier:
    def verify(self, record: ResolutionRunRecord) -> VerifiedResolutionArtifact:
        if record.manifest_sha256 != "a" * 64:
            raise ResolutionArtifactVerificationError("record SHA mismatch")
        return VerifiedResolutionArtifact(
            run_id=record.run_id,
            relative_path=record.artifact_relative_path,
            manifest_sha256=record.manifest_sha256,
            summary={
                "default_decision": None,
                "difference_report_ids": [str(_id("data_diff", 1))],
                "run_blockers": [],
            },
            exceptions=(),
            conflicts=tuple(_Decision(index) for index in range(61)),  # type: ignore[arg-type]
        )


class _Repository:
    def __init__(self, records: list[ResolutionRunRecord]) -> None:
        self.records = records

    def get_resolution_run(self, run_id: str) -> ResolutionRunRecord:
        return next(item for item in self.records if str(item.run_id) == run_id)

    def list_resolution_runs_page(self, **filters: object) -> list[ResolutionRunRecord]:
        rows = self.records
        if filters["conflicts_only"]:
            rows = [item for item in rows if item.conflict_count > 0]
        return rows[: int(filters["limit"])]


class _Uow:
    def __init__(self, repository: _Repository) -> None:
        self.data = repository

    def __enter__(self) -> _Uow:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _service(records: list[ResolutionRunRecord]) -> DataCenterQueryService:
    repository = _Repository(records)
    return DataCenterQueryService(
        data_uow_factory=lambda: _Uow(repository),  # type: ignore[arg-type]
        preview=SimpleNamespace(),
        difference_verifier=SimpleNamespace(),
        resolution_verifier=_Verifier(),
        jobs=SimpleNamespace(),
    )


def test_resolution_detail_is_bounded_and_decision_pages_are_complete() -> None:
    record = _record()
    service = _service([record])

    detail = service.get_resolution_run(str(record.run_id))
    assert "exceptions" not in detail
    assert "conflicts" not in detail
    first = service.resolution_decisions(
        run_id=str(record.run_id), conflicts=True, cursor=None, limit=50
    )
    assert len(first.items) == 50
    assert first.next_cursor is not None
    second = service.resolution_decisions(
        run_id=str(record.run_id),
        conflicts=True,
        cursor=first.next_cursor,
        limit=50,
    )
    assert len(second.items) == 11
    assert second.next_cursor is None
    assert len({str(item["symbol"]) for item in (*first.items, *second.items)}) == 61


def test_resolution_cursor_cannot_cross_run_kind_or_list_filter() -> None:
    first_record = _record()
    second_record = _record(suffix=2)
    service = _service([first_record, second_record])
    page = service.resolution_decisions(
        run_id=str(first_record.run_id), conflicts=True, cursor=None, limit=1
    )
    assert page.next_cursor is not None

    with pytest.raises(InvalidDataCenterQueryError):
        service.resolution_decisions(
            run_id=str(first_record.run_id),
            conflicts=False,
            cursor=page.next_cursor,
            limit=1,
        )
    with pytest.raises(InvalidDataCenterQueryError):
        service.resolution_decisions(
            run_id=str(second_record.run_id),
            conflicts=True,
            cursor=page.next_cursor,
            limit=1,
        )

    list_page = service.list_resolution_runs(conflicts_only=False, limit=1)
    assert list_page.next_cursor is not None
    with pytest.raises(InvalidDataCenterQueryError):
        service.list_resolution_runs(
            conflicts_only=True,
            cursor=list_page.next_cursor,
            limit=1,
        )


def test_resolution_record_artifact_sha_mismatch_is_a_stable_integrity_error() -> None:
    record = replace(_record(), manifest_sha256="b" * 64)
    service = _service([record])

    with pytest.raises(DataCenterReadIntegrityError, match="resolution lineage"):
        service.get_resolution_run(str(record.run_id))
