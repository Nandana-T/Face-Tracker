import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { analyticsStreamUrl, api } from "@/lib/api";
import type { AnalyticsScope } from "@/lib/api-types";

export type LiveState = "connecting" | "live" | "offline";

const BACKOFF_MS = [1000, 2000, 5000, 10000];
const DEBOUNCE_MS = 300;
/** Authoritative poll: even a missed notice converges on the database within this time. */
export const POLL_MS = 20_000;

const keyOf = (scope: AnalyticsScope) => [scope.since ?? "", scope.until ?? "", scope.source_id ?? "", scope.session_id ?? ""];

export const analyticsKeys = {
  all: ["analytics"] as const,
  overview: (s: AnalyticsScope) => ["analytics", "overview", ...keyOf(s)] as const,
  timeline: (s: AnalyticsScope, b: string) => ["analytics", "timeline", b, ...keyOf(s)] as const,
  sources: (s: AnalyticsScope) => ["analytics", "sources", ...keyOf(s)] as const,
  sessions: (s: AnalyticsScope) => ["analytics", "sessions", ...keyOf(s)] as const,
  coverage: (s: AnalyticsScope) => ["analytics", "coverage", ...keyOf(s)] as const,
  recent: (s: AnalyticsScope) => ["analytics", "recent", ...keyOf(s)] as const,
};

export function useOverview(scope: AnalyticsScope) {
  return useQuery({
    queryKey: analyticsKeys.overview(scope),
    queryFn: ({ signal }) => api.analyticsOverview(scope, signal),
    refetchInterval: POLL_MS,
  });
}

/**
 * Listens to the analytics change stream and refetches. The stream carries NOTICES only ("something
 * changed"), never counts: every number on screen comes from the database through the REST API, so
 * there is no second counter that could drift. A reconnect refetches too, and the poll above is the
 * safety net.
 */
export function useAnalyticsLive(enabled = true): LiveState {
  const client = useQueryClient();
  const [state, setState] = useState<LiveState>("connecting");
  const attempt = useRef(0);

  useEffect(() => {
    if (!enabled) return;
    let closed = false;
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let debounce: ReturnType<typeof setTimeout> | null = null;

    const refresh = () => {
      if (debounce) clearTimeout(debounce);
      debounce = setTimeout(() => void client.invalidateQueries({ queryKey: analyticsKeys.all }), DEBOUNCE_MS);
    };
    const connect = async () => {
      if (closed) return;
      setState("connecting");
      try {
        const { ticket } = await api.analyticsTicket();
        if (closed) return;
        ws = new WebSocket(analyticsStreamUrl(ticket));
      } catch {
        schedule();
        return;
      }
      ws.onopen = () => {
        attempt.current = 0;
        setState("live");
        refresh(); // reconcile with the database after (re)connecting
      };
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(String(e.data)) as { type?: string };
          if (msg.type === "analytics_changed") refresh();
        } catch {
          /* ignore malformed notices */
        }
      };
      ws.onclose = () => {
        if (!closed) schedule();
      };
      ws.onerror = () => ws?.close();
    };
    const schedule = () => {
      setState("offline");
      const delay = BACKOFF_MS[Math.min(attempt.current++, BACKOFF_MS.length - 1)]!;
      retry = setTimeout(() => void connect(), delay);
    };
    void connect();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      if (debounce) clearTimeout(debounce);
      ws?.close();
    };
  }, [client, enabled]);

  return state;
}
