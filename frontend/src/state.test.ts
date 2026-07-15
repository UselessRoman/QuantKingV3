import { describe, expect, it } from "vitest";

import { initialStreamState, streamReducer } from "./state";
import type { JobEvent } from "./types";

const event = (eventId: number): JobEvent => ({
  event_id: eventId,
  job_id: "job:fixture",
  event_type: "job_progress",
  stage: "test",
  progress: 0.5,
  summary: `事件 ${eventId}`,
  occurred_at: "2026-07-15T04:05:06Z",
});

describe("persisted event reducer", () => {
  it("advances a monotonic cursor and ignores duplicate or stale events", () => {
    const first = streamReducer(initialStreamState, { type: "events", events: [event(1), event(2)] });
    const repeated = streamReducer(first, { type: "events", events: [event(2), event(1)] });
    const advanced = streamReducer(repeated, { type: "events", events: [event(4), event(3)] });

    expect(first.cursor).toBe(2);
    expect(repeated).toEqual(first);
    expect(advanced.cursor).toBe(4);
    expect(advanced.events.map((item) => item.event_id)).toEqual([1, 2, 3, 4]);
  });
});
