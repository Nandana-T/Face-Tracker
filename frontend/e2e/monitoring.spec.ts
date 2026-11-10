import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { stubApi } from "./helpers";
import { E2E_PASSWORD } from "./prepare";

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
    await expect(page.getByText(/Face detection and temporary tracking/)).toBeVisible();
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
async function bearer(request: APIRequestContext) {
  const r = await request.post("/api/v1/auth/login", { data: { password: E2E_PASSWORD } });
  const { access_token } = await r.json();
  return { Authorization: `Bearer ${access_token}` };
}

async function stopAnythingRunning(request: APIRequestContext) {
  const headers = await bearer(request);
  const active = await (await request.get("/api/v1/sessions/active", { headers })).json();
  if (active.session) {
    await request.post(`/api/v1/sessions/${active.session.id}/stop`, { headers });
    for (let i = 0; i < 50; i++) {
      const s = await (await request.get("/api/v1/sessions/active", { headers })).json();
      if (!s.session) return;
      await new Promise((r) => setTimeout(r, 200));
    }
  }
}

async function signIn(page: Page) {
  await page.getByLabel("Password").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Live Monitoring" })).toBeVisible();
}

async function openLive(page: Page) {
  await page.goto("/live");
  await signIn(page);
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

    // The canvas really contains video and the green confirmed-track stroke.
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
          // CONFIRMED track stroke (#22c55e); labels use neutral text so they do not match.
          if (d[i]! < 90 && d[i + 1]! > 170 && d[i + 2]! < 140) {
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
    const recent = await (
      await request.get("/api/v1/sessions?limit=1", { headers: await bearer(request) })
    ).json();
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

// ---------------------------------------------------------------------------------------------
// Access control against the real API (ADMIN_PASSWORD is set for this stack).
// ---------------------------------------------------------------------------------------------
test.describe("@live access control", () => {
  test("@live the app and the API refuse anonymous visitors", async ({ page, request }) => {
    await page.goto("/live");
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    await expect(page.getByTestId("viewport")).toHaveCount(0); // nothing of the console is rendered

    for (const [method, path] of [
      ["GET", "/api/v1/sources"],
      ["GET", "/api/v1/sessions"],
      ["GET", "/api/v1/system/status"],
      ["GET", "/api/v1/config"],
      ["POST", "/api/v1/sessions"],
    ] as const) {
      const r = await request.fetch(path, {
        method,
        data: method === "POST" ? { source_id: "e2e-align" } : undefined,
      });
      expect(r.status(), `${method} ${path}`).toBe(401);
    }
    expect((await request.get("/api/v1/health")).status()).toBe(200); // liveness stays public
  });

  test("@live a wrong password is refused clearly; the right one signs in and out", async ({ page }) => {
    await page.goto("/live");
    await page.getByLabel("Password").fill("definitely-wrong");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("alert")).toContainText("not correct");
    await signIn(page);
    await expect(page.getByTestId("start-button")).toBeEnabled({ timeout: 45_000 });
    await page.getByTestId("sign-out").click();
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible(); // the token did not survive
  });

  test("@live the frame stream cannot be opened without a valid single-use ticket", async ({
    page,
    request,
  }) => {
    await stopAnythingRunning(request);
    await openLive(page);
    await source(page, "E2E long clip").check({ force: true });
    await page.getByTestId("start-button").click();
    await expect(viewport(page)).toHaveAttribute("data-view", "processing", { timeout: 30_000 });
    const headers = await bearer(request);
    const active = await (await request.get("/api/v1/sessions/active", { headers })).json();
    const id: string = active.session.id;

    const outcome = (url: string) =>
      page.evaluate(
        (u) =>
          new Promise<string>((resolve) => {
            const ws = new WebSocket(u);
            ws.onopen = () => {
              resolve("opened");
              ws.close();
            };
            ws.onclose = () => resolve("refused");
            setTimeout(() => resolve("timeout"), 5000);
          }),
        url,
      );
    const base = `ws://127.0.0.1:5173/api/v1/sessions/${id}/stream`;
    expect(await outcome(base)).toBe("refused"); // no ticket
    expect(await outcome(`${base}?ticket=forged.ticket`)).toBe("refused");
    const { ticket } = await (
      await request.post(`/api/v1/sessions/${id}/stream-ticket`, { headers })
    ).json();
    expect(await outcome(`${base}?ticket=${ticket}`)).toBe("opened"); // a real ticket works...
    expect(await outcome(`${base}?ticket=${ticket}`)).toBe("refused"); // ...exactly once
    await page.getByTestId("stop-button").click();
    await expect(viewport(page)).toHaveAttribute("data-view", "stopped", { timeout: 20_000 });
  });
});


// ---------------------------------------------------------------------------------------------
// Phase 2: multi-face tracking on the real stack (real worker, real YOLO model, real tracker).
// ---------------------------------------------------------------------------------------------
const greenPixels = (page: Page) =>
  page.evaluate(() => {
    const c = document.querySelector<HTMLCanvasElement>('[data-testid="video-canvas"]')!;
    const d = c.getContext("2d")!.getImageData(0, 0, c.width, c.height).data;
    let n = 0;
    for (let i = 0; i < d.length; i += 4) if (d[i]! < 90 && d[i + 1]! > 170 && d[i + 2]! < 140) n++;
    return n;
  });

const whitePixels = (page: Page) =>
  page.evaluate(() => {
    const c = document.querySelector<HTMLCanvasElement>('[data-testid="video-canvas"]')!;
    const d = c.getContext("2d")!.getImageData(0, 0, c.width, c.height).data;
    let n = 0;
    for (let i = 0; i < d.length; i += 4) if (d[i]! > 250 && d[i + 1]! > 250 && d[i + 2]! > 250) n++;
    return n;
  });

test.describe("@live Phase 2 tracking", () => {
  test.beforeEach(async ({ request }) => stopAnythingRunning(request));

  test("@live tracks a real face: stable id, details, overlay toggles, honest statistics", async ({ page, request }) => {
    await openLive(page);
    await source(page, "E2E alignment clip").check({ force: true });
    await page.getByTestId("start-button").click();
    await expect(viewport(page)).toHaveAttribute("data-view", "processing", { timeout: 30_000 });

    // One face moves smoothly through the clip: one track, id 1, becomes CONFIRMED.
    const chip = page.getByTestId("track-chip-1");
    await expect(chip).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId("track-list").getByRole("button")).toHaveCount(1);
    await expect(viewport(page)).toHaveAttribute("data-view", "completed", { timeout: 30_000 });
    await expect(chip).toContainText("Confirmed");
    await page.waitForTimeout(800); // final sync of the last frame

    // Overlay toggles act on the real canvas.
    expect(await greenPixels(page)).toBeGreaterThan(0);
    await page.getByTestId("toggle-overlay").click();
    await expect.poll(() => greenPixels(page)).toBe(0);
    await page.getByTestId("toggle-overlay").click();
    await expect.poll(() => greenPixels(page)).toBeGreaterThan(0);

    // Track details.
    const whiteBefore = await whitePixels(page);
    await chip.click();
    const details = page.getByTestId("track-details");
    await expect(details).toBeVisible();
    await expect(details.getByRole("heading", { name: "Track #1" })).toBeVisible();
    await expect(page.getByTestId("detail-first")).toHaveText("0.0 s");
    await expect(page.getByTestId("detail-source")).toContainText("detected in this frame");
    await expect(page.getByTestId("detail-conf")).toHaveText(/^\d+%$/);
    await expect(chip).toHaveAttribute("aria-pressed", "true");
    // Selected-track feedback on the canvas: the box is drawn white instead of green.
    await expect.poll(() => whitePixels(page)).toBeGreaterThan(whiteBefore + 20);

    // Closing the details clears the selection.
    await page.getByTestId("detail-close").click();
    await expect(details).toHaveCount(0);

    // Diagnostics come from the session's real counters.
    await page.getByTestId("diagnostics-toggle").click();
    await expect(page.getByTestId("trk-created")).toHaveText("1");
    await expect(page.getByTestId("trk-confirmed-total")).toHaveText("1");
    await expect(page.getByTestId("trk-rejected")).toHaveText("0");
    await expect(page.getByTestId("trk-errors")).toHaveText("0");
    await expect(page.getByTestId("trk-ms")).toContainText("ms");

    // The persisted session carries the same counters - and no notion of visitors.
    const recent = await (await request.get("/api/v1/sessions?limit=1", { headers: await bearer(request) })).json();
    const s = recent.sessions[0];
    expect(s.state).toBe("COMPLETED");
    expect(s.settings.tracking_enabled).toBe(true);
    expect(s.stats.tracks_created_total).toBe(1);
    expect(s.stats.tracks_confirmed_total).toBe(1);
    expect(JSON.stringify(s)).not.toMatch(/visitor/i);
    expect(await page.getByTestId("tracking-panel").innerText()).not.toMatch(/unique visitors?:/i);
  });

  test("@live a new session starts with a clean tracker and the stream re-attaches after a reload", async ({ page, request }) => {
    await openLive(page);
    await source(page, "E2E long clip").check({ force: true });
    await page.getByTestId("start-button").click();
    await expect(page.getByTestId("track-chip-1")).toBeVisible({ timeout: 40_000 });
    await page.reload();
    await expect(viewport(page)).toHaveAttribute("data-view", "processing", { timeout: 30_000 });
    // Reconnecting is stateless: the first frame after the reload already carries full track state.
    await expect(page.getByTestId("track-list")).toBeVisible({ timeout: 30_000 });
    await page.getByTestId("stop-button").click();
    await expect(viewport(page)).toHaveAttribute("data-view", "stopped", { timeout: 20_000 });

    // Clean-up: nothing keeps running, and the next session numbers its tracks from 1 again.
    const headers = await bearer(request);
    const active = await (await request.get("/api/v1/sessions/active", { headers })).json();
    expect(active.session).toBeNull();
    await source(page, "E2E alignment clip").check({ force: true });
    await page.getByTestId("start-button").click();
    await expect(viewport(page)).toHaveAttribute("data-view", "completed", { timeout: 45_000 });
    await expect(page.getByTestId("track-chip-1")).toBeVisible();
    await expect(page.getByTestId("track-list").getByRole("button")).toHaveCount(1);
  });
});
