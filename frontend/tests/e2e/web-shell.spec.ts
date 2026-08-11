import { expect, test } from "@playwright/test";

const FIXED_JOB_ID = "job:00000000-0000-4000-8000-000000000004";
const FIXED_RESOLUTION_ID = "data_resolution:00000000-0000-4000-8000-000000000026";

test.afterEach(async ({ request }) => {
  try {
    await request.post("/__playwright__/shutdown", { timeout: 2_000 });
  } catch {
    // Best effort: preserve the original assertion failure if the fixture is already gone.
  }
});

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
  await page.route("**/api/data/resolution-runs**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/exceptions")) {
      await route.fulfill({ json: { items: [{ symbol: "600000.SH", reason_code: "AC_AGREE_A_SELECTED" }], next_cursor: null } });
    } else if (path.endsWith("/conflicts")) {
      await route.fulfill({ json: { items: [{ symbol: "000001.SZ", reason_code: "NO_TWO_SOURCE_CONSENSUS" }], next_cursor: null } });
    } else if (path.endsWith(FIXED_RESOLUTION_ID)) {
      await route.fulfill({ json: { run_id: FIXED_RESOLUTION_ID, status: "BLOCKED", default_decision: null, difference_report_ids: [], run_blockers: [] } });
    } else {
      await route.fulfill({ json: { items: [{ run_id: FIXED_RESOLUTION_ID, status: "BLOCKED", conflict_count: 1 }], next_cursor: null } });
    }
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "QuantKing 数据中心" })).toBeVisible();
  await expect(page.getByText("服务正常")).toBeVisible();

  await page.getByRole("button", { name: "版本" }).click();
  await expect(page.getByRole("cell", { name: "PUBLISHED" }).first()).toBeVisible();
  await expect(page.getByText("2026-07-15").first()).toBeVisible();
  await page.getByRole("button", { name: "显式预览" }).first().click();
  await expect(page.getByText("版本预览")).toBeVisible();
  await expect(page.getByText("2026-07-28").first()).toBeVisible();

  await page.getByRole("button", { name: "差异" }).click();
  await expect(page.getByRole("cell", { name: /^data_diff:/ })).toBeVisible();
  await page.getByRole("button", { name: "行级差异" }).first().click();
  await expect(
    page.getByText("comparison-thresholds.daily-bar-unadjusted.k1.v1"),
  ).toBeVisible();
  await expect(page.getByText(/000001.SZ/).first()).toBeVisible();
  await page.getByRole("button", { name: "仲裁" }).click();
  await expect(page.getByRole("cell", { name: /^data_resolution:/ })).toBeVisible();
  await page.getByRole("button", { name: "仲裁详情" }).click();
  await expect(page.getByLabel("仲裁完整血缘")).toBeVisible();
  await expect(page.getByText("AC_AGREE_A_SELECTED")).toBeVisible();
  await expect(page.getByText("NO_TWO_SOURCE_CONSENSUS")).toBeVisible();

  await page.getByRole("button", { name: "EOD" }).click();
  await page.getByLabel("完整精确 version mapping").check();
  for (const dataset of ["daily_bar_unadjusted"]) {
    await page.getByLabel(`${dataset} version`).fill(
      "data_version:00000000-0000-4000-8000-000000000006",
    );
  }
  await page.getByRole("button", { name: "入队 EOD" }).click();
  await expect(page.getByText("任务已入队；Worker 未在页面中启动。")).toBeVisible();
  await expect(page.locator(".job-card strong").filter({ hasText: "PENDING" })).toBeVisible();
  await page.getByRole("button", { name: /row_version 取消/ }).click();
  await expect(page.getByText("取消请求已提交。")).toBeVisible();
  await expect(page.locator(".job-card strong").filter({ hasText: "CANCELLED" })).toBeVisible();

  await page.getByLabel("任务 ID").fill(FIXED_JOB_ID);
  await page.getByRole("button", { name: "查询任务" }).click();
  await expect(page.getByText(FIXED_JOB_ID)).toBeVisible();
  await expect(page.locator(".job-card strong").filter({ hasText: "RUNNING" })).toBeVisible();

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
