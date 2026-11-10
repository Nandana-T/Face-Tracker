# API contracts (v1)

Base path `/api/v1`. OpenAPI: `/api/openapi.json`, Swagger UI: `/api/docs`. Source of truth:
`backend/app/schemas/{system,processing,detection}.py`; mirrored by `frontend/src/lib/api-types.ts` (kept in sync by
hand; a generated client is a candidate once the surface grows).

Every response carries `X-Request-ID` (a valid client-supplied value is echoed, otherwise generated).

## GET /health – liveness (never touches dependencies)
`200 {"status":"ok","service":"katomaran-api"}`

## GET /ready – readiness (real `SELECT 1` against PostgreSQL, with bounded retries)
`200`
```json
{"status":"ready","checks":{"database":{"status":"ok","latency_ms":4.2,"error":null}}}
```
`503` (same schema; used by the UI to tell "API up, DB down" from "API down")
```json
{"status":"not_ready","checks":{"database":{"status":"unavailable","latency_ms":null,"error":"connection_failed"}}}
```
`error` is a coarse category (`connection_failed`, `query_failed`), never a driver message.

## GET /system/status
```json
{
  "service":"katomaran-api","version":"0.1.0","environment":"development","uptime_seconds":125.0,
  "database":{"status":"ok","migration_revision":"0002","migrations_applied":true},
  "components":{
    "video_worker":{"state":"ready","detail":"Worker 'worker-1' connected."},
    "face_detector":{"state":"ready","detail":"Model 'yolov11n-face' available."},
    "face_recognizer":{"state":"not_implemented","detail":"Planned for Phase 2."},
    "tracker":{"state":"not_implemented","detail":"Planned for Phase 3."}},
  "active_video_sources":0,"video_source_configured":true
}
```
Component `state` is one of `not_implemented | not_configured | not_running | ready | active | unavailable`.
`video_worker` / `face_detector` are `not_running` until a worker connects (the model is verified by the worker, which
may live on another host), `ready` once connected and the model is verified, `active` while a session runs, and
`unavailable` when the worker reports the model missing or corrupt. If the database is down this endpoint still returns
`200` with `database.status = "unavailable"`.

## GET /config – sanitised configuration
```json
{"environment":"development",
 "video":{"source_type":"file","source_configured":false,"reconnect_interval_seconds":5.0},
 "detection":{"frame_skip":2,"confidence_threshold":0.5,"model":"yolov11n-face","input_size":640},
 "stream":{"max_fps":15.0,"max_width":960},"recognition":{"similarity_threshold":0.55},
 "tracking":{"max_missing_seconds":2.0},"logging":{"level":"INFO"}}
```
Never exposed: `DATABASE_URL`, `WORKER_TOKEN`, video source locations (paths and RTSP URLs can embed credentials), `detection.model_path`, absolute paths, log file locations.

## Error convention
All errors: `{"error":{"code","message","request_id","details"}}`

| Status | code | When |
|---|---|---|
| 404 | `not_found` | unknown route/resource |
| 422 | `validation_error` | invalid request; `details` = `[{loc,message,type}]` |
| 503 | `database_unavailable` | unhandled SQLAlchemy error |
| 500 | `internal_error` | unexpected failure; message is generic, details are only in logs |

---

# Phase 1 – sources, processing sessions, live stream

## Detection metadata (the contract everything else builds on)
```json
{"frame_id":120,"timestamp_ms":4800,"frame_width":960,"frame_height":540,
 "faces":[{"bbox":[243,155,297,224],"confidence":0.8018}],"inference_ms":30.19}
```
(illustrative values; real values come from the detector - nothing here is persisted.)
* `bbox` = `[x1, y1, x2, y2]` **integer pixels in the original frame** (`frame_width × frame_height`), origin top-left,
  `x1 < x2`, `y1 < y2`, clipped to the frame. Enforced by the `FaceBox` / `DetectionResult` validators.
* `confidence` ∈ [0, 1] is a *detector* score, **not** an identity or match score.
* `frame_id` is the 0-based index among frames delivered by the source; `timestamp_ms` = `frame_id/fps·1000` for
  files, monotonic ms since open for live streams. `frame_skip = N` ⇒ the detector runs on `0, N+1, 2(N+1), …`.

## GET /sources
```json
{"sources":[{"id":"sample-synthetic-portrait","label":"Sample: synthetic-portrait","type":"file",
             "availability":"available","detail":null},
            {"id":"front-door","label":"Front door","type":"rtsp","availability":"unavailable",
             "detail":"The camera address is not configured on the server."}],
 "worker":{"connected":true,"worker_id":"worker-1","model_available":true,"model_detail":null}}
```
Sources are trusted server-side definitions (`config.json` `video.sources[]`, `video.sample_dir`, legacy
`video.source`). **No path, URL, credential or env-var name is returned**; the browser can only pick an `id`.
`GET /sources/{id}` returns one source (404 `source_not_found`).

## Sessions
`SessionOut`:
```json
{"id":"b7c3…","state":"RUNNING","source":{"id":"…","label":"…","type":"file","availability":"available","detail":null},
 "created_at":"…","started_at":"…","ended_at":null,"stop_reason":null,"error":null,"connection":"connected",
 "stats":{"frames_read":90,"frames_processed":30,"frames_skipped":60,"frames_dropped":0,"corrupt_frames":0,
          "detections_total":28,"faces_last_frame":1,"max_faces_in_frame":1,"last_inference_ms":24.0,
          "avg_inference_ms":22.5,"processing_fps":8.4,"last_frame_id":87,"frame_width":960,"frame_height":540,
          "source_fps":25.0,"elapsed_seconds":10.8},
 "settings":{"model":"yolov11n-face","frame_skip":2,"confidence_threshold":0.5}}
```
`state` ∈ `IDLE | STARTING | RUNNING | STOPPING | STOPPED | COMPLETED | ERROR`. `detections_total` is the sum of faces
over analysed frames - **not** a count of people.

| Endpoint | Behaviour |
|---|---|
| `POST /sessions {"source_id"}` | `202` new session in `STARTING`. `200` + the same session if that source is already starting/running (idempotent). `404 source_not_found`, `409 source_unavailable`, `409 session_active` (a different source is running; `details.session_id`), `503 worker_unavailable`, `503 model_unavailable`, `422` for ids not matching `[a-z0-9][a-z0-9_-]*` or extra fields |
| `POST /sessions/{id}/stop` | Idempotent. `STARTING/RUNNING → STOPPING` (command sent to the worker); terminal sessions are returned unchanged; unknown id → `404 session_not_found`. No confirmation within `stop_timeout_seconds` ⇒ `STOPPED (stop_timeout)` |
| `GET /sessions/active` | `{"session": SessionOut or null}` |
| `GET /sessions?limit=10` | recent sessions, newest first (max 50) |
| `GET /sessions/{id}` and `/stats` | status / `SessionStats` only |

Error codes surfaced in `SessionOut.error.code`: `source_not_found`, `source_invalid` (file unreadable),
`source_corrupted`, `camera_unavailable`, `model_unavailable`, `worker_busy`, `worker_lost`, `worker_lost_session`,
`start_timeout`, `api_restarted`, `api_shutdown`, `processing_error`. Messages never contain paths or URLs.

## WebSocket `/sessions/{id}/stream` (browser)
Server → client only (client messages are ignored). The first message is always a snapshot.
* text: `{"type":"snapshot"|"state"|"ended","session":SessionOut}` and `{"type":"stats","stats":SessionStats}`
* binary: `[uint32 big-endian N][N bytes UTF-8 JSON header][JPEG]`, header = `{"session_id", …DetectionResult,
  "published_at_ms"}`.

Unknown session ⇒ close `4404`; disallowed `Origin` ⇒ close `1008`; a finished session gets its snapshot and a normal
close. Slow clients lose frames (latest wins) but never accumulate memory.

## WebSocket `/worker/ws` (worker only)
`Authorization: Bearer $WORKER_TOKEN` is required (otherwise HTTP 403). Message types are listed in ADR 0008.
