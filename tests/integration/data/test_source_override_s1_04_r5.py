"""S1-04-R5 red tests for override migration, row inserts and recovery."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from tests.integration.data.test_source_override_s1_04_r4_e2e import (
    _publish,
    _table,
)

from quantking.common.errors import OverrideNotApplicableError
from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate, utc_now
from quantking.data.adapters.override_transform import apply_override_to_table
from quantking.data.adapters.persistence.sqlite import (
    DataRepository,
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.adapters.warehouse.query import CanonicalQueryAdapter
from quantking.data.models import (
    DatasetKind,
    DatasetVersionStatus,
    SourceOverrideStatus,
)
from quantking.data.override import service as override_service_module
from quantking.data.quality.artifacts import QualityArtifactWriter
from quantking.data.quality.models import QualityRequest, ReferenceDataBinding
from quantking.data.quality.references import PublishedReferenceLoader
from quantking.data.quality.runner import QualityRunner
from quantking.jobs.adapters.persistence.sqlite import _REPOSITORY_ROOT, initialize_database

PRE_R5_HEAD = "20260721_0007"
R5_HEAD = "20260721_0008"


def _alembic_config(database: Path) -> Config:
    configuration = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(_REPOSITORY_ROOT / "migrations"))
    configuration.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database.as_posix()}")
    return configuration


def _run_migration(database: Path, action: str, revision: str) -> None:
    engine = create_data_engine(database, busy_timeout_ms=250)
    configuration = _alembic_config(database)
    try:
        with engine.connect() as connection:
            configuration.attributes["connection"] = connection
            getattr(command, action)(configuration, revision)
    finally:
        engine.dispose()


def _revision(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def test_r5_migration_keeps_0007_historical_and_adds_unique_0008_head(
    tmp_path: Path,
) -> None:
    database = tmp_path / "r5_migration.db"
    _run_migration(database, "upgrade", R5_HEAD)
    assert _revision(database) == R5_HEAD

    # 0007 must remain the historical R3 table-only migration; the R5
    # constraints/triggers belong to 0008 so already-run 0007 databases can
    # upgrade forward normally.
    old_0007 = (
        _REPOSITORY_ROOT
        / "migrations"
        / "versions"
        / "20260721_0007_s1_04_r3_override_application.py"
    ).read_text(encoding="utf-8")
    assert "_create_validation_triggers" not in old_0007
    assert "sa.ForeignKey" not in old_0007

    _run_migration(database, "downgrade", PRE_R5_HEAD)
    assert _revision(database) == PRE_R5_HEAD
    _run_migration(database, "upgrade", R5_HEAD)
    assert _revision(database) == R5_HEAD


def test_r5_old_0007_database_upgrades_to_0008_with_constraints(tmp_path: Path) -> None:
    database = tmp_path / "old_0007.db"
    _run_migration(database, "upgrade", PRE_R5_HEAD)
    with sqlite3.connect(database) as connection:
        before = connection.execute(
            "PRAGMA foreign_key_list(source_override_applications)"
        ).fetchall()
    assert before == []

    _run_migration(database, "upgrade", R5_HEAD)
    with sqlite3.connect(database) as connection:
        app_fks = connection.execute(
            "PRAGMA foreign_key_list(source_override_applications)"
        ).fetchall()
        attempt_fks = connection.execute(
            "PRAGMA foreign_key_list(source_override_application_attempts)"
        ).fetchall()
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
    assert {row[2] for row in app_fks} == {"source_overrides", "dataset_versions"}
    assert {row[2] for row in attempt_fks} == {"source_overrides", "dataset_versions"}
    assert "source_override_applications_freeze_terminal_update" in triggers
    assert "source_override_attempts_freeze_terminal_update" in triggers


def test_r5_dataful_downgrade_upgrade_preserves_terminal_application(
    tmp_path: Path,
) -> None:
    ctx = _r5_context(tmp_path)
    applied = ctx.service.apply_override(
        ctx.override_id,
        utc_now(),
        actor="analyst",
        quality_request=_quality_request(ctx),
    )
    assert applied.applied_version_id is not None
    database = tmp_path / "data.db"
    ctx.engine.dispose()

    _run_migration(database, "downgrade", PRE_R5_HEAD)
    with sqlite3.connect(database) as connection:
        application_before = connection.execute(
            "SELECT override_id, child_version_id, status, report_sha256, "
            "contribution_scope_sha256 FROM source_override_applications"
        ).fetchall()
        attempt_before = connection.execute(
            "SELECT override_id, child_version_id, status, ended_at "
            "FROM source_override_application_attempts"
        ).fetchall()

    _run_migration(database, "upgrade", R5_HEAD)
    with sqlite3.connect(database) as connection:
        application_after = connection.execute(
            "SELECT override_id, child_version_id, status, report_sha256, "
            "contribution_scope_sha256 FROM source_override_applications"
        ).fetchall()
        attempt_after = connection.execute(
            "SELECT override_id, child_version_id, status, ended_at "
            "FROM source_override_application_attempts"
        ).fetchall()
    assert application_after == application_before
    assert attempt_after == attempt_before


class _R5Context:
    pass


def _r5_context(tmp_path: Path) -> _R5Context:
    from quantking.data.adapters.warehouse.canonical_warehouse import CanonicalWarehouse
    from quantking.data.adapters.warehouse.publish_coordinator import (
        CanonicalPublishCoordinator,
    )
    from quantking.data.comparison.artifacts import DifferenceReportArtifactWriter
    from quantking.data.comparison.comparator import (
        CrossSourceComparator,
        CrossSourceComparisonRequest,
    )
    from quantking.data.comparison.thresholds import default_thresholds
    from quantking.data.comparison.verifier import DifferenceReportVerifier
    from quantking.data.models import DataUpstream
    from quantking.data.override.service import CreateOverrideRequest, SourceOverrideService
    from quantking.data.quality.engine import QualityEngine
    from quantking.data.quality.references import PublishedReferenceLoader
    from quantking.data.quality.rules import default_rule_set
    from quantking.data.quality.runner import QualityRunner

    database = tmp_path / "data.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=1000)
    uow_factory = DataSqliteUnitOfWorkFactory(engine)
    artifacts_root = tmp_path / "artifacts"
    warehouse = CanonicalWarehouse(data_root=tmp_path)
    coordinator = CanonicalPublishCoordinator(warehouse=warehouse, uow_factory=uow_factory)
    loader = PublishedReferenceLoader(data_root=tmp_path, uow_factory=uow_factory)
    runner = QualityRunner(
        engine=QualityEngine(rule_set=default_rule_set()), reference_loader=loader
    )
    calendar, _, _ = _publish(
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        source=DataUpstream.EASTMONEY,
        table=_table(
            "exchange_calendar",
            [
                {"exchange": "SZ", "trade_date": date(2026, 7, 15), "is_open": True},
            ],
        ),
        data_root=tmp_path,
        artifacts_root=artifacts_root,
        uow_factory=uow_factory,
        coordinator=coordinator,
        runner=runner,
        reference_data={},
    )
    calendar_binding = ReferenceDataBinding(
        dataset=calendar.dataset,
        version_id=calendar.version_id,
        content_sha256=calendar.content_sha256 or "",
    )
    primary_table = _table(
        "daily_bar_unadjusted",
        [
            {
                "code": "000001.SZ",
                "trade_date": date(2026, 7, 15),
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "pre_close": 10.0,
                "volume": 1_000_000,
                "amount": 10_500_000.0,
            }
        ],
    )
    fallback_table = _table(
        "daily_bar_unadjusted",
        [
            {
                "code": "000001.SZ",
                "trade_date": date(2026, 7, 15),
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "pre_close": 10.0,
                "volume": 1_000_000,
                "amount": 10_500_000.0,
            },
            {
                "code": "000002.SZ",
                "trade_date": date(2026, 7, 15),
                "open": 20.0,
                "high": 21.0,
                "low": 19.5,
                "close": 20.5,
                "pre_close": 20.0,
                "volume": 2_000_000,
                "amount": 41_000_000.0,
            },
            {
                "code": "000003.SZ",
                "trade_date": date(2026, 7, 15),
                "open": 30.0,
                "high": 31.0,
                "low": 29.5,
                "close": 30.5,
                "pre_close": 30.0,
                "volume": 3_000_000,
                "amount": 91_500_000.0,
            },
        ],
    )
    primary, primary_run_id, primary_batch = _publish(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        source=DataUpstream.EASTMONEY,
        table=primary_table,
        data_root=tmp_path,
        artifacts_root=artifacts_root,
        uow_factory=uow_factory,
        coordinator=coordinator,
        runner=runner,
        reference_data={"exchange_calendar": calendar_binding},
    )
    fallback, fallback_run_id, fallback_batch = _publish(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        source=DataUpstream.TENCENT,
        table=fallback_table,
        data_root=tmp_path,
        artifacts_root=artifacts_root,
        uow_factory=uow_factory,
        coordinator=coordinator,
        runner=runner,
        reference_data={"exchange_calendar": calendar_binding},
    )
    comparison = CrossSourceComparator().compare(
        request=CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version=primary.schema_version,
            primary_source=DataUpstream.EASTMONEY.value,
            fallback_source=DataUpstream.TENCENT.value,
            primary_version_id=primary.version_id,
            fallback_version_id=fallback.version_id,
            primary_batch_id=primary_batch.batch_id,
            fallback_batch_id=fallback_batch.batch_id,
            primary_content_sha256=primary.content_sha256 or "",
            fallback_content_sha256=fallback.content_sha256 or "",
            primary_quality_run_id=primary_run_id,
            fallback_quality_run_id=fallback_run_id,
            primary_table=primary_table,
            fallback_table=fallback_table,
            thresholds=default_thresholds(),
            sample_spec=("fixed:fallback-only",),
            created_at=utc_now(),
        )
    )
    DifferenceReportArtifactWriter(artifacts_root=artifacts_root).write(outcome=comparison)
    verifier = DifferenceReportVerifier(artifacts_root=artifacts_root)
    verified = verifier.verify(report_id=comparison.report_id)
    with uow_factory() as uow:
        uow.data.save_difference_report(verified)
        uow.commit()
    query_adapter = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=uow_factory)
    quality_engine = QualityEngine(rule_set=default_rule_set())
    quality_artifact_writer = QualityArtifactWriter(artifacts_root=artifacts_root)
    service = SourceOverrideService(
        uow_factory=uow_factory,
        query_adapter=query_adapter,
        quality_engine=quality_engine,
        reference_loader=loader,
        quality_artifact_writer=quality_artifact_writer,
        publish_coordinator=coordinator,
        difference_report_verifier=verifier,
    )
    pending = service.create_override(
        CreateOverrideRequest(
            difference_report_id=comparison.report_id,
            primary_batch_id=primary_batch.batch_id,
            fallback_batch_id=fallback_batch.batch_id,
            primary_source=DataUpstream.EASTMONEY.value,
            fallback_source=DataUpstream.TENCENT.value,
            scope_codes=("000002.SZ",),
            scope_dates=(TradingDate(date(2026, 7, 15)),),
            fields=(),
            fallback_only_keys=({"code": "000002.SZ", "trade_date": "2026-07-15"},),
            reason="approved missing row",
        ),
        created_at=utc_now(),
    )
    service.approve_override(pending.override_id, actor="analyst", at=utc_now())
    ctx = _R5Context()
    ctx.engine = engine
    ctx.uow_factory = uow_factory
    ctx.service = service
    ctx.query_adapter = query_adapter
    ctx.quality_engine = quality_engine
    ctx.reference_loader = loader
    ctx.quality_artifact_writer = quality_artifact_writer
    ctx.publish_coordinator = coordinator
    ctx.difference_report_verifier = verifier
    ctx.override_id = pending.override_id
    ctx.calendar_binding = calendar_binding
    ctx.tmp_path = tmp_path
    return ctx


def _quality_request(ctx: _R5Context) -> QualityRequest:
    return QualityRequest(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        version_id=new_entity_id("data_version"),
        rule_version="quality-rules.v1",
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-04-r4",
        content_sha256="0" * 64,
        input_batch_ids=(),
        expected_securities=("000001.SZ", "000002.SZ"),
        expected_exchanges=("SZ",),
        coverage_start=TradingDate(date(2026, 7, 15)),
        coverage_end=TradingDate(date(2026, 7, 15)),
        reference_data={"exchange_calendar": ctx.calendar_binding},
        empty_result=None,
    )


def test_r5_fallback_only_override_inserts_only_approved_missing_row(tmp_path: Path) -> None:
    ctx = _r5_context(tmp_path)
    applied = ctx.service.apply_override(
        ctx.override_id,
        utc_now(),
        actor="analyst",
        quality_request=_quality_request(ctx),
    )
    assert applied.status is SourceOverrideStatus.APPLIED
    assert applied.applied_version_id is not None
    result = CanonicalQueryAdapter(data_root=tmp_path, uow_factory=ctx.uow_factory).query_by_id(
        version_id=str(applied.applied_version_id),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
    )
    rows = sorted(result.to_pylist(), key=lambda row: (row["code"], row["trade_date"]))
    assert [row["code"] for row in rows] == ["000001.SZ", "000002.SZ"]
    assert rows[0]["close"] == 10.5
    assert rows[1]["close"] == 20.5
    manifest = (
        CanonicalQueryAdapter(data_root=tmp_path, uow_factory=ctx.uow_factory)
        .verify_manifest_by_id(
            version_id=str(applied.applied_version_id),
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
        )
        .manifest
    )
    contributions = manifest["source_override"]["fallback_contributions"]
    assert contributions == [
        {
            "kind": "ROW_INSERT",
            "primary_key": {"code": "000002.SZ", "trade_date": "2026-07-15"},
            "fields": [
                "amount",
                "close",
                "high",
                "low",
                "open",
                "pre_close",
                "volume",
            ],
        }
    ]


def test_r5_exact_field_unit_does_not_expand_cartesian_scope() -> None:
    primary = _table(
        "daily_bar_unadjusted",
        [
            {
                "code": code,
                "trade_date": date(2026, 7, 15),
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "pre_close": 10.0,
                "volume": 1_000_000,
                "amount": 10_500_000.0,
            }
            for code in ("000001.SZ", "000002.SZ")
        ],
    )
    fallback = _table(
        "daily_bar_unadjusted",
        [
            {
                "code": code,
                "trade_date": date(2026, 7, 15),
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.8,
                "pre_close": 10.0,
                "volume": 1_000_000,
                "amount": 10_800_000.0,
            }
            for code in ("000001.SZ", "000002.SZ")
        ],
    )
    result = apply_override_to_table(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        primary_table=primary,
        fallback_table=fallback,
        field_replacements=(
            {
                "code": "000001.SZ",
                "trade_date": "2026-07-15",
                "field": "close",
            },
        ),
    )
    rows = {row["code"]: row for row in result.table.to_pylist()}
    assert rows["000001.SZ"]["close"] == 10.8
    assert rows["000001.SZ"]["amount"] == 10_500_000.0
    assert rows["000002.SZ"]["close"] == 10.5
    assert result.changed_cells == 1


def test_r5_deterministic_pre_publish_failure_marks_attempt_failed_and_retry_new_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _r5_context(tmp_path)
    original_write = QualityArtifactWriter.write

    def fail_once(self: QualityArtifactWriter, **kwargs: object):
        raise RuntimeError("absolute path E:/secret/raw vendor token 123")

    monkeypatch.setattr(QualityArtifactWriter, "write", fail_once)
    with pytest.raises(OverrideNotApplicableError):
        ctx.service.apply_override(
            ctx.override_id,
            utc_now(),
            actor="analyst",
            quality_request=_quality_request(ctx),
        )
    with ctx.uow_factory() as uow:
        failed_child = uow.data.find_last_override_application_attempt(str(ctx.override_id))
        child = uow.data.get_version(str(failed_child.child_version_id))
        assert child.status is DatasetVersionStatus.FAILED
        assert child.failure is not None
        assert "E:/" not in child.failure.message
        assert "secret" not in child.failure.message
        assert failed_child.status == "FAILED"
        uow.rollback()
    with sqlite3.connect(tmp_path / "data.db") as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE source_override_applications SET failure_message='rewritten' "
                "WHERE override_id=? AND status='FAILED'",
                (str(ctx.override_id),),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE source_override_application_attempts SET status=status "
                "WHERE override_id=? AND status='FAILED'",
                (str(ctx.override_id),),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM source_override_application_attempts "
                "WHERE override_id=? AND status='FAILED'",
                (str(ctx.override_id),),
            )

    monkeypatch.setattr(QualityArtifactWriter, "write", original_write)
    applied = ctx.service.apply_override(
        ctx.override_id,
        utc_now(),
        actor="analyst",
        quality_request=_quality_request(ctx),
    )
    assert applied.applied_version_id is not None
    assert applied.applied_version_id != failed_child.child_version_id


@pytest.mark.parametrize(
    ("target_name", "attribute"),
    [
        ("transformation", "apply_override_to_table"),
        ("reference_loader", "load"),
        ("quality_runner", "run"),
        ("quality_run_repository", "save_quality_run"),
        ("warehouse", "_write_partitions"),
        ("warehouse", "_write_manifest"),
        ("warehouse", "_write_complete_marker"),
        ("warehouse", "_atomic_promote"),
    ],
)
def test_r5_pre_publish_failure_matrix_uses_failed_child_then_new_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    attribute: str,
) -> None:
    from quantking.data.adapters.warehouse.canonical_warehouse import CanonicalWarehouse

    ctx = _r5_context(tmp_path)
    target = {
        "transformation": override_service_module,
        "reference_loader": PublishedReferenceLoader,
        "quality_runner": QualityRunner,
        "quality_run_repository": DataRepository,
        "warehouse": CanonicalWarehouse,
    }[target_name]
    original = getattr(target, attribute)

    def fail(*args: object, **kwargs: object) -> object:
        raise RuntimeError("E:/private/vendor secret=raw failure")

    monkeypatch.setattr(target, attribute, fail)
    with pytest.raises(OverrideNotApplicableError):
        ctx.service.apply_override(
            ctx.override_id,
            utc_now(),
            actor="analyst",
            quality_request=_quality_request(ctx),
        )
    with ctx.uow_factory() as uow:
        failed_attempt = uow.data.find_last_override_application_attempt(str(ctx.override_id))
        failed_child = uow.data.get_version(str(failed_attempt.child_version_id))
        uow.rollback()
    assert failed_attempt.status == "FAILED"
    assert failed_child.status is DatasetVersionStatus.FAILED
    assert failed_child.failure is not None
    assert "E:/" not in failed_child.failure.message
    assert "private" not in failed_child.failure.message

    monkeypatch.setattr(target, attribute, original)
    applied = ctx.service.apply_override(
        ctx.override_id,
        utc_now(),
        actor="analyst",
        quality_request=_quality_request(ctx),
    )
    assert applied.applied_version_id is not None
    assert applied.applied_version_id != failed_child.version_id


def test_r5_validated_interruption_recovers_same_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantking.data.adapters.warehouse.publish_coordinator import (
        CanonicalPublishCoordinator,
    )

    ctx = _r5_context(tmp_path)
    original = CanonicalPublishCoordinator._transition_to_published
    calls = 0

    def interrupt_once(self: CanonicalPublishCoordinator, version: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("interrupt after VALIDATED")
        return original(self, version)  # type: ignore[arg-type]

    monkeypatch.setattr(CanonicalPublishCoordinator, "_transition_to_published", interrupt_once)
    with pytest.raises(OverrideNotApplicableError, match="durable promotion"):
        ctx.service.apply_override(
            ctx.override_id,
            utc_now(),
            actor="analyst",
            quality_request=_quality_request(ctx),
        )
    with ctx.uow_factory() as uow:
        child = uow.data.find_recoverable_override_child(str(ctx.override_id))
        uow.rollback()
    assert child.status is DatasetVersionStatus.VALIDATED
    monkeypatch.setattr(CanonicalPublishCoordinator, "_transition_to_published", original)
    applied = ctx.service.apply_override(
        ctx.override_id,
        utc_now(),
        actor="analyst",
        quality_request=_quality_request(ctx),
    )
    assert applied.applied_version_id == child.version_id


@pytest.mark.parametrize("stage", ["binding", "applied_audit"])
def test_r5_post_publish_interruptions_recover_same_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    ctx = _r5_context(tmp_path)
    if stage == "binding":
        original = DataRepository.bind_published_override_child
        calls = 0

        def interrupt_binding(self: DataRepository, **kwargs: object) -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("interrupt after PUBLISHED before binding")
            return original(self, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(DataRepository, "bind_published_override_child", interrupt_binding)
    else:
        original_audit = DataRepository.append_audit
        calls = 0

        def interrupt_audit(self: DataRepository, **kwargs: object) -> object:
            nonlocal calls
            if kwargs.get("action") == "override_applied":
                calls += 1
                if calls == 1:
                    raise RuntimeError("interrupt during APPLIED audit")
            return original_audit(self, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(DataRepository, "append_audit", interrupt_audit)

    with pytest.raises(RuntimeError):
        ctx.service.apply_override(
            ctx.override_id,
            utc_now(),
            actor="analyst",
            quality_request=_quality_request(ctx),
        )
    with ctx.uow_factory() as uow:
        child = uow.data.find_recoverable_override_child(str(ctx.override_id))
        uow.rollback()
    assert child.status is DatasetVersionStatus.PUBLISHED

    if stage == "applied_audit":
        with sqlite3.connect(tmp_path / "data.db") as connection:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE source_override_applications SET report_sha256=? "
                    "WHERE override_id=? AND status='PUBLISHED'",
                    ("f" * 64, str(ctx.override_id)),
                )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE source_override_applications SET status=status "
                    "WHERE override_id=? AND status='PUBLISHED'",
                    (str(ctx.override_id),),
                )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE source_override_application_attempts SET claimed_by='intruder' "
                    "WHERE override_id=? AND status='PUBLISHED'",
                    (str(ctx.override_id),),
                )

    if stage == "binding":
        monkeypatch.setattr(DataRepository, "bind_published_override_child", original)
    else:
        monkeypatch.setattr(DataRepository, "append_audit", original_audit)
    applied = ctx.service.apply_override(
        ctx.override_id,
        utc_now(),
        actor="analyst",
        quality_request=_quality_request(ctx),
    )
    assert applied.applied_version_id == child.version_id


def test_r5_terminal_application_and_attempt_updates_are_frozen(tmp_path: Path) -> None:
    ctx = _r5_context(tmp_path)
    applied = ctx.service.apply_override(
        ctx.override_id,
        utc_now(),
        actor="analyst",
        quality_request=_quality_request(ctx),
    )
    assert applied.applied_version_id is not None
    with sqlite3.connect(tmp_path / "data.db") as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE source_override_applications SET claimed_by='intruder' WHERE override_id=?",
                (str(ctx.override_id),),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE source_override_application_attempts SET status=status WHERE override_id=?",
                (str(ctx.override_id),),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM source_override_applications WHERE override_id=?",
                (str(ctx.override_id),),
            )


def test_r5_claimed_interruption_and_concurrent_retry_resolve_without_permanent_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _r5_context(tmp_path)
    original_run = ctx.service._quality_runner.run
    calls = 0

    def interrupt_once(*, request: QualityRequest, table: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated process interruption before quality")
        return original_run(request=request, table=table)  # type: ignore[arg-type]

    monkeypatch.setattr(ctx.service._quality_runner, "run", interrupt_once)
    with pytest.raises(OverrideNotApplicableError):
        ctx.service.apply_override(
            ctx.override_id,
            utc_now(),
            actor="analyst-a",
            quality_request=_quality_request(ctx),
        )
    monkeypatch.setattr(ctx.service._quality_runner, "run", original_run)

    from quantking.data.override.service import SourceOverrideService

    second_service = SourceOverrideService(
        uow_factory=ctx.uow_factory,
        query_adapter=ctx.query_adapter,
        quality_engine=ctx.quality_engine,
        reference_loader=ctx.reference_loader,
        quality_artifact_writer=ctx.quality_artifact_writer,
        publish_coordinator=ctx.publish_coordinator,
        difference_report_verifier=ctx.difference_report_verifier,
    )

    def apply(service: SourceOverrideService) -> object:
        try:
            return service.apply_override(
                ctx.override_id,
                utc_now(),
                actor="analyst-b",
                quality_request=_quality_request(ctx),
            )
        except Exception as error:  # pragma: no cover - asserted below
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(apply, (ctx.service, second_service)))
    errors = [item for item in results if isinstance(item, Exception)]
    assert not errors
    versions = {item.applied_version_id for item in results}  # type: ignore[attr-defined]
    assert len(versions) == 1
