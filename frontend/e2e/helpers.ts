import type { Page } from "@playwright/test";

const ok = (db: "ok" | "unavailable") => ({
  status: db === "ok" ? "ready" : "not_ready",
  checks: { database: { status: db, latency_ms: db === "ok" ? 4 : null, error: db === "ok" ? null : "connection_failed" } },
});

const status = (dbOk: boolean) => ({
  service: "katomaran-api",
  version: "0.1.0",
  environment: "test",
  uptime_seconds: 42,
  database: { status: dbOk ? "ok" : "unavailable", migration_revision: dbOk ? "0001" : null, migrations_applied: dbOk },
  components: {
    video_worker: { state: "not_running", detail: "No worker connected." },
    face_detector: { state: "not_running", detail: "Model status is reported by the worker." },
    face_recognizer: { state: "not_implemented", detail: "Planned for Phase 2." },
    tracker: { state: "not_implemented", detail: "Planned for Phase 3." },
  },
  active_video_sources: 0,
  video_source_configured: false,
});

const config = {
  environment: "test",
  video: { source_type: "file", source_configured: false, reconnect_interval_seconds: 5 },
  detection: { frame_skip: 2, confidence_threshold: 0.5, model: "yolov11n-face", input_size: 640 },
  stream: { max_fps: 15, max_width: 960 },
  recognition: { similarity_threshold: 0.55 },
  tracking: { max_missing_seconds: 2 },
  logging: { level: "INFO" },
};

/** Deterministic API stub for browser tests that must not depend on a database or network. */
const sources = (workerConnected: boolean) => ({
  sources: [
    { id: "sample-demo", label: "Sample: demo", type: "file", availability: "available", detail: null },
    { id: "gone", label: "Missing clip", type: "file", availability: "unavailable", detail: "The video file was not found on the server." },
  ],
  worker: { connected: workerConnected, worker_id: workerConnected ? "w1" : null, model_available: workerConnected ? true : null, model_detail: null },
});

export async function stubApi(page: Page, mode: "ok" | "db-down" | "offline" | "no-worker") {
  await page.route("**/api/v1/**", async (route) => {
    if (mode === "offline") return route.abort("connectionrefused");
    const p = new URL(route.request().url()).pathname.replace("/api/v1", "");
    if (mode === "no-worker" && p === "/sources") {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(sources(false)) });
    }
    const json = (body: unknown, code = 200) =>
      route.fulfill({ status: code, contentType: "application/json", body: JSON.stringify(body) });
    switch (p) {
      case "/health":
        return json({ status: "ok", service: "katomaran-api" });
      case "/ready":
        return mode === "ok" || mode === "no-worker" ? json(ok("ok")) : json(ok("unavailable"), 503);
      case "/system/status":
        return json(status(mode === "ok" || mode === "no-worker"));
      case "/config":
        return json(config);
      case "/sources":
        return json(sources(true));
      case "/sessions/active":
        return json({ session: null });
      default:
        return json({ error: { code: "not_found", message: "Not Found" } }, 404);
    }
  });
}
