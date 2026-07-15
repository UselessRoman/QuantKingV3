"""Persisted-cursor Server-Sent Events transport helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from itertools import pairwise

from quantking.web.ports import EventReadPort
from quantking.web.schemas import map_job_event

DisconnectCheck = Callable[[], Awaitable[bool]]
Sleep = Callable[[float], Awaitable[None]]
SSE_EVENT_NAME = "job-event"


def _cursor(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("event cursor must be a non-negative integer") from error
    if value < 0 or str(value) != raw.strip():
        raise ValueError("event cursor must be a non-negative integer")
    return value


def resolve_event_cursor(*, last_event_id: str | None, after_event_id: str | None) -> int:
    """Resolve resume input; equal header/query values are allowed, conflicts fail."""
    header_cursor = _cursor(last_event_id)
    query_cursor = _cursor(after_event_id)
    if header_cursor is not None and query_cursor is not None and header_cursor != query_cursor:
        raise ValueError("event cursor header and query conflict")
    return header_cursor if header_cursor is not None else query_cursor or 0


def _frame(event_id: int, data: str) -> str:
    return f"id: {event_id}\nevent: {SSE_EVENT_NAME}\ndata: {data}\n\n"


async def event_stream(
    events: EventReadPort,
    *,
    cursor: int,
    batch_size: int,
    poll_interval_seconds: float,
    is_disconnected: DisconnectCheck,
    sleep: Sleep = asyncio.sleep,
) -> AsyncIterator[str]:
    """Read only persisted batches and advance a strictly monotonic cursor."""
    while not await is_disconnected():
        # SQLite busy_timeout and bounded retries are synchronous; keep them off
        # the event loop so disconnect detection and other local requests progress.
        batch = await asyncio.to_thread(
            events.list_events,
            after_event_id=cursor,
            limit=batch_size,
        )
        event_ids = [event.event_id for event in batch]
        if event_ids and (
            event_ids[0] <= cursor
            or any(current <= previous for previous, current in pairwise(event_ids))
        ):
            raise RuntimeError("event port results must be strictly increasing")
        for event in batch:
            payload = map_job_event(event).model_dump_json()
            yield _frame(event.event_id, payload)
            cursor = event.event_id
        if not batch:
            await sleep(poll_interval_seconds)
