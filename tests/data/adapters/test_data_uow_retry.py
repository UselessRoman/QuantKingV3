"""S1-08R2 A1: data UOW SQLITE_BUSY/SQLITE_LOCKED bounded retry contract."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from quantking.common.errors import PersistenceBusyError
from quantking.common.time import utc_now
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWork,
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.jobs.adapters.persistence.sqlite import initialize_database
from quantking.jobs.models import RetryPolicy


def _lock_error() -> OperationalError:
    return OperationalError(
        "INSERT INTO audit_records", {}, sqlite3.OperationalError("database is locked")
    )


def _unique_violation() -> OperationalError:
    return OperationalError(
        "INSERT INTO audit_records",
        {},
        sqlite3.IntegrityError("UNIQUE constraint failed: audit_records.audit_id"),
    )


def _write_audit(repository: object) -> int:
    repository.append_audit(  # type: ignore[attr-defined]
        actor="pytest",
        entity="test",
        action="r2-retry",
        correlation_id="corr-1",
        occurred_at=utc_now(),
        details={"attempt": "bounded"},
    )
    return 1


def _factory(tmp_path: Path, **kwargs: object) -> DataSqliteUnitOfWorkFactory:
    database = tmp_path / "data.db"
    initialize_database(database, busy_timeout_ms=250)
    return DataSqliteUnitOfWorkFactory(
        create_data_engine(database, busy_timeout_ms=250),
        **kwargs,  # type: ignore[arg-type]
    )


def test_execute_retries_lock_errors_with_bounded_backoff_and_injected_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    factory = _factory(tmp_path, sleeper=sleeps.append, jitter=lambda low, high: 0.0)
    real_commit = DataSqliteUnitOfWork.commit
    remaining = {"failures": 2}

    def flaky_commit(self: DataSqliteUnitOfWork) -> None:
        if remaining["failures"] > 0:
            remaining["failures"] -= 1
            raise _lock_error()
        real_commit(self)

    monkeypatch.setattr(DataSqliteUnitOfWork, "commit", flaky_commit)
    policy = RetryPolicy(
        max_attempts=5,
        base_delay_ms=10,
        cap_delay_ms=160,
        jitter_ratio=0.0,
        heartbeat_min_interval_ms=1000,
        progress_min_interval_ms=250,
    )

    result = factory.execute(_write_audit, policy)

    assert result == 1
    assert sleeps == [0.01, 0.02]
    with factory() as uow:
        assert len(uow.data.list_audits(action="r2-retry", limit=10)) == 1


def test_execute_raises_persistence_busy_after_bounded_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    factory = _factory(tmp_path, sleeper=sleeps.append, jitter=lambda low, high: 0.0)

    def always_busy(self: DataSqliteUnitOfWork) -> None:
        raise _lock_error()

    monkeypatch.setattr(DataSqliteUnitOfWork, "commit", always_busy)
    policy = RetryPolicy(
        max_attempts=3,
        base_delay_ms=5,
        cap_delay_ms=20,
        jitter_ratio=0.0,
        heartbeat_min_interval_ms=1000,
        progress_min_interval_ms=250,
    )

    with pytest.raises(PersistenceBusyError) as caught:
        factory.execute(_write_audit, policy)

    message = str(caught.value)
    assert "busy" in message.casefold()
    assert "E:" not in message and ".py" not in message
    assert sleeps == [0.005, 0.01]


def test_execute_does_not_retry_constraint_or_unknown_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    factory = _factory(tmp_path, sleeper=sleeps.append, jitter=lambda low, high: 0.0)

    def constraint_commit(self: DataSqliteUnitOfWork) -> None:
        raise _unique_violation()

    monkeypatch.setattr(DataSqliteUnitOfWork, "commit", constraint_commit)
    policy = RetryPolicy(
        max_attempts=5,
        base_delay_ms=10,
        cap_delay_ms=160,
        jitter_ratio=0.0,
        heartbeat_min_interval_ms=1000,
        progress_min_interval_ms=250,
    )

    with pytest.raises(OperationalError):
        factory.execute(_write_audit, policy)
    assert sleeps == []


def test_real_cross_connection_contention_is_retried_until_release(
    tmp_path: Path,
) -> None:
    database = tmp_path / "contention.db"
    initialize_database(database, busy_timeout_ms=250)
    holder = sqlite3.connect(database, timeout=0.1, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(
        "INSERT INTO audit_records"
        "(actor, entity, action, correlation_id, occurred_at, details_json)"
        " VALUES ('holder', 'test', 'hold', 'corr', '2026-08-17T00:00:00Z', '{}')"
    )
    factory = DataSqliteUnitOfWorkFactory(
        create_data_engine(database, busy_timeout_ms=1),
        jitter=lambda low, high: 0.0,
    )
    policy = RetryPolicy(
        max_attempts=20,
        base_delay_ms=5,
        cap_delay_ms=20,
        jitter_ratio=0.0,
        heartbeat_min_interval_ms=1000,
        progress_min_interval_ms=250,
    )
    timer = threading.Timer(0.05, lambda: (holder.commit(), holder.close()))
    timer.start()
    try:
        result = factory.execute(_write_audit, policy)
        assert result == 1
    finally:
        timer.join()
        holder.close()
    with factory() as uow:
        rows = uow.data.list_audits(action="r2-retry", limit=10)
        assert len(rows) == 1


def test_real_unreleased_contention_fails_closed_after_bounded_attempts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "held.db"
    initialize_database(database, busy_timeout_ms=250)
    holder = sqlite3.connect(database, timeout=0.1)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(
        "INSERT INTO audit_records"
        "(actor, entity, action, correlation_id, occurred_at, details_json)"
        " VALUES ('holder', 'test', 'hold', 'corr', '2026-08-17T00:00:00Z', '{}')"
    )
    factory = DataSqliteUnitOfWorkFactory(
        create_data_engine(database, busy_timeout_ms=1),
        jitter=lambda low, high: 0.0,
    )
    policy = RetryPolicy(
        max_attempts=3,
        base_delay_ms=1,
        cap_delay_ms=2,
        jitter_ratio=0.0,
        heartbeat_min_interval_ms=1000,
        progress_min_interval_ms=250,
    )
    try:
        with pytest.raises(PersistenceBusyError):
            factory.execute(_write_audit, policy)
    finally:
        holder.rollback()
        holder.close()
