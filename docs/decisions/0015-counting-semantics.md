# ADR 0015 – Counting semantics and analytics architecture

Status: accepted (Phase 5).

## Decision
1. **Identities, not people.** The primary metric is `COUNT(DISTINCT visits.visitor_id)` (in scope) and `COUNT(visitors)` (global). Unresolved tracks never add to it; they are a labelled coverage diagnostic. Real-world person accuracy is claimed only where identity labels justify it (an LFW-based set), never for the hackathon footage.
2. **One scope for everything visit-derived**: visits selected by entry time, half-open `[since, until)` in UTC, plus source/session. Globally-scoped (registered) and live (active/stale) numbers are labelled in the UI.
3. **Returning** = a visitor in scope with a visit in a session other than the one that registered them. Not a second track, not a second visit in the same session.
4. **Terminal events stay distinguishable** (`absence`, `observation_ended`, `interrupted`); none is called a departure. **Active** means open visits of the live session; open rows of non-live sessions are *stale*, reported, never counted.
5. **Erasure** removes the identity from registered/unique counts; de-identified visits and events stay counted (`visits_without_identity`). No biometric data is kept to preserve numbers.
6. **PostgreSQL is the only source of numbers.** Aggregation is SQL. Live updates are *notices* (`analytics_changed`) that make the browser refetch; no count travels on the socket, so nothing can drift. No Redis, no analytics store, no migration: the measured cost is linear and small at the project's scale (see benchmark); rollups are the documented next step if all-time views over ≫ 100 k visits are needed.
7. **Reconciliation is report-only.** An independent recount compares with the aggregates and checks invariants; it never repairs.

## Consequences
Unique counts can fall after an erasure; returning is under-counted when a first appearance was unresolved; per-bucket unique visitors are not additive. All stated in the API definitions and the UI.
