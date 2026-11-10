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
    video_worker: { state: "not_implemented", detail: "Planned for Phase 1." },
    face_detector: { state: "not_implemented", detail: "Planned for Phase 1." },
    face_recognizer: { state: "not_implemented", detail: "Planned for Phase 2." },
    tracker: { state: "not_implemented", detail: "Planned for Phase 3." },
  },
  active_video_sources: 0,
  video_source_configured: false,
});

const config = {
  environment: "test",
  video: { source_type: "file", source_configured: false, reconnect_interval_seconds: 5 },
  detection: { frame_skip: 2, confidence_threshold: 0.5 },
  recognition: { similarity_threshold: 0.55 },
  tracking: { max_missing_seconds: 2 },
  logging: { level: "INFO" },
};

/** Deterministic API stub for browser tests that must not depend on a database or network. */
export async function stubApi(page: Page, mode: "ok" | "db-down" | "offline") {
  await page.route("**/api/v1/**", async (route) => {
    if (mode === "offline") return route.abort("connectionrefused");
    const p = new URL(route.request().url()).pathname.replace("/api/v1", "");
    const json = (body: unknown, code = 200) =>
      route.fulfill({ status: code, contentType: "application/json", body: JSON.stringify(body) });
    switch (p) {
      case "/health":
        return json({ status: "ok", service: "katomaran-api" });
      case "/ready":
        return mode === "ok" ? json(ok("ok")) : json(ok("unavailable"), 503);
      case "/system/status":
        return json(status(mode === "ok"));
      case "/config":
        return json(config);
      default:
        return json({ error: { code: "not_found", message: "Not Found" } }, 404);
    }
  });
}
