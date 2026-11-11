# Gate A runbook – hosted Supabase migration `0002 → 0004`

**Status: prepared, NOT executed.** Everything run so far was read-only on the hosted database (preflight, `pg_dump`) or local (review, restore rehearsal). **No cloud write has been made.** The migration needs your explicit approval (§5).
All commands: PowerShell, from `D:\Face-Tracker`. Secrets are never printed (connection details reach child processes through environment variables; output shows host/port/database/user only).

## 1. Preflight report (run 2026-11-11, read-only) — `python scripts\gate_a.py preflight`
| Check | Result |
|---|---|
| Destination | host `aws-0-ap-southeast-2.pooler.supabase.com` (Supavisor **session** pooler, port 5432), database `postgres`, user `postgres.pmrrthdvtiqtikdllgll` → **compare the project ref `pmrrthdvtiqtikdllgll` with Supabase → Project Settings → General before approving** |
| Credential | rotated; exposed-password fingerprint check: ok; `sslmode=require` |
| TLS (client → pooler) | **in use** (libpq `ssl_in_use=True`). The server-side `pg_stat_ssl` shows `False` only because behind Supavisor it describes the pooler → database hop inside Supabase (informational, not our connection) |
| Server | PostgreSQL 17.11 (aarch64), database size 11 MB |
| Alembic | hosted **`0002`**; repository single head **`0004`** |
| Tables | `app_metadata` (0 rows, RLS on), `processing_sessions` (0 rows, RLS on), `alembic_version` (1 row). **None of the six new tables exist** (no partial earlier attempt) |
| Extensions | pg_stat_statements, pgcrypto, plpgsql, supabase_vault, uuid-ossp (none needed or touched) |
| Verdict | **READY for backup + migration** |
Note: the hosted application tables are **empty**, so the data-loss exposure of this migration is nil in practice; the backup is a structural/recovery safeguard.

## 2. Backup (done) — `python scripts\gate_a.py backup`
* File: **`D:\Face-Tracker\backups\supabase-pre-0003-20261111T064307Z.dump`** (+ `…manifest.json`), 7,186 bytes, custom format, `pg_dump 18.2` of the `public` schema (the only schema this app uses; Supabase-managed schemas such as `auth`/`storage` are provider-owned and unused), `--no-owner --no-privileges`.
* SHA-256 `87cef3bac7cb6ea6e98215aa1944c621400838f3b65cffab0adbebf9b2bb780a`; 15 TOC entries; recorded revision `0002`, row counts `app_metadata 0, processing_sessions 0, alembic_version 1`.
* **Outside Git:** `backups/` is git-ignored (verified). Keep it private. Re-run `backup` immediately before migrating if more than a few hours pass or anything writes to the hosted DB in between (a new timestamped file is created; old ones are not overwritten).
* Why not Supabase's own backup: the Free plan has no point-in-time recovery; this dump is the recovery artefact.

## 3. Backup verification (done) — `python scripts\gate_a.py restore-test backups\supabase-pre-0003-20261111T064307Z.dump`
1. Checksum matches the manifest. 2. The archive is converted by `pg_restore` (proving it is readable) and restored into a **throw-away local database**; revision `0002` and all row counts equal the source at backup time. 3. **The migration was rehearsed on that restored copy**: `alembic upgrade head` → `0004`, six new tables present, RLS enabled on all six, the four exactly-once unique constraints present, pre-existing tables unchanged. 4. The clone was dropped (none left behind).
*Caveat, stated plainly:* the local server is PostgreSQL 16 and the archive uses a PG17 setting, so the restore went through plain SQL with that one parameter (`transaction_timeout`) filtered; a real disaster-recovery restore targets a PG17 Supabase project and can use `pg_restore -d` directly. The rehearsal used empty tables, so it proves structure and migration behaviour, not behaviour with large data.

## 4. Migration review (done) — `python scripts\gate_a.py review` (offline; SQL saved to `temp\gate-a-upgrade.sql`)
29 statements in **one transaction** (`BEGIN … COMMIT`): 6 `CREATE TABLE`, 13 `CREATE INDEX`, 6 `ALTER TABLE … ENABLE ROW LEVEL SECURITY`, 2 `UPDATE alembic_version` (bookkeeping), nothing else. **No `DROP`, `TRUNCATE`, `DELETE`, data `UPDATE`, `ALTER COLUMN`, rename, grant/revoke, extension creation or RLS disabling.** Static check of the `upgrade()` bodies: no `drop_*`/`alter_column`/rename calls. Risks:
| Risk | Assessment |
|---|---|
| Destructive statements | none in the upgrade path (`downgrade()` drops the new tables – see §6) |
| Locks | FKs to `visitors`/`processing_sessions` take a brief `SHARE ROW EXCLUSIVE`; tables are empty → milliseconds. Guard anyway with `lock_timeout` below |
| Partial application | PostgreSQL DDL is transactional: all-or-nothing. A dropped connection aborts the transaction server-side |
| Pooler | session mode (5432) supports transactions and DDL; do **not** use the transaction pooler (6543) for migrations/`pg_dump` |
| RLS | enabled on all new tables with no policies: the Supabase REST/anon API cannot read them; the backend connects as owner. Not weakened |
| App compatibility | the API needs `0004` for `/events`, `/visitors`, analytics: do not point a deployed API at the database before this step completes |

## 5. Execution (**STOP – requires your explicit approval; not run**)
```powershell
# 0. (optional but recommended) fresh backup if the last one is old, then re-run preflight
.\.venv\Scripts\python.exe scripts\gate_a.py preflight          # must say READY (revision 0002, no new tables)

# 1. guard against long lock waits for this shell only, then migrate
$env:PGOPTIONS = "-c lock_timeout=15000 -c statement_timeout=120000"
.\scripts\migrate-cloud.ps1                                      # alembic upgrade head (0002 -> 0004), one transaction
Remove-Item Env:PGOPTIONS

# 2. verify (read-only)
.\.venv\Scripts\python.exe scripts\gate_a.py verify              # revision 0004, 8 tables, FKs, unique/check constraints, RLS, new tables empty
.\.venv\Scripts\python.exe scripts\gate_a.py preflight           # (will now report revision 0004 / new tables present - expected)
```
`gate_a.py verify` PASS list: revision `0004` · all 8 tables · RLS on the six new tables and still on `processing_sessions` · the 8 foreign keys · `uq_visit_events_visit_slot`, `uq_visit_events_idempotency_key`, `uq_visits_visit_key`, `uq_event_delivery_outbox_event_target`, `uq_visitors_registration_key` · check constraints · new tables empty.
Application-level verification comes later (Gates B–E): `python scripts\smoke_deployed.py` against an API configured with the hosted `DATABASE_URL` (readiness, revision, analytics, reconciliation – read-only), then one controlled fixture run for registration/event persistence.
*Not done by this runbook:* the existing `migrate-cloud.ps1` prints only the old tables in its own summary; use `gate_a.py verify` as the authoritative check.

## 6. Failure and recovery plan (does not assume a downgrade is safe)
`alembic downgrade` **drops tables together with their rows**; use it only after the checks below, never by reflex.
| Situation | Do this |
|---|---|
| Migration command errors or the connection drops **before commit** | Nothing to undo: the transaction rolls back. Run `gate_a.py preflight`: it must still show revision `0002` and none of the six tables. Fix the cause (credentials, network, lock timeout), re-run §5. |
| Unsure whether it committed | `gate_a.py preflight` is the source of truth: `0004` + tables present = committed; `0002` + none = rolled back. There is no in-between state. |
| Committed, but you want it undone **and the new tables are empty** (`gate_a.py verify` shows rows = 0) | `$env:DATABASE_URL`-based `alembic downgrade 0002` drops only empty tables: low risk. Re-run `preflight` (expect `0002`). |
| Committed and the new tables **contain data** | **Do not downgrade.** First export: `pg_dump --schema=public --table=visitors --table=face_embeddings --table=identity_associations --table=visits --table=visit_events --table=event_delivery_outbox -Fc -f backups\post-0004-data-<date>.dump` (same environment variables approach as `gate_a.py backup`). Prefer **fix-forward** (a new migration `0005` or an application fix); downgrade only if the export is verified and you accept losing those rows. |
| Pre-existing data damaged (should not happen: nothing is altered) | Restore `backups\supabase-pre-0003-…dump` into a **new** Supabase project (PG17) with `pg_restore --no-owner --no-privileges -d <new project connection string> <dump>`, point `DATABASE_URL` at it. The backup holds only the `public` schema as of `0002` (currently empty tables). |
| App errors after migrating (e.g. 500 on `/events`) | Not a database failure: check `GET /api/v1/system/status` revision and API logs; do not touch the schema. |
| Free project paused / unreachable mid-way | Wait/unpause; verify with `preflight`; retry. |
Every recovery step is local or read-only until you decide otherwise; nothing in this plan is automatic.

## 7. Not done / out of scope here
Gates B–E (private bucket, Render, Vercel, worker/smoke), commit, push and any change to a deployed environment. No hosted-database write has occurred.
