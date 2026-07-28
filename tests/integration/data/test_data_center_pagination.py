"""Database keyset paging used by the S1-06 data-center service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from quantking.common.ids import new_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.models import (
    DatasetKind,
    DataSource,
    IngestionBatch,
    IngestionBatchStatus,
)
from quantking.jobs.adapters.persistence.sqlite import initialize_database


def _batch(index: int, *, at: datetime) -> IngestionBatch:
    return IngestionBatch.running(
        batch_id=new_entity_id("data_batch"),
        source=DataSource.XTQUANT,
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        adapter_version="s1-06-test",
        request_params={"index": str(index)},
        started_at=UtcTimestamp(at),
    )


def test_database_keyset_page_is_stable_across_concurrent_insert(tmp_path: Path) -> None:
    database = tmp_path / "data-center.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    base = datetime(2026, 7, 28, tzinfo=UTC)
    originals = [_batch(index, at=base + timedelta(seconds=index)) for index in range(5)]
    with factory() as uow:
        for item in originals:
            uow.data.save_batch(item)
        uow.commit()

    with factory() as uow:
        first = uow.data.list_batches_page(
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            source=DataSource.XTQUANT,
            status=IngestionBatchStatus.RUNNING,
            started_from=None,
            started_to=None,
            before=None,
            limit=2,
        )
    assert len(first) == 2
    boundary = (first[-1].started_at.to_iso(), str(first[-1].batch_id))

    with factory() as uow:
        uow.data.save_batch(_batch(99, at=base + timedelta(minutes=1)))
        uow.commit()

    seen = {str(item.batch_id) for item in first}
    before = boundary
    for _ in range(3):
        with factory() as uow:
            page = uow.data.list_batches_page(
                dataset=DatasetKind.EXCHANGE_CALENDAR,
                source=DataSource.XTQUANT,
                status=IngestionBatchStatus.RUNNING,
                started_from=None,
                started_to=None,
                before=before,
                limit=2,
            )
        assert not (seen & {str(item.batch_id) for item in page})
        seen.update(str(item.batch_id) for item in page)
        if not page:
            break
        before = (page[-1].started_at.to_iso(), str(page[-1].batch_id))
    else:
        raise AssertionError("keyset pagination did not terminate")

    assert seen == {str(item.batch_id) for item in originals}
    engine.dispose()
