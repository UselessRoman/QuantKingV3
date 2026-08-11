import { type FormEvent, useEffect, useMemo, useReducer, useRef, useState } from "react";

import { webApi } from "./api";
import { initialStreamState, streamReducer } from "./state";
import type {
  CursorPage,
  EodRequest,
  JobEvent,
  JobResponse,
  WebApi,
} from "./types";

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

type Tab =
  | "overview"
  | "batches"
  | "versions"
  | "quality"
  | "differences"
  | "resolutions"
  | "eod"
  | "failures";

const tabs: Array<{ id: Tab; label: string; path?: string }> = [
  { id: "overview", label: "总览", path: "/api/data/summary" },
  { id: "batches", label: "批次", path: "/api/data/batches" },
  { id: "versions", label: "版本", path: "/api/data/versions" },
  { id: "quality", label: "质量", path: "/api/data/quality-runs" },
  { id: "differences", label: "差异", path: "/api/data/difference-reports" },
  { id: "resolutions", label: "仲裁", path: "/api/data/resolution-runs" },
  { id: "eod", label: "EOD", path: "/api/data/eod-runs" },
  { id: "failures", label: "失败", path: "/api/data/failures" },
];
const actionLabels: Partial<Record<Tab, string>> = {
  versions: "显式预览",
  quality: "规则结果",
  differences: "行级差异",
  resolutions: "仲裁详情",
};

const datasets = ["daily_bar_unadjusted"] as const;
const upstreams = ["EASTMONEY", "TENCENT", "SINA"] as const;
const filterDefaults: Record<Tab, Record<string, string>> = {
  overview: {},
  batches: { dataset: "", upstream: "", status: "", started_from: "", started_to: "" },
  versions: { dataset: "", status: "" },
  quality: { version_id: "", status: "" },
  differences: { version_id: "", primary_source: "" },
  resolutions: { conflicts_only: "" },
  eod: { status: "" },
  failures: {},
};

const connectionLabels = {
  connecting: "正在连接",
  connected: "已连接",
  polling: "轮询恢复",
  reconnecting: "正在重连",
  error: "连接错误",
} as const;

const defaultEventSourceFactory = (url: string): EventSourceLike =>
  new EventSource(url) as unknown as EventSourceLike;

const safeMessage = (error: unknown): string =>
  error instanceof Error && error.message ? error.message : "请求失败。";

const shanghaiTime = (value: unknown): string => {
  if (typeof value !== "string" || !value.includes("T")) return String(value ?? "—");
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? value
    : new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai",
        dateStyle: "medium",
        timeStyle: "medium",
        hour12: false,
      }).format(parsed);
};

const displayValue = (key: string, value: unknown): string => {
  if (value === null || value === undefined || value === "") return "—";
  if (key.endsWith("_at")) return shanghaiTime(value);
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
};

const idempotencyKey = (): string =>
  globalThis.crypto?.randomUUID?.() ?? `eod-${Date.now()}-${Math.random().toString(16).slice(2)}`;

function DataTable({
  rows,
  onPreview,
  actionLabel = "查看详情",
}: {
  rows: Array<Record<string, unknown>>;
  onPreview?: (row: Record<string, unknown>) => void;
  actionLabel?: string;
}) {
  if (rows.length === 0) return <p className="empty">暂无记录</p>;
  const preferred = [
    "dataset",
    "provider",
    "upstream",
    "version_id",
    "batch_id",
    "run_id",
    "report_id",
    "job_id",
    "status",
    "coverage_start",
    "coverage_end",
    "row_count",
    "created_at",
    "blocking_reason",
  ];
  const keys = [...new Set(rows.flatMap((row) => Object.keys(row)))]
    .sort((left, right) => {
      const l = preferred.indexOf(left);
      const r = preferred.indexOf(right);
      return (l < 0 ? 999 : l) - (r < 0 ? 999 : r);
    })
    .slice(0, 9);
  return (
    <div className="table-scroll">
      <table>
        <thead><tr>{keys.map((key) => <th key={key}>{key}</th>)}{onPreview && <th>操作</th>}</tr></thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={String(row.version_id ?? row.batch_id ?? row.run_id ?? row.job_id ?? index)}>
              {keys.map((key) => <td key={key}>{displayValue(key, row[key])}</td>)}
              {onPreview && (
                <td><button className="secondary" onClick={() => onPreview(row)}>{actionLabel}</button></td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FilterBar({
  tab,
  values,
  onChange,
}: {
  tab: Tab;
  values: Record<string, string>;
  onChange: (name: string, value: string) => void;
}) {
  const datasetSelect = ["batches", "versions"].includes(tab);
  if (!datasetSelect && !["quality", "differences", "resolutions", "eod"].includes(tab)) return null;
  return (
    <div className="filters" aria-label={`${tab} 过滤`}>
      {datasetSelect && (
        <label>数据集<select aria-label={`${tab} dataset`} value={values.dataset} onChange={(event) => onChange("dataset", event.target.value)}>
          <option value="">全部</option>{datasets.map((value) => <option key={value}>{value}</option>)}
        </select></label>
      )}
      {tab === "batches" && (
        <>
          <label>上游<select aria-label="batches upstream" value={values.upstream} onChange={(event) => onChange("upstream", event.target.value)}>
            <option value="">全部</option>{upstreams.map((value) => <option key={value}>{value}</option>)}
          </select></label>
          <label>状态<select aria-label="batches status" value={values.status} onChange={(event) => onChange("status", event.target.value)}>
            <option value="">全部</option>{["RUNNING", "SUCCEEDED", "PARTIAL", "FAILED"].map((value) => <option key={value}>{value}</option>)}
          </select></label>
          <label>开始时间<input aria-label="batches started from" type="datetime-local" value={values.started_from} onChange={(event) => onChange("started_from", event.target.value)} /></label>
          <label>结束时间<input aria-label="batches started to" type="datetime-local" value={values.started_to} onChange={(event) => onChange("started_to", event.target.value)} /></label>
        </>
      )}
      {tab === "versions" && <label>状态<select aria-label="versions status" value={values.status} onChange={(event) => onChange("status", event.target.value)}><option value="">全部</option>{["BUILDING", "VALIDATED", "PUBLISHED", "FAILED"].map((value) => <option key={value}>{value}</option>)}</select></label>}
      {tab === "quality" && (
        <>
          <label>Version ID<input aria-label="quality version" value={values.version_id} onChange={(event) => onChange("version_id", event.target.value)} /></label>
          <label>状态<select aria-label="quality status" value={values.status} onChange={(event) => onChange("status", event.target.value)}><option value="">全部</option>{["RUNNING", "PASSED", "FAILED", "SKIPPED"].map((value) => <option key={value}>{value}</option>)}</select></label>
        </>
      )}
      {tab === "differences" && (
        <>
          <label>Version ID<input aria-label="differences version" value={values.version_id} onChange={(event) => onChange("version_id", event.target.value)} /></label>
          <label>主上游<select aria-label="differences source" value={values.primary_source} onChange={(event) => onChange("primary_source", event.target.value)}><option value="">全部</option>{upstreams.map((value) => <option key={value}>{value}</option>)}</select></label>
        </>
      )}
      {tab === "resolutions" && <label>视图<select aria-label="resolutions conflicts" value={values.conflicts_only} onChange={(event) => onChange("conflicts_only", event.target.value)}><option value="">全部仲裁</option><option value="true">仅冲突隔离</option></select></label>}
      {tab === "eod" && <label>状态<select aria-label="eod status" value={values.status} onChange={(event) => onChange("status", event.target.value)}><option value="">全部</option>{["PENDING", "RUNNING", "CANCEL_REQUESTED", "CANCELLED", "SUCCEEDED", "FAILED"].map((value) => <option key={value}>{value}</option>)}</select></label>}
    </div>
  );
}

function VersionLineage({ value }: { value: Record<string, unknown> }) {
  return (
    <div className="lineage-detail" aria-label="Version 完整血缘">
      <h3>Version 血缘</h3>
      <dl>
        {["version_id", "parent_version_id", "input_batch_ids", "coverage", "coverage_start", "coverage_end", "quality_run_id", "schema_version", "rule_version", "content_sha256"].map((key) => (
          <div key={key}><dt>{key}</dt><dd>{displayValue(key, value[key])}</dd></div>
        ))}
      </dl>
    </div>
  );
}

interface ResolutionDetailState {
  value: Record<string, unknown>;
  exceptions: Record<string, unknown>[];
  conflicts: Record<string, unknown>[];
  exceptionsNext: string | null;
  conflictsNext: string | null;
}

function ResolutionLineage({
  detail,
  onMore,
}: {
  detail: ResolutionDetailState;
  onMore: (kind: "exceptions" | "conflicts") => void;
}) {
  const { value, exceptions, conflicts } = detail;
  return (
    <div className="lineage-detail" aria-label="仲裁完整血缘">
      <h3>ResolutionRun 仲裁与来源例外</h3>
      {["run_id", "status", "created_job_id", "candidate_version_id", "rule_version", "threshold_rule_version", "input_fingerprint", "manifest_sha256", "default_decision", "difference_report_ids", "run_blockers"].map((key) => (
        <div className="lineage-row" key={key}><strong>{key}</strong><pre>{displayValue(key, value[key])}</pre></div>
      ))}
      <h4>来源例外</h4><DataTable rows={exceptions} />
      {detail.exceptionsNext && <button onClick={() => onMore("exceptions")}>加载更多来源例外</button>}
      <h4>冲突隔离</h4><DataTable rows={conflicts} />
      {detail.conflictsNext && <button onClick={() => onMore("conflicts")}>加载更多冲突</button>}
    </div>
  );
}

export function App({ api = webApi, eventSourceFactory = defaultEventSourceFactory }: AppProps) {
  const [health, setHealth] = useState<"checking" | "healthy" | "unhealthy">("checking");
  const [tab, setTab] = useState<Tab>("overview");
  const [data, setData] = useState<unknown>(null);
  const [dataState, setDataState] = useState<"loading" | "ready" | "error">("loading");
  const [dataError, setDataError] = useState("");
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [pageHistory, setPageHistory] = useState<string[]>([]);
  const [filters, setFilters] = useState(filterDefaults);
  const [jobId, setJobId] = useState("");
  const [job, setJob] = useState<JobResponse | null>(null);
  const [jobError, setJobError] = useState<string | null>(null);
  const [stream, dispatch] = useReducer(streamReducer, initialStreamState);
  const cursorRef = useRef(0);
  const [versionDetail, setVersionDetail] = useState<Record<string, unknown> | null>(null);
  const [preview, setPreview] = useState<{
    versionId: string;
    rows: Record<string, unknown>[];
    nextCursor: string | null;
    history: string[];
  } | null>(null);
  const [resolutionDetail, setResolutionDetail] = useState<ResolutionDetailState | null>(null);
  const [differenceDetail, setDifferenceDetail] = useState<{
    reportId: string;
    rows: Record<string, unknown>[];
    nextCursor: string | null;
    history: string[];
  } | null>(null);
  const [differenceReport, setDifferenceReport] = useState<Record<string, unknown> | null>(null);
  const [detail, setDetail] = useState<{ title: string; rows: Record<string, unknown>[] } | null>(null);
  const [startDate, setStartDate] = useState("2026-07-28");
  const [endDate, setEndDate] = useState("2026-07-28");
  const [selected, setSelected] = useState<string[]>([...datasets]);
  const [baseMode, setBaseMode] = useState<"root" | "mapping">("root");
  const [baseVersions, setBaseVersions] = useState<Record<string, string>>({});
  const [eodResult, setEodResult] = useState<JobResponse | null>(null);
  const [submittedEod, setSubmittedEod] = useState<EodRequest | null>(null);
  const [eodMessage, setEodMessage] = useState("");
  const keyCache = useRef<{ fingerprint: string; key: string } | null>(null);

  useEffect(() => {
    let active = true;
    void api.getHealth().then(
      () => active && setHealth("healthy"),
      () => active && setHealth("unhealthy"),
    );
    return () => { active = false; };
  }, [api]);

  const load = async (target: Tab, cursor?: string) => {
    const item = tabs.find((candidate) => candidate.id === target);
    if (!item?.path || !api.getData) {
      setData([]);
      setDataState("ready");
      return;
    }
    setDataState("loading");
    setDataError("");
    try {
      const params = new URLSearchParams();
      for (const [name, value] of Object.entries(filters[target])) {
        if (value) {
          const normalized = name.startsWith("started_")
            ? new Date(`${value}:00+08:00`).toISOString()
            : value;
          params.set(name, normalized);
        }
      }
      if (cursor) params.set("cursor", cursor);
      const result = await api.getData(`${item.path}${params.size ? `?${params}` : ""}`);
      setData(result);
      const page = result as Partial<CursorPage>;
      setNextCursor(page.next_cursor ?? null);
      setDataState("ready");
    } catch (error) {
      setDataError(safeMessage(error));
      setDataState("error");
    }
  };

  useEffect(() => {
    setPageHistory([]);
    setNextCursor(null);
    setDetail(null);
    setPreview(null);
    setVersionDetail(null);
    setResolutionDetail(null);
    setDifferenceDetail(null);
    setDifferenceReport(null);
    void load(tab);
  }, [tab, JSON.stringify(filters[tab])]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let active = true;
    let source: EventSourceLike | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let retry = 0;
    const ingest = (events: JobEvent[]) => {
      cursorRef.current = events.reduce(
        (latest, event) => Math.max(latest, event.event_id),
        cursorRef.current,
      );
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
        if (active) ingest(page.events);
      } catch {
        if (active) dispatch({ type: "connection", connection: "error" });
      }
      scheduleReconnect();
    };
    function connect() {
      if (!active) return;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      const current = eventSourceFactory(`/api/job-events/stream?after_event_id=${cursorRef.current}`);
      source = current;
      let recovering = false;
      current.onopen = () => {
        if (!active || current !== source) return;
        retry = 0;
        dispatch({ type: "connection", connection: "connected" });
      };
      current.addEventListener("job-event", (message) => {
        if (!active || current !== source) return;
        try { ingest([JSON.parse(message.data) as JobEvent]); }
        catch { dispatch({ type: "connection", connection: "error" }); }
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
      if (reconnectTimer) clearTimeout(reconnectTimer);
    };
  }, [api, eventSourceFactory]);

  const rows = useMemo<Array<Record<string, unknown>>>(() => {
    if (Array.isArray(data)) return data as Array<Record<string, unknown>>;
    if (data && typeof data === "object" && Array.isArray((data as CursorPage).items)) {
      return (data as CursorPage).items;
    }
    if (tab === "overview" && data && typeof data === "object") {
      return ((data as { datasets?: Array<Record<string, unknown>> }).datasets ?? []).map((row) => ({
        dataset: row.dataset,
        ...((row.version as Record<string, unknown> | null) ?? { status: "EMPTY" }),
      }));
    }
    return [];
  }, [data, tab]);

  const queryJob = async (event: FormEvent) => {
    event.preventDefault();
    setJob(null);
    setJobError(null);
    try { setJob(await api.getJob(jobId.trim())); }
    catch (error) { setJobError(safeMessage(error)); }
  };

  const loadPreviewPage = async (
    versionId: string,
    cursor?: string,
    history: string[] = [],
  ) => {
    if (!api.getData) return;
    const params = new URLSearchParams({ limit: "50" });
    if (cursor) params.set("cursor", cursor);
    const result = await api.getData(
      `/api/data/versions/${encodeURIComponent(versionId)}/preview?${params}`,
    ) as CursorPage;
    setPreview({
      versionId,
      rows: result.items,
      nextCursor: result.next_cursor,
      history,
    });
  };

  const showPreview = async (row: Record<string, unknown>) => {
    const versionId = String(row.version_id ?? "");
    if (!versionId || row.status !== "PUBLISHED" || !api.getData) {
      setDataError("只有明确的 PUBLISHED version_id 可以预览。");
      return;
    }
    setDataState("loading");
    try {
      const detailResult = await api.getData(
        `/api/data/versions/${encodeURIComponent(versionId)}`,
      ) as Record<string, unknown>;
      setVersionDetail(detailResult);
      await loadPreviewPage(versionId);
      setDataState("ready");
    } catch (error) {
      setDataError(safeMessage(error));
      setDataState("error");
    }
  };

  const loadDifferencePage = async (
    reportId: string,
    cursor?: string,
    history: string[] = [],
  ) => {
    if (!api.getData) return;
    const params = new URLSearchParams({ limit: "50" });
    if (cursor) params.set("cursor", cursor);
    const result = await api.getData(
      `/api/data/difference-reports/${encodeURIComponent(reportId)}/rows?${params}`,
    ) as CursorPage;
    setDifferenceDetail({
      reportId,
      rows: result.items,
      nextCursor: result.next_cursor,
      history,
    });
  };

  const inspectRow = async (row: Record<string, unknown>) => {
    if (!api.getData) return;
    setDataState("loading");
    try {
      if (tab === "quality") {
        const runId = String(row.run_id);
        const result = await api.getData(
          `/api/data/quality-runs/${encodeURIComponent(runId)}`,
        ) as { results?: Record<string, unknown>[] };
        setDetail({ title: `质量规则 · ${runId}`, rows: result.results ?? [] });
      } else if (tab === "differences") {
        const reportId = String(row.report_id);
        setDifferenceReport(await api.getData(
          `/api/data/difference-reports/${encodeURIComponent(reportId)}`,
        ) as Record<string, unknown>);
        await loadDifferencePage(reportId);
      } else if (tab === "resolutions") {
        const runId = String(row.run_id);
        const encoded = encodeURIComponent(runId);
        const [result, exceptions, conflicts] = await Promise.all([
          api.getData(`/api/data/resolution-runs/${encoded}`),
          api.getData(`/api/data/resolution-runs/${encoded}/exceptions?limit=50`),
          api.getData(`/api/data/resolution-runs/${encoded}/conflicts?limit=50`),
        ]) as [Record<string, unknown>, CursorPage, CursorPage];
        setResolutionDetail({
          value: result,
          exceptions: exceptions.items,
          conflicts: conflicts.items,
          exceptionsNext: exceptions.next_cursor,
          conflictsNext: conflicts.next_cursor,
        });
      }
      setDataState("ready");
    } catch (error) {
      setDataError(safeMessage(error));
      setDataState("error");
    }
  };

  const loadMoreResolution = async (kind: "exceptions" | "conflicts") => {
    if (!api.getData || !resolutionDetail) return;
    const cursor = kind === "exceptions"
      ? resolutionDetail.exceptionsNext
      : resolutionDetail.conflictsNext;
    if (!cursor) return;
    try {
      const runId = String(resolutionDetail.value.run_id);
      const result = await api.getData(
        `/api/data/resolution-runs/${encodeURIComponent(runId)}/${kind}?limit=50&cursor=${encodeURIComponent(cursor)}`,
      ) as CursorPage;
      setResolutionDetail((current) => current && ({
        ...current,
        [kind]: [...current[kind], ...result.items],
        [`${kind}Next`]: result.next_cursor,
      }));
    } catch (error) {
      setDataError(safeMessage(error));
    }
  };

  const submitEod = async (event: FormEvent) => {
    event.preventDefault();
    setEodMessage("");
    if (!api.enqueueEod || selected.length === 0) return setEodMessage("至少选择一个数据集。");
    const normalizedDatasets = datasets.filter((dataset) => selected.includes(dataset));
    const mapping = baseMode === "mapping"
      ? Object.fromEntries(normalizedDatasets.map((dataset) => [dataset, baseVersions[dataset]?.trim() ?? ""]))
      : {};
    if (baseMode === "mapping" && Object.values(mapping).some((value) => !value)) {
      return setEodMessage("完整映射模式要求每个所选数据集都有精确 version_id。");
    }
    const request: EodRequest = {
      start_date: startDate,
      end_date: endDate,
      datasets: normalizedDatasets,
      publish_intent: "PUBLISH_AFTER_QUALITY",
      base_versions: mapping,
    };
    const fingerprint = JSON.stringify(request);
    if (keyCache.current?.fingerprint !== fingerprint) {
      keyCache.current = { fingerprint, key: idempotencyKey() };
    }
    try {
      const result = await api.enqueueEod(request, keyCache.current.key);
      setSubmittedEod(request);
      const current = await api.getJob(result.job_id);
      setEodResult(current);
      setEodMessage(result.created ? "任务已入队；Worker 未在页面中启动。" : "已复用相同请求。");
    } catch (error) {
      setEodMessage(safeMessage(error));
    }
  };

  const cancelEod = async () => {
    if (!eodResult || !api.cancelJob) return;
    try {
      setEodResult(await api.cancelJob(eodResult.job_id, eodResult.row_version, "用户从数据中心取消"));
      setEodMessage("取消请求已提交。");
    } catch (error) {
      const stale = (error as { status?: number }).status === 409;
      setEodMessage(stale ? "任务已变化（stale 409），请刷新后重试。" : safeMessage(error));
    }
  };

  return (
    <div className="app-shell">
      <aside>
        <p className="eyebrow">本机 · Stage 1</p>
        <h1>QuantKing 数据中心</h1>
        <p className={`health health-${health}`}>
          {health === "checking" ? "健康检查中" : health === "healthy" ? "服务正常" : "服务不可用"}
        </p>
        <nav aria-label="数据中心">
          {tabs.map((item) => (
            <button
              key={item.id}
              className={tab === item.id ? "nav-active" : ""}
              onClick={() => setTab(item.id)}
            >{item.label}</button>
          ))}
        </nav>
      </aside>
      <main>
        <header className="topbar">
          <div><p className="eyebrow">DATA CENTER / {tab.toUpperCase()}</p><h2>{tabs.find((item) => item.id === tab)?.label}</h2></div>
          <p className={`stream stream-${stream.connection}`}>
            SSE：{connectionLabels[stream.connection]} · 游标：{stream.cursor}
          </p>
        </header>

        {dataState === "loading" && <section><p role="status">正在加载数据…</p></section>}
        {dataState === "error" && <section className="error-panel"><p role="alert">{dataError}</p><button onClick={() => void load(tab)}>重试</button></section>}
        {dataState === "ready" && tab !== "eod" && (
          <section>
            <FilterBar
              tab={tab}
              values={filters[tab]}
              onChange={(name, value) => setFilters((current) => ({
                ...current,
                [tab]: { ...current[tab], [name]: value },
              }))}
            />
            <DataTable
              rows={rows}
              onPreview={
                tab === "versions"
                  ? showPreview
                  : ["quality", "differences", "resolutions"].includes(tab)
                    ? inspectRow
                    : undefined
              }
              actionLabel={actionLabels[tab]}
            />
            <div className="pager">
              <button className="secondary" disabled={pageHistory.length === 0} onClick={() => {
                const history = pageHistory.slice(0, -1);
                setPageHistory(history);
                void load(tab, history.at(-1));
              }}>上一页</button>
              <button disabled={!nextCursor} onClick={() => {
                if (!nextCursor) return;
                setPageHistory((value) => [...value, nextCursor]);
                void load(tab, nextCursor);
              }}>下一页</button>
            </div>
          </section>
        )}

        {preview && tab === "versions" && (
          <section>
            <div className="section-title"><h3>版本预览</h3><code>{preview.versionId}</code></div>
            {versionDetail && <VersionLineage value={versionDetail} />}
            <DataTable rows={preview.rows} />
            <div className="pager">
              <button className="secondary" disabled={preview.history.length === 0} onClick={() => {
                const history = preview.history.slice(0, -1);
                void loadPreviewPage(preview.versionId, history.at(-1), history);
              }}>预览上一页</button>
              <button disabled={!preview.nextCursor} onClick={() => {
                if (!preview.nextCursor) return;
                void loadPreviewPage(
                  preview.versionId,
                  preview.nextCursor,
                  [...preview.history, preview.nextCursor],
                );
              }}>预览下一页</button>
            </div>
          </section>
        )}

        {differenceDetail && tab === "differences" && (
          <section>
            <div className="section-title"><h3>行级差异</h3><code>{differenceDetail.reportId}</code></div>
            {differenceReport && (
              <p className="notice">
                threshold_rule_version：<code>{displayValue("threshold_rule_version", differenceReport.threshold_rule_version)}</code>
                {" · "}total_differences：{displayValue("total_differences", differenceReport.total_differences)}
              </p>
            )}
            <DataTable rows={differenceDetail.rows} />
            <div className="pager">
              <button className="secondary" disabled={differenceDetail.history.length === 0} onClick={() => {
                const history = differenceDetail.history.slice(0, -1);
                void loadDifferencePage(differenceDetail.reportId, history.at(-1), history);
              }}>差异上一页</button>
              <button disabled={!differenceDetail.nextCursor} onClick={() => {
                if (!differenceDetail.nextCursor) return;
                void loadDifferencePage(
                  differenceDetail.reportId,
                  differenceDetail.nextCursor,
                  [...differenceDetail.history, differenceDetail.nextCursor],
                );
              }}>差异下一页</button>
            </div>
          </section>
        )}

        {resolutionDetail && tab === "resolutions" && (
          <section><ResolutionLineage detail={resolutionDetail} onMore={(kind) => void loadMoreResolution(kind)} /></section>
        )}

        {detail && (
          <section>
            <div className="section-title"><h3>{detail.title}</h3></div>
            <DataTable rows={detail.rows} />
          </section>
        )}

        {tab === "eod" && dataState !== "loading" && (
          <>
            <section>
              <FilterBar
                tab={tab}
                values={filters[tab]}
                onChange={(name, value) => setFilters((current) => ({
                  ...current,
                  [tab]: { ...current[tab], [name]: value },
                }))}
              />
              <h3>手动入队 EOD</h3>
              <form onSubmit={submitEod} className="eod-form">
                <div className="form-grid">
                  <label>起始日期<input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} /></label>
                  <label>结束日期<input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} /></label>
                </div>
                <fieldset><legend>数据集（非空、无重复）</legend>
                  <div className="checks">{datasets.map((dataset) => <label key={dataset}><input type="checkbox" checked={selected.includes(dataset)} onChange={() => setSelected((value) => value.includes(dataset) ? value.filter((item) => item !== dataset) : [...value, dataset])} />{dataset}</label>)}</div>
                </fieldset>
                <fieldset><legend>Base version 模式</legend>
                  <label><input type="radio" checked={baseMode === "root"} onChange={() => setBaseMode("root")} />明确根版本（提交 base_versions={"{ }"}）</label>
                  <label><input type="radio" checked={baseMode === "mapping"} onChange={() => setBaseMode("mapping")} />完整精确 version mapping</label>
                  {baseMode === "mapping" && selected.map((dataset) => <label key={dataset}>{dataset}<input aria-label={`${dataset} version`} placeholder="data_version:…" value={baseVersions[dataset] ?? ""} onChange={(event) => setBaseVersions((value) => ({ ...value, [dataset]: event.target.value }))} /></label>)}
                </fieldset>
                <p className="notice">发布意图固定为 <code>PUBLISH_AFTER_QUALITY</code>；POST 只入队，不启动 Worker。</p>
                <button type="submit">入队 EOD</button>
              </form>
              {eodMessage && <p role="status">{eodMessage}</p>}
              {submittedEod && (
                <div className="submitted-command">
                  <strong>实际提交的显式版本</strong>
                  <pre>{JSON.stringify(submittedEod.base_versions, null, 2)}</pre>
                </div>
              )}
              {eodResult && <div className="job-card"><code>{eodResult.job_id}</code><strong>{eodResult.status}</strong><span>row_version {eodResult.row_version}</span><button className="danger" onClick={cancelEod}>按当前 row_version 取消</button></div>}
            </section>
            <section><h3>EOD 任务</h3><DataTable rows={rows} /></section>
          </>
        )}

        <section className="operations">
          <div><h3>任务查询</h3><form onSubmit={queryJob} className="query-row"><label className="sr-only" htmlFor="job-id">任务 ID</label><input id="job-id" value={jobId} onChange={(event) => setJobId(event.target.value)} placeholder="job:…" /><button disabled={!jobId.trim()}>查询任务</button></form>{jobError && <p role="alert">{jobError}</p>}{job && <div className="job-card"><code>{job.job_id}</code><strong>{job.status}</strong><span>{Math.round(job.progress * 100)}%</span></div>}</div>
          <div><h3>持久事件</h3>{stream.events.length === 0 ? <p className="empty">暂无事件</p> : <ul>{stream.events.slice(-8).map((item) => <li key={item.event_id}><span>#{item.event_id} · {item.event_type}</span><strong>{item.summary}</strong></li>)}</ul>}</div>
        </section>
      </main>
    </div>
  );
}
