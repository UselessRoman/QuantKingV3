import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type { EventPage, HealthResponse, JobEvent, JobResponse, WebApi } from "./types";

const health: HealthResponse = {
  status: "ok",
  service: "quantking-web",
  checked_at: "2026-07-15T04:05:06Z",
};

const job: JobResponse = {
  job_id: "job:fixture",
  job_type: "synthetic",
  status: "RUNNING",
  created_at: "2026-07-15T04:05:06Z",
  started_at: "2026-07-15T04:05:06Z",
  ended_at: null,
  heartbeat_at: "2026-07-15T04:05:06Z",
  stage: "test",
  progress: 0.5,
  worker_id: "worker",
  failure: null,
  row_version: 2,
};

const event = (eventId: number): JobEvent => ({
  event_id: eventId,
  job_id: job.job_id,
  event_type: "job_progress",
  stage: "test",
  progress: 0.5,
  summary: `事件 ${eventId}`,
  occurred_at: "2026-07-15T04:05:06Z",
});

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readonly url: string;
  readonly createdAt: number;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  private jobEventListener: ((message: MessageEvent<string>) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    this.createdAt = Date.now();
    FakeEventSource.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }

  addEventListener(
    type: "job-event",
    listener: (message: MessageEvent<string>) => void,
  ): void {
    expect(type).toBe("job-event");
    this.jobEventListener = listener;
  }

  emit(item: JobEvent): void {
    this.jobEventListener?.(new MessageEvent("job-event", { data: JSON.stringify(item) }));
  }
}

const api = (overrides: Partial<WebApi> = {}): WebApi => ({
  getHealth: vi.fn(async () => health),
  getJob: vi.fn(async () => job),
  getEvents: vi.fn(async (cursor: number): Promise<EventPage> => ({ events: [], next_cursor: cursor })),
  ...overrides,
});

describe("Stage 0 Web shell", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows healthy only after a successful health response", async () => {
    render(<App api={api()} eventSourceFactory={(url) => new FakeEventSource(url)} />);
    expect(screen.getByText("健康检查中")).toBeInTheDocument();
    expect(await screen.findByText("服务正常")).toBeInTheDocument();
  });

  it("keeps a failed health check visibly unhealthy", async () => {
    render(
      <App
        api={api({ getHealth: vi.fn(async () => { throw new Error("secret backend detail"); }) })}
        eventSourceFactory={(url) => new FakeEventSource(url)}
      />,
    );
    expect(await screen.findByText("服务不可用")).toBeInTheDocument();
    expect(screen.queryByText("服务正常")).not.toBeInTheDocument();
    expect(screen.queryByText(/secret/)).not.toBeInTheDocument();
  });

  it("queries a job and shows a stable not-found message", async () => {
    const webApi = api({ getJob: vi.fn(async () => { throw new Error("任务不存在。"); }) });
    render(<App api={webApi} eventSourceFactory={(url) => new FakeEventSource(url)} />);
    fireEvent.change(screen.getByLabelText("任务 ID"), { target: { value: "job:missing" } });
    fireEvent.click(screen.getByRole("button", { name: "查询任务" }));
    expect(await screen.findByText("任务不存在。")).toBeInTheDocument();
  });

  it("ingests SSE events once and ignores duplicates", async () => {
    render(<App api={api()} eventSourceFactory={(url) => new FakeEventSource(url)} />);
    const source = FakeEventSource.instances[0];
    act(() => {
      source.onopen?.();
      source.emit(event(1));
      source.emit(event(1));
      source.emit(event(2));
    });
    expect(screen.getByText(/已连接/)).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText(/游标：2/)).toBeInTheDocument();
  });

  it("polls after disconnect, reconnects with the recovered cursor, and cleans up", async () => {
    vi.useFakeTimers();
    const getEvents = vi.fn(async (): Promise<EventPage> => ({ events: [event(3)], next_cursor: 3 }));
    const view = render(
      <App api={api({ getEvents })} eventSourceFactory={(url) => new FakeEventSource(url)} />,
    );
    const first = FakeEventSource.instances[0];
    act(() => {
      first.emit(event(2));
      first.onerror?.();
    });
    await act(async () => { await Promise.resolve(); });
    expect(getEvents).toHaveBeenCalledWith(2);
    expect(screen.getByText(/轮询恢复/)).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(FakeEventSource.instances[1].url).toBe("/api/job-events/stream?after_event_id=3");

    view.unmount();
    expect(FakeEventSource.instances.every((source) => source.closed)).toBe(true);
    await act(async () => { await vi.runOnlyPendingTimersAsync(); });
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  it("backs off at 1/2/4/8 seconds, caps at 8, and resets after open", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const view = render(
      <App api={api()} eventSourceFactory={(url) => new FakeEventSource(url)} />,
    );

    const failAndAdvance = async (sourceIndex: number, delay: number) => {
      act(() => FakeEventSource.instances[sourceIndex].onerror?.());
      await act(async () => { await Promise.resolve(); });
      await act(async () => { await vi.advanceTimersByTimeAsync(delay); });
    };

    await failAndAdvance(0, 1000);
    await failAndAdvance(1, 2000);
    await failAndAdvance(2, 4000);
    await failAndAdvance(3, 8000);
    await failAndAdvance(4, 8000);

    expect(FakeEventSource.instances.map((source) => source.createdAt)).toEqual([
      0, 1000, 3000, 7000, 15_000, 23_000,
    ]);

    act(() => FakeEventSource.instances[5].onopen?.());
    await failAndAdvance(5, 1000);
    expect(FakeEventSource.instances[6].createdAt).toBe(24_000);

    view.unmount();
    expect(FakeEventSource.instances.every((source) => source.closed)).toBe(true);
  });
});
