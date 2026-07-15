from __future__ import annotations

import json
from asyncio import run
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from quantking.common.time import UtcTimestamp
from quantking.jobs.models import JobEvent
from quantking.jobs.services import JobService
from quantking.web.app import create_app
from quantking.web.schemas import map_job_event
from quantking.web.sse import event_stream, resolve_event_cursor


async def _no_sleep(_: float) -> None:
    return None


def _frames(chunks: list[str]) -> list[dict[str, object]]:
    frames: list[dict[str, object]] = []
    for chunk in chunks:
        lines = chunk.rstrip("\n").splitlines()
        frames.append(
            {
                "id": int(lines[0].removeprefix("id: ")),
                "event": lines[1].removeprefix("event: "),
                "data": json.loads(lines[2].removeprefix("data: ")),
            }
        )
    return frames


@pytest.mark.parametrize(
    ("header", "query", "expected"),
    [(None, None, 0), ("4", None, 4), (None, "5", 5), ("6", "6", 6)],
)
def test_resume_cursor_accepts_header_or_query(
    header: str | None,
    query: str | None,
    expected: int,
) -> None:
    assert resolve_event_cursor(last_event_id=header, after_event_id=query) == expected


@pytest.mark.parametrize(
    ("header", "query"),
    [("-1", None), ("bad", None), (None, "-1"), (None, "bad"), ("1", "2")],
)
def test_resume_cursor_rejects_invalid_or_conflicting_values(
    header: str | None,
    query: str | None,
) -> None:
    with pytest.raises(ValueError, match="cursor"):
        resolve_event_cursor(last_event_id=header, after_event_id=query)


def test_sse_batches_resume_without_duplicates_and_stop_on_disconnect(
    job_service: JobService,
    populated_job: str,
) -> None:
    del populated_job

    async def collect() -> list[str]:
        calls = 0

        async def disconnected() -> bool:
            nonlocal calls
            calls += 1
            return calls > 3

        return [
            chunk
            async for chunk in event_stream(
                job_service,
                cursor=0,
                batch_size=2,
                poll_interval_seconds=0.001,
                is_disconnected=disconnected,
                sleep=_no_sleep,
            )
        ]

    chunks = run(collect())
    frames = _frames(chunks)
    ids = [frame["id"] for frame in frames]

    assert ids == sorted(set(ids))
    assert len(ids) == 3
    assert {frame["event"] for frame in frames} == {"job-event"}
    assert [frame["data"] for frame in frames] == [
        map_job_event(event).model_dump(mode="json")
        for event in job_service.list_events(after_event_id=0, limit=100)
    ]


def test_sse_defensively_rejects_out_of_order_or_duplicate_port_results() -> None:
    occurred_at = UtcTimestamp(datetime(2026, 7, 15, 4, 5, 6, tzinfo=UTC))
    events = [
        JobEvent(2, "job:test", "second", None, None, "second", occurred_at),
        JobEvent(1, "job:test", "first", None, None, "first", occurred_at),
    ]

    class BadPort:
        def list_events(self, *, after_event_id: int, limit: int) -> list[JobEvent]:
            del after_event_id, limit
            return events

    async def collect() -> None:
        async def connected() -> bool:
            return False

        stream = event_stream(
            BadPort(),
            cursor=0,
            batch_size=10,
            poll_interval_seconds=0.001,
            is_disconnected=connected,
            sleep=_no_sleep,
        )
        await anext(stream)

    with pytest.raises(RuntimeError, match="strictly increasing"):
        run(collect())


def test_sse_runs_persisted_query_off_the_event_loop(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    occurred_at = UtcTimestamp(datetime(2026, 7, 15, 4, 5, 6, tzinfo=UTC))
    event = JobEvent(1, "job:test", "created", None, 0.0, "created", occurred_at)
    delegated = False

    class Port:
        def list_events(self, *, after_event_id: int, limit: int) -> list[JobEvent]:
            assert (after_event_id, limit) == (0, 10)
            return [event]

    async def fake_to_thread(function, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal delegated
        delegated = True
        return function(*args, **kwargs)

    monkeypatch.setattr("quantking.web.sse.asyncio.to_thread", fake_to_thread)

    async def first() -> str:
        async def connected() -> bool:
            return False

        return await anext(
            event_stream(
                Port(),
                cursor=0,
                batch_size=10,
                poll_interval_seconds=0.001,
                is_disconnected=connected,
                sleep=_no_sleep,
            )
        )

    assert "id: 1" in run(first())
    assert delegated


def test_stream_endpoint_rejects_bad_cursor_before_opening(job_service: JobService) -> None:
    with TestClient(create_app(job_service)) as client:
        bad = client.get("/api/job-events/stream?after_event_id=bad")
        conflict = client.get(
            "/api/job-events/stream?after_event_id=2",
            headers={"Last-Event-ID": "1"},
        )
    assert bad.status_code == 400
    assert conflict.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_request"
