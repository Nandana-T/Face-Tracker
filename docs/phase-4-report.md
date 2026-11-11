# Phase 4 report – visit lifecycle, entry/exit events, durable persistence

Status: implemented and verified as described below; **awaiting approval**. Nothing is committed or pushed. **Hosted Supabase is still at migration `0002`; nothing was written to it** (see [migration preflight](migration-0004-preflight.md)). Phase 5 was not started.
Decision record: [ADR 0014](decisions/0014-visit-lifecycle-and-event-delivery.md). Measurements: [benchmarks/phase4-visits.md](benchmarks/phase4-visits.md).

## 1. What was built
A confirmed presence of a recognised/registered visitor opens **one visit**; the visit has **one entry event** and **one terminal event** (exit, or interrupted), each recorded in PostgreSQL, as a face crop on D: and as an `events.log` line, delivered idempotently and recoverably. Identifiers: `track_id` (temporary) ≠ `visitor_id` (person) ≠ `visit_id` (one episode) – a visitor can have many visits, a visit may span several tracks, and a new track never opens a visit by itself.

| Area | Files |
|---|---|
| Lifecycle (pure state machine) | `services/visit_manager.py` |
| Durable events, outbox, reconciliation, erasure | `services/event_recorder.py`, `services/evidence_store.py`, `services/event_audit.py` |
| Queue, retries, live notifications | `services/visit_service.py`; hooks in `services/session_manager.py` |
| Schema | `models/visit.py`, migration `0004_visits_and_events.py` |
| API | `api/v1/events.py`, `schemas/events.py`; `ws.py` replays open visits; `DELETE /visitors` erases evidence |
| Logging | `core/events.py` (`audit`, strict `EventsLogWriter`), `core/logging.py` (`StrictFileHandler`, explicit `session_id` kept) |
| Tools | `scripts/verify_events_log.py`, `benchmarks/evaluate_visits_video.py` |
| Frontend | `features/events/` (timeline, filters, visit drawer, evidence image), `features/monitoring/visit-activity-panel.tsx`, `use-monitoring.ts`, `lib/visits.ts`, `lib/object-url.ts` |

## 2. Lifecycle design
```
identity bound to a track ──► CANDIDATE (memory only; never an event)
CANDIDATE ──track CONFIRMED & seen──────────────► ACTIVE ............ ENTRY (time = earliest sighting of the track)
ACTIVE ──not seen ≥ missing_after (1.5 s)──────► TEMPORARILY_MISSING . nothing logged
TEMPORARILY_MISSING ──seen again (any track)───► ACTIVE ............. nothing logged, visit kept
ACTIVE|MISSING ──not seen ≥ exit_after (8 s)───► EXIT_PENDING ........ EXIT  basis=absence      (time = LAST sighting)
ACTIVE|MISSING ──end of file / manual stop─────► EXIT_PENDING ........ EXIT  basis=observation_ended
ACTIVE|MISSING ──worker lost / API shutdown / source failure──► EXIT_PENDING ... INTERRUPTED basis=interrupted
EXIT_PENDING ──recorded & acknowledged─────────► CLOSED | INTERRUPTED
API restart finds a visit still open in the database ──► INTERRUPTED at its last persisted observation (api_restarted)
```
* **Time.** Absence is the sum of **video-time** deltas between processed frames. A gap longer than `max_frame_gap_seconds` (2 s: worker stall, RTSP outage, reconnect) is *blind time* = 0 absence; `on_connection(reconnecting)` also resets the previous-frame time. Skipped detection cycles never create an exit.
* **Late identity.** A track tracked for 4 s before it is recognised enters at its first sighting, not at the moment of recognition.
* **Out-of-order / duplicate frames** (frame id not increasing, time going backwards) are ignored and counted (`stale_frames_ignored`); negative timestamps and malformed track metadata are dropped.
* **Two visitors** are independent; the same visitor on two tracks shares one visit (`track_segments`); a visitor returning after a finalised exit gets a **new visit and the same visitor id**.
* **Closure policy (what the UI says).** `absence`: "Left the view" (the last sighting, not a measured departure). `observation_ended`: "the video ended or monitoring stopped while the visitor was still in view – this does not mean they left". `interrupted`: end unknown, **no duration is shown**. A duration (video time) exists only for a visit that closed normally.
* **Hackathon requirement "exit at EOF".** The exit event exists at end of file but is labelled `observation_ended`; it represents the end of observed presence, not verified movement outside the location.

## 3. Exactly-once *logical* events (and what is not claimed)
Atomicity across PostgreSQL, the filesystem and a log file is impossible and **not claimed**. Provided instead: deterministic visit/event ids; `UNIQUE(visit_id, slot)`, `UNIQUE(idempotency_key)`, `UNIQUE(visit_key)`; payload hash (identical retry = duplicate, different payload = `EventConflict`, reported and logged); entry-before-exit enforced (`OrderViolation`) and `exit_time ≥ entry_time` by CHECK; one transaction for visit + event + outbox row; side effects after the commit.
Order: **image** (atomic, name = event id) → **DB transaction** → **`events.log` from the outbox** (attempt counter claimed by compare-and-swap with a 30 s lease and committed *before* the write).

### Crash and failure boundaries (each is a test)
| Boundary | Behaviour | Test |
|---|---|---|
| Before the DB insert (image already written) | orphan file; the retry adopts the same file (no duplicate); otherwise removed by reconciliation after a grace period | `test_crash_after_image_before_database…`, `test_orphan_files_are_removed…` |
| Evidence cannot be written (storage down) | event committed with `image_status=PENDING`, bytes retried in memory; after a restart reported `MISSING` – never fabricated | `test_storage_outage…`, `test_evidence_lost_in_a_restart…` |
| Evidence file later deleted | detected by reconciliation → `MISSING` | `test_deleted_evidence_file_is_detected` |
| Blank / black / corrupt / tiny crop | never stored → `UNAVAILABLE` | `test_unusable_crops…` (3 cases) |
| After commit, before `events.log` | outbox row stays `PENDING`; sweep (30 s) / reconciliation delivers exactly one line, `delivery_attempt` 2 | `test_log_failure_keeps_the_event_pending…` |
| Persistent log failure | back-off, then `DEAD` (reported in health and in the UI) – the event itself stays | `test_persistent_log_failure…` |
| After the log write, before the acknowledgement | redelivery = a second line with a higher `delivery_attempt`; the verifier calls it *explained*, not a duplicate | `test_crash_between_log_write…` |
| Database outage | recorder raises; the service retries with exponential back-off (8 attempts), then a reported dead letter; later actions are not blocked | `test_a_database_outage_is_retried…`, `test_a_permanently_failing_event…` |
| Worker terminated | session ends `ERROR worker_lost` → open visits `interrupted` | `test_worker_loss…` (+ source error, stop, EOF variants) |
| API restart / crash while a visitor is inside | reconciliation closes the visit as `INTERRUPTED` at its last persisted observation (touched every 30 s of video), exit image = entry image labelled `entry_representative`; idempotent | `test_open_visit_after_a_crash…`, **Playwright crash test (real models)** |
| Repeated / concurrent delivery | 8 racing threads → one visit, one event, one file, one log line | `test_concurrent_identical_deliveries…`, `test_two_recorders…` |
| Queue full | the loss of an event is counted and logged (`persistence.failure`), never silent | `test_a_full_queue_is_reported…` |

**Residual window (documented, not hidden):** an event the lifecycle has decided but the consumer has not yet committed lives only in memory. If the API process dies in that window (milliseconds normally; up to the retry horizon during a database outage) the entry/exit is not recorded; a visit whose entry was committed is still interrupted-closed at restart, one whose entry was not committed leaves no trace.

## 4. Evidence images
`<evidence_dir>/entries|exits/YYYY-MM-DD/<event_id>.jpg` (default `logs/` on D:; the repository already ignored these folders). Validated references (regex + resolve + inside-root), atomic temp-file + fsync + rename with a per-writer temp name (found and fixed by the concurrency test), JPEG decode, 32–1024 px, rejection of blank/black frames, idempotent per event. Only crops that passed the recognition quality gate and belong to the visitor's own track qualify; a track whose identity changes loses its earlier crops. **The exit image is the last good observation, not a new capture** (or the entry image labelled `entry_representative`); the UI says which. Served only by authenticated `GET /events/{id}/image` (`private, no-store`); there is no static route.

## 5. `events.log`
JSON lines (existing formatter). Narrative records, low rate: `track.created`, `face.detected`, `track.recovered`, `embedding.generated` (once per track), `recognition.recognized|ambiguous|failed`, `visitor.registered`, `source.reconnecting|connected`, `persistence.retry|failure|rejected`, `reconcile.completed`, `event.conflict`, `visitor.erased`. Durable records `visit.entry|exit|interrupted` carry `event_id, visit_id, visitor_id, visitor_label, event_type, event_time, video_ms, session_id, source_id, source_label, image_ref, image_status, image_kind, persistence:"committed", delivery_attempt, exit_basis, closure_reason`. No embeddings, images or credentials; no per-frame INFO.
Verification: `scripts/verify_events_log.py [--repair]` and `GET /events/log-verification` list events missing from the file, lines unknown to the database, **unexplained** duplicates (same event and same attempt) and **explained** redeliveries. A defect found while testing: the logging filter overwrote an explicit `session_id` with the empty request context, so audit lines lost their session – fixed and tested.

## 6. Worker/API contract
Unchanged on the wire. The API derives presence from the per-frame track metadata (`frame_id`, `timestamp_ms`, `tracks[].{state, observed, first_seen_ms}`) and the Phase 3 observations the worker already sends on its authenticated channel; the worker has no database credentials and no new message type. Bounded sizes (existing limits), duplicates/out-of-order ignored, session end mapped onto closure rules.
*Deviation from the brief, stated openly:* it suggested explicit messages for "visitor observed / missing / reacquired / session completed / interrupted". The worker has no visitor identity (recognition is API-side), so such messages would only repeat what the frame and session messages already say; the API emits the equivalent lifecycle (`visit_event`, `visit_state`, `visits` snapshot) to browsers.

## 7. Frontend
* **Event Logs** (`/events`): real records only; timeline with entry/exit/interrupted badges, authenticated thumbnails (or the honest reason: *no usable image captured / image file missing / erased*), visitor links, persistence state; filters (type, visitor, from/to, sort/order) with an inverted-range guard; pagination; skeleton, empty, filtered-empty and error+retry states; Motion insertion/transition animations (reduced motion respected via `MotionConfig`).
* **Visit drawer** (`?visit=<id>`): visitor, entry/exit panels with images and image kind, state, source/session, track segments, **observed duration only for a normally closed visit** ("in progress" / "unknown" otherwise), closure reason and an explanation of what the basis means, data-integrity badge, erased-visitor notice.
* **Live Monitoring:** a *Visits* panel (in view / waiting, recent events, persistence-problem notice, link to Event Logs), toasts deduplicated by event id, silent replay for late joiners, nothing carried into the next session. Overview card and visitor drawer link to the page.
* A frontend defect found by the real-stack test and fixed: authenticated images used an object URL created in render and revoked in an effect cleanup, which StrictMode (dev) revoked while the image was still shown whenever the data was cached (the Phase 3 visitor image had the same flaw). `lib/object-url.ts` ties the URL to the Blob's lifetime.

## 8. Erasure
`DELETE /visitors/{id}` deletes the visitor's evidence files, sets `visitor_id/visitor_number` of their visits and events to NULL (rows stay as an anonymous audit trail: times, source, outcome), then deletes the identity; an open visit of the erased person ends by absence, is recorded de-identified and **no image is stored after the erasure** (found by review, now tested). An interrupted erasure is completed by reconciliation. `events.log` is append-only: older lines keep the pseudonymous visitor id/label (no biometric content); an erasure line is written.

## 9. Real-video validation (summary; details in the benchmark)
6 hackathon clips → 9 visits (absence 6, observation ended 3, interrupted 0), 18 events, 18/18 evidence images, 0 duplicates, `events.log` ≡ database, reconciliation found nothing to repair, 0 recorder errors. Entry and exit crops of all 9 visits show the same person (manual, one reviewer). **Only 9 of 74 confirmed tracks (12 %) became visits**: the unchanged quality gate rejects most real faces, so event coverage on this footage is low by design. 9 visits ≠ 9 people and no accuracy or people count is claimed (no identity labels).
**Sensitivity of the grace period** (not calibrated against labelled departures – there are none): same 6 clips with `exit_after_seconds` = 3 / 8 (default) / 15 s: **9 / 9 / 10 visits**, each visitor always has exactly one visit, 6 / 6 / 5 exits by absence and 3 / 3 / 5 by observation-ended, visit spans 4.4–8.6 s. 3 s and 8 s give identical results; the 10th visit at 15 s is a 10th *registered visitor* in that run (recognition is timing-dependent - its queue drops work when busy - so identities vary slightly between runs), not a split visit. **Conclusion: on this footage the grace period was never decisive** - no visitor left and came back inside a clip, so the reacquisition rule is exercised by the unit/relay tests, not by real footage. The defaults are therefore engineering choices, not calibrated.

## 10. Performance
State machine ≈ 17–26 µs per processed frame; event persistence (image 3.0 ms + database 5.8 ms + log 6.0 ms) ≈ 14–18 ms mean, 37 ms max, all off the frame path; visit layer on vs off: +1–3 % wall time, +0–1 % CPU, ≤ 5 MB RSS – inside run-to-run noise.

## 11. Tests (all executed)
| Layer | Result |
|---|---|
| Backend (pytest, real PostgreSQL where marked) | **499 passed** (Phase 3: 389) |
| Frontend (Vitest) | **219 passed** (Phase 3: 184; one placeholder-page row was replaced by real Event Logs tests) |
| Playwright (Edge, real stack, authenticated) | **28 passed** (Phase 3: 25), leaving no process behind |
| **Total** | **746** (baseline 598) |
ruff, ruff format, mypy (strict, app + tests), ESLint, `tsc`, Vite build clean. New: `test_visit_manager.py` (state machine, 27), `test_event_recorder.py` (PostgreSQL: idempotency, constraints, crash windows, concurrency, erasure, log verification, path safety, 40), `test_visit_service.py` (6), `test_visits_relay.py` (fake worker → API → DB/browser, 11), `test_events_api.py` (22), `test_events_log.py` (4), migration test extended; frontend `events.test.tsx` (25), `visit-live.test.tsx` (9), `object-url.test.tsx` (2); **3 real-model Playwright tests**: (1) entry → disappearance → exit with images, timeline, drawer, filters, private image access and `events.log` lines; (2) end of video = *observation ended*, same person later = new visit / same visitor, replayed reconciliation changes nothing, full restart keeps everything; (3) crash while inside → exactly one honest `INTERRUPTED` record after restart, idempotent on a second restart.
Bugs found *by these tests* and fixed: out-of-order inserts (no ORM relationship between event and outbox → explicit flush), shared temp-file name between concurrent evidence writers (Windows `PermissionError`), a race where two deliverers could both claim attempt 1 (→ compare-and-swap claim), `session_id` lost in log records, tie ordering of an entry and its terminal event with equal times, StrictMode-revoked image URLs.
Not proven by them: behaviour with a real RTSP camera, GPU, hosted database, multiple API processes, exit times against physical reality.

## 12. Security and privacy
Authenticated APIs (every new route has a 401 test), no embeddings/paths/credentials in responses or logs (asserted), evidence private and path-confined, crops only from the quality gate, erasure as above, no demographic inference, no new roles. Unchanged limitations: one shared admin role, no audit trail of who viewed an image, no encryption of evidence files beyond disk encryption, consent is an operator decision. Details: [privacy-and-security.md](privacy-and-security.md).

## 13. Definition of Done
| Item | Verdict |
|---|---|
| Confirmed presence opens one visit; one logical entry event | ✅ unit + integration + real-model E2E |
| Temporary disappearance creates no duplicates; reappearance in the grace period keeps the visit | ✅ |
| Finalised disappearance creates one exit | ✅ |
| EOF / interruptions follow explicit closure rules | ✅ (RTSP *reconnect/disconnect*: rules unit-tested, **no real camera tested**) |
| Multiple visitors independent; visitor id ≠ visit id | ✅ |
| Duplicate messages idempotent; DB uniqueness verified | ✅ (PostgreSQL) |
| Timestamped crops stored locally; metadata persisted | ✅ |
| `events.log` contains verifiable records; crash/retry reconciliation | ✅ |
| Event timeline from real records; visit details; authorized images | ✅ |
| Real-video behaviour evaluated; performance measured | ✅ with the limits above (no labels) |
| Backend / frontend / Playwright pass; 598 baseline not regressed; lint, types, build | ✅ |
| No unique-visitor analytics claimed | ✅ (UI/API/docs say visits ≠ people) |
| Hosted migration applied | ⛔ **deliberately not applied** (needs your approval) |
**Verdict: accepted at hackathon scope**, with these partial items stated: no real RTSP/GPU/hosted-DB evidence, event coverage on real footage limited by the recognition gate, exit-time correctness unverifiable without ground truth, default grace periods are engineering choices (sensitivity shown, not calibrated).

## 14. Remaining risks
1. In-memory window before commit (§3). 2. One API process owns the lifecycle (no HA). 3. `exit_after_seconds` / `missing_after_seconds` / `max_frame_gap_seconds` are not calibrated against labelled departures; a visitor who leaves for longer than the grace period gets two visits. 4. ID switches or recognition errors produce wrong or split visits – visits are only as good as identities. 5. Interrupted-by-crash exit time is the last *persisted* observation (≤ 30 s of video stale) and its image is the entry image. 6. Masked/blurred faces can still become visits (Phase 3 gate limits). 7. `events.log` keeps pseudonymous ids of erased visitors. 8. Hosted migrations `0003` and `0004` are pending.

## 15. Readiness for Phase 5 (unique visitor counting and analytics)
Ready **structurally**: visits, events and persistent visitor ids are stored and queryable. **Not ready to claim accuracy**: unique-visitor counts depend on recognition quality that has been measured only on LFW and a simulation, never on labelled hackathon footage. Recommended before Phase 5: label a few clips (who is who, when they leave), apply `0003`+`0004` to the hosted database after review, decide the licence stance on the recognition model (ADR 0013), and calibrate the grace periods on those labels.
