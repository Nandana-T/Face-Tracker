# Migration 0004 – preflight report

Revision `0004` (`backend/alembic/versions/0004_visits_and_events.py`, down-revision `0003`). **Not applied to the hosted Supabase database** (it is at `0002`; `0003` is also still pending there). Nothing was written to Supabase in Phase 4.

## What it does
Creates three tables, nothing else: `visits`, `visit_events`, `event_delivery_outbox` (plus their constraints, indexes and row-level security). **It does not alter, rewrite or drop any existing table or row**, so applying it to a database that holds Phase 0–3 data cannot lose data.
Foreign keys: `visits.visitor_id → visitors.id ON DELETE SET NULL` (erasing a visitor de-identifies their visits instead of deleting the audit trail), `visits.session_id → processing_sessions.id ON DELETE CASCADE`, `visit_events.visit_id → visits.id CASCADE`, `visit_events.visitor_id → visitors.id SET NULL`, `event_delivery_outbox.event_id → visit_events.id CASCADE`.
Database invariants: `UNIQUE(visit_id, slot)` (≤ 1 entry and ≤ 1 terminal event per visit), `UNIQUE(idempotency_key)`, `UNIQUE(visits.visit_key)`, `UNIQUE(event_id, target)`; CHECKs on states/types/statuses, `slot` matches `event_type`, `exit_time ≥ entry_time`.

> **Phase 5 added no migration.** The Alembic head is still `0004`; analytics read these tables. The hosted-write checklist (backup, TLS, destination, approval) is in [phase-5-report.md §11](phase-5-report.md).

## Order and prerequisites
1. `0003` must be applied first (it creates `visitors`, which `0004` references). `alembic upgrade head` applies both in order.
2. Apply with `.\scripts\migrate-cloud.ps1` (idempotent; refuses the old exposed password). **This needs your explicit approval; it was not run.**

## Locking and risk (hosted)
* DDL is transactional (PostgreSQL): the whole migration commits or rolls back together.
* `CREATE TABLE` takes no lock on existing rows. Adding the foreign keys takes a short `SHARE ROW EXCLUSIVE` lock on `visitors` and `processing_sessions` (reads continue; writes wait for milliseconds). Tables are empty, so there is no backfill and no long lock.
* `ENABLE ROW LEVEL SECURITY` on the three new tables, same posture as `0003` (the backend connects as the table owner; the Supabase public API gets no access).
* Rollback: `alembic downgrade 0003` drops the three new tables (and every visit/event in them - destructive by nature, so never on hosted data without approval). Nothing else is touched.

## Verification performed (local PostgreSQL only; isolated throw-away schemas)
| Check | Result |
|---|---|
| `0001 → 0002 → 0003 (+ a visitor row) → 0004`, repeated `upgrade head` ×3 | revision `0004`; earlier rows kept (`app_metadata`, `processing_sessions`, `visitors`) |
| `downgrade 0003` | only the three new tables disappear; `visitors` row kept; `0002`, `0001`, then `upgrade head` again all succeed |
| Model-versus-migration drift (`alembic.autogenerate.compare_metadata`) | no differences |
| Single head | `0004` |
| Constraint tests on real PostgreSQL | duplicate entry/terminal slot rejected, mismatched slot/type rejected, unknown status rejected, `exit_time < entry_time` rejected, unknown state rejected, missing parent rejected, concurrent identical writers → one row |
Tests: `backend/tests/integration/test_database.py`, `backend/tests/integration/test_event_recorder.py`.

## After applying to a hosted database (checklist for you)
1. `alembic current` → `0004 (head)`; `GET /api/v1/system/status` → `migration_revision: "0004"`.
2. `\d+ visits`, `\d+ visit_events`, `\d+ event_delivery_outbox` show the constraints below; RLS enabled (`relrowsecurity = true`).
3. Start the API with `visits.enabled` true and run one short session; `GET /api/v1/events/health` should report zero `DEAD` events and `GET /api/v1/events/log-verification` `consistent: true`.

## Generated SQL (`alembic upgrade 0003:0004 --sql`)
```sql
BEGIN;
-- Running upgrade 0003 -> 0004
CREATE TABLE visits (
    id VARCHAR(32) NOT NULL,
    visit_key VARCHAR(100) NOT NULL,
    visitor_id VARCHAR(32),
    visitor_number INTEGER,
    session_id VARCHAR(32) NOT NULL,
    source_id VARCHAR(64) NOT NULL,
    source_label VARCHAR(80) NOT NULL,
    state VARCHAR(24) NOT NULL,
    entry_time TIMESTAMP WITH TIME ZONE NOT NULL,
    entry_video_ms INTEGER NOT NULL,
    last_observed_time TIMESTAMP WITH TIME ZONE NOT NULL,
    last_observed_video_ms INTEGER NOT NULL,
    exit_time TIMESTAMP WITH TIME ZONE,
    exit_video_ms INTEGER,
    exit_confirmed_at TIMESTAMP WITH TIME ZONE,
    exit_basis VARCHAR(24),
    closure_reason VARCHAR(40),
    track_segments INTEGER NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_visits PRIMARY KEY (id),
    CONSTRAINT ck_visits_state_valid CHECK (state IN ('ACTIVE', 'TEMPORARILY_MISSING', 'EXIT_PENDING', 'CLOSED', 'INTERRUPTED')),
    CONSTRAINT ck_visits_exit_basis_valid CHECK (exit_basis IS NULL OR exit_basis IN ('absence', 'observation_ended', 'interrupted')),
    CONSTRAINT ck_visits_exit_after_entry CHECK (exit_time IS NULL OR exit_time >= entry_time),
    CONSTRAINT fk_visits_visitor_id_visitors FOREIGN KEY(visitor_id) REFERENCES visitors (id) ON DELETE SET NULL,
    CONSTRAINT fk_visits_session_id_processing_sessions FOREIGN KEY(session_id) REFERENCES processing_sessions (id) ON DELETE CASCADE,
    CONSTRAINT uq_visits_visit_key UNIQUE (visit_key)
);
CREATE INDEX ix_visits_visitor_id ON visits (visitor_id);
CREATE INDEX ix_visits_session_id ON visits (session_id);
CREATE INDEX ix_visits_entry_time ON visits (entry_time);
CREATE INDEX ix_visits_state ON visits (state);
CREATE TABLE visit_events (
    id VARCHAR(32) NOT NULL,
    visit_id VARCHAR(32) NOT NULL,
    visitor_id VARCHAR(32),
    visitor_number INTEGER,
    event_type VARCHAR(12) NOT NULL,
    slot VARCHAR(12) NOT NULL,
    idempotency_key VARCHAR(120) NOT NULL,
    payload_hash VARCHAR(64) NOT NULL,
    event_time TIMESTAMP WITH TIME ZONE NOT NULL,
    video_ms INTEGER NOT NULL,
    recorded_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    image_status VARCHAR(12) NOT NULL,
    image_path VARCHAR(120),
    image_sha256 VARCHAR(64),
    image_kind VARCHAR(24),
    image_video_ms INTEGER,
    persistence_status VARCHAR(12) NOT NULL,
    CONSTRAINT pk_visit_events PRIMARY KEY (id),
    CONSTRAINT ck_visit_events_type_valid CHECK (event_type IN ('entry', 'exit', 'interrupted')),
    CONSTRAINT ck_visit_events_slot_valid CHECK (slot IN ('entry', 'terminal')),
    CONSTRAINT ck_visit_events_slot_matches_type CHECK ((slot = 'entry') = (event_type = 'entry')),
    CONSTRAINT ck_visit_events_image_status_valid CHECK (image_status IN ('PENDING', 'STORED', 'UNAVAILABLE', 'MISSING', 'ERASED')),
    CONSTRAINT ck_visit_events_persistence_status_valid CHECK (persistence_status IN ('PENDING', 'DELIVERED', 'DEAD')),
    CONSTRAINT fk_visit_events_visit_id_visits FOREIGN KEY(visit_id) REFERENCES visits (id) ON DELETE CASCADE,
    CONSTRAINT fk_visit_events_visitor_id_visitors FOREIGN KEY(visitor_id) REFERENCES visitors (id) ON DELETE SET NULL,
    CONSTRAINT uq_visit_events_visit_slot UNIQUE (visit_id, slot),
    CONSTRAINT uq_visit_events_idempotency_key UNIQUE (idempotency_key)
);
CREATE INDEX ix_visit_events_visitor_id ON visit_events (visitor_id);
CREATE INDEX ix_visit_events_event_time ON visit_events (event_time);
CREATE INDEX ix_visit_events_persistence_status ON visit_events (persistence_status);
CREATE TABLE event_delivery_outbox (
    id VARCHAR(32) NOT NULL,
    event_id VARCHAR(32) NOT NULL,
    target VARCHAR(24) NOT NULL,
    status VARCHAR(12) NOT NULL,
    attempts INTEGER NOT NULL,
    last_error VARCHAR(200),
    next_retry_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    delivered_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_event_delivery_outbox PRIMARY KEY (id),
    CONSTRAINT ck_event_delivery_outbox_status_valid CHECK (status IN ('PENDING', 'DELIVERED', 'DEAD')),
    CONSTRAINT fk_event_delivery_outbox_event_id_visit_events FOREIGN KEY(event_id) REFERENCES visit_events (id) ON DELETE CASCADE,
    CONSTRAINT uq_event_delivery_outbox_event_target UNIQUE (event_id, target)
);
CREATE INDEX ix_event_delivery_outbox_status_retry ON event_delivery_outbox (status, next_retry_at);
ALTER TABLE visits ENABLE ROW LEVEL SECURITY;
ALTER TABLE visit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE event_delivery_outbox ENABLE ROW LEVEL SECURITY;
UPDATE alembic_version SET version_num='0004' WHERE alembic_version.version_num = '0003';
COMMIT;
```
