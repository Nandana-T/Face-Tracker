# ADR 0014 – Visit lifecycle and idempotent event delivery

Status: accepted (Phase 4). Supersedes nothing; builds on ADR 0010 (session failure handling) and ADR 0013 (recognition).

## Context
Phase 4 must record **one entry and one exit per presence episode**, in PostgreSQL, as a face crop on disk and as a line in the
mandatory `events.log` – and survive retries, crashes, outages and duplicate worker messages. The three stores cannot be updated
atomically: a database transaction does not cover the filesystem or an append-only log file. "Exactly-once delivery" across them is
not achievable, and we do not claim it.

## Decision
### 1. Three identifiers
`track_id` (temporary, per session) ≠ `visitor_id` (persistent identity) ≠ `visit_id` (one presence episode of one visitor in one
session; a visitor has many visits, a visit may span several tracks). A new track never creates a visit by itself; a returning
*recognised* person after a finalised exit gets a new visit and the **same** visitor.

### 2. The API owns visit state; the worker only observes
The worker already sends (a) per-frame track metadata and (b) recognition observations on its authenticated channel. The API's
`VisitManager` derives presence from those; the worker needs no database credentials and gets no new message type for presence.
Frames arrive at the stream cadence (≈ 15 per second), carry a frame id and the video timestamp, and are validated as before.
Duplicate and out-of-order frames (id not increasing, time going backwards) are ignored and counted.

### 3. Deterministic state machine in video time
`CANDIDATE → ACTIVE ⇄ TEMPORARILY_MISSING → EXIT_PENDING → CLOSED | INTERRUPTED` (CANDIDATE is memory-only).
* A visit opens when a track that is bound to a visitor is CONFIRMED and observed. Its entry time is the earliest sighting of that
  track (identity may be found seconds later).
* Absence is accumulated from the **video-time delta between processed frames**. A gap larger than `max_frame_gap_seconds`
  (worker stall, RTSP outage, reconnect) is *blind time* and counts as zero absence, so skipped detection cycles, slow workers and
  reconnects never create an exit. `missing_after_seconds` → TEMPORARILY_MISSING (no event); `exit_after_seconds` → the exit.
* The exit event time is the **last defensible observation**, never the later moment of the decision; the decision time is stored
  separately (`exit_confirmed_at`). We do not invent a departure moment.
* End-of-session rules: end of file and manual stop → *observation ended* (`exit`, basis `observation_ended`); worker loss, API
  shutdown, source failure → `interrupted` (basis `interrupted`). A stream ending is not proof that anybody left, and the UI says so.

### 4. Idempotent logical events, recoverable side effects
* Visit id and event id are **derived** (SHA-256 of the stable key `session:visitor:ordinal` and of `visit:slot`), so any retry
  recomputes the same ids.
* `UNIQUE(visit_id, slot)` (slots: `entry`, `terminal`) allows at most one entry and one terminal event per visit;
  `UNIQUE(idempotency_key)` makes a repeated write find the existing row. A repeat with an identical payload hash is a duplicate; a
  different payload under the same key raises `EventConflict` (logged, never silently accepted). A terminal event without an entry is refused
  (`OrderViolation`). `exit_time ≥ entry_time` is a CHECK constraint.
* Order of side effects: **(1)** evidence image written atomically under the event id (a retry rewrites nothing); **(2)** one database
  transaction: visit, event, outbox row; **(3)** `events.log` line delivered from the outbox *after* the commit. The attempt counter is
  committed (compare-and-swap claim with a short lease) **before** the line is written, so a crash after writing shows up as a second line with
  `delivery_attempt` = 2 – an explained redelivery – and concurrent deliverers cannot both write attempt 1.
* PostgreSQL is the source of truth; image and log line are delivery artifacts. If evidence cannot be recovered the event stays and
  reports `image_status` MISSING / UNAVAILABLE – evidence is never fabricated. The one substitution is explicit: an exit image is the
  *last good observation* (or the entry image, labelled `entry_representative`), never a new capture.
* A background queue (bounded) and one consumer task keep the frame path free of database/disk work; failures retry with exponential back-off,
  then become a reported dead letter. Start-up reconciliation delivers missing log lines, adopts/flags evidence, removes orphans, completes interrupted erasures and
  closes visits left open by a crash as `INTERRUPTED` at their last persisted observation (a throttled "touch" writes the last observation of an open visit every 30 s of video).

### 5. Erasure
Deleting a visitor deletes its evidence images and sets `visitor_id` / `visitor_number` of its visits and events to NULL. Rows stay as
an anonymous audit trail (times, source, outcome). `events.log` is append-only and keeps the pseudonymous ids of past lines; erasure is logged.

## Consequences
* No claim of atomicity across PostgreSQL, files and the log; the guarantees are the constraints and the recovery paths listed above.
* One API process owns the lifecycle (as sessions do today). A multi-API deployment would need leader election or moving the state machine into the worker; not needed now.
* Visits are **not** unique-visitor counts. Counting people is Phase 5 and needs identity ground truth to be claimed accurate.
