# ADR 0010 – Processing-session lifecycle, and the local database fallback

**Status:** accepted (Phase 1).

## States
`IDLE` (no row) → `STARTING` → `RUNNING` → `STOPPING` → `STOPPED`; terminal alternatives `COMPLETED` (end of file)
and `ERROR`. Allowed transitions live in `services/session_store.py:TRANSITIONS` (terminal states are final).
`RUNNING` is only set after the **worker** reports its first processed frame - the UI can trust it.

## Invariants
* At most one active session: `processing_sessions.active_slot` is `1` while active, `NULL` otherwise, with a
  `UNIQUE` constraint ⇒ two racing starts cannot both insert (tested with six concurrent requests).
* `POST /sessions` for the *same* source that is already starting/running is idempotent (`200`, same id); a different
  source gets `409 session_active`. `POST …/stop` is idempotent in every state.
* Every non-terminal session has an accountable worker: worker disconnect, heartbeat silence, missing heartbeat
  ownership, `start_timeout_seconds` or `stop_timeout_seconds` each end it in `ERROR`/`STOPPED` with a reason.
* After a crash or restart the API marks leftover active rows `ERROR (api_restarted)` at start-up and releases the slot.

## Persistence
One summary row per session (counters + last stats snapshot + settings snapshot). Never frames, images or per-frame
detections. Sources are defined in `config.json` (not a table): `video.sources[]` (+ `video.sample_dir` auto-discovery);
RTSP URLs live in environment variables referenced by `url_env`.

## Local PostgreSQL fallback (credential-rotation blocker)
The Supabase password from Phase 0 was pasted into a chat and is still in use (the `.env` value was never rotated).
Until it is rotated, Phase 1 development and **all tests run on an embedded PostgreSQL** (`pgserver`, data in
`storage\pgdata` on D:, `scripts\local-db.ps1`) so that no credential-dependent cloud operation is performed. Migration
`0002` has therefore been verified on real PostgreSQL 16-class semantics **locally** but **not applied to Supabase**.
