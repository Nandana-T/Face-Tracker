import type {
  IdentityEventMessage,
  Visitor,
  VisitorAssociation,
  VisitorDetail,
} from "@/lib/api-types";

export const visitorFixture = (n: number, over: Partial<Visitor> = {}): Visitor => ({
  id: n.toString(16).padStart(32, "0"),
  number: n,
  label: `V${String(n).padStart(5, "0")}`,
  status: "REGISTERED",
  first_seen_at: "2026-11-10T10:00:00Z",
  last_seen_at: "2026-11-10T12:30:00Z",
  recognition_count: 2,
  has_image: true,
  model_version: "sface-2021dec",
  ...over,
});

export const associationFixture = (over: Partial<VisitorAssociation> = {}): VisitorAssociation => ({
  session_id: "s".repeat(32),
  source_label: "Hall camera",
  track_id: 7,
  state: "REGISTERED",
  best_similarity: null,
  observations: 5,
  conflicts: 0,
  registered_in_session: true,
  first_seen_at: "2026-11-10T10:00:00Z",
  last_seen_at: "2026-11-10T10:05:00Z",
  ...over,
});

export const detailFixture = (v: Visitor, over: Partial<VisitorDetail> = {}): VisitorDetail => ({
  ...v,
  first_session_id: "s".repeat(32),
  image_quality: 0.72,
  sessions_count: 2,
  tracks_count: 2,
  associations: [associationFixture(), associationFixture({ track_id: 3, state: "RECOGNIZED", registered_in_session: false, best_similarity: 0.83, conflicts: 1 })],
  ...over,
});

export const identityEvent = (over: Partial<IdentityEventMessage> = {}): IdentityEventMessage => ({
  type: "identity",
  session_id: "a".repeat(32),
  track_id: 1,
  state: "REGISTERED",
  event: "registered",
  visitor: { id: "b".repeat(32), number: 124, label: "V00124" },
  similarity: null,
  reason: null,
  ...over,
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

export interface VisitorsController {
  visitors: Visitor[];
  /** "error" = every list call fails; "recognition_disabled" = 503 like the real API. */
  list: "ok" | "error";
  detail: "ok" | "error";
  image: "ok" | "missing";
  deleted: string[];
  calls: string[];
}

export function createVisitors(n: number, over: Partial<VisitorsController> = {}): VisitorsController {
  return {
    visitors: Array.from({ length: n }, (_, i) => visitorFixture(i + 1)),
    list: "ok",
    detail: "ok",
    image: "ok",
    deleted: [],
    calls: [],
    ...over,
  };
}

/** Layers the visitor endpoints over the base mock (health/status/config). */
export function visitorsFetch(c: VisitorsController) {
  return (base: typeof fetch): typeof fetch =>
    (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      const path = url.pathname.replace(/^.*\/v1/, "");
      if (!path.startsWith("/visitors")) return base(input, init);
      const method = init?.method ?? "GET";
      c.calls.push(`${method} ${path}${url.search}`);
      const err = (status: number, code: string, message: string) =>
        json({ error: { code, message, request_id: "r1", details: null } }, status);

      if (path === "/visitors") {
        if (c.list === "error") return err(500, "internal_error", "list boom");
        const limit = Number(url.searchParams.get("limit") ?? 24);
        const offset = Number(url.searchParams.get("offset") ?? 0);
        const q = (url.searchParams.get("q") ?? "").toLowerCase().replace(/^v/, "");
        const sort = url.searchParams.get("sort") ?? "last_seen";
        const order = url.searchParams.get("order") ?? "desc";
        let rows = c.visitors.filter((v) => !q || String(v.number) === String(Number(q)) || v.id.startsWith(q));
        const key = sort === "number" ? (v: Visitor) => v.number : sort === "first_seen" ? (v: Visitor) => v.first_seen_at : (v: Visitor) => v.last_seen_at;
        rows = [...rows].sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : a.number - b.number));
        if (order === "desc") rows.reverse();
        return json({ items: rows.slice(offset, offset + limit), total: rows.length, limit, offset });
      }
      const m = path.match(/^\/visitors\/([^/]+)(\/image)?$/);
      const visitor = m ? c.visitors.find((v) => v.id === m[1] || v.label === m[1]) : undefined;
      if (m && method === "DELETE") {
        if (!visitor) return err(404, "visitor_not_found", "No such visitor.");
        c.deleted.push(visitor.id);
        c.visitors = c.visitors.filter((v) => v.id !== visitor.id);
        return new Response(null, { status: 204 });
      }
      if (m?.[2]) {
        if (c.image === "missing" || !visitor?.has_image) return err(404, "image_not_found", "No image.");
        return new Response(new Blob([new Uint8Array([0xff, 0xd8, 1, 2])], { type: "image/jpeg" }), { status: 200 });
      }
      if (m) {
        if (c.detail === "error") return err(500, "internal_error", "detail boom");
        return visitor ? json(detailFixture(visitor)) : err(404, "visitor_not_found", "No such visitor.");
      }
      return err(404, "not_found", "Not Found");
    }) as typeof fetch;
}
