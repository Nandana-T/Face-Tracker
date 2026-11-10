# Architecture

Katomaran AI is a **modular monolith with a separately executable video-processing worker**. Status: Phase 2
(ingestion + face *detection* + temporary multi-face *tracking*) is implemented; recognition, persistent visitors, visits, entry/exit events and analytics are later phases.

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
| `vision/recognition` | interface only (Phase 3) |
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
### Identifiers - never conflate these (future phases)
* `visitor_id` - persistent identity. * `track_id` - tracker-local, per session. * `visit_id` - one entry→exit lifecycle.
Phase 1 introduces none of them: it detects faces, it does not identify them.

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

### Planned (with their slices)
```
visitors 1──* embeddings;  visitors 1──* visits 1──* track_segments;  visits 1──0..1 entry event, 1──0..1 exit event
events(id, visit_id FK, session_id FK, type ∈ {entry, exit}, occurred_at, image_path)   UNIQUE (visit_id, type)
```
Image paths are stored relative to the storage root; storage goes behind an `ImageStore` protocol in Phase 4 (local → object storage).

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

## Measured resource usage
See [`benchmarks/phase1-benchmark.md`](benchmarks/phase1-benchmark.md): one real-time stream at the shipped defaults uses
≈ 7 % of an i5-1240P and ≈ 230 MB RSS; ≈ 31 ms inference per frame. GPU: not measured (no usable GPU). Tracking cost (measured in Phase 2): [`benchmarks/phase2-tracking.md`](benchmarks/phase2-tracking.md). Phase 3 embedding cost
is still an unmeasured estimate.
