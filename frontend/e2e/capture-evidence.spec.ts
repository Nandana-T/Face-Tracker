import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { E2E_PASSWORD } from "./prepare";

/**
 * On-demand capture of the submission evidence (screenshots + sanitised exports) from the REAL stack.
 * Run explicitly:  npx playwright test e2e/capture-evidence.spec.ts
 * The only face is the public-domain NASA portrait fixture (datasets/e2e/leaves.mp4); the database is the
 * embedded local PostgreSQL. Nothing is fabricated: every file is the app's own output.
 */
const root = path.resolve(__dirname, "..", "..");
const OUT = path.join(root, "docs", "sample-output");
const eventsLog = path.join(root, "logs", "e2e-events.log");

async function bearer(request: APIRequestContext) {
  const r = await request.post("/api/v1/auth/login", { data: { password: E2E_PASSWORD } });
  return { Authorization: `Bearer ${(await r.json()).access_token}` };
}

async function loaded(page: Page, selector: string) {
  await expect
    .poll(() =>
      page.$$eval(selector, (imgs) => imgs.length > 0 && imgs.every((i) => (i as HTMLImageElement).complete && (i as HTMLImageElement).naturalWidth > 0)),
    )
    .toBe(true);
}

test("@evidence capture dashboard screenshots and sanitised database exports", async ({ page, request }) => {
  test.setTimeout(240_000);
  fs.mkdirSync(path.join(OUT, "screenshots"), { recursive: true });
  const headers = await bearer(request);
  const list = await (await request.get("/api/v1/visitors?limit=100", { headers })).json();
  for (const v of list.items as { id: string }[]) await request.delete(`/api/v1/visitors/${v.id}`, { headers });

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/live");
  await page.getByLabel("Password").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Live Monitoring" })).toBeVisible();
  await expect(page.getByTestId("start-button")).toBeEnabled({ timeout: 45_000 });
  await page.getByRole("radio", { name: /E2E leaves clip/ }).check({ force: true });
  await page.getByTestId("start-button").click();
  await expect(page.getByText("New visitor registered: V00001")).toBeVisible({ timeout: 40_000 });
  await expect(page.getByText("V00001 entered")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("V00001 left the view")).toBeVisible({ timeout: 40_000 });
  await expect(page.getByTestId("viewport")).toHaveAttribute("data-view", "completed", { timeout: 40_000 });

  const nav = async (name: string, url: string, heading: RegExp | string) => {
    await page.goto(url);
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible({ timeout: 20_000 });
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(1200); // let charts and images settle
    await page.screenshot({ path: path.join(OUT, "screenshots", name) });
  };
  await nav("overview-dashboard.png", "/", /Welcome to Katomaran AI/);
  await page.goto("/visitors");
  await expect(page.getByRole("heading", { level: 1, name: /Visitors/ })).toBeVisible();
  await loaded(page, "img");
  await page.waitForTimeout(800);
  await page.screenshot({ path: path.join(OUT, "screenshots", "visitor-gallery.png") });
  await nav("analytics.png", "/analytics", /Analytics/);

  // Sanitised exports of the app's own API output (the API never returns embeddings or server paths)
  const get = async (p: string) => (await request.get(`/api/v1${p}`, { headers })).json();
  const visitors = await get("/visitors?limit=10");
  const visits = await get("/visits?limit=10");
  const events = await get("/events?limit=10&order=asc");
  const analytics = await get("/analytics/overview");
  const write = (name: string, data: unknown) => fs.writeFileSync(path.join(OUT, name), JSON.stringify(data, null, 2) + "\n");
  write("visitor-records.json", visitors);
  write("visit-records.json", visits);
  write("event-records.json", events);
  write("analytics-dashboard-overview.json", analytics);

  const ids = (events.items as { id: string }[]).map((e) => e.id);
  const lines = fs
    .readFileSync(eventsLog, "utf8")
    .split("\n")
    .filter((l) => l.startsWith("{") && /visit\.(entry|exit|interrupted)/.test(l) && ids.some((id) => l.includes(id)));
  fs.writeFileSync(path.join(OUT, "events-log-dashboard-run.jsonl"), lines.join("\n") + "\n");
  expect(lines.length).toBe(ids.length); // every database event has its events.log record
});
