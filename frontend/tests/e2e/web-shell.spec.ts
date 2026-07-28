import { expect, test } from "@playwright/test";

const FIXED_JOB_ID = "job:00000000-0000-4000-8000-000000000004";

test("数据中心显式版本、血缘、EOD 入队取消与 SSE 重连", async ({ page }) => {
  const streamCursors: number[] = [];
  const pollCursors: number[] = [];

  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/job-events") {
      pollCursors.push(Number(url.searchParams.get("after_event_id")));
    }
  });

  await page.route("**/api/job-events/stream?after_event_id=*", async (route) => {
    const url = new URL(route.request().url());
    streamCursors.push(Number(url.searchParams.get("after_event_id")));
    if (streamCursors.length === 1) {
      await route.abort("failed");
      return;
    }
    await route.continue();
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "QuantKing 数据中心" })).toBeVisible();
  await expect(page.getByText("服务正常")).toBeVisible();

  await page.getByRole("button", { name: "数据源" }).click();
  await expect(page.getByText("PRODUCTION_READY")).toBeVisible();
  await expect(page.getByText("BLOCKED_EXTERNAL_VALIDATION").first()).toBeVisible();

  await page.getByRole("button", { name: "版本" }).click();
  await expect(page.getByText("PUBLISHED")).toBeVisible();
  await expect(page.getByText("2026-07-15")).toBeVisible();
  await page.getByRole("button", { name: "显式预览" }).click();
  await expect(page.getByText("版本预览")).toBeVisible();
  await expect(page.getByText("2026-07-28").first()).toBeVisible();

  await page.getByRole("button", { name: "差异" }).click();
  await expect(page.getByText("comparison-thresholds.v1")).toBeVisible();
  await page.getByRole("button", { name: "行级差异" }).click();
  await expect(page.getByText(/000001.SZ/)).toBeVisible();
  await page.getByRole("button", { name: "Override" }).click();
  await expect(page.getByText("APPLIED", { exact: true })).toBeVisible();
  await expect(page.getByText(/cccccccc/)).toBeVisible();
  await page.getByRole("button", { name: "审计血缘" }).click();
  await expect(page.getByText(/field_replacements/)).toBeVisible();

  await page.getByRole("button", { name: "EOD" }).click();
  await page.getByLabel("完整精确 version mapping").check();
  for (const dataset of [
    "security_master",
    "exchange_calendar",
    "daily_bar_unadjusted",
    "adjustment_factor",
    "trading_status",
    "benchmark_index_daily",
  ]) {
    await page.getByLabel(`${dataset} version`).fill(
      "data_version:00000000-0000-4000-8000-000000000006",
    );
  }
  await page.getByRole("button", { name: "入队 EOD" }).click();
  await expect(page.getByText("任务已入队；Worker 未在页面中启动。")).toBeVisible();
  await expect(page.getByText("PENDING", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /row_version 取消/ }).click();
  await expect(page.getByText("取消请求已提交。")).toBeVisible();
  await expect(page.getByText("CANCELLED", { exact: true })).toBeVisible();

  await page.getByLabel("任务 ID").fill(FIXED_JOB_ID);
  await page.getByRole("button", { name: "查询任务" }).click();
  await expect(page.getByText(FIXED_JOB_ID)).toBeVisible();
  await expect(page.getByText("RUNNING", { exact: true })).toBeVisible();

  await expect.poll(() => pollCursors).toContain(0);
  await expect.poll(async () => page.getByRole("listitem").count()).toBeGreaterThanOrEqual(3);
  await expect.poll(() => streamCursors.length).toBeGreaterThanOrEqual(2);
  expect(streamCursors.slice(0, 2)).toEqual([0, 3]);

  const visibleEventIds = await page.getByRole("listitem").evaluateAll((items) =>
    items.map((item) => Number(item.textContent?.match(/^#(\d+)/)?.[1])),
  );
  expect(visibleEventIds).toEqual([...visibleEventIds].sort((a, b) => a - b));
  expect(new Set(visibleEventIds).size).toBe(visibleEventIds.length);
});
