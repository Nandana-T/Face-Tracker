import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { stubApi } from "./helpers";

const nav = (page: Page) => page.getByRole("navigation", { name: "Main navigation" });
const viewport = (page: Page) => page.getByTestId("viewport");
const source = (page: Page, label: string) => page.getByRole("radio", { name: new RegExp(label) });

// ---------------------------------------------------------------------------------------------
// Stubbed backend: fast checks of the page shell and the failure states that need no worker.
// ---------------------------------------------------------------------------------------------
test.describe("Live Monitoring (stubbed backend)", () => {
  test("is reachable from the sidebar and lists sources", async ({ page }) => {
    await stubApi(page, "ok");
    await page.goto("/");
    await nav(page).getByRole("link", { name: "Live Monitoring" }).click();
    await expect(page).toHaveURL(/\/live$/);
    await expect(page.getByRole("heading", { level: 1, name: "Live Monitoring" })).toBeVisible();
    await expect(viewport(page)).toHaveAttribute("data-view", "idle");
    await expect(page.getByRole("radiogroup", { name: "Video source" }).getByRole("radio")).toHaveCount(2);
    await expect(source(page, "Missing clip")).toBeDisabled();
    await expect(page.getByTestId("start-button")).toBeEnabled();
    await expect(page.getByText(/Face detection only/)).toBeVisible();
  });

  test("tells the user to start the worker when none is connected", async ({ page }) => {
    await stubApi(page, "no-worker");
    await page.goto("/live");
    await expect(viewport(page)).toHaveAttribute("data-view", "worker_offline");
    await expect(page.getByRole("heading", { name: "Video worker not connected" })).toBeVisible();
    await expect(page.getByTestId("start-button")).toBeDisabled();
    await expect(page.getByText(/start-worker\.ps1/).first()).toBeVisible();
  });

  test("shows a clear state when the backend is unreachable", async ({ page }) => {
    await stubApi(page, "offline");
    await page.goto("/live");
    await expect(page.getByRole("heading", { name: "Backend not reachable" }).first()).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId("start-button")).toBeDisabled();
  });

  test("works on a tablet viewport with keyboard navigation", async ({ page }) => {
    await stubApi(page, "ok");
    await page.setViewportSize({ width: 820, height: 1100 });
    await page.goto("/live");
    await expect(viewport(page)).toBeVisible();
    const box = await viewport(page).boundingBox();
    expect(box!.width / box!.height).toBeCloseTo(16 / 9, 1); // aspect ratio is preserved
    await source(page, "Sample: demo").focus();
    await expect(source(page, "Sample: demo")).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByTestId("start-button")).toBeFocused();
    expect(box!.x + box!.width).toBeLessThanOrEqual(820); // no horizontal overflow
  });

  test("exposes status changes to assistive technology", async ({ page }) => {
    await stubApi(page, "ok");
    await page.goto("/live");
    const live = page.getByTestId("status-announcement");
    await expect(live).toHaveAttribute("aria-live", "polite");
    await expect(live).toContainText("Ready to start");
  });
});

// ---------------------------------------------------------------------------------------------
// Real stack (@live): API + worker process + real YOLO face model + real (synthetic) video.
// ---------------------------------------------------------------------------------------------
async function stopAnythingRunning(request: APIRequestContext) {
  const active = await (await request.get("/api/v1/sessions/active")).json();
  if (active.session) {
    await request.post(`/api/v1/sessions/${active.session.id}/stop`);
    for (let i = 0; i < 50; i++) {
      const s = await (await request.get("/api/v1/sessions/active")).json();
      if (!s.session) return;
      await new Promise((r) => setTimeout(r, 200));
    }
  }
}

async function openLive(page: Page) {
  await page.goto("/live");
  // The worker connects a moment after the API; Start becomes enabled once it has.
  await expect(page.getByTestId("start-button")).toBeEnabled({ timeout: 45_000 });
}

test.describe("@live Live Monitoring with the real worker and model", () => {
  test.beforeEach(async ({ request }) => stopAnythingRunning(request));

  test("@live processes a real video, draws aligned boxes, then completes", async ({ page }) => {
    await openLive(page);
    await source(page, "E2E alignment clip").check({ force: true });
    await page.getByTestId("start-button").click();

    // Never "Processing" until the backend says so; then real frames arrive.
    await expect(viewport(page)).toHaveAttribute("data-view", /connecting|processing/);
    await expect(viewport(page)).toHaveAttribute("data-view", "processing", { timeout: 30_000 });
    await expect(page.getByTestId("frame-info")).toContainText(/Frame #\d+/, { timeout: 20_000 });
    await expect(page.getByTestId("stat-faces")).toHaveText("1", { timeout: 20_000 });
    await expect(page.getByTestId("confidence-line")).toContainText(/Highest detection confidence .*: \d+%/);

    // The canvas really contains video and the cyan detection stroke.
    const sample = await page.evaluate(() => {
      const c = document.querySelector<HTMLCanvasElement>('[data-testid="video-canvas"]')!;
      const d = c.getContext("2d")!.getImageData(0, 0, c.width, c.height).data;
      let nonBlack = 0;
      for (let i = 0; i < d.length; i += 4 * 97) if (d[i]! + d[i + 1]! + d[i + 2]! > 30) nonBlack++;
      return nonBlack;
    });
    expect(sample).toBeGreaterThan(50);

    // 3 s @ 25 fps = 75 frames; frame_skip = 2 -> detector on 25 of them, 50 skipped.
    await expect(viewport(page)).toHaveAttribute("data-view", "completed", { timeout: 30_000 });
    await expect(page.getByTestId("stat-read")).toHaveText("75");
    await expect(page.getByTestId("stat-processed")).toHaveText("25");
    await expect(page.getByTestId("stat-skipped")).toHaveText("50");
    await expect(page.getByTestId("start-button")).toBeEnabled();

    // --- alignment: the stroke must sit on the face in the last analysed frame ----------------
    await page.waitForTimeout(800); // final sync of the frame info
    const infoText = (await page.getByTestId("frame-info").innerText()).match(/Frame #(\d+)/);
    const frameId = Number(infoText![1]);
    expect(frameId % 3).toBe(0);
    const stroke = await page.evaluate(() => {
      const c = document.querySelector<HTMLCanvasElement>('[data-testid="video-canvas"]')!;
      const { width, height } = c;
      const d = c.getContext("2d")!.getImageData(0, 0, width, height).data;
      let x1 = 1e9, y1 = 1e9, x2 = -1, y2 = -1, n = 0;
      for (let y = 0; y < height; y++) {
        for (let x = 0; x < width; x++) {
          const i = (y * width + x) * 4;
          if (d[i]! < 70 && d[i + 1]! > 190 && d[i + 2]! > 210) {
            n++;
            if (x < x1) x1 = x;
            if (y < y1) y1 = y;
            if (x > x2) x2 = x;
            if (y > y2) y2 = y;
          }
        }
      }
      return { width, height, x1, y1, x2, y2, n };
    });
    expect(stroke.n).toBeGreaterThan(40);

    // Where the generator put the face (backend/app/vision/sample_video.py: portrait_rects).
    const total = 75;
    const t = frameId / (total - 1);
    const side = Math.trunc(300 + 90 * t);
    const cx = Math.trunc(960 * (0.3 + 0.4 * t));
    const x0 = Math.min(Math.max(cx - Math.trunc(side / 2), 0), 960 - side);
    const y0 = Math.trunc((540 - side) / 2);
    const face = [180 / 512, 57 / 512, 271 / 512, 178 / 512]; // face inside the 512 px portrait
    const frameBox = [x0 + face[0]! * side, y0 + face[1]! * side, x0 + face[2]! * side, y0 + face[3]! * side];
    const scale = Math.min(stroke.width / 960, stroke.height / 540);
    const dx = (stroke.width - 960 * scale) / 2;
    const dy = (stroke.height - 540 * scale) / 2;
    const expected = [dx + frameBox[0]! * scale, dy + frameBox[1]! * scale, dx + frameBox[2]! * scale, dy + frameBox[3]! * scale];
    const ix = Math.max(0, Math.min(stroke.x2, expected[2]!) - Math.max(stroke.x1, expected[0]!));
    const iy = Math.max(0, Math.min(stroke.y2, expected[3]!) - Math.max(stroke.y1, expected[1]!));
    const inter = ix * iy;
    const union = (stroke.x2 - stroke.x1) * (stroke.y2 - stroke.y1) + (expected[2]! - expected[0]!) * (expected[3]! - expected[1]!) - inter;
    expect(inter / union, `stroke ${JSON.stringify(stroke)} vs expected ${JSON.stringify(expected)}`).toBeGreaterThan(0.6);
  });

  test("@live stops a running session and can start again", async ({ page }) => {
    await openLive(page);
    await source(page, "E2E long clip").check({ force: true });
    await page.getByTestId("start-button").click();
    await expect(viewport(page)).toHaveAttribute("data-view", "processing", { timeout: 30_000 });
    await expect(source(page, "E2E alignment clip")).toBeDisabled(); // locked while running
    await page.getByTestId("stop-button").click();
    await expect(viewport(page)).toHaveAttribute("data-view", "stopped", { timeout: 20_000 });
    await expect(page.getByRole("heading", { name: "Processing stopped" })).toBeVisible();
    await expect(page.getByTestId("start-button")).toBeEnabled();
    // Restarting works: the worker released the previous source.
    await page.getByTestId("start-button").click();
    await expect(viewport(page)).toHaveAttribute("data-view", "processing", { timeout: 30_000 });
    await page.getByTestId("stop-button").click();
    await expect(viewport(page)).toHaveAttribute("data-view", "stopped", { timeout: 20_000 });
  });

  test("@live explains unavailable and unreadable sources", async ({ page }) => {
    await openLive(page);
    await expect(source(page, "E2E missing clip")).toBeDisabled();
    await expect(page.getByText(/video file was not found on the server/i)).toBeVisible();

    await source(page, "E2E broken file").check({ force: true });
    await page.getByTestId("start-button").click();
    await expect(viewport(page)).toHaveAttribute("data-view", "source_problem", { timeout: 30_000 });
    await expect(page.getByRole("heading", { name: "The video source cannot be used" })).toBeVisible();
    await expect(page.getByTestId("retry-button")).toBeVisible();
    // No server paths in the page.
    expect(await page.locator("body").innerText()).not.toMatch(/datasets|[A-Z]:\\/);
  });

  test("@live keeps processing across a reload and re-attaches", async ({ page }) => {
    await openLive(page);
    await source(page, "E2E long clip").check({ force: true });
    await page.getByTestId("start-button").click();
    await expect(viewport(page)).toHaveAttribute("data-view", "processing", { timeout: 30_000 });
    await page.reload();
    await expect(viewport(page)).toHaveAttribute("data-view", "processing", { timeout: 30_000 });
    await expect(page.getByTestId("stop-button")).toBeVisible();
    await expect(page.getByTestId("frame-info")).toContainText(/Frame #\d+/, { timeout: 20_000 });
    await page.getByTestId("stop-button").click();
    await expect(viewport(page)).toHaveAttribute("data-view", "stopped", { timeout: 20_000 });
  });

  test("@live the API reports consistent status and never exposes visitor counts", async ({ page, request }) => {
    await openLive(page);
    await source(page, "E2E alignment clip").check({ force: true });
    await page.getByTestId("start-button").click();
    await expect(viewport(page)).toHaveAttribute("data-view", "completed", { timeout: 45_000 });
    const recent = await (await request.get("/api/v1/sessions?limit=1")).json();
    const s = recent.sessions[0];
    expect(s.state).toBe("COMPLETED");
    expect(s.stats.frames_read).toBe(75);
    expect(JSON.stringify(s)).not.toMatch(/visitor/i);
    // Overview shows the real worker/detector state and does not invent activity.
    await nav(page).getByRole("link", { name: "Overview" }).click();
    await expect(page.getByTestId("system-status")).toContainText("Ready");
    for (const v of await page.getByTestId("metric-value").all()) await expect(v).toHaveText("—");
  });
});
