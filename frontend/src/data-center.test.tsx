import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type {
  EodRequest,
  EventPage,
  HealthResponse,
  JobResponse,
  WebApi,
} from "./types";

const health: HealthResponse = {
  status: "ok",
  service: "quantking-web",
  checked_at: "2026-07-28T00:00:00Z",
};

const job: JobResponse = {
  job_id: "job:00000000-0000-4000-8000-000000000006",
  job_type: "data.eod.v1",
  status: "PENDING",
  created_at: "2026-07-28T00:00:00Z",
  started_at: null,
  ended_at: null,
  heartbeat_at: null,
  stage: null,
  progress: 0,
  worker_id: null,
  failure: null,
  row_version: 1,
};

class SilentEventSource {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  addEventListener(): void {}
  close(): void {}
}

const api = (overrides: Partial<WebApi> = {}): WebApi => ({
  getHealth: vi.fn(async () => health),
  getJob: vi.fn(async () => job),
  getEvents: vi.fn(async (cursor: number): Promise<EventPage> => ({
    events: [],
    next_cursor: cursor,
  })),
  getData: vi.fn(async () => ({ items: [], next_cursor: null })),
  ...overrides,
});

describe("Stage 1 data center", () => {
  it("renders source blocking text without claiming synthetic production readiness", async () => {
    const getData = vi.fn(async (path: string) => path.endsWith("/sources")
      ? [{
          source: "BAOSTOCK",
          dataset: "daily_bar_unadjusted",
          role: "FALLBACK",
          status: "BLOCKED_EXTERNAL_VALIDATION",
          evidence_kind: null,
          blocking_reason: "requires authorized external validation",
          auto_switch: false,
        }]
      : { datasets: [], failures: 0, blocked_sources: 23 });
    render(<App api={api({ getData })} eventSourceFactory={() => new SilentEventSource()} />);
    fireEvent.click(screen.getByRole("button", { name: "数据源" }));
    expect(await screen.findByText("BLOCKED_EXTERNAL_VALIDATION")).toBeInTheDocument();
    expect(screen.getByText("requires authorized external validation")).toBeInTheDocument();
    expect(screen.queryByText("SYNTHETIC PRODUCTION_READY")).not.toBeInTheDocument();
  });

  it("shows loading, empty and error states", async () => {
    let reject = false;
    const getData = vi.fn(async () => {
      if (reject) throw new Error("稳定错误");
      return { items: [], next_cursor: null };
    });
    render(<App api={api({ getData })} eventSourceFactory={() => new SilentEventSource()} />);
    expect(await screen.findByText("暂无记录")).toBeInTheDocument();
    reject = true;
    fireEvent.click(screen.getByRole("button", { name: "批次" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("稳定错误");
  });

  it("previews only the explicit PUBLISHED version id", async () => {
    const versionId = "data_version:00000000-0000-4000-8000-000000000006";
    const getData = vi.fn(async (path: string) => {
      if (path.includes("/preview")) {
        return { items: [{ exchange: "SZ", trade_date: "2026-07-28" }], next_cursor: null };
      }
      if (path.endsWith("/versions")) {
        return { items: [{ version_id: versionId, dataset: "exchange_calendar", status: "PUBLISHED" }], next_cursor: null };
      }
      return { datasets: [] };
    });
    render(<App api={api({ getData })} eventSourceFactory={() => new SilentEventSource()} />);
    fireEvent.click(screen.getByRole("button", { name: "版本" }));
    fireEvent.click(await screen.findByRole("button", { name: "显式预览" }));
    expect(await screen.findAllByText(versionId)).toHaveLength(2);
    expect(getData).toHaveBeenCalledWith(
      `/api/data/versions/${encodeURIComponent(versionId)}/preview?limit=50`,
    );
  });

  it("requires complete base mapping and reuses the key until content changes", async () => {
    const calls: Array<{ request: EodRequest; key: string }> = [];
    const enqueueEod = vi.fn(async (request: EodRequest, key: string) => {
      calls.push({ request, key });
      return { job_id: job.job_id, status: "PENDING", row_version: 1, created: true };
    });
    render(
      <App
        api={api({ enqueueEod })}
        eventSourceFactory={() => new SilentEventSource()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "EOD" }));
    fireEvent.click(await screen.findByLabelText("完整精确 version mapping"));
    fireEvent.click(screen.getByRole("button", { name: "入队 EOD" }));
    expect(await screen.findByRole("status")).toHaveTextContent("完整映射模式要求");
    for (const dataset of [
      "security_master",
      "exchange_calendar",
      "daily_bar_unadjusted",
      "adjustment_factor",
      "trading_status",
      "benchmark_index_daily",
    ]) {
      fireEvent.change(screen.getByLabelText(`${dataset} version`), {
        target: { value: `data_version:${dataset}` },
      });
    }
    fireEvent.click(screen.getByRole("button", { name: "入队 EOD" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "入队 EOD" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].key).toBe(calls[0].key);
    fireEvent.change(screen.getByLabelText("结束日期"), { target: { value: "2026-07-29" } });
    fireEvent.click(screen.getByRole("button", { name: "入队 EOD" }));
    await waitFor(() => expect(calls).toHaveLength(3));
    expect(calls[2].key).not.toBe(calls[1].key);
    expect(Object.keys(calls[0].request.base_versions)).toHaveLength(6);
  });

  it("reports stale cancel conflicts and sends the current row_version", async () => {
    const stale = Object.assign(new Error("conflict"), { status: 409 });
    const cancelJob = vi.fn(async () => { throw stale; });
    render(
      <App
        api={api({
          enqueueEod: vi.fn(async () => ({
            job_id: job.job_id,
            status: "PENDING",
            row_version: 1,
            created: true,
          })),
          cancelJob,
        })}
        eventSourceFactory={() => new SilentEventSource()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "EOD" }));
    fireEvent.click(await screen.findByRole("button", { name: "入队 EOD" }));
    fireEvent.click(await screen.findByRole("button", { name: /row_version 取消/ }));
    expect(await screen.findByText(/stale 409/)).toBeInTheDocument();
    expect(cancelJob).toHaveBeenCalledWith(job.job_id, 1, expect.any(String));
  });

  it("applies allowlisted filters and clears cursor history when filters change", async () => {
    const getData = vi.fn(async (path: string) => path.includes("/batches")
      ? { items: [{ batch_id: "data_batch:1", dataset: "exchange_calendar" }], next_cursor: "cursor-2" }
      : { datasets: [] });
    render(<App api={api({ getData })} eventSourceFactory={() => new SilentEventSource()} />);
    fireEvent.click(screen.getByRole("button", { name: "批次" }));
    fireEvent.click(await screen.findByRole("button", { name: "下一页" }));
    await waitFor(() => expect(getData).toHaveBeenCalledWith(expect.stringContaining("cursor=cursor-2")));
    fireEvent.change(screen.getByLabelText("batches dataset"), {
      target: { value: "exchange_calendar" },
    });
    await waitFor(() => expect(getData).toHaveBeenCalledWith(
      "/api/data/batches?dataset=exchange_calendar",
    ));
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
  });

  it("loads Version lineage and pages preview independently", async () => {
    const versionId = "data_version:00000000-0000-4000-8000-000000000016";
    const getData = vi.fn(async (path: string) => {
      if (path.includes("/preview?limit=50&cursor=")) {
        return { items: [{ trade_date: "2026-07-29" }], next_cursor: null };
      }
      if (path.includes("/preview")) {
        return { items: [{ trade_date: "2026-07-28" }], next_cursor: "preview-next" };
      }
      if (path === `/api/data/versions/${encodeURIComponent(versionId)}`) {
        return {
          version_id: versionId,
          parent_version_id: "data_version:parent",
          input_batch_ids: ["data_batch:primary"],
          quality_run_id: "data_quality:passed",
          schema_version: "daily-bar.v1",
          rule_version: "quality.v1",
        };
      }
      if (path.endsWith("/versions")) {
        return { items: [{ version_id: versionId, status: "PUBLISHED" }], next_cursor: null };
      }
      return { datasets: [] };
    });
    render(<App api={api({ getData })} eventSourceFactory={() => new SilentEventSource()} />);
    fireEvent.click(screen.getByRole("button", { name: "版本" }));
    fireEvent.click(await screen.findByRole("button", { name: "显式预览" }));
    expect(await screen.findByText("data_version:parent")).toBeInTheDocument();
    expect(screen.getByText(/data_batch:primary/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "预览下一页" }));
    expect(await screen.findByText("2026-07-29")).toBeInTheDocument();
    expect(getData).toHaveBeenCalledWith(expect.stringContaining("cursor=preview-next"));
  });

  it("pages DifferenceReport rows independently and renders full Override lineage", async () => {
    const reportId = "data_diff:00000000-0000-4000-8000-000000000026";
    const overrideId = "data_override:00000000-0000-4000-8000-000000000026";
    const getData = vi.fn(async (path: string) => {
      if (path.includes("/difference-reports/") && path.includes("/rows?limit=50&cursor=")) {
        return { items: [{ field: "volume" }], next_cursor: null };
      }
      if (path.includes("/difference-reports/") && path.includes("/rows")) {
        return { items: [{ field: "close" }], next_cursor: "difference-next" };
      }
      if (path === `/api/data/difference-reports/${encodeURIComponent(reportId)}`) {
        return { report_id: reportId, threshold_rule_version: "threshold.v1" };
      }
      if (path.endsWith("/difference-reports")) {
        return { items: [{ report_id: reportId }], next_cursor: null };
      }
      if (path === `/api/data/source-overrides/${encodeURIComponent(overrideId)}`) {
        return {
          override_id: overrideId,
          exact_scope_sha256: "a".repeat(64),
          field_replacements: [{ code: "000001.SZ", field: "close" }],
          fallback_only_keys: [],
          application: { status: "APPLIED" },
          attempt: { status: "APPLIED" },
          child: { version_id: "data_version:child" },
          audits: [{ action: "override_applied" }],
        };
      }
      if (path.endsWith("/source-overrides")) {
        return { items: [{ override_id: overrideId, status: "APPLIED" }], next_cursor: null };
      }
      return { datasets: [] };
    });
    render(<App api={api({ getData })} eventSourceFactory={() => new SilentEventSource()} />);
    fireEvent.click(screen.getByRole("button", { name: "差异" }));
    fireEvent.click(await screen.findByRole("button", { name: "行级差异" }));
    fireEvent.click(await screen.findByRole("button", { name: "差异下一页" }));
    expect(await screen.findByText("volume")).toBeInTheDocument();
    expect(getData).toHaveBeenCalledWith(expect.stringContaining("cursor=difference-next"));

    fireEvent.click(screen.getByRole("button", { name: "Override" }));
    fireEvent.click(await screen.findByRole("button", { name: "审计血缘" }));
    expect(await screen.findByLabelText("Override 完整血缘")).toBeInTheDocument();
    expect(screen.getByText(/data_version:child/)).toBeInTheDocument();
    expect(screen.getByText(/override_applied/)).toBeInTheDocument();
  });

  it("keeps EOD idempotency key stable after deselecting and reselecting a dataset", async () => {
    const calls: Array<{ request: EodRequest; key: string }> = [];
    const enqueueEod = vi.fn(async (request: EodRequest, key: string) => {
      calls.push({ request, key });
      return { job_id: job.job_id, status: "PENDING", row_version: 1, created: true };
    });
    render(<App api={api({ enqueueEod })} eventSourceFactory={() => new SilentEventSource()} />);
    fireEvent.click(screen.getByRole("button", { name: "EOD" }));
    fireEvent.click(await screen.findByRole("button", { name: "入队 EOD" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    const checkbox = screen.getByLabelText("exchange_calendar");
    fireEvent.click(checkbox);
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole("button", { name: "入队 EOD" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].request).toEqual(calls[0].request);
    expect(calls[1].key).toBe(calls[0].key);
  });
});
