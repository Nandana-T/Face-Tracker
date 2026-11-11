import type {
  AnalyticsCoverage,
  AnalyticsOverview,
  AnalyticsReconciliation,
  AnalyticsSessions,
  AnalyticsTimeline,
  SourceStat,
  TimelinePoint,
} from "@/lib/api-types";
import { entryEvent, exitEvent } from "./events-mocks";

export const overviewFixture = (over: Partial<AnalyticsOverview> = {}): AnalyticsOverview => ({
  scope: {},
  registered_visitors: 5,
  unique_visitors_in_scope: 4,
  returning_visitors_in_scope: 1,
  new_visitors_in_scope: 3,
  visits: 7,
  visits_without_identity: 0,
  entry_events: 7,
  terminal_events: 6,
  terminal_by_basis: { absence: 3, observation_ended: 2, interrupted: 1 },
  active_visits: 1,
  active_visits_temporarily_missing: 0,
  stale_open_visits: 0,
  unresolved_tracks: 9,
  generated_at: "2026-11-11T12:00:00.000+00:00",
  last_event_recorded_at: "2026-11-11T11:59:00+00:00",
  definitions: { unique_visitors_in_scope: "Distinct registered identities with at least one visit entering in scope." },
  ...over,
});

export const pointFixture = (hour: number, over: Partial<TimelinePoint> = {}): TimelinePoint => ({
  bucket_start: `2026-11-10T${String(hour).padStart(2, "0")}:00:00Z`,
  visits: 2,
  entries: 2,
  unique_visitors: 1,
  exits_absence: 1,
  exits_observation_ended: 1,
  interrupted: 0,
  ...over,
});

export const timelineFixture = (points: TimelinePoint[], bucket: "hour" | "day" = "hour"): AnalyticsTimeline => ({
  scope: {},
  bucket,
  timezone: "UTC",
  truncated: false,
  points,
  note: "Buckets are UTC.",
});

export const coverageFixture = (over: Partial<AnalyticsCoverage> = {}): AnalyticsCoverage => ({
  scope: {},
  sessions_in_scope: 3,
  confirmed_tracks: 74,
  tracks_with_recognition_observations: 20,
  tracks_resolved: 9,
  tracks_unresolved: 11,
  tracks_by_state: {},
  unresolved_breakdown: { low_quality: 6, undecided: 2, registration_pending: 0, ambiguous: 2, rejected: 1 },
  resolved_share_of_evaluated_tracks: 0.45,
  face_crops_analysed: 200,
  face_crops_usable: 60,
  face_crops_rejected_by_quality_gate: 140,
  face_crops_dropped_busy: 0,
  recognition_errors: 0,
  disclaimer:
    "Operational diagnostic: the share of evaluated tracks that ended with a resolved identity. It is not person-level recall, tracks are not people, and unresolved tracks are never added to unique visitors.",
  ...over,
});

export const sourceFixture = (id: string, visits = 4): SourceStat => ({
  source_id: id,
  source_label: id.toUpperCase(),
  visits,
  unique_visitors: 2,
  sessions: 1,
  last_entry_at: "2026-11-10T10:00:00Z",
});

export const reconcileFixture = (over: Partial<AnalyticsReconciliation> = {}): AnalyticsReconciliation => ({
  consistent: true,
  errors: 0,
  warnings: 0,
  checks: [],
  mode: "report-only: no data was modified",
  duration_ms: 12,
  generated_at: "2026-11-11T12:00:00+00:00",
  ...over,
});

export interface AnalyticsController {
  overview: AnalyticsOverview;
  timeline: AnalyticsTimeline;
  coverage: AnalyticsCoverage;
  sources: SourceStat[];
  sessions: AnalyticsSessions;
  reconcile: AnalyticsReconciliation;
  /** "error" = every call of that kind answers 500. */
  fail: Partial<Record<"overview" | "timeline" | "coverage" | "sources" | "sessions" | "recent" | "reconcile" | "ticket", boolean>>;
  calls: string[];
}

export function createAnalytics(over: Partial<AnalyticsController> = {}): AnalyticsController {
  return {
    overview: overviewFixture(),
    timeline: timelineFixture([pointFixture(9), pointFixture(10, { visits: 3, entries: 3, unique_visitors: 2 }), pointFixture(11)]),
    coverage: coverageFixture(),
    sources: [sourceFixture("hall"), sourceFixture("gate", 3)],
    sessions: {
      scope: {},
      total: 2,
      limit: 8,
      offset: 0,
      note: "",
      items: [
        { session_id: "a".repeat(32), source_id: "hall", source_label: "Hall", state: "RUNNING", live: true, started_at: "2026-11-10T10:00:00Z", ended_at: null, visits: 4, unique_visitors: 2, interrupted_visits: 1, visitors_registered_here: 2 },
        { session_id: "b".repeat(32), source_id: "gate", source_label: "Gate", state: "COMPLETED", live: false, started_at: "2026-11-09T10:00:00Z", ended_at: null, visits: 3, unique_visitors: 2, interrupted_visits: 0, visitors_registered_here: 1 },
      ],
    },
    reconcile: reconcileFixture(),
    fail: {},
    calls: [],
    ...over,
  };
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Layers the analytics endpoints over the base mock. */
export function analyticsFetch(c: AnalyticsController) {
  return (base: typeof fetch): typeof fetch =>
    (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      const path = url.pathname.replace(/^.*\/v1/, "");
      if (!path.startsWith("/analytics")) return base(input, init);
      c.calls.push(`${init?.method ?? "GET"} ${path}${url.search}`);
      const name = path.split("/")[2] as keyof AnalyticsController["fail"] | "stream-ticket" | "reconciliation";
      const key = name === "stream-ticket" ? "ticket" : name === "reconciliation" ? "reconcile" : name;
      if (c.fail[key as keyof AnalyticsController["fail"]]) {
        return json({ error: { code: "internal_error", message: `${name} boom`, request_id: "r1", details: null } }, 500);
      }
      switch (name) {
        case "overview":
          return json(c.overview);
        case "timeline":
          return json({ ...c.timeline, bucket: url.searchParams.get("bucket") ?? c.timeline.bucket });
        case "coverage":
          return json(c.coverage);
        case "sources":
          return json({ scope: {}, items: c.sources });
        case "sessions":
          return json(c.sessions);
        case "reconciliation":
          return json(c.reconcile);
        case "recent":
          return json({ items: [exitEvent(1), entryEvent(1)], total: 2, limit: 8, offset: 0 });
        case "stream-ticket":
          return json({ ticket: "t1", expires_in: 30 });
        default:
          return json({ error: { code: "not_found", message: "Not Found", request_id: null, details: null } }, 404);
      }
    }) as typeof fetch;
}
