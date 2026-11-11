import { authToken, notifyUnauthorized } from "./auth";
import type {
  AnalyticsCoverage,
  AnalyticsOverview,
  AnalyticsReconciliation,
  AnalyticsScope,
  AnalyticsSessions,
  AnalyticsTimeline,
  ErrorEnvelope,
  EventList,
  SourceStat,
  EventSort,
  EventType,
  EventsHealth,
  VisitDetailRecord,
  VisitEventRecord,
  HealthResponse,
  PublicConfigResponse,
  ReadinessResponse,
  RecognitionStatus,
  SessionOut,
  SourceOut,
  SourcesResponse,
  SystemStatusResponse,
  VisitorDetail,
  VisitorList,
  VisitorSort,
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
  init?: { method: "POST" | "DELETE"; body?: unknown },
): Promise<Response> {
  const timeout = AbortSignal.timeout(REQUEST_TIMEOUT_MS);
  try {
    return await fetch(`${API_BASE_URL}${path}`, {
      method: init?.method ?? "GET",
      headers: {
        Accept: "application/json",
        ...(init?.body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(authToken.get() ? { Authorization: `Bearer ${authToken.get()}` } : {}),
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
  // A rejected/expired token anywhere sends the user back to the sign-in screen. (A wrong password
  // on the login form is also 401 but must not clear anything: it carries `invalid_credentials`.)
  const clone = response.clone();
  let envelope: Partial<ErrorEnvelope> | undefined;
  try {
    envelope = (await response.json()) as ErrorEnvelope;
  } catch {
    /* non-JSON error body (e.g. proxy 502) */
  }
  const err = envelope?.error;
  if (clone.status === 401 && err?.code === "unauthorized") notifyUnauthorized();
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
export function streamUrl(sessionId: string, ticket?: string): string {
  const base = API_BASE_URL.startsWith("http")
    ? API_BASE_URL.replace(/^http/, "ws")
    : `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${API_BASE_URL}`;
  const url = `${base}/sessions/${encodeURIComponent(sessionId)}/stream`;
  return ticket ? `${url}?ticket=${encodeURIComponent(ticket)}` : url;
}

function scopeParams(scope: AnalyticsScope, extra: Record<string, string> = {}): string {
  const p = new URLSearchParams(extra);
  if (scope.since) p.set("since", scope.since);
  if (scope.until) p.set("until", scope.until);
  if (scope.source_id) p.set("source_id", scope.source_id);
  if (scope.session_id) p.set("session_id", scope.session_id);
  const text = p.toString();
  return text ? `?${text}` : "";
}

/** WebSocket URL of the analytics change stream (notices only - never counts). */
export function analyticsStreamUrl(ticket?: string): string {
  const base = API_BASE_URL.startsWith("http")
    ? API_BASE_URL.replace(/^http/, "ws")
    : `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${API_BASE_URL}`;
  return ticket ? `${base}/analytics/stream?ticket=${encodeURIComponent(ticket)}` : `${base}/analytics/stream`;
}

export interface EventQuery {
  limit: number;
  offset: number;
  type?: EventType;
  /** Visitor label (V00012), number or the start of a visitor id. */
  visitor?: string;
  /** ISO timestamps (inclusive). */
  since?: string;
  until?: string;
  sort: EventSort;
  order: "asc" | "desc";
}

export interface VisitorQuery {
  limit: number;
  offset: number;
  q?: string;
  sort: VisitorSort;
  order: "asc" | "desc";
}

/** Authenticated image fetch (an <img src> cannot send the bearer token) -> a short-lived blob URL. */
async function fetchImageBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  const response = await send(path, signal);
  if (!response.ok) throw await toHttpError(response);
  return response.blob();
}

export interface AuthStatus {
  required: boolean;
  authenticated: boolean;
}

export interface UploadHandle {
  promise: Promise<SourceOut>;
  abort(): void;
}

/**
 * Authenticated upload with progress (XMLHttpRequest: fetch cannot report upload progress). The raw file is
 * the request body; the server validates it as a real video and returns the new source.
 */
function uploadVideo(file: File, onProgress: (fraction: number) => void): UploadHandle {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<SourceOut>((resolve, reject) => {
    xhr.open("PUT", `${API_BASE_URL}/uploads?filename=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("Accept", "application/json");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    const token = authToken.get();
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    };
    xhr.onerror = () => reject(new ApiError("Unable to reach the API", "network"));
    xhr.ontimeout = () => reject(new ApiError("The upload timed out", "network"));
    xhr.onabort = () => reject(new ApiError("Upload cancelled", "network", undefined, "aborted"));
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* non-JSON body */
      }
      if (xhr.status >= 200 && xhr.status < 300 && body) {
        onProgress(1);
        resolve(body as SourceOut);
        return;
      }
      const err = (body as Partial<ErrorEnvelope> | null)?.error;
      if (xhr.status === 401 && err?.code === "unauthorized") notifyUnauthorized();
      reject(new ApiError(err?.message ?? `Upload failed (HTTP ${xhr.status})`, "http", xhr.status, err?.code, err?.request_id));
    };
    xhr.send(file);
  });
  return { promise, abort: () => xhr.abort() };
}

export const api = {
  uploadVideo,
  async deleteUpload(sourceId: string): Promise<void> {
    const id = sourceId.replace(/^upload-/, "");
    const response = await send(`/uploads/${encodeURIComponent(id)}`, undefined, { method: "DELETE" });
    if (!response.ok) throw await toHttpError(response);
  },
  authStatus: (signal?: AbortSignal) => getJson<AuthStatus>("/auth/status", signal),
  async login(password: string): Promise<void> {
    const { access_token } = await postJson<{ access_token: string }>("/auth/login", { password });
    authToken.set(access_token);
  },
  /** Single-use (30 s) ticket that authorises one WebSocket connection for this session. */
  streamTicket: (sessionId: string) =>
    postJson<{ ticket: string; expires_in: number }>(
      `/sessions/${encodeURIComponent(sessionId)}/stream-ticket`,
    ),
  sources: (signal?: AbortSignal) => getJson<SourcesResponse>("/sources", signal),
  activeSession: (signal?: AbortSignal) =>
    getJson<{ session: SessionOut | null }>("/sessions/active", signal),
  session: (id: string, signal?: AbortSignal) =>
    getJson<SessionOut>(`/sessions/${encodeURIComponent(id)}`, signal),
  /** 202 = new session, 200 = the identical session was already active (idempotent). */
  startSession: (sourceId: string) => postJson<SessionOut>("/sessions", { source_id: sourceId }),
  stopSession: (id: string) => postJson<SessionOut>(`/sessions/${encodeURIComponent(id)}/stop`),

  visitors: (query: VisitorQuery, signal?: AbortSignal) => {
    const params = new URLSearchParams({
      limit: String(query.limit),
      offset: String(query.offset),
      sort: query.sort,
      order: query.order,
    });
    if (query.q?.trim()) params.set("q", query.q.trim());
    return getJson<VisitorList>(`/visitors?${params.toString()}`, signal);
  },
  visitor: (id: string, signal?: AbortSignal) =>
    getJson<VisitorDetail>(`/visitors/${encodeURIComponent(id)}`, signal),
  visitorImage: (id: string, signal?: AbortSignal) =>
    fetchImageBlob(`/visitors/${encodeURIComponent(id)}/image`, signal),
  async deleteVisitor(id: string): Promise<void> {
    const response = await send(`/visitors/${encodeURIComponent(id)}`, undefined, { method: "DELETE" });
    if (!response.ok) throw await toHttpError(response);
  },
  recognitionStatus: (signal?: AbortSignal) =>
    getJson<RecognitionStatus>("/recognition/status", signal),

  events: (query: EventQuery, signal?: AbortSignal) => {
    const params = new URLSearchParams({
      limit: String(query.limit),
      offset: String(query.offset),
      sort: query.sort,
      order: query.order,
    });
    if (query.type) params.set("type", query.type);
    if (query.visitor?.trim()) params.set("visitor", query.visitor.trim());
    if (query.since) params.set("since", query.since);
    if (query.until) params.set("until", query.until);
    return getJson<EventList>(`/events?${params.toString()}`, signal);
  },
  event: (id: string, signal?: AbortSignal) =>
    getJson<VisitEventRecord>(`/events/${encodeURIComponent(id)}`, signal),
  eventImage: (id: string, signal?: AbortSignal) =>
    fetchImageBlob(`/events/${encodeURIComponent(id)}/image`, signal),
  visit: (id: string, signal?: AbortSignal) =>
    getJson<VisitDetailRecord>(`/visits/${encodeURIComponent(id)}`, signal),
  eventsHealth: (signal?: AbortSignal) => getJson<EventsHealth>("/events/health", signal),

  analyticsOverview: (scope: AnalyticsScope, signal?: AbortSignal) =>
    getJson<AnalyticsOverview>(`/analytics/overview${scopeParams(scope)}`, signal),
  analyticsTimeline: (scope: AnalyticsScope, bucket: "hour" | "day", signal?: AbortSignal) =>
    getJson<AnalyticsTimeline>(`/analytics/timeline${scopeParams(scope, { bucket })}`, signal),
  analyticsSources: (scope: AnalyticsScope, signal?: AbortSignal) =>
    getJson<{ scope: AnalyticsScope; items: SourceStat[] }>(`/analytics/sources${scopeParams(scope)}`, signal),
  analyticsSessions: (scope: AnalyticsScope, limit = 10, signal?: AbortSignal) =>
    getJson<AnalyticsSessions>(`/analytics/sessions${scopeParams(scope, { limit: String(limit) })}`, signal),
  analyticsCoverage: (scope: AnalyticsScope, signal?: AbortSignal) =>
    getJson<AnalyticsCoverage>(`/analytics/coverage${scopeParams(scope)}`, signal),
  analyticsRecent: (scope: AnalyticsScope, limit = 8, signal?: AbortSignal) =>
    getJson<EventList>(`/analytics/recent${scopeParams(scope, { limit: String(limit) })}`, signal),
  analyticsReconciliation: (signal?: AbortSignal) =>
    getJson<AnalyticsReconciliation>("/analytics/reconciliation", signal),
  analyticsTicket: () => postJson<{ ticket: string; expires_in: number }>("/analytics/stream-ticket"),

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
