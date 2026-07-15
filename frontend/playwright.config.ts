import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:8766",
    channel: "msedge",
  },
  webServer: {
    command:
      "uv run --offline --frozen python ../scripts/run_web_e2e_server.py --port 8766 --static-root dist",
    url: "http://127.0.0.1:8766/api/health",
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
