import { defineConfig } from "@playwright/test";

// The @live tests run against the real stack (API + video worker + YOLO model + embedded local
// PostgreSQL), and the Vite dev server, all started by e2e/global-setup.ts and stopped by
// global-teardown.ts (Playwright's own webServer teardown does not terminate process trees on this
// Windows setup). The hosted database is never used by tests.
export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false, // live tests share one backend with a single active session
  workers: 1,
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
});
