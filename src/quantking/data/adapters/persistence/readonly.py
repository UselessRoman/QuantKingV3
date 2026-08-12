"""Immutable read-only SQLAlchemy UOW for offline release verification."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from quantking.data.adapters.persistence.sqlite import DataSqliteUnitOfWorkFactory


@contextmanager
def readonly_data_uow_factory(
    database: Path,
) -> Iterator[DataSqliteUnitOfWorkFactory]:
    """Yield a NullPool UOW whose every connection is immutable and query-only."""
    if not isinstance(database, Path) or not database.is_absolute() or not database.is_file():
        raise ValueError("database must be an existing absolute file")
    uri = f"{database.resolve().as_uri()}?mode=ro&immutable=1"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(uri, uri=True, timeout=0, check_same_thread=False)
        connection.execute("PRAGMA query_only=ON")
        return connection

    engine: Engine = create_engine("sqlite://", creator=connect, poolclass=NullPool)
    try:
        yield DataSqliteUnitOfWorkFactory(engine)
    finally:
        engine.dispose()


__all__ = ["readonly_data_uow_factory"]
