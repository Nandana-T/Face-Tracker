# Architecture

Katomaran AI is a **modular monolith with a separately executable video-processing worker**.

```
 Browser (React SPA, Vercel or Vite dev server)
     │  HTTPS /api/v1/*  (dev: Vite proxy; prod: VITE_API_BASE_URL + CORS allow-list)
     ▼
 FastAPI API process ──────────────┐            AI worker process (python -m app.workers.video_worker)
   api → services → repositories   │              source → detector → embedder → tracker → lifecycle
   (no model inference here)       │              (Phase 1+; CPU/GPU heavy, own process/host)
     │ SQLAlchemy (psycopg 3)      │                  │ SQLAlchemy         │ images
     ▼                             ▼                  ▼                    ▼
 PostgreSQL (Supabase / local) ◄───────────────────────┘          Image storage (D:\…\storage → object storage)
```

## Backend layout (`backend/app`)

| Package | Responsibility |
|---|---|
| `api/` | HTTP routing, versioned under `/api/v1`; thin handlers + dependencies |
| `core/` | settings (env), validated processing config (`config.json`), logging, errors, middleware, event-logger interface |
| `db/` | declarative base (naming convention), engine/session lifecycle, DB health check with retry |
| `models/` | SQLAlchemy models (only `app_metadata` in Phase 0) |
| `schemas/` | Pydantic request/response models (the API contract) |
| `services/`, `repositories/` | use-cases and data access; services never import FastAPI |
| `vision/{sources,detection,recognition,tracking}` | **Protocol interfaces only** – extension points, no behaviour yet |
| `workers/` | standalone process entrypoints |

Dependency direction: `api → services → repositories → db`; `vision` and `workers` depend on `core`/`db`, never on `api`.
Importing `app.main` loads no models and opens no connection (engines connect lazily).

## Worker boundary

Inference runs **only** in the worker process, so the API event loop stays responsive. The API and worker share
nothing but PostgreSQL (state, events) and the image store. The worker is configured with the same `Settings` /
`config.json`, so it can run on a different host (see `deployment.md`). Real-time push to the UI (Phase 6) will be
fed from the database/event stream, not from in-process calls. Phase 0 ships the entrypoint only: it validates
config, configures logging, checks the DB and exits (non-zero when the DB is unreachable, 2 on invalid config).

The API uses synchronous SQLAlchemy in FastAPI's threadpool (`def` endpoints). That is simple, robust with psycopg
and sufficient for dashboard-scale queries; the decision is revisited if WebSocket fan-out needs async I/O (ADR 0003).

## Persistence design

### Identifiers – never conflate these
* `visitor_id` – persistent identity (one row per person, survives across sessions).
* `track_id` – temporary id from the tracker, unique only within one processing session.
* `visit_id` – one presence lifecycle of a visitor (entry → exit).

### Phase 0 schema (implemented)
`app_metadata(key PK, value, updated_at)` – instance-level key/value. RLS is enabled (no policies) so Supabase's
public Data API cannot read it; the backend connects as the table owner.

### Planned schema (implemented with their vertical slices)
```
video_sources 1──* processing_sessions 1──* tracks
visitors      1──* embeddings                       (embedding vector stored as bytes/float array; never logged)
visitors      1──* visits 1──* track_segments  (track_segments.track_id is session-scoped)
visits        1──0..1 entry event,  1──0..1 exit event
events(id, visit_id FK, session_id FK, type ∈ {entry, exit}, occurred_at, image_path)
UNIQUE (visit_id, type)   -- at most one entry and one exit per visit
```
Constraints: FKs everywhere, `UNIQUE(visit_id, type)` on events, image paths stored **relative** to the storage root.
Face crops and videos are never stored in PostgreSQL.

### Storage abstraction (Phase 4)
`ImageStore` protocol (`put(relative_path, bytes)`, `url_for(relative_path)`), with `LocalImageStore` (rooted at
`STORAGE_DIR`) and an S3-compatible/Supabase Storage implementation selected by configuration. DB rows hold only
relative keys, so migrating storage = copying objects + switching the implementation.

## Future video-processing pipeline
`VideoSource.frames()` → frame skipping (`detection.frame_skip`) → `FaceDetector` → `FaceTracker` →
`FaceEmbedder` + `IdentityMatcher` (`recognition.similarity_threshold`) → visit lifecycle (entry on first confirmed
sighting; exit after `tracking.max_missing_seconds` without sighting) → events (DB + `events.log` + image store).

## Resource estimates (ESTIMATES, not measurements)
| Workload | Estimate | Status |
|---|---|---|
| API + PostgreSQL dashboard queries | < 0.5 vCPU, < 512 MB RAM | estimate |
| YOLO face detection, 720p, frame_skip 2, CPU | ~1–2 vCPU cores per stream at ~10 FPS detect | estimate |
| InsightFace/ArcFace embedding (buffalo_l) on CPU | tens of ms per face; ~1–2 GB RAM for models | estimate |
| GPU (e.g. 4–8 GB) | one GPU should comfortably serve several streams | estimate |

**Nothing above has been measured.** Phase 1+ will add a benchmark script (FPS, per-stage latency, CPU/RAM/GPU) run
on the target hardware, and these tables will be replaced with measured values and the hardware used.
