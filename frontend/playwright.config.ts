import path from "node:path";
import { defineConfig } from "@playwright/test";

const isWin = process.platform === "win32";
const python =
  process.env.KATOMARAN_PYTHON ??
  path.resolve(__dirname, "..", ".venv", isWin ? "Scripts/python.exe" : "bin/python");

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  outputDir: "test-results",
  use: {
    baseURL: "http://127.0.0.1:5173",
    // Use an installed browser by channel (e.g. PW_CHANNEL=msedge on Windows) to avoid a download;
    // CI installs Playwright's own Chromium and leaves this unset.
    channel: process.env.PW_CHANNEL || undefined,
    trace: "retain-on-failure",
  },
  webServer: [
    {
      // Real backend (used by the @live test; mocked tests never reach it).
      command: `"${python}" -m uvicorn app.main:app_factory --factory --host 127.0.0.1 --port 8000`,
      cwd: path.resolve(__dirname, "..", "backend"),
      url: "http://127.0.0.1:8000/api/v1/health",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
    {
      command: "npx vite --host 127.0.0.1",
      url: "http://127.0.0.1:5173",
      // Playwright gives the child a closed stdin; Vite treats that as "terminal closed" and exits
      // unless it believes it runs in CI.
      env: { CI: "true" },
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
  ],
});
