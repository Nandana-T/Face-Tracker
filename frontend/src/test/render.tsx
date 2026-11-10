import { render } from "@testing-library/react";
import { RouterProvider } from "react-router-dom";
import { vi } from "vitest";
import { Providers } from "@/app/providers";
import { createTestRouter } from "@/app/router";
import type {
  PublicConfigResponse,
  ReadinessResponse,
  SystemStatusResponse,
} from "@/lib/api-types";
import { createQueryClient } from "@/lib/query-client";

export const statusFixture: SystemStatusResponse = {
  service: "katomaran-api",
  version: "0.1.0",
  environment: "test",
  uptime_seconds: 125,
  database: { status: "ok", migration_revision: "0001", migrations_applied: true },
  components: {
    video_worker: { state: "not_implemented", detail: "Planned for Phase 1." },
    face_detector: { state: "not_implemented", detail: "Planned for Phase 1." },
    face_recognizer: { state: "not_implemented", detail: "Planned for Phase 2." },
    tracker: { state: "not_implemented", detail: "Planned for Phase 3." },
  },
  active_video_sources: 0,
  video_source_configured: false,
};

export const configFixture: PublicConfigResponse = {
  environment: "test",
  video: { source_type: "file", source_configured: false, reconnect_interval_seconds: 5 },
  detection: { frame_skip: 2, confidence_threshold: 0.5 },
  recognition: { similarity_threshold: 0.55 },
  tracking: { max_missing_seconds: 2 },
  logging: { level: "INFO" },
};

export interface Scenario {
  /** "down" simulates a refused connection (fetch rejects). */
  health?: "ok" | "down";
  ready?: "ok" | "db-down" | "down";
  status?: "ok" | "error" | "pending";
  config?: "ok" | "error";
}

const ok = (db: "ok" | "unavailable"): ReadinessResponse => ({
  status: db === "ok" ? "ready" : "not_ready",
  checks: {
    database: {
      status: db,
      latency_ms: db === "ok" ? 3 : null,
      error: db === "ok" ? null : "connection_failed",
    },
  },
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

export function mockApi(s: Scenario = {}) {
  const sc = { health: "ok", ready: "ok", status: "ok", config: "ok", ...s };
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), "http://localhost").pathname.replace(/^.*\/v1/, "");
    switch (path) {
      case "/health":
        if (sc.health === "down") throw new TypeError("Failed to fetch");
        return json({ status: "ok", service: "katomaran-api" });
      case "/ready":
        if (sc.ready === "down" || sc.health === "down") throw new TypeError("Failed to fetch");
        return sc.ready === "ok" ? json(ok("ok")) : json(ok("unavailable"), 503);
      case "/system/status":
        if (sc.health === "down") throw new TypeError("Failed to fetch");
        if (sc.status === "pending") return new Promise<Response>(() => {});
        if (sc.status === "error")
          return json({ error: { code: "internal_error", message: "boom", request_id: "r1", details: null } }, 500);
        return json(
          sc.ready === "db-down"
            ? { ...statusFixture, database: { status: "unavailable", migration_revision: null, migrations_applied: false } }
            : statusFixture,
        );
      case "/config":
        if (sc.config === "error")
          return json({ error: { code: "internal_error", message: "config boom", request_id: null, details: null } }, 500);
        return json(configFixture);
      default:
        return json({ error: { code: "not_found", message: "Not Found", request_id: null, details: null } }, 404);
    }
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

export function renderApp(path = "/", scenario: Scenario = {}) {
  const fetchMock = mockApi(scenario);
  const client = createQueryClient();
  client.setDefaultOptions({
    queries: { retry: false, refetchInterval: false, staleTime: 0 },
  });
  const router = createTestRouter([path]);
  const utils = render(
    <Providers client={client}>
      <RouterProvider router={router} />
    </Providers>,
  );
  return { ...utils, router, fetchMock, client };
}
