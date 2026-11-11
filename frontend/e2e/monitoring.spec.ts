import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { restartApiStack } from "./global-setup";
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
    await expect(page.getByText(/Face detection, temporary tracking and recognition/)).toBeVisible();
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
      const COLORS = [[34, 197, 94], [45, 212, 191], [167, 139, 250], [245, 158, 11]]; const isStroke = (r: number, g: number, b: number) => COLORS.some(([x, y, z]) => Math.abs(r - x!) + Math.abs(g - y!) + Math.abs(b - z!) < 40);
      const c = document.querySelector<HTMLCanvasElement>('[data-testid="video-canvas"]')!;
      const { width, height } = c;
      const d = c.getContext("2d")!.getImageData(0, 0, width, height).data;
      let x1 = 1e9, y1 = 1e9, x2 = -1, y2 = -1, n = 0;
      for (let y = 0; y < height; y++) {
        for (let x = 0; x < width; x++) {
          const i = (y * width + x) * 4;
          // Track stroke colour = track/identity state (green, teal when registered, violet when recognised, amber when ambiguous).
          if (isStroke(d[i]!, d[i + 1]!, d[i + 2]!)) {
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
    // Since Phase 5 the cards show real database figures - and exactly those: nothing is invented.
    const o = await (await request.get("/api/v1/analytics/overview", { headers: await bearer(request) })).json();
    const exits = o.terminal_by_basis.absence + o.terminal_by_basis.observation_ended;
    const cards = page.getByTestId("metric-value");
    await expect(cards.nth(0)).toHaveText(String(o.registered_visitors));
    await expect(cards.nth(1)).toHaveText(String(o.active_visits));
    await expect(cards.nth(2)).toHaveText(String(o.entry_events));
    await expect(cards.nth(3)).toHaveText(String(exits));
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
    const COLORS = [[34, 197, 94], [45, 212, 191], [167, 139, 250], [245, 158, 11]]; const isStroke = (r: number, g: number, b: number) => COLORS.some(([x, y, z]) => Math.abs(r - x!) + Math.abs(g - y!) + Math.abs(b - z!) < 40);
    const c = document.querySelector<HTMLCanvasElement>('[data-testid="video-canvas"]')!;
    const d = c.getContext("2d")!.getImageData(0, 0, c.width, c.height).data;
    let n = 0;
    for (let i = 0; i < d.length; i += 4) if (isStroke(d[i]!, d[i + 1]!, d[i + 2]!)) n++;
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

// ---------------------------------------------------------------------------------------------
// Phase 3: recognition + automatic registration + visitor gallery (real detector, tracker,
// landmarks, SFace embedder, identity manager, PostgreSQL and face-image store).
// Person A = the public-domain portrait; person B = two different LFW photographs of one person.
// ---------------------------------------------------------------------------------------------
const personB = path.join(__dirname, "..", "..", "datasets", "e2e", "person-b2.mp4");

async function clearVisitors(request: APIRequestContext) {
  const headers = await bearer(request);
  const list = await (await request.get("/api/v1/visitors?limit=100", { headers })).json();
  for (const v of list.items as { id: string }[]) {
    expect((await request.delete(`/api/v1/visitors/${v.id}`, { headers })).status()).toBe(204);
  }
}

async function processClip(page: Page, label: string) {
  await page.goto("/live");
  await expect(page.getByTestId("start-button")).toBeEnabled({ timeout: 45_000 });
  await source(page, label).check({ force: true });
  await page.getByTestId("start-button").click();
  await expect(viewport(page)).toHaveAttribute("data-view", /connecting|processing/);
  await expect(viewport(page)).toHaveAttribute("data-view", "completed", { timeout: 60_000 });
}

test.describe("@live Phase 3 recognition", () => {
  test.beforeEach(async ({ request }) => {
    await stopAnythingRunning(request);
    await clearVisitors(request);
  });

  test("@live registers a new face, shows it in the gallery, recognises it again without a duplicate, and survives a restart", async ({ page, request }) => {
    test.setTimeout(240_000);
    await page.goto("/live");
    await signIn(page);

    // 1-5: process a known video; the first reliable face is registered automatically.
    await expect(page.getByTestId("start-button")).toBeEnabled({ timeout: 45_000 });
    await source(page, "E2E alignment clip").check({ force: true });
    await page.getByTestId("start-button").click();
    await expect(page.getByText("New visitor registered: V00001")).toBeVisible({ timeout: 40_000 });
    await expect(viewport(page)).toHaveAttribute("data-view", "completed", { timeout: 40_000 });
    await expect(page.getByTestId("rec-registered")).toHaveText("1");
    await expect(page.getByTestId("recognition-panel")).toContainText("Visitor labels (V…) are persistent");
    // the track keeps its own temporary number; the visitor id is a different thing
    await expect(page.getByTestId("track-chip-1")).toBeVisible();
    await expect(page.getByTestId("chip-visitor-1")).toHaveText("V00001");

    // 6-7: the visitor is in the gallery with a real, authenticated thumbnail
    await nav(page).getByRole("link", { name: "Visitors" }).click();
    const card = page.getByTestId("visitor-card-1");
    await expect(card).toBeVisible();
    const thumb = card.getByRole("img", { name: /Face of visitor V00001/ });
    await expect.poll(() => thumb.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth > 0)).toBe(true);

    // 8: details
    await card.click();
    const drawer = page.getByTestId("visitor-drawer");
    await expect(drawer.getByTestId("drawer-title")).toHaveText("V00001");
    await expect(drawer.getByTestId("history-item")).toHaveCount(1);
    await expect(drawer.getByTestId("history-item").first()).toContainText("registered here");
    await expect(drawer).not.toContainText(/embedding vector/i);
    await drawer.getByTestId("drawer-close").click();
    await expect(drawer).toHaveCount(0);

    // 9-10: the same person in a second processing session is recognised, not registered again
    await processClip(page, "E2E alignment clip");
    await expect(page.getByText("Visitor recognized: V00001")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("rec-registered")).toHaveText("0");
    await expect(page.getByTestId("rec-recognized")).toHaveText("1");
    await nav(page).getByRole("link", { name: "Visitors" }).click();
    await expect(page.getByTestId("visitor-grid").getByRole("button", { name: /Open visitor/ })).toHaveCount(1);
    await page.getByTestId("visitor-card-1").click();
    await expect(page.getByTestId("drawer-sessions")).toHaveText("2");

    // API truth: one visitor, no embeddings or file paths anywhere, image only with a token
    const headers = await bearer(request);
    const list = await (await request.get("/api/v1/visitors", { headers })).json();
    expect(list.total).toBe(1);
    expect(JSON.stringify(list)).not.toMatch(/vector|embedding|image_path|\.jpg/i);
    const id: string = list.items[0].id;
    expect((await request.get(`/api/v1/visitors/${id}/image`)).status()).toBe(401);
    const image = await request.get(`/api/v1/visitors/${id}/image`, { headers });
    expect(image.status()).toBe(200);
    expect(image.headers()["content-type"]).toBe("image/jpeg");
    expect(image.headers()["cache-control"]).toContain("no-store");

    // 11: records persist across an application restart (API + worker), and recognition still works
    await restartApiStack();
    await page.goto("/visitors");
    await expect(page.getByTestId("visitor-card-1")).toBeVisible({ timeout: 30_000 });
    const again = page.getByTestId("visitor-card-1").getByRole("img", { name: /Face of visitor V00001/ });
    await expect.poll(() => again.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth > 0)).toBe(true);
    await processClip(page, "E2E alignment clip");
    await expect(page.getByTestId("rec-registered")).toHaveText("0"); // still no duplicate
    await expect(page.getByTestId("rec-recognized")).toHaveText("1");
    expect((await (await request.get("/api/v1/visitors", { headers: await bearer(request) })).json()).total).toBe(1);
  });

  test("@live two different people become two visitors; a different photo of the same person is recognised", async ({ page, request }) => {
    test.skip(!fs.existsSync(personB), "LFW fixtures are not on this machine (datasets/lfw)");
    test.setTimeout(240_000);
    await page.goto("/live");
    await signIn(page);
    await expect(page.getByTestId("start-button")).toBeEnabled({ timeout: 45_000 });

    await source(page, "E2E alignment clip").check({ force: true }); // person A
    await page.getByTestId("start-button").click();
    await expect(page.getByText("New visitor registered: V00001")).toBeVisible({ timeout: 40_000 });
    await expect(viewport(page)).toHaveAttribute("data-view", "completed", { timeout: 40_000 });

    await processClip(page, "E2E person B photo 1"); // a different person
    await expect(page.getByText("New visitor registered: V00002")).toBeVisible({ timeout: 10_000 });

    await processClip(page, "E2E person B photo 2"); // person B again, another photograph
    await expect(page.getByText("Visitor recognized: V00002")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("rec-registered")).toHaveText("0");

    const list = await (await request.get("/api/v1/visitors?sort=number&order=asc", { headers: await bearer(request) })).json();
    expect(list.items.map((v: { label: string }) => v.label)).toEqual(["V00001", "V00002"]); // no third visitor
    await page.goto("/visitors");
    await expect(page.getByTestId("visitor-grid").getByRole("button", { name: /Open visitor/ })).toHaveCount(2);
    await page.getByTestId("visitor-search").fill("V00002");
    await page.getByTestId("visitor-search-submit").click();
    await expect(page.getByTestId("visitors-range")).toContainText("of 1");

    // erasing a visitor removes it everywhere
    await page.getByTestId("visitor-card-2").click();
    await page.getByTestId("drawer-erase").click();
    await page.getByTestId("erase-confirm").click();
    await expect(page.getByText("Visitor erased")).toBeVisible();
    await expect(page.getByRole("heading", { name: "No visitors match your search" })).toBeVisible();
    expect((await request.get(`/api/v1/visitors/V00002`, { headers: await bearer(request) })).status()).toBe(404);
  });
});

// ---------------------------------------------------------------------------------------------
// Phase 4: visit lifecycle, entry/exit events, evidence, events.log (real detector, tracker,
// landmarks, SFace embedder, identity manager, visit lifecycle, PostgreSQL, evidence store and the
// mandatory events.log). Person A = the public-domain portrait.
// ---------------------------------------------------------------------------------------------
const eventsLog = path.join(__dirname, "..", "..", "logs", "e2e-events.log");

interface EventItem {
  id: string;
  visit_id: string;
  type: "entry" | "exit" | "interrupted";
  event_time: string;
  video_ms: number;
  image_status: string;
  image_kind: string | null;
  persistence_status: string;
  exit_basis: string | null;
  closure_reason: string | null;
  visitor: { id: string; label: string } | null;
}

async function listEvents(request: APIRequestContext, query = ""): Promise<{ items: EventItem[]; total: number }> {
  return (await request.get(`/api/v1/events?limit=100&order=asc${query}`, { headers: await bearer(request) })).json();
}

function logLinesFor(ids: string[]): Record<string, unknown>[] {
  return fs
    .readFileSync(eventsLog, "utf8")
    .split("\n")
    .filter((l) => l.startsWith("{") && ids.some((id) => l.includes(id)))
    .map((l) => JSON.parse(l));
}

async function imageLoads(locator: ReturnType<Page["getByRole"]>) {
  await expect.poll(() => locator.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth > 0)).toBe(true);
}

test.describe("@live Phase 4 visits", () => {
  test.beforeEach(async ({ request }) => {
    await stopAnythingRunning(request);
    await clearVisitors(request);
  });

  test("@live a visitor enters and leaves the view: one entry, one exit, evidence, timeline, details and events.log", async ({ page, request }) => {
    test.setTimeout(240_000);
    await page.goto("/live");
    await signIn(page);
    await expect(page.getByTestId("start-button")).toBeEnabled({ timeout: 45_000 });
    await source(page, "E2E leaves clip").check({ force: true });
    await page.getByTestId("start-button").click();

    // registered, then the entry is confirmed and announced exactly once
    await expect(page.getByText("New visitor registered: V00001")).toBeVisible({ timeout: 40_000 });
    await expect(page.getByText("V00001 entered")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId("visit-active-count")).toHaveText("1");
    // the face disappears from the video; after the grace period exactly one exit is recorded
    await expect(page.getByText("V00001 left the view")).toBeVisible({ timeout: 40_000 });
    await expect(page.getByTestId("visit-active-count")).toHaveText("0");
    await expect(viewport(page)).toHaveAttribute("data-view", "completed", { timeout: 40_000 });
    await expect(page.getByTestId("visit-recent-item")).toHaveCount(2);

    // API truth: exactly one entry and one exit of ONE visit, nothing duplicated
    const events = await listEvents(request, "&visitor=V00001");
    expect(events.total).toBe(2);
    const [entry, exit] = events.items;
    expect([entry!.type, exit!.type]).toEqual(["entry", "exit"]);
    expect(entry!.visit_id).toBe(exit!.visit_id);
    expect(entry!.visit_id).not.toBe(entry!.visitor!.id); // visit id != visitor id
    expect(exit!.exit_basis).toBe("absence");
    expect(exit!.closure_reason).toBe("absence_timeout");
    expect(exit!.video_ms).toBeGreaterThan(entry!.video_ms);
    expect(new Date(exit!.event_time).getTime()).toBeGreaterThanOrEqual(new Date(entry!.event_time).getTime());
    for (const e of events.items) {
      expect(e.image_status).toBe("STORED");
      expect(e.persistence_status).toBe("DELIVERED");
    }
    expect(entry!.image_kind).toBe("observation");
    expect(exit!.image_kind).toBe("last_good_observation"); // the last good observation, not a new capture
    const headers = await bearer(request);
    const visit = await (await request.get(`/api/v1/visits/${entry!.visit_id}`, { headers })).json();
    expect(visit.state).toBe("CLOSED");
    expect(visit.duration_seconds).toBeGreaterThan(0);
    expect(visit.integrity).toBe("complete");
    expect(JSON.stringify([events, visit])).not.toMatch(/\.jpg|image_path|embedding|vector|logs\/|datasets/i);

    // Event Logs shows the real records with authenticated thumbnails; the visit opens with both images
    await nav(page).getByRole("link", { name: "Event Logs" }).click();
    const timeline = page.getByRole("list", { name: "Event timeline" });
    await expect(timeline).toBeVisible();
    const rows = timeline.getByTestId("event-row").filter({ hasText: "V00001" });
    await expect(rows).toHaveCount(2);
    await expect(rows.first().getByTestId("event-type")).toHaveText("Exit"); // newest first
    await expect(rows.nth(1).getByTestId("event-type")).toHaveText("Entry");
    await imageLoads(rows.first().getByRole("img"));
    await imageLoads(rows.nth(1).getByRole("img"));
    await rows.first().getByTestId("event-open").click();
    const drawer = page.getByTestId("visit-drawer");
    await expect(drawer.getByTestId("visit-title")).toHaveText("Visit of V00001");
    await expect(drawer.getByTestId("visit-state")).toHaveText("Closed");
    await expect(drawer.getByTestId("visit-basis")).toHaveText("Left the view");
    await expect(drawer.getByTestId("visit-duration")).not.toHaveText(/unknown|in progress/);
    await expect(drawer.getByTestId("visit-integrity")).toHaveAttribute("data-integrity", "complete");
    await imageLoads(drawer.getByRole("img").first());
    await imageLoads(drawer.getByRole("img").nth(1));
    await expect(drawer.getByTestId("evidence-note").nth(1)).toContainText("Last good observation");
    await page.keyboard.press("Escape");
    await expect(drawer).toHaveCount(0);

    // filters work against the real data
    await page.getByTestId("event-filter-type").selectOption("entry");
    await expect(timeline.getByTestId("event-row").filter({ hasText: "V00001" })).toHaveCount(1);
    await page.getByTestId("event-filter-type").selectOption("");
    await page.getByTestId("event-filter-visitor").fill("V00099");
    await page.getByTestId("event-search-submit").click();
    await expect(page.getByText("No events match these filters")).toBeVisible();

    // image access is private
    expect((await request.get(`/api/v1/events/${entry!.id}/image`)).status()).toBe(401);
    const img = await request.get(`/api/v1/events/${entry!.id}/image`, { headers });
    expect(img.status()).toBe(200);
    expect(img.headers()["content-type"]).toBe("image/jpeg");
    expect(img.headers()["cache-control"]).toContain("no-store");

    // events.log: one auditable line per event, with the stable ids, no biometric payload
    const lines = logLinesFor([entry!.id, exit!.id]).filter((l) => String(l.message).startsWith("visit."));
    expect(lines).toHaveLength(2);
    expect(lines.map((l) => l.message).sort()).toEqual(["visit.entry", "visit.exit"]);
    const data = lines[0]!.data as Record<string, unknown>;
    expect(data).toMatchObject({ visit_id: entry!.visit_id, persistence: "committed", delivery_attempt: 1 });
    expect(JSON.stringify(lines)).not.toMatch(/embedding|vector/i);
    const report = await (await request.get("/api/v1/events/log-verification", { headers })).json();
    expect(report.unexplained_duplicates_count).toBe(0);
    expect(report.missing_in_log_count).toBe(0);
  });

  test("@live end of the video closes the visit as observation-ended; the same person later gets a NEW visit, not a new visitor; replays and a restart change nothing", async ({ page, request }) => {
    test.setTimeout(300_000);
    await page.goto("/live");
    await signIn(page);
    await expect(page.getByTestId("start-button")).toBeEnabled({ timeout: 45_000 });

    await source(page, "E2E alignment clip").check({ force: true });
    await page.getByTestId("start-button").click();
    await expect(page.getByText("V00001 entered")).toBeVisible({ timeout: 40_000 });
    await expect(viewport(page)).toHaveAttribute("data-view", "completed", { timeout: 40_000 });
    // the video ended while the visitor was still in view: observation ended, NOT a departure
    await expect(page.getByText("V00001: observation ended")).toBeVisible({ timeout: 10_000 });

    await processClip(page, "E2E alignment clip"); // the same person again, in a new session
    await expect(page.getByText("Visitor recognized: V00001")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText("V00001 entered")).toBeVisible({ timeout: 10_000 });

    const headers = await bearer(request);
    const events = await listEvents(request, "&visitor=V00001");
    expect(events.total).toBe(4);
    expect(events.items.map((e) => e.type)).toEqual(["entry", "exit", "entry", "exit"]);
    const visitIds = new Set(events.items.map((e) => e.visit_id));
    expect(visitIds.size).toBe(2); // two visits ...
    expect(new Set(events.items.map((e) => e.visitor!.id)).size).toBe(1); // ... one persistent visitor
    expect(events.items.filter((e) => e.type === "exit").every((e) => e.exit_basis === "observation_ended")).toBe(true);
    expect((await (await request.get("/api/v1/visitors", { headers })).json()).total).toBe(1);

    // Replaying the delivery machinery (reconciliation twice) creates nothing and changes nothing.
    const before = JSON.stringify(await listEvents(request));
    for (let i = 0; i < 2; i++) {
      const r = await request.post("/api/v1/events/reconcile", { headers });
      expect(r.status()).toBe(200);
    }
    expect(JSON.stringify(await listEvents(request))).toBe(before);
    const report = await (await request.get("/api/v1/events/log-verification", { headers })).json();
    expect(report.unexplained_duplicates_count).toBe(0);
    expect(report.missing_in_log_count).toBe(0);
    const lines = logLinesFor(events.items.map((e) => e.id)).filter((l) => String(l.message).startsWith("visit."));
    expect(lines).toHaveLength(4);

    // Restart the whole application: everything persists, evidence is still served, nothing is re-created.
    await restartApiStack();
    const after = await listEvents(request);
    expect(JSON.stringify(after)).toBe(before);
    await page.goto("/events");
    await expect(page.getByRole("list", { name: "Event timeline" })).toBeVisible({ timeout: 30_000 });
    const first = page.getByTestId("event-row").filter({ hasText: "V00001" }).first();
    await imageLoads(first.getByRole("img"));
    const post = await (await request.get("/api/v1/events/log-verification", { headers: await bearer(request) })).json();
    expect(post.unexplained_duplicates_count).toBe(0);
    expect(post.missing_in_log_count).toBe(0);
  });

  test("@live a crash while a visitor is inside leaves exactly one honest INTERRUPTED record after the restart", async ({ page, request }) => {
    test.setTimeout(240_000);
    await page.goto("/live");
    await signIn(page);
    await expect(page.getByTestId("start-button")).toBeEnabled({ timeout: 45_000 });
    await source(page, "E2E long clip").check({ force: true });
    await page.getByTestId("start-button").click();
    await expect(page.getByText("V00001 entered")).toBeVisible({ timeout: 40_000 });

    await restartApiStack(); // API and worker die while the visitor is in view

    const events = await listEvents(request, "&visitor=V00001");
    expect(events.items.map((e) => e.type)).toEqual(["entry", "interrupted"]);
    const [entry, interrupted] = events.items;
    expect(interrupted!.closure_reason).toBe("api_restarted");
    expect(interrupted!.exit_basis).toBe("interrupted");
    expect(interrupted!.image_kind).toBe("entry_representative"); // no new face was captured; this is said openly
    expect(interrupted!.image_status).toBe("STORED");
    const headers = await bearer(request);
    const visit = await (await request.get(`/api/v1/visits/${entry!.visit_id}`, { headers })).json();
    expect(visit.state).toBe("INTERRUPTED");
    expect(visit.duration_seconds).toBeNull(); // the end of an interrupted visit is unknown

    await page.goto(`/events?visit=${entry!.visit_id}`);
    const drawer = page.getByTestId("visit-drawer");
    await expect(drawer.getByTestId("visit-state")).toHaveText("Interrupted", { timeout: 30_000 });
    await expect(drawer.getByTestId("visit-duration")).toHaveText("unknown");
    await expect(drawer.getByTestId("visit-reason")).toHaveText("API restarted");
    // another restart changes nothing (reconciliation is idempotent)
    await restartApiStack();
    expect((await listEvents(request, "&visitor=V00001")).total).toBe(2);
  });
});

// ---------------------------------------------------------------------------------------------
// Phase 5: unique visitor counting and analytics (real detector, tracker, landmarks, SFace, identity
// manager, visit lifecycle, PostgreSQL aggregation, dashboard). Person A = the public-domain portrait;
// person B = a labelled LFW photograph. Counts are IDENTITIES, never people.
// ---------------------------------------------------------------------------------------------
interface Overview {
  registered_visitors: number;
  unique_visitors_in_scope: number;
  returning_visitors_in_scope: number;
  new_visitors_in_scope: number;
  visits: number;
  entry_events: number;
  terminal_events: number;
  terminal_by_basis: { absence: number; observation_ended: number; interrupted: number };
  active_visits: number;
}

async function overviewOf(request: APIRequestContext): Promise<Overview> {
  return (await request.get("/api/v1/analytics/overview", { headers: await bearer(request) })).json();
}

const tile = (page: Page, name: string) => page.locator(`[data-testid="analytics-metric"][data-metric="${name}"]`).getByTestId("analytics-value");

test.describe("@live Phase 5 analytics", () => {
  test.beforeEach(async ({ request }) => {
    await stopAnythingRunning(request);
    await clearVisitors(request);
  });

  test("@live unique visitors, visits and returning visitors stay separate through the whole workflow, survive a refresh and a restart, and ignore replays", async ({ page, request }) => {
    test.skip(!fs.existsSync(personB), "LFW fixtures are not on this machine (datasets/lfw)");
    test.setTimeout(420_000);
    await page.goto("/live");
    await signIn(page);
    await expect(page.getByTestId("start-button")).toBeEnabled({ timeout: 45_000 });
    const base = await overviewOf(request); // earlier tests leave de-identified visits behind: assert deltas
    expect(base.registered_visitors).toBe(0);
    expect(base.unique_visitors_in_scope).toBe(0);

    // 2-4: process person A; one registered identity, one visit
    await source(page, "E2E alignment clip").check({ force: true });
    await page.getByTestId("start-button").click();
    await expect(page.getByText("V00001 entered")).toBeVisible({ timeout: 40_000 });
    await expect(viewport(page)).toHaveAttribute("data-view", "completed", { timeout: 40_000 });
    let o = await overviewOf(request);
    expect(o.registered_visitors).toBe(1);
    expect(o.unique_visitors_in_scope).toBe(1);
    expect(o.visits - base.visits).toBe(1);
    expect(o.returning_visitors_in_scope).toBe(0); // first appearance is not "returning"

    // 5-8: the same person again (a new session): still ONE identity, one more visit, now returning
    await processClip(page, "E2E alignment clip");
    await expect(page.getByText("Visitor recognized: V00001")).toBeVisible({ timeout: 10_000 });
    await expect.poll(async () => (await overviewOf(request)).visits - base.visits, { timeout: 15_000 }).toBe(2);
    o = await overviewOf(request);
    expect(o.registered_visitors).toBe(1);
    expect(o.unique_visitors_in_scope).toBe(1); // repeated visits never inflate the unique count
    expect(o.entry_events - base.entry_events).toBe(2);
    expect(o.returning_visitors_in_scope).toBe(1);

    // 9-10: a different known person
    await processClip(page, "E2E person B photo 1");
    await expect(page.getByText("New visitor registered: V00002")).toBeVisible({ timeout: 10_000 });
    await expect.poll(async () => (await overviewOf(request)).registered_visitors, { timeout: 15_000 }).toBe(2);
    o = await overviewOf(request);
    expect(o.unique_visitors_in_scope).toBe(2);
    expect(o.visits - base.visits).toBe(3);
    expect(o.returning_visitors_in_scope).toBe(1);
    expect(o.new_visitors_in_scope).toBe(1);
    expect(o.active_visits).toBe(0); // nothing is processing
    // every video ended while the person was in view, so these are observation-ended exits, not departures
    expect(o.terminal_by_basis.observation_ended - base.terminal_by_basis.observation_ended).toBe(3);

    // 11-12: the dashboard shows exactly what the API says
    await nav(page).getByRole("link", { name: "Analytics" }).click();
    await expect(page.getByTestId("counting-note")).toContainText("not a count of physical people");
    await expect(tile(page, "registered_visitors")).toHaveText("2");
    await expect(tile(page, "unique_visitors")).toHaveText("2");
    await expect(tile(page, "returning_visitors")).toHaveText("1");
    await expect(tile(page, "visits")).toHaveText(String(o.visits));
    await expect(tile(page, "entries")).toHaveText(String(o.entry_events));
    await expect(tile(page, "active_visits")).toHaveText("0");
    await expect(page.getByTestId("chart-activity")).toBeVisible();
    await expect(page.getByTestId("coverage-panel").getByTestId("cov-disclaimer")).toContainText("not person-level recall");
    await expect(page.getByTestId("sources-table").getByTestId("source-row").first()).toBeVisible();

    // filters follow the same rules: one session = one visit
    const sessions = await (await request.get("/api/v1/analytics/sessions?limit=3", { headers: await bearer(request) })).json();
    const last = sessions.items[0];
    expect(last.visits).toBe(1);
    expect(last.unique_visitors).toBe(1);
    const scoped = await (await request.get(`/api/v1/analytics/overview?session_id=${last.session_id}`, { headers: await bearer(request) })).json();
    expect(scoped.unique_visitors_in_scope).toBe(1);
    expect(scoped.registered_visitors).toBe(2); // the global number does not follow the filter

    // 13-14: a browser refresh keeps the numbers
    await page.reload();
    await expect(tile(page, "unique_visitors")).toHaveText("2");
    await expect(tile(page, "visits")).toHaveText(String(o.visits));

    // 17-18: retried / replayed delivery creates nothing
    const headers = await bearer(request);
    for (let i = 0; i < 2; i++) expect((await request.post("/api/v1/events/reconcile", { headers })).status()).toBe(200);
    const again = await overviewOf(request);
    expect(again).toMatchObject({ registered_visitors: 2, unique_visitors_in_scope: 2, visits: o.visits, entry_events: o.entry_events, terminal_events: o.terminal_events });

    // 15-16: after an application restart the numbers are rebuilt from PostgreSQL
    await restartApiStack();
    const restarted = await overviewOf(request);
    expect(restarted).toMatchObject({ registered_visitors: 2, unique_visitors_in_scope: 2, returning_visitors_in_scope: 1, visits: o.visits, entry_events: o.entry_events });
    const loadStart = Date.now();
    await page.goto("/analytics");
    await expect(tile(page, "unique_visitors")).toHaveText("2", { timeout: 30_000 });
    // measured (local stack, tiny database): navigation until the first real figure is on screen
    fs.writeFileSync(path.join(__dirname, "..", "..", "temp", "dashboard-load-ms.txt"), String(Date.now() - loadStart));

    // the independent recount agrees with the aggregates, read-only
    await page.getByTestId("consistency-run").click();
    await expect(page.getByTestId("consistency-result")).toHaveAttribute("data-consistent", "true", { timeout: 15_000 });
    expect(JSON.stringify(await restartedBlob(request))).not.toMatch(/\.jpg|image_path|embedding_b64|vector/i);
  });

  test("@live the dashboard updates while a session runs, without a reload, from notices that carry no numbers", async ({ page, request }) => {
    test.setTimeout(240_000);
    await page.goto("/live");
    await signIn(page);
    await nav(page).getByRole("link", { name: "Analytics" }).click();
    await expect(page.getByTestId("live-indicator")).toHaveAttribute("data-state", "live", { timeout: 30_000 });
    await expect(tile(page, "registered_visitors")).toHaveText("0");
    const headers = await bearer(request);
    await expect(async () => {
      const s = await (await request.get("/api/v1/sources", { headers })).json();
      expect(s.worker.connected).toBe(true);
    }).toPass({ timeout: 45_000 });
    const started = await request.post("/api/v1/sessions", { headers, data: { source_id: "e2e-align" } });
    expect(started.status()).toBe(202);
    // the new identity appears on the open page by itself
    await expect(tile(page, "registered_visitors")).toHaveText("1", { timeout: 60_000 });
    await expect(tile(page, "unique_visitors")).toHaveText("1");
    await expect(page.getByTestId("recent-list").getByTestId("recent-item").first()).toBeVisible({ timeout: 30_000 });
    // nothing is "present" once the video is over
    await expect(tile(page, "active_visits")).toHaveText("0", { timeout: 60_000 });
    const o = await overviewOf(request);
    expect(o.registered_visitors).toBe(1);
    expect(o.unique_visitors_in_scope).toBe(1);
  });
});

async function restartedBlob(request: APIRequestContext) {
  const headers = await bearer(request);
  const paths = ["overview", "timeline", "sources", "sessions", "coverage", "recent", "reconciliation"];
  return Promise.all(paths.map(async (p) => (await request.get(`/api/v1/analytics/${p}`, { headers })).json()));
}
