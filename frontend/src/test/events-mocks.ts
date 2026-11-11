import type {
  VisitDetailRecord,
  VisitEventMessage,
  VisitEventRecord,
  VisitRecord,
} from "@/lib/api-types";

const SID = "5".repeat(32);

export const visitorRef = (n: number) => ({
  id: n.toString(16).padStart(32, "0"),
  number: n,
  label: `V${String(n).padStart(5, "0")}`,
});

export const visitId = (n: number) => `c${n.toString(16).padStart(31, "0")}`;
export const eventId = (n: number, slot: "e" | "x") => `${slot}${n.toString(16).padStart(31, "0")}`;

export function entryEvent(n: number, over: Partial<VisitEventRecord> = {}): VisitEventRecord {
  return {
    id: eventId(n, "e"),
    visit_id: visitId(n),
    type: "entry",
    visitor: visitorRef(n),
    session_id: SID,
    source_id: "hall",
    source_label: "Hall camera",
    event_time: `2026-11-10T10:${String(n).padStart(2, "0")}:05Z`,
    video_ms: 1000,
    recorded_at: `2026-11-10T10:${String(n).padStart(2, "0")}:07Z`,
    image_status: "STORED",
    has_image: true,
    image_kind: "observation",
    persistence_status: "DELIVERED",
    exit_basis: null,
    closure_reason: null,
    ...over,
  };
}

export function exitEvent(n: number, over: Partial<VisitEventRecord> = {}): VisitEventRecord {
  return {
    ...entryEvent(n),
    id: eventId(n, "x"),
    type: "exit",
    event_time: `2026-11-10T10:${String(n).padStart(2, "0")}:55Z`,
    video_ms: 51_000,
    image_kind: "last_good_observation",
    exit_basis: "absence",
    closure_reason: "absence_timeout",
    ...over,
  };
}

export function visitFixture(n: number, over: Partial<VisitRecord> = {}): VisitRecord {
  return {
    id: visitId(n),
    visitor: visitorRef(n),
    session_id: SID,
    source_id: "hall",
    source_label: "Hall camera",
    state: "CLOSED",
    entry_time: entryEvent(n).event_time,
    last_observed_time: exitEvent(n).event_time,
    exit_time: exitEvent(n).event_time,
    exit_confirmed_at: `2026-11-10T10:${String(n).padStart(2, "0")}:58Z`,
    exit_basis: "absence",
    closure_reason: "absence_timeout",
    track_segments: 1,
    duration_seconds: 50,
    integrity: "complete",
    ...over,
  };
}

export const visitDetail = (n: number, over: Partial<VisitDetailRecord> = {}): VisitDetailRecord => ({
  ...visitFixture(n),
  events: [entryEvent(n), exitEvent(n)],
  ...over,
});

export const liveEvent = (
  n: number,
  type: "entry" | "exit" | "interrupted" = "entry",
  over: Partial<VisitEventMessage["event"]> = {},
): VisitEventMessage => ({
  type: "visit_event",
  event: {
    id: eventId(n, type === "entry" ? "e" : "x"),
    visit_id: visitId(n),
    type,
    visitor: visitorRef(n),
    event_time: "2026-11-10T10:00:05Z",
    session_id: SID,
    source_label: "Hall camera",
    image_status: "STORED",
    persistence_status: "DELIVERED",
    exit_basis: type === "entry" ? null : type === "interrupted" ? "interrupted" : "absence",
    closure_reason: null,
    ...over,
  },
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

export interface EventsController {
  events: VisitEventRecord[];
  visits: Record<string, VisitDetailRecord>;
  list: "ok" | "error";
  visit: "ok" | "error";
  image: "ok" | "missing";
  calls: string[];
}

export function createEvents(n: number, over: Partial<EventsController> = {}): EventsController {
  const events: VisitEventRecord[] = [];
  const visits: Record<string, VisitDetailRecord> = {};
  for (let i = 1; i <= n; i++) {
    events.push(entryEvent(i), exitEvent(i));
    visits[visitId(i)] = visitDetail(i);
  }
  return { events, visits, list: "ok", visit: "ok", image: "ok", calls: [], ...over };
}

/** Layers /events and /visits over the base mock. Filtering/sorting mirrors the backend contract. */
export function eventsFetch(c: EventsController) {
  return (base: typeof fetch): typeof fetch =>
    (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      const path = url.pathname.replace(/^.*\/v1/, "");
      if (!path.startsWith("/events") && !path.startsWith("/visits")) return base(input, init);
      c.calls.push(`${init?.method ?? "GET"} ${path}${url.search}`);
      const err = (status: number, code: string, message: string) =>
        json({ error: { code, message, request_id: "r1", details: null } }, status);

      if (path === "/events") {
        if (c.list === "error") return err(500, "internal_error", "list boom");
        const p = url.searchParams;
        const limit = Number(p.get("limit") ?? 25);
        const offset = Number(p.get("offset") ?? 0);
        const type = p.get("type");
        const visitor = (p.get("visitor") ?? "").toLowerCase().replace(/^v0*/, "");
        const since = p.get("since");
        const until = p.get("until");
        let rows = c.events.filter(
          (e) =>
            (!type || e.type === type) &&
            (!visitor || String(e.visitor?.number) === visitor) &&
            (!since || e.event_time >= since) &&
            (!until || e.event_time <= until),
        );
        rows = [...rows].sort((a, b) => (a.event_time < b.event_time ? -1 : a.event_time > b.event_time ? 1 : 0));
        if ((p.get("order") ?? "desc") === "desc") rows.reverse();
        return json({ items: rows.slice(offset, offset + limit), total: rows.length, limit, offset });
      }
      const image = path.match(/^\/events\/([^/]+)\/image$/);
      if (image) {
        const ev = c.events.find((e) => e.id === image[1]);
        if (c.image === "missing" || !ev?.has_image) return err(404, "image_not_found", "No image.");
        return new Response(new Blob([new Uint8Array([0xff, 0xd8, 1, 2])], { type: "image/jpeg" }), { status: 200 });
      }
      const visit = path.match(/^\/visits\/([^/]+)$/);
      if (visit) {
        if (c.visit === "error") return err(500, "internal_error", "visit boom");
        const v = c.visits[visit[1]!];
        return v ? json(v) : err(404, "visit_not_found", "No such visit.");
      }
      return err(404, "not_found", "Not Found");
    }) as typeof fetch;
}
