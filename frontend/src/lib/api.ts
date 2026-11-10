import type {
  ErrorEnvelope,
  HealthResponse,
  PublicConfigResponse,
  ReadinessResponse,
  SessionOut,
  SourcesResponse,
  SystemStatusResponse,
} from "./api-types";

/** Centralised API base URL. Dev: "/api/v1" through the Vite proxy. Prod: set VITE_API_BASE_URL. */
export const API_BASE_URL: string = (import.meta.env.VITE_API_BASE_URL ?? "/api/v1").replace(
  /\/+$/,
  "",
);

const REQUEST_TIMEOUT_MS = 8000;

export type ApiErrorKind = "network" | "http" | "parse";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly kind: ApiErrorKind,
    readonly status?: number,
    readonly code?: string,
    readonly requestId?: string | null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function send(
  path: string,
  signal?: AbortSignal,
  init?: { method: "POST"; body?: unknown },
): Promise<Response> {
  const timeout = AbortSignal.timeout(REQUEST_TIMEOUT_MS);
  try {
    return await fetch(`${API_BASE_URL}${path}`, {
      method: init?.method ?? "GET",
      headers: {
        Accept: "application/json",
        ...(init?.body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
      signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    });
  } catch (cause) {
    if (signal?.aborted) throw cause; // query cancellation, not an outage
    throw new ApiError("Unable to reach the API", "network", undefined, undefined, undefined);
  }
}

async function parseJson<T>(response: Response): Promise<T> {
  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError("The API returned an unreadable response", "parse", response.status);
  }
}

async function toHttpError(response: Response): Promise<ApiError> {
  let envelope: Partial<ErrorEnvelope> | undefined;
  try {
    envelope = (await response.json()) as ErrorEnvelope;
  } catch {
    /* non-JSON error body (e.g. proxy 502) */
  }
  const err = envelope?.error;
  return new ApiError(
    err?.message ?? `Request failed with status ${response.status}`,
    "http",
    response.status,
    err?.code,
    err?.request_id,
  );
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await send(path, signal);
  if (!response.ok) throw await toHttpError(response);
  return parseJson<T>(response);
}

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const response = await send(path, undefined, { method: "POST", body });
  if (!response.ok) throw await toHttpError(response);
  return parseJson<T>(response);
}

/** WebSocket base derived from the API base (relative in dev -> same origin as the page). */
export function streamUrl(sessionId: string): string {
  const base = API_BASE_URL.startsWith("http")
    ? API_BASE_URL.replace(/^http/, "ws")
    : `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${API_BASE_URL}`;
  return `${base}/sessions/${encodeURIComponent(sessionId)}/stream`;
}

export const api = {
  sources: (signal?: AbortSignal) => getJson<SourcesResponse>("/sources", signal),
  activeSession: (signal?: AbortSignal) =>
    getJson<{ session: SessionOut | null }>("/sessions/active", signal),
  session: (id: string, signal?: AbortSignal) =>
    getJson<SessionOut>(`/sessions/${encodeURIComponent(id)}`, signal),
  /** 202 = new session, 200 = the identical session was already active (idempotent). */
  startSession: (sourceId: string) => postJson<SessionOut>("/sessions", { source_id: sourceId }),
  stopSession: (id: string) => postJson<SessionOut>(`/sessions/${encodeURIComponent(id)}/stop`),

  health: (signal?: AbortSignal) => getJson<HealthResponse>("/health", signal),

  /** 503 is a *valid* readiness answer (database down), so its body is returned, not thrown. */
  async readiness(signal?: AbortSignal): Promise<ReadinessResponse> {
    const response = await send("/ready", signal);
    if (response.status === 200 || response.status === 503) {
      const body = await parseJson<ReadinessResponse>(response);
      if (body?.checks?.database) return body;
    }
    throw await toHttpError(response);
  },

  systemStatus: (signal?: AbortSignal) => getJson<SystemStatusResponse>("/system/status", signal),
  config: (signal?: AbortSignal) => getJson<PublicConfigResponse>("/config", signal),
};
