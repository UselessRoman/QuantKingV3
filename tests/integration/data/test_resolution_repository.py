from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from quantking.common.errors import InvalidDataTransitionError
from quantking.common.ids import EntityId, parse_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.comparison.thresholds import THRESHOLD_RULE_VERSION
from quantking.data.models import DatasetKind, DatasetVersion, DataUpstream
from quantking.data.resolution import (
    RESOLUTION_RULE_VERSION,
    CRequestTrace,
    ResolutionArtifactWriter,
    ResolutionDecision,
    ResolutionDefaultSummary,
    ResolutionReasonCode,
    ResolutionRun,
    ResolutionSourceBinding,
    ResolutionSourceStatus,
    VerifiedResolutionArtifact,
    verify_resolution_artifact,
)
from quantking.jobs.adapters.persistence.sqlite import initialize_database


def _id(namespace: str, suffix: int) -> EntityId:
    return parse_entity_id(f"{namespace}:00000000-0000-4000-8000-{suffix:012d}")


def _job(database: Path, suffix: int) -> EntityId:
    job_id = _id("job", suffix)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO job_runs (job_id,job_type,idempotency_key,input_manifest_json,"
            "config_manifest_json,version_manifest_json,requester,status,created_at,progress,"
            "log_id,artifacts_json,row_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(job_id),
                "data.eod.v1",
                f"resolution-repository-{suffix}",
                "{}",
                "{}",
                "{}",
                "test",
                "SUCCEEDED",
                f"2026-08-10T00:{suffix:02d}:00Z",
                1.0,
                f"logs/{suffix}.jsonl",
                "[]",
                1,
            ),
        )
        connection.commit()
    return job_id


def _binding(
    upstream: DataUpstream,
    status: ResolutionSourceStatus,
    suffix: int | None,
    character: str | None,
) -> ResolutionSourceBinding:
    return ResolutionSourceBinding(
        upstream=upstream,
        status=status,
        batch_id=None if suffix is None else _id("data_batch", suffix),
        content_sha256=None if character is None else character * 64,
    )


def _run(
    *,
    run_suffix: int,
    job: EntityId,
    created_at: str,
    fingerprint: str,
    conflict: bool,
    candidate: EntityId | None = None,
) -> ResolutionRun:
    a = _binding(DataUpstream.EASTMONEY, ResolutionSourceStatus.AVAILABLE, 101, "a")
    b = _binding(DataUpstream.TENCENT, ResolutionSourceStatus.AVAILABLE, 102, "b")
    c = _binding(
        DataUpstream.SINA,
        ResolutionSourceStatus.AVAILABLE if conflict else ResolutionSourceStatus.NOT_CALLED,
        103 if conflict else None,
        "c" if conflict else None,
    )
    reports = (_id("data_diff", 301),)
    conflict_decisions: tuple[ResolutionDecision, ...] = ()
    if conflict:
        conflict_decisions = (
            ResolutionDecision(
                symbol="600000.SH",
                trade_date=date(2026, 8, 7),
                source_bindings=(a, b, c),
                compared_upstreams=tuple(DataUpstream),
                agreeing_upstreams=(),
                selected_upstream=None,
                selected_row_present=None,
                reason_code=ResolutionReasonCode.NO_TWO_SOURCE_CONSENSUS,
                rule_version=RESOLUTION_RULE_VERSION,
                difference_report_ids=reports,
                created_job_id=job,
                created_at=UtcTimestamp.from_iso(created_at),
                c_request=CRequestTrace(
                    symbol="600000.SH",
                    start_date=date(2026, 8, 7),
                    end_date=date(2026, 8, 7),
                    trigger_reason="FIELD_DIFFERENCE:close",
                ),
            ),
        )
    default_c = _binding(
        DataUpstream.SINA,
        ResolutionSourceStatus.NOT_CALLED,
        None,
        None,
    )
    return ResolutionRun(
        run_id=_id("data_resolution", run_suffix),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        created_job_id=job,
        created_at=UtcTimestamp.from_iso(created_at),
        rule_version=RESOLUTION_RULE_VERSION,
        threshold_rule_version=THRESHOLD_RULE_VERSION,
        input_fingerprint=fingerprint,
        candidate_version_id=candidate,
        input_bindings=(a, b, c),
        difference_report_ids=reports,
        default_summary=(
            None
            if conflict
            else ResolutionDefaultSummary(
                source_bindings=(a, b, default_c),
                compared_upstreams=(DataUpstream.EASTMONEY, DataUpstream.TENCENT),
                agreeing_upstreams=(DataUpstream.EASTMONEY, DataUpstream.TENCENT),
                selected_upstream=DataUpstream.EASTMONEY,
                reason_code=ResolutionReasonCode.AB_AGREE_DEFAULT,
                decision_count=1,
            )
        ),
        run_blockers=(),
        exceptions=(),
        conflicts=conflict_decisions,
    )


@pytest.fixture()
def repository(tmp_path: Path) -> tuple[DataSqliteUnitOfWorkFactory, Path, Path]:
    database = tmp_path / "data.sqlite3"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    yield DataSqliteUnitOfWorkFactory(engine), database, tmp_path / "artifacts"
    engine.dispose()


def _verified(run: ResolutionRun, root: Path) -> VerifiedResolutionArtifact:
    directory = ResolutionArtifactWriter(artifacts_root=root).write(run)
    return verify_resolution_artifact(directory, artifacts_root=root)


def test_resolution_repository_roundtrip_idempotency_and_immutability(
    repository: tuple[DataSqliteUnitOfWorkFactory, Path, Path],
) -> None:
    factory, database, artifacts = repository
    job = _job(database, 1)
    run = _run(
        run_suffix=1,
        job=job,
        created_at="2026-08-10T00:01:00Z",
        fingerprint="f" * 64,
        conflict=True,
    )
    verified = _verified(run, artifacts)

    with factory() as uow:
        record = uow.data.save_resolution_run(run, verified)
        uow.commit()
    with factory() as uow:
        assert uow.data.save_resolution_run(run, verified) == record
        assert uow.data.get_resolution_run(str(run.run_id)) == record
        uow.commit()

    with factory() as uow, pytest.raises(InvalidDataTransitionError, match="immutable"):
        uow.data.save_resolution_run(
            run,
            replace(verified, manifest_sha256="d" * 64),
        )


def test_resolution_repository_rejects_second_run_for_same_job(
    repository: tuple[DataSqliteUnitOfWorkFactory, Path, Path],
) -> None:
    factory, database, artifacts = repository
    job = _job(database, 2)
    first = _run(
        run_suffix=2,
        job=job,
        created_at="2026-08-10T00:02:00Z",
        fingerprint="e" * 64,
        conflict=True,
    )
    second = replace(first, run_id=_id("data_resolution", 3))

    with factory() as uow:
        uow.data.save_resolution_run(first, _verified(first, artifacts))
        uow.commit()
    with factory() as uow, pytest.raises(InvalidDataTransitionError, match="already"):
        uow.data.save_resolution_run(second, _verified(second, artifacts))


def test_resolution_repository_pages_conflicts_and_fingerprint_deterministically(
    repository: tuple[DataSqliteUnitOfWorkFactory, Path, Path],
) -> None:
    factory, database, artifacts = repository
    shared = "9" * 64
    first = _run(
        run_suffix=11,
        job=_job(database, 11),
        created_at="2026-08-10T00:11:00Z",
        fingerprint=shared,
        conflict=True,
    )
    candidate = _id("data_version", 501)
    second = _run(
        run_suffix=12,
        job=_job(database, 12),
        created_at="2026-08-10T00:12:00Z",
        fingerprint=shared,
        conflict=False,
        candidate=candidate,
    )
    with factory() as uow:
        uow.data.save_version(
            DatasetVersion.building(
                version_id=candidate,
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                schema_version="daily_bar_unadjusted.v1",
                code_version="s1-07k2",
                parent_version_id=None,
                input_batch_ids=(),
                created_at=second.created_at,
            )
        )
        uow.data.save_resolution_run(first, _verified(first, artifacts))
        uow.data.save_resolution_run(second, _verified(second, artifacts))
        uow.commit()

    with factory() as uow:
        assert uow.data.find_resolution_run_by_fingerprint(shared).run_id == second.run_id
        first_page = uow.data.list_resolution_runs_page(before=None, limit=1)
        assert [item.run_id for item in first_page] == [second.run_id]
        before = (second.created_at.to_iso(), str(second.run_id))
        assert [
            item.run_id for item in uow.data.list_resolution_runs_page(before=before, limit=2)
        ] == [first.run_id]
        assert [
            item.run_id
            for item in uow.data.list_resolution_runs_page(
                before=None,
                limit=10,
                conflicts_only=True,
            )
        ] == [first.run_id]
        uow.rollback()
