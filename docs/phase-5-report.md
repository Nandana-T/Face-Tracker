# Phase 5 report – unique visitor counting and analytics

Status: implemented and verified as described below; **awaiting approval**. Nothing is committed or pushed. **The hosted Supabase database is still at revision `0002`; nothing was written to it, and Phase 5 needs no new migration** (it reads the tables of `0003`/`0004`). Phase 6 was not started.
Measurements: [benchmarks/phase5-analytics.md](benchmarks/phase5-analytics.md). Decision record: [ADR 0015](decisions/0015-counting-semantics.md).

## 0. The one-sentence truth
**Registered identities are counted exactly. Physical people are not.** Every figure below is a count of face *identities*, visits or events held in PostgreSQL; none is a count of real-world persons, and faces the recognition gate never resolved are never added to a unique count. Real-world accuracy was measured only on a labelled LFW-based set (§8), not on the hackathon footage, which has no person-level labels.

## 1. Counting definitions (exact)
| Term | Definition | Scope |
|---|---|---|
| **Registered visitors** | `COUNT(*)` of `visitors` rows – persistent face identities | **Global**, ignores every filter |
| **Unique visitors (in scope)** | `COUNT(DISTINCT visits.visitor_id)` over the visits in scope (NULL – erased – excluded) | Follows the filters |
| **Visits** | `COUNT(*)` of `visits` in scope. One presence episode of one visitor in one session; a visitor can have many | Filters |
| **Entry events** | logical entry events of the visits in scope (one per visit; `UNIQUE(visit_id, slot)`) | Filters |
| **Terminal events** | logical ends of the visits in scope, never merged: `absence` (left the view), `observation_ended` (video ended / monitoring stopped), `interrupted` (worker/API/source failed). **None is a verified physical departure** | Filters |
| **Returning visitors** | unique visitors in scope with ≥ 1 in-scope visit in a **session different from the one in which they were first registered** (`visitors.first_session_id`). A second track or a second visit in the registering session does **not** count; new = unique − returning | Filters |
| **Active visits** | open visits (ACTIVE / TEMPORARILY_MISSING / EXIT_PENDING) of the **live** session. Temporarily-missing ones are reported separately but still counted (the visit is open) | Live, source/session filters only |
| **Stale open visits** | open rows of sessions that are **not** live (crash not yet reconciled). Reported, **never** counted as present | Live |
| **Unresolved tracks** | tracks with ≥ 1 recognition observation that ended without a resolved identity – a **historical coverage diagnostic**, never additional people | Sessions in scope |

**Scope.** `since` inclusive, `until` exclusive (half-open `[since, until)`, UTC) on the visit's **entry time**; `source_id` / `session_id` narrow by the visit's source/session. Timestamps are stored in UTC; any ISO input with an offset is converted to the same instant; the browser converts for display only (hourly ticks local, day buckets UTC dates, both labelled). Every visit-derived number, chart and table uses this one scope; globally-scoped and live numbers carry a visible badge (*all time · ignores filters*, *right now · ignores dates*) so they cannot be mistaken for filtered ones.
**Example.** A visitor appears, leaves and returns in a later session ⇒ visitor identities 1, visits 2, entry events 2, terminal events 2 (per their closure basis), returning visitors 1 (in a scope that contains the later visit).
**Erasure policy.** Erasing a visitor deletes the `visitors` row; their visits/events remain de-identified (`visitor_id` NULL, evidence deleted). The visitor therefore leaves *registered* and *unique* counts – no identity or biometric data is retained to preserve dashboard numbers – but their visits, entries and terminal events stay counted, and `visits_without_identity` (shown in the UI) says how many visits are no longer attributable. Unique counts can therefore **fall** after an erasure; this is intended and tested.

## 2. Database aggregation
`services/analytics.py`: SQL aggregates only (`COUNT`, `COUNT(DISTINCT)`, grouped counts, `date_trunc`/`to_char` UTC buckets on PostgreSQL, `strftime` on SQLite for unit tests); no records are loaded to count; no per-frame work. Overview = 5 statements (registered; visits/unique/de-identified in one pass; events grouped by slot/type/basis in one pass; returning join; open visits grouped by session) plus the coverage diagnostic. Timeline = two grouped queries (entries/visits/unique by entry bucket; terminal events by their own bucket), empty buckets filled in Python, capped at 1000 newest buckets (flagged `truncated`). Sources and sessions = grouped/outer-joined aggregates, sessions paged with a stable order (`created_at DESC, id`). Existing indexes (`visits.visitor_id/session_id/entry_time/state`) suffice at the target scale; **no migration, no Redis, no separate store** (numbers in §9). Coverage sums a few counters from the session `summary` JSON (≤ 5000 sessions) in Python – a documented exception.

## 3. API (all authenticated, validated, no paths/embeddings/secrets)
`GET /analytics/overview|timeline|sources|sessions|coverage|recent|reconciliation`, `POST /analytics/stream-ticket`, WebSocket `/analytics/stream`. Parameters: `since`, `until` (ISO, naive = UTC; `since ≥ until` → 422), `source_id` (charset-restricted), `session_id` (32 hex), `bucket=hour|day`, `limit` 1–100, `offset`. Responses are typed Pydantic models and carry their definitions. Details: [api-contracts.md](api-contracts.md).

## 4. Real-time architecture
`event → notice → refetch`. When a visitor is registered, a visit event is committed, a visit changes state or a session starts/ends, the API pushes `{"type":"analytics_changed","reason":…}` to subscribers of `/analytics/stream`. **The notice carries no number.** The browser debounces (300 ms) and invalidates its TanStack queries, so every figure is re-read from PostgreSQL through the REST API – there is no second counter that can drift. Safety nets: refetch after every (re)connect, a 20 s poll, exponential reconnect (1–10 s), and an explicit *Live updates off* indicator. The socket needs a single-use 30 s ticket bound to the string `analytics` (a *session* ticket cannot open it and vice versa) plus the Origin check; no frames flow on it. Late joiners and refreshes need no replay – they simply read the database.

## 5. Frontend
* **Overview:** the four existing cards now show real values (*Unique visitors* = registered identities, *Currently present* = active visits of the live session, *Entries*, *Exits* = left the view + observation ended, with interrupted visits explicitly excluded), a second row (visits, returning, unresolved-tracks diagnostic) and a real recent-activity list; dashes and an honest notice when the backend is down.
* **Analytics:** a visible counting note ("distinct registered face identities … not a count of physical people"); filters (period presets and custom range with an inverted-range guard, source, session, chart interval, reset); eight metric tiles with scope badges; *Visitor activity* chart (unique visitors ↔ visits); *Entries and exits* chart with the three terminal kinds distinguishable; new-vs-returning composition; source and session tables; **recognition-coverage diagnostics** with its disclaimer; recent activity; a read-only **Data consistency** check.
* Charts are dependency-free SVG (no new package): values are exactly the API's, no smoothing, no count-up (tiles and bars only fade), each chart also renders a visually hidden data table; Motion respects reduced motion (`MotionConfig`). States: loading skeletons, empty database, filter without results, per-panel errors with retry, backend down (no numbers rather than stale ones), *Live updates off*.

## 6. Recognition-coverage diagnostics
Shown next to – never inside – the unique count: confirmed tracks, tracks evaluated, resolved / unresolved (low quality, undecided, registration pending, ambiguous, rejected), crops analysed / usable / rejected by the quality gate / dropped, recognition errors, and the resolved share of evaluated tracks, labelled **"operational diagnostic: not person-level recall; tracks are not people"**. On the Phase 4 real-footage run the picture was: 74 confirmed tracks over 6 clips, 9 of them resolved to a registered identity (≈ 12 %); most faces fail the unchanged quality gate. That percentage says how often the pipeline *committed to an identity*, not how many of the people present it found.

## 7. Count reconciliation
`AnalyticsService.reconcile()` (also `GET /analytics/reconciliation`, the **Data consistency** panel and `python scripts/reconcile_counts.py [--json]`) recomputes the global numbers independently from raw rows in Python and compares them with the SQL aggregates, and checks invariants: registered / unique / visits / entries / terminal counts; one entry and the right terminal events per visit; events without a visit; foreign keys; duplicate idempotency keys; terminal type ↔ closure basis; `exit_time` set exactly for final visits; erased visitors keep no evidence reference; stale open visits (warning, **not** counted as active); live-session consistency; pending outbox; identities without embeddings (warning). **Report-only: it never modifies data and has no repair mode** (event-level repairs stay explicit in `scripts/verify_events_log.py --repair`).
Result on the real local database after the Phase 4/5 end-to-end runs: 20 visits (19 of erased test visitors, de-identified), 1 registered identity, all checks passed (0 errors, 0 warnings). A deliberately corrupted database is detected, not repaired (tests).

## 8. Ground-truth evaluation (LFW identities through the real pipeline)
Labels = LFW folders (verified identities), a different photograph per appearance. **Synthetic video** (still photographs, full frame; pairs side by side; small faces ≈ ¼ size; a mask-like patch for "occluded"), real detector/tracker/landmarks/SFace/identity manager/visit lifecycle/analytics on PostgreSQL. 23 clips, 14 people, 25 labelled appearances:

| Metric | Result |
|---|---|
| Ground-truth unique people | 14 |
| Registered identities | 14 (**absolute error 0 – but see below**) |
| People correctly registered (exactly one identity, no merge) | 12 |
| People missed | 1 (an occluded person, never resolved in 2 appearances) |
| Duplicate registrations | 1 (a small-face person registered twice) |
| Incorrect merges (one identity ↔ two people) | **0** |
| Appearances resolved to an identity | normal 20/21, small 2/2, occluded 0/2 |
| Ground-truth returning people / analytics returning visitors | 11 / 8 |
| Visits vs labelled appearances | 22 vs 25 |
| Reconciliation | consistent |

**Read this carefully:** the count error is 0 only because one miss and one duplicate cancel each other – the *individual* error rate is 2 of 14 people. Returning is under-counted (8 of 11) because a person whose first appearance was not resolved is later counted as *new*, and a duplicated person never counts as returning. No wrong merge occurred. These numbers are optimistic for surveillance video (clean photographs, no motion, no crowds, a patch instead of a real mask) and **must not be quoted as the accuracy on the hackathon footage**, for which no person-level labels exist.

## 9. Performance (synthetic rows on an isolated local PostgreSQL schema; never the hosted DB)
| Visits (events ×2) | Overview all-time | Overview date range | Timeline (day) | Sources | Full reconciliation recount |
|---|---|---|---|---|---|
| 1,000 | 11 ms | 6 ms | 7 ms | 4 ms | 55 ms |
| 10,000 | 72 ms | 46 ms | 55 ms | 46 ms | 246 ms |
| 100,000 | 664 ms | 121 ms | 670 ms | 662 ms | 2.1 s |
Linear growth, no index problem; date/session-filtered views are 5–15× cheaper than all-time at 100 k. The full dashboard issues ≈ 6 reads (≈ 0.25 s at 10 k, ≈ 2.5 s all-time at 100 k); at the project's scale (tens to hundreds of visits) every query is a few milliseconds. If all-time views over ≫ 100 k visits are ever needed, add pre-aggregated daily rollups – decided by these numbers, not before. Other measurements: notice delivery after the commit **5.3 ms** in-process (TestClient; network extra), dashboard first figure after navigation **1046 (first load right after an application restart) ms** on the local stack (real Playwright), Python memory growth ≤ 5 MB while querying 100 k visits, the live socket carries no frames and a few bytes per notice (no measurable CPU). Not measured: hosted-database latency, many simultaneous dashboards.

## 10. Tests (all executed)
| Layer | Result |
|---|---|
| Backend (pytest; PostgreSQL where marked) | **538 passed** (Phase 4: 499) |
| Frontend (Vitest) | **245 passed** (Phase 4: 219; the "/analytics placeholder" row was replaced by real tests) |
| Playwright (Edge, real stack and models, authenticated) | **30 passed** (Phase 4: 28) |
| **Total** | **813** (baseline 746) |
ruff, ruff format, mypy (strict), ESLint, `tsc`, Vite build clean.
New: `test_analytics.py` (15, **real PostgreSQL**: empty database; one visitor with many tracks and visits; returning vs second track/visit; simultaneous visitors; interrupted / observation-ended; live vs stale open visits; erasure; half-open UTC and timezone boundaries; source/session filters; timeline = overview; sessions/sources; coverage never adds visitors; reconciliation detects corruption without repairing; restart + retried events do not inflate; randomised independent recount), `test_analytics_api.py` (24: auth on every route, validation, filters consistent across endpoints, no paths/embeddings, ticket scoping, live notices without counts, notice latency), `analytics.test.tsx` (27: tiles and scopes, counting note, coverage disclaimer, empty/filtered-empty, charts + table, metric switch, composition, filters on every request, inverted range, backend failure + retry, isolated panel errors, live refetch, malformed notice / offline / reconnect / no ticket, consistency check, keyboard, overview page); **2 real-model Playwright tests**: (1) controlled workflow – register A ⇒ 1 identity/1 visit; process A again ⇒ still 1 identity, 2 visits, returning 1; process B ⇒ 2 identities; dashboard = API; session filter; browser refresh; replayed reconciliation changes nothing; full restart rebuilds the same numbers from PostgreSQL; read-only consistency check passes; (2) the open dashboard updates by itself while a session runs. **Bugs found by this work:** none in earlier phases; one design correction – the first draft of the E2E assumed a clean `visits` table, but erased visitors' visits legitimately remain (de-identified), so assertions are deltas.
Not covered: real RTSP, GPU, hosted database, many concurrent dashboards, person-level accuracy on the hackathon footage.

## 11. Database and migration status
No new migration. Local PostgreSQL is at `0004`; **Supabase is at `0002`; `0003` and `0004` are not applied.** Before any hosted write (nothing was done): verify the Alembic chain (`alembic heads` → one head `0004`, done locally), re-read [migration-0004-preflight.md](migration-0004-preflight.md) (additive, no existing data touched), take a recoverable backup (Supabase point-in-time recovery / `pg_dump`), confirm TLS (`sslmode=require`; the migrate script checks it) and the destination project/host, and obtain explicit approval. Do not weaken test isolation: tests refuse hosted URLs.

## 12. Remaining accuracy limitations
1. Unique visitors = registered **identities**, not people: unrecognised faces (quality gate), masks, look-alikes, duplicate registrations (§8: 1 of 14) and misses (1 of 14) all make it differ from the true number; the sign and size on real footage are unknown. 2. Returning is under-counted when a first appearance was unresolved. 3. Exit/terminal events are last sightings or observation ends, not departures. 4. Visits are only as good as identities. 5. The labelled evaluation is synthetic and optimistic. 6. Erasure lowers unique counts by design. 7. Coverage percentages are operational diagnostics.

## 13. Deployment blockers
Hosted migrations `0003`+`0004` unapplied (approval); no real-RTSP test; model-licence stance (ADR 0013); single API process owns the visit lifecycle; one shared admin role / no audit trail; Render/Vercel/Docker unexercised; all-time analytics at ≫ 100 k visits would need rollups.

## 14. Acceptance verdict
**Accepted at hackathon scope.** Every item of the Definition of Done is met except the ones that cannot honestly be: *ground-truth accuracy is not overstated* – it is bounded to the LFW-based set above and flagged everywhere; *person counting accuracy on the hackathon footage* – unmeasurable without labels; the hosted migration – deliberately not applied.

## 15. Readiness for Phase 6 (deployment, RTSP validation, final submission)
Ready to start once you decide: (a) apply `0003`+`0004` to Supabase (checklist §11), (b) label a few real clips if you want an accuracy statement, (c) provide a real RTSP source for the reconnect rules that are only unit-tested, (d) accept the model-licence stance. Phase 6 was not started.
