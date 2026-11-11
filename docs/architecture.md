# Architecture

Katomaran AI is a **modular monolith with a separately executable video-processing worker**. Status: Phase 3
(ingestion + face *detection* + temporary multi-face *tracking* + face *recognition* with automatic registration and a visitor gallery) is implemented; visits, entry/exit events and analytics are later phases.

```
 Browser (React SPA - Vercel / Vite dev server)
   │  REST  /api/v1/sources, /sessions...        WebSocket  /api/v1/sessions/{id}/stream
   │  (dev: Vite proxy incl. ws)                  text: state + stats      binary: JPEG + detections
   ▼                                                                ▲
 FastAPI API process                                                │ relays the worker's binary message
   api ──► services/SessionManager ──► SessionStore ──► PostgreSQL (processing_sessions)
   │            │  one active session, state machine, watchdogs
   │            ▼
   │      /api/v1/worker/ws  ◄──────────────  outbound WebSocket (Bearer WORKER_TOKEN)  ◄────┐
   │                                                                                         │
 ┌─┴───────────────────────────────────────────────────────────────────────────────────────┐ │
 │ Video worker process  (python -m app.workers.video_worker)  - may run on another host    │─┘
 │  asyncio loop: connect / heartbeat / commands / send                                     │
 │  session thread:  VideoSource ──► reader thread ──bounded queue──► frame_skip ──►        │
 │     (file | RTSP)                   (drop-oldest if live)           FaceDetector (YOLO11n-face, ONNX Runtime)
 │                                                    └──► FaceByteTracker (Phase 2, per session, analysed frames only)
 │                                                    └──► JPEG(≤960 px) + DetectionResult + tracks ──► latest-wins slot
 │            confirmed tracks ──(bounded queue, drop when busy)──► recognition thread: crop → YuNet landmarks → align → quality → SFace
 │                                                    └──► observation (embedding + crop) ──► ordered, bounded outbox ──► API
 └─────────────────────────────────────────────────────────────────────────────────────────┘
```

Inference never runs in the API process. The worker needs **no database credentials and no inbound port**.

## Backend layout (`backend/app`)
| Package | Responsibility |
|---|---|
| `api/v1` | `system` (health/ready/status/config), `processing` (sources + sessions REST), `ws` (worker link + browser stream) |
| `core/` | settings (env), validated `config.json` (sources, RTSP, detection, worker, stream), logging, errors, middleware |
| `db/`, `models/` | engine/session lifecycle; `app_metadata`, `processing_sessions` |
| `schemas/` | Pydantic contracts: `system`, `processing`, `detection` (bbox convention), `tracking` (`TrackOut`, `TrackingSummary`, `TrackingFrameMeta`) |
| `services/` | `SessionManager` (orchestration, in-memory live view), `SessionStore` (persistence + state machine), `system` |
| `vision/sources` | `VideoSource` protocol; `FileVideoSource`, `RtspVideoSource` (timeouts, back-off reconnect, redacted logging); `registry` (trusted source ids → concrete sources) |
| `vision/detection` | `FaceDetector` protocol; `YoloOnnxFaceDetector` (letterbox, decode, NMS); `model_registry` (pinned weights, checksum, explicit fetch) |
| `vision/tracking` | **Phase 2**: `FaceByteTracker` (ByteTrack-style two-stage IoU association, per-axis Kalman filters, time-based lifecycle), `kalman`, `interfaces` (`TrackState`, `TrackSnapshot`, events, counters) |
| `vision/recognition` | **Phase 3**: `registry` (pinned models, explicit fetch), `landmarks` (YuNet), `alignment` (ArcFace template), `quality` (gate), `embedder` (ONNX Runtime SFace, validated/normalised), `matching` (in-memory index, partitioned by model version), `service` (scheduler + bounded recognition thread) |
| `services/identity_manager`, `services/image_store` | **Phase 3**: the single, serialized decision point (recognise / defer / register), duplicate prevention, idempotent registration, reconciliation, erasure, retention; the local face-image store (content-addressed, confined, never served statically) |
| `models/visitor`, `api/v1/visitors`, `schemas/visitors` | `visitors`, `face_embeddings`, `identity_associations`; gallery/recognition REST API |
| `workers/` | `pipeline` (frame skip, back-pressure, stats, shutdown), `agent` (WebSocket client, one session at a time), `protocol` (wire format), `video_worker` (entrypoint) |

Dependency direction: `api → services → store → db`; `workers` and `vision` depend on `core` only (never on `api`,
and the worker never touches the database).

## Data flow of one processing session
1. Browser `POST /sessions {source_id}`. API validates the id against **server-side** sources, requires a connected worker
   with a verified model, inserts the row `STARTING` (unique `active_slot` ⇒ one active session), sends `session.start`.
2. Worker resolves the id to a path/RTSP URL **locally**, loads the detector, opens the source, starts the reader thread,
   reports `running` after the first frame. Only now does the API move the row to `RUNNING`.
3. Frames flow `source → queue → [detect every (N+1)th] → [track] → JPEG + metadata (+ tracks) → WebSocket → API → every subscribed browser`.
   Only the newest frame is kept at each hop; stats (~1/s) travel as text and are persisted every 5 s.
4. End of file ⇒ `COMPLETED`; Stop ⇒ `STOPPING` → `STOPPED`; any failure ⇒ `ERROR` with a stable code. The worker releases the
   source, the detector and joins the reader thread on every path (tested).

Failure handling and invariants: ADR 0010; transport: ADR 0008; frame delivery/back-pressure: ADR 0009; model: ADR 0007.

## Frontend (`frontend/src`)
`lib/` (API client, wire protocol, geometry, `LiveStream`), `features/monitoring/` (page, canvas, source controls,
detection summary, state copy, `useMonitoring`), plus the Phase 0 shell/design system. The canvas is painted
imperatively; React state changes at most every 400 ms for numbers and on backend-confirmed state changes.

## Persistence design
### Identifiers - never conflate these
* `visitor_id` - persistent facial identity (UUID hex; label `V00124` = `visitor_number`). * `track_id` - tracker-local, per session; stored only in `identity_associations` together with its `session_id`. * `visit_id` - one confirmed presence episode of one visitor in one session (Phase 4; a visitor has many visits, a visit may span several tracks).

### Implemented schema
* `app_metadata(key PK, value, updated_at)`.
* `processing_sessions(id PK, source_id, source_label, source_type, state, active_slot UNIQUE NULL, stop_reason, error_code,
  error_message, worker_id, created_at, started_at, ended_at, updated_at, frames_read/processed/skipped/dropped,
  corrupt_frames, detections_total, max_faces_in_frame, avg_inference_ms, avg_processing_fps, summary JSON,
  settings_snapshot JSON)`, `CHECK state IN (...)`, index on `created_at` (the only list query is "newest sessions").
  RLS enabled. Frames, face images and per-frame boxes are **never** stored.

Phase 2 added **no table and no migration**: the per-session tracking counters travel with the existing stats and are stored in
`processing_sessions.summary` (JSON). Per-track rows were deliberately not added: a track is not an entity anyone needs after the
session, and Phase 3/4 will define what must be stored once identity and visits exist.

* Phase 3 (migration `0003`, additive): `visitors(id, visitor_number UNIQUE, registration_key UNIQUE, status, model_version, first_session_id, first/last_seen_at, recognition_count, image_path, image_sha256, image_quality, …)`,
  `face_embeddings(id, visitor_id FK CASCADE, model_version, dim, vector BYTEA float32, vector_sha256, quality_score; UNIQUE(visitor_id, vector_sha256))`,
  `identity_associations(id, session_id FK, track_id, visitor_id FK NULL, state CHECK, best_similarity, observations, conflicts, registered_here; UNIQUE(session_id, track_id))`. RLS enabled. Embeddings live **only** in the database (and the worker/API memory); images only in the face-image store.

* Phase 4 (migration `0004`, additive): `visits(id, visit_key UNIQUE, visitor_id FK SET NULL, visitor_number, session_id FK, source_id/label, state CHECK, entry_time/entry_video_ms, last_observed_*, exit_time/exit_video_ms/exit_confirmed_at, exit_basis, closure_reason, track_segments; CHECK exit_time ≥ entry_time)`,
  `visit_events(id, visit_id FK, visitor_id FK SET NULL, event_type ∈ {entry, exit, interrupted}, slot ∈ {entry, terminal}, idempotency_key UNIQUE, payload_hash, event_time, video_ms, recorded_at, image_status, image_path/sha256/kind, persistence_status; UNIQUE(visit_id, slot))`,
  `event_delivery_outbox(id, event_id FK, target, status, attempts, last_error, next_retry_at, delivered_at; UNIQUE(event_id, target))`. RLS enabled. **No per-frame data is written to PostgreSQL**; an open visit's last observation is persisted at most every 30 s of video.

Evidence images are stored relative to the evidence root (`entries/YYYY-MM-DD/<event_id>.jpg`, `exits/...`); the store is a small class behind which object storage can later be placed.

## Tracking (Phase 2)
```
Video source → reader → YOLO11n-face (every (N+1)th frame) → FaceByteTracker.update(dets, timestamp_ms, frame_id)
            → TrackerOutput → DetectionResult.tracks + TrackingSummary → binary frame header → API (validates) → browser overlay
```
* The tracker lives **inside the worker**, in the pipeline's inference thread, one instance per session (ids restart at 1, nothing
  leaks between sessions, it is reset on every exit path). The API only validates and relays; it never tracks.
* **Frame skipping:** the tracker is updated *only on analysed frames*, with the frame's real timestamp. Skipped frames publish
  nothing (no stale boxes), and age, expiry and velocity use elapsed seconds, so they do not depend on `frame_skip` or the FPS.
  The alternative (predicting boxes on skipped frames) was rejected because every skipped frame would show a *guess* styled
  like a result; instead each published box says whether it was detected (`observed`) or predicted.
* A tracker failure is contained: it is counted (`tracks_errors`), the tracker is reset and detection continues.
* Memory is bounded (`tracking.max_tracks`; removed tracks are dropped immediately); no extra frame copies are made.
* Boundary: a **track** is a temporary hypothesis inside one processing session. It is not a visitor, has no embedding, and is
  never matched across sessions. Persistent identity (`visitor_id`) and visits arrive with recognition (Phase 3) and the visit
  lifecycle (Phase 4). Decision record: [ADR 0012](decisions/0012-tracker-selection.md).

## Identity architecture (Phase 3)
```
worker: confirmed track → [scheduler: warm-up every 0.5 s, then every 5 s, time-budgeted] → crop copy → recognition thread
        landmarks → alignment → quality gate → embedding ───────► binary "observation" (embedding + small crop)
API:    IdentityManager (ONE lock; PostgreSQL advisory lock for multi-process)
        index lookup → vote {match | ambiguous | grey | novel} → per-track state machine → [register under the lock]
        → identity event to browsers; associations persisted when the session ends
```
* **Decision rules** (docs/phase-3-report.md has the evidence behind each number): `best ≥ 0.50` and margin ≥ 0.05 over the next visitor ⇒ *match vote*; `0.40 ≤ best < 0.50` ⇒ *grey* (no recognition, no registration);
  `best < 0.40` ⇒ *novel*; best within the margin of another visitor ⇒ *ambiguous*. Recognition needs ≥ 2 match votes; registration needs ≥ 3 consecutive novel votes over ≥ 0.8 s, mean quality ≥ 0.35 and mutually consistent embeddings
  (mean pairwise cosine ≥ 0.6) – a single frame never creates an identity.
* **Duplicate prevention:** serialized match-then-register; the aggregated embedding is re-checked against a freshly synced index *inside* the lock; `registration_key = session:track` is UNIQUE so a retry returns the existing visitor;
  duplicate/delayed observations are dropped by id; the same visitor is never propagated to two simultaneous tracks of one session; a database constraint alone cannot prevent duplicate *people* and is not claimed to.
* **Index:** exact numpy cosine search over embeddings loaded from PostgreSQL at start-up and updated incrementally (watermark sync before every registration). pgvector/ANN were not introduced: a few thousand 128-d vectors search in microseconds and the database remains authoritative.
* **Failure handling:** a failed registration rolls back completely (visitor + embeddings + image file), backs off and retries on fresh evidence; interrupted writes are repaired by `reconcile()` at start-up (orphan image files removed after a grace period, dangling image references cleared).

## Visit lifecycle and event delivery (Phase 4)
```
worker ──frames (tracks + video time) + observations──► API
API:   VisitManager (pure state machine, video time)     IdentityManager (Phase 3)
         CANDIDATE → ACTIVE ⇄ TEMPORARILY_MISSING → EXIT_PENDING → CLOSED | INTERRUPTED
         │ entry / terminal actions                       │ identity bound to a track
         ▼
       bounded queue → ONE consumer (off the frame path) → EventRecorder
         1 evidence image  (atomic, name = event id)
         2 DB transaction  (visit + event + outbox row; UNIQUE constraints = exactly-once *logical* event)
         3 events.log line (from the outbox, after commit; attempt counter committed before the write)
       → live `visit_event` / `visit_state` messages to browsers; start-up and on-demand reconciliation
```
* The worker never touches the database and has no new message type: presence is derived from the per-frame track metadata and the recognition observations it already sends on its authenticated channel (duplicate / out-of-order frames are ignored).
* Rules: a visit opens when a track bound to a visitor is CONFIRMED and observed (entry time = earliest sighting of the track); a track disappearing is not an exit; absence is measured in **video time** between processed frames, gaps above `visits.max_frame_gap_seconds` are blind time; the exit time is the **last sighting**; end of file / manual stop ⇒ *observation ended*; worker loss / API shutdown / source failure ⇒ *interrupted* (no duration). Full argument: [ADR 0014](decisions/0014-visit-lifecycle-and-event-delivery.md), [phase-4-report](phase-4-report.md).
* The API process owns the lifecycle (single-API design, like sessions). Events are exactly-once as **logical records** (constraints + deterministic ids); delivery of the image and the log line is retried until consistent. Atomicity across PostgreSQL, files and the log is **not** claimed.

## Analytics (Phase 5)
```
PostgreSQL (visitors, visits, visit_events, processing_sessions, identity_associations)
   └─ AnalyticsService  - SQL aggregates only, one scope [since, until) x source x session
        └─ /analytics/overview|timeline|sources|sessions|coverage|recent|reconciliation   (REST, authenticated)
SessionManager ──"analytics_changed" notice (no counts)──► /analytics/stream (single-use ticket) ──► browser
browser: TanStack Query invalidation + refetch (+ refetch on reconnect, 20 s poll)
```
No analytics store, no cache, no second counter: every number is re-read from PostgreSQL. Definitions (identities vs people, returning, active vs stale, erasure) are in [phase-5-report.md](phase-5-report.md) and [ADR 0015](decisions/0015-counting-semantics.md). The independent recount (`reconcile`) is report-only.

## Storage and deployment (Phase 6)
Image I/O goes through `BlobBackend` (`LocalBlobBackend`, `SupabaseBlobBackend`) beneath `LocalImageStore`/`EvidenceStore`; see [ADR 0016](decisions/0016-private-object-storage.md). Target topology (Vercel + Render + Supabase + local worker), limits and recovery: [deployment-guide.md](deployment-guide.md).

## Measured resource usage
See [`benchmarks/phase1-benchmark.md`](benchmarks/phase1-benchmark.md): one real-time stream at the shipped defaults uses
≈ 7 % of an i5-1240P and ≈ 230 MB RSS; ≈ 31 ms inference per frame. GPU: not measured (no usable GPU). Tracking cost (measured in Phase 2): [`benchmarks/phase2-tracking.md`](benchmarks/phase2-tracking.md). Phase 3 embedding cost
is still an unmeasured estimate.
