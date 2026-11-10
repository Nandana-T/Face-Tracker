import { vi } from "vitest";
import type { FrameHeader, SessionOut, SessionStats, SourcesResponse } from "@/lib/api-types";

export const emptyStats: SessionStats = {
  frames_read: 0,
  frames_processed: 0,
  frames_skipped: 0,
  frames_dropped: 0,
  corrupt_frames: 0,
  detections_total: 0,
  faces_last_frame: 0,
  max_faces_in_frame: 0,
  last_inference_ms: 0,
  avg_inference_ms: 0,
  processing_fps: 0,
  last_frame_id: -1,
  frame_width: 0,
  frame_height: 0,
  source_fps: 0,
  elapsed_seconds: 0,
};

export const sourcesFixture = (over: Partial<SourcesResponse> = {}): SourcesResponse => ({
  sources: [
    { id: "sample-demo", label: "Sample: demo", type: "file", availability: "available", detail: null },
    { id: "front-door", label: "Front door camera", type: "rtsp", availability: "available", detail: null },
    { id: "missing", label: "Missing clip", type: "file", availability: "unavailable", detail: "The video file was not found on the server." },
  ],
  worker: { connected: true, worker_id: "worker-1", model_available: true, model_detail: null },
  ...over,
});

export const sessionFixture = (over: Partial<SessionOut> = {}): SessionOut => ({
  id: "a".repeat(32),
  state: "STARTING",
  source: { id: "sample-demo", label: "Sample: demo", type: "file", availability: "available", detail: null },
  created_at: "2026-11-10T10:00:00Z",
  started_at: null,
  ended_at: null,
  stop_reason: null,
  error: null,
  connection: null,
  stats: emptyStats,
  settings: { model: "yolov11n-face", frame_skip: 2, confidence_threshold: 0.5 },
  ...over,
});

export class FakeSocket {
  static instances: FakeSocket[] = [];
  binaryType = "blob";
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  open() {
    this.onopen?.();
  }
  message(data: unknown) {
    this.onmessage?.({ data });
  }
  serverClose() {
    this.onclose?.();
  }
  close() {
    this.closed = true;
    this.onclose?.();
  }
  static get last(): FakeSocket {
    return FakeSocket.instances[FakeSocket.instances.length - 1]!;
  }
  static reset() {
    FakeSocket.instances = [];
  }
}

export function installFakeStreaming() {
  FakeSocket.reset();
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal(
    "createImageBitmap",
    vi.fn(async () => ({ width: 640, height: 360, close: vi.fn() })),
  );
}

export function packFrame(header: FrameHeader): ArrayBuffer {
  const head = new TextEncoder().encode(JSON.stringify(header));
  const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3]);
  const out = new Uint8Array(4 + head.length + jpeg.length);
  new DataView(out.buffer).setUint32(0, head.length, false);
  out.set(head, 4);
  out.set(jpeg, 4 + head.length);
  return out.buffer;
}

export const frameHeader = (over: Partial<FrameHeader> = {}): FrameHeader => ({
  session_id: "a".repeat(32),
  frame_id: 3,
  timestamp_ms: 120,
  frame_width: 640,
  frame_height: 360,
  faces: [{ bbox: [100, 50, 200, 150], confidence: 0.93 }],
  inference_ms: 24,
  ...over,
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

export const apiError = (status: number, code: string, message: string) =>
  json({ error: { code, message, request_id: "r1", details: null } }, status);

export interface ProcessingController {
  sources: SourcesResponse | "error";
  active: SessionOut | null;
  /** What GET /sessions/{id} returns (backend truth). */
  current: SessionOut | null;
  startResponse: (sourceId: string) => Response | Promise<Response>;
  stopResponse: () => Response | Promise<Response>;
  calls: { method: string; path: string; body?: unknown }[];
}

export function createController(over: Partial<ProcessingController> = {}): ProcessingController {
  const c: ProcessingController = {
    sources: sourcesFixture(),
    active: null,
    current: null,
    startResponse: () => json(sessionFixture(), 202),
    stopResponse: () => json(sessionFixture({ state: "STOPPING" })),
    calls: [],
    ...over,
  };
  return c;
}

/** Layers the processing endpoints over the Phase 0 mock (health/ready/status/config). */
export function processingFetch(c: ProcessingController) {
  return (base: typeof fetch): typeof fetch =>
    (async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "http://localhost").pathname.replace(/^.*\/v1/, "");
      const method = init?.method ?? "GET";
      const processing = path.startsWith("/sources") || path.startsWith("/sessions");
      if (!processing) return base(input, init);
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      c.calls.push({ method, path, body });
      if (path === "/sources") {
        return c.sources === "error" ? apiError(500, "internal_error", "boom") : json(c.sources);
      }
      if (path === "/sessions/active") return json({ session: c.active });
      if (method === "POST" && path === "/sessions") return c.startResponse(body.source_id);
      if (method === "POST" && path.endsWith("/stop")) return c.stopResponse();
      if (method === "GET" && path.startsWith("/sessions/")) {
        return c.current ? json(c.current) : apiError(404, "session_not_found", "No such processing session.");
      }
      return apiError(404, "not_found", "Not Found");
    }) as typeof fetch;
}
