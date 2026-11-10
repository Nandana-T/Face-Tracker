# API contracts (v1)

Base path `/api/v1`. OpenAPI: `/api/openapi.json`, Swagger UI: `/api/docs`. Source of truth:
`backend/app/schemas/{system,processing,detection}.py`; mirrored by `frontend/src/lib/api-types.ts` (kept in sync by
hand; a generated client is a candidate once the surface grows).

## Authentication (Phase 1.5)
Enabled when `ADMIN_PASSWORD` is set (mandatory in production). **Public:** `GET /health`, `GET /ready`, `GET /auth/status`,
`POST /auth/login`. **Everything else requires `Authorization: Bearer <token>`** and answers `401 {"error":{"code":"unauthorized"}}` otherwise.

| Endpoint | Behaviour |
|---|---|
| `GET /auth/status` | `{"required": bool, "authenticated": bool}` |
| `POST /auth/login {"password"}` | `200 {"access_token","token_type":"bearer","expires_in"}` (HMAC-signed, default 8 h); `401 invalid_credentials`; `429 too_many_attempts` after 5 failures per client in 5 min; `400 auth_disabled` when auth is off |
| `POST /sessions/{id}/stream-ticket` | `{"ticket","expires_in":30}`: single-use ticket for `…/stream?ticket=…`, bound to that session (`404` for unknown sessions) |

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
 "tracking":{"enabled":true,"high_confidence":0.6,"confirm_hits":3,"tentative_timeout_seconds":0.5,
             "max_missing_seconds":2.0,"match_iou":0.2,"low_match_iou":0.3,"lost_match_iou":0.1,
             "lost_velocity_decay_per_second":1.0,"box_buffer":0.1,"max_tracks":200},
 "logging":{"level":"INFO"}}
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

## Tracking metadata (Phase 2, additive – everything above is unchanged)
Every binary frame header gains two optional fields. Phase 1 consumers that ignore them keep working; `faces` still holds the
raw detections.
```json
{"session_id":"b7c3…","frame_id":125,"timestamp_ms":5000,"frame_width":960,"frame_height":540,
 "faces":[{"bbox":[120,80,220,200],"confidence":0.94}],"inference_ms":30.2,
 "tracks":[{"track_id":7,"state":"CONFIRMED","bbox":[120,80,220,200],"observed":true,
            "detection_confidence":0.94,"last_observed_confidence":0.94,
            "first_seen_ms":1200,"last_seen_ms":5000,"age_ms":3800,"hits":28,"missed_updates":0,"last_update_frame":125},
           {"track_id":9,"state":"LOST","bbox":[400,90,470,180],"observed":false,
            "detection_confidence":null,"last_observed_confidence":0.81,
            "first_seen_ms":3000,"last_seen_ms":4760,"age_ms":2000,"hits":14,"missed_updates":2,"last_update_frame":119}],
 "tracking":{"schema_version":1,"enabled":true,"tentative":0,"confirmed":1,"lost":1,
             "created_total":12,"confirmed_total":9,"tracking_ms":0.07}}
```
(illustrative values.)
* **`session_id` is in every header**; `track_id` is only meaningful together with it. Ids are small integers that restart at 1
  for each session, are never reused inside a session, and are **not** visitor identities (no recognition exists yet).
* `tracks` is the **complete current set** (TENTATIVE / CONFIRMED / LOST) for this frame – no history is needed, so a browser that
  connects or reconnects mid-session is correct from its first frame. Tentative tracks that were not detected in this frame are not
  published. Tracks whose predicted box lies entirely outside the picture are not published either.
* `observed: true` ⇔ a detection was matched to the track **in this frame** and `detection_confidence` is its detector score.
  `observed: false` ⇔ the box is a motion **prediction**; `detection_confidence` is then `null` (never invented) and only
  `last_observed_confidence` says how sure the last real detection was. A `LOST` track is never observed.
* Times are source-clock milliseconds (the same clock as `timestamp_ms`): `age_ms` = now − `first_seen_ms`; `last_seen_ms` = last
  time a detection was matched. `missed_updates` = consecutive analysed frames without a match; `last_update_frame` = frame index
  of the last match. Because the tracker is updated only on analysed frames (`frame_skip`), "updates" are analysed frames.
* Validation (worker and API, `schemas/tracking.py`): ids ≥ 1 and unique per frame, boxes inside the frame, observed ⇔ has a
  confidence, `LOST` ⇒ not observed. The API drops (and logs) a frame whose tracking metadata is invalid instead of relaying it.
* Tracking disabled (`tracking.enabled=false`): `tracks` is `[]`, `tracking` is `null`, and the UI shows raw detections only.
* `SessionStats` gains flat counters (all `0`/absent with tracking off): `tracks_enabled`, `tracks_confirmed`, `tracks_tentative`,
  `tracks_lost` (current), `tracks_created_total` (every track ever started, **including rejected tentative ones – not a number
  of people**), `tracks_confirmed_total`, `tracks_rejected_total`, `tracks_removed_total`, `tracks_recovered_total`,
  `tracks_invalid_detections`, `tracks_errors`, `tracking_ms_last`, `tracking_ms_avg`. They are persisted with the rest of the
  stats in `processing_sessions.summary`; `SessionSettings.tracking_enabled` records whether the session tracked.
* `GET /config` returns the validated `tracking` section (see the configuration reference in the README).

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

With authentication on, a valid single-use `?ticket=` is required (otherwise close `1008`; the access token is never put in a URL).
Unknown session ⇒ close `4404`; disallowed `Origin` ⇒ close `1008`; a finished session gets its snapshot and a normal
close. Slow clients lose frames (latest wins) but never accumulate memory.

## WebSocket `/worker/ws` (worker only)
`Authorization: Bearer $WORKER_TOKEN` is required (otherwise HTTP 403). Message types are listed in ADR 0008.
