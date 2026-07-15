import { expect, test } from "@playwright/test";

const FIXED_JOB_ID = "job:00000000-0000-4000-8000-000000000004";

test("中文壳通过 REST 补洞并从持久游标重连 SSE", async ({ page }) => {
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
  await expect(page.getByRole("heading", { name: "QuantKing V3 状态台" })).toBeVisible();
  await expect(page.getByText("服务正常")).toBeVisible();

  await page.getByLabel("任务 ID").fill(FIXED_JOB_ID);
  await page.getByRole("button", { name: "查询任务" }).click();
  await expect(page.getByText(FIXED_JOB_ID)).toBeVisible();
  await expect(page.getByText("RUNNING", { exact: true })).toBeVisible();

  await expect.poll(() => pollCursors).toContain(0);
  await expect(page.getByRole("listitem")).toHaveCount(3);
  await expect.poll(() => streamCursors.length).toBeGreaterThanOrEqual(2);
  expect(streamCursors.slice(0, 2)).toEqual([0, 3]);

  const visibleEventIds = await page.getByRole("listitem").evaluateAll((items) =>
    items.map((item) => Number(item.textContent?.match(/^#(\d+)/)?.[1])),
  );
  expect(visibleEventIds).toEqual([1, 2, 3]);
  expect(new Set(visibleEventIds).size).toBe(visibleEventIds.length);
});
