import { type FormEvent, useEffect, useReducer, useRef, useState } from "react";

import { webApi } from "./api";
import { initialStreamState, streamReducer } from "./state";
import type { JobEvent, JobResponse, WebApi } from "./types";

interface EventSourceLike {
  onopen: (() => void) | null;
  onerror: (() => void) | null;
  addEventListener(type: "job-event", listener: (event: MessageEvent<string>) => void): void;
  close(): void;
}

interface AppProps {
  api?: WebApi;
  eventSourceFactory?: (url: string) => EventSourceLike;
}

const connectionLabels = {
  connecting: "正在连接",
  connected: "已连接",
  polling: "轮询恢复",
  reconnecting: "正在重连",
  error: "连接错误",
} as const;

const defaultEventSourceFactory = (url: string): EventSourceLike =>
  new EventSource(url) as unknown as EventSourceLike;

function safeMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "请求失败。";
}

export function App({ api = webApi, eventSourceFactory = defaultEventSourceFactory }: AppProps) {
  const [health, setHealth] = useState<"checking" | "healthy" | "unhealthy">("checking");
  const [jobId, setJobId] = useState("");
  const [job, setJob] = useState<JobResponse | null>(null);
  const [jobError, setJobError] = useState<string | null>(null);
  const [stream, dispatch] = useReducer(streamReducer, initialStreamState);
  const cursorRef = useRef(0);

  useEffect(() => {
    let active = true;
    void api.getHealth().then(
      () => active && setHealth("healthy"),
      () => active && setHealth("unhealthy"),
    );
    return () => {
      active = false;
    };
  }, [api]);

  useEffect(() => {
    let active = true;
    let source: EventSourceLike | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let retry = 0;

    const ingest = (events: JobEvent[]) => {
      const cursor = events.reduce(
        (latest, event) => (event.event_id > latest ? event.event_id : latest),
        cursorRef.current,
      );
      cursorRef.current = cursor;
      dispatch({ type: "events", events });
    };

    const scheduleReconnect = () => {
      if (!active) return;
      const delay = Math.min(1000 * 2 ** retry, 8000);
      retry = Math.min(retry + 1, 3);
      reconnectTimer = setTimeout(() => {
        dispatch({ type: "connection", connection: "reconnecting" });
        connect();
      }, delay);
    };

    const recover = async () => {
      dispatch({ type: "connection", connection: "polling" });
      try {
        const page = await api.getEvents(cursorRef.current);
        if (!active) return;
        ingest(page.events);
      } catch {
        if (!active) return;
        dispatch({ type: "connection", connection: "error" });
      }
      scheduleReconnect();
    };

    function connect() {
      if (!active) return;
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      const current = eventSourceFactory(
        `/api/job-events/stream?after_event_id=${cursorRef.current}`,
      );
      source = current;
      let recovering = false;
      current.onopen = () => {
        if (!active || current !== source) return;
        retry = 0;
        dispatch({ type: "connection", connection: "connected" });
      };
      current.addEventListener("job-event", (message) => {
        if (!active || current !== source) return;
        try {
          ingest([JSON.parse(message.data) as JobEvent]);
        } catch {
          dispatch({ type: "connection", connection: "error" });
        }
      });
      current.onerror = () => {
        if (!active || current !== source || recovering) return;
        recovering = true;
        current.close();
        void recover();
      };
    }

    connect();
    return () => {
      active = false;
      source?.close();
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
    };
  }, [api, eventSourceFactory]);

  const queryJob = async (event: FormEvent) => {
    event.preventDefault();
    setJob(null);
    setJobError(null);
    try {
      setJob(await api.getJob(jobId.trim()));
    } catch (error) {
      setJobError(safeMessage(error));
    }
  };

  return (
    <main className="shell">
      <header>
        <p className="eyebrow">本机 · Stage 0</p>
        <h1>QuantKing V3 状态台</h1>
      </header>

      <section aria-labelledby="health-heading">
        <h2 id="health-heading">健康状态</h2>
        <p className={`status status-${health}`}>
          {health === "checking" ? "健康检查中" : health === "healthy" ? "服务正常" : "服务不可用"}
        </p>
      </section>

      <section aria-labelledby="job-heading">
        <h2 id="job-heading">任务查询</h2>
        <form onSubmit={queryJob}>
          <label htmlFor="job-id">任务 ID</label>
          <div className="query-row">
            <input id="job-id" value={jobId} onChange={(event) => setJobId(event.target.value)} />
            <button type="submit" disabled={!jobId.trim()}>查询任务</button>
          </div>
        </form>
        {jobError && <p role="alert">{jobError}</p>}
        {job && (
          <dl className="job-result">
            <div><dt>任务</dt><dd>{job.job_id}</dd></div>
            <div><dt>状态</dt><dd>{job.status}</dd></div>
            <div><dt>阶段</dt><dd>{job.stage ?? "尚未开始"}</dd></div>
            <div><dt>进度</dt><dd>{Math.round(job.progress * 100)}%</dd></div>
          </dl>
        )}
      </section>

      <section aria-labelledby="events-heading">
        <div className="section-heading">
          <h2 id="events-heading">持久事件</h2>
          <p>{connectionLabels[stream.connection]} · 游标：{stream.cursor}</p>
        </div>
        {stream.events.length === 0 ? (
          <p>暂无事件</p>
        ) : (
          <ul>
            {stream.events.map((event) => (
              <li key={event.event_id}>
                <span>#{event.event_id} · {event.event_type}</span>
                <strong>{event.summary}</strong>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
