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
 "stream":{"max_fps":15.0,"max_width":960},
 "recognition":{"enabled":true,"model":"sface-2021dec","similarity_threshold":0.5,"novelty_threshold":0.4,"ambiguity_margin":0.05,"…":"no paths"},
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

# Phase 3 – recognition, persistent visitors, gallery

Three identifiers, never interchangeable: **`track_id`** (temporary, one processing session), **`visitor_id`** (persistent facial identity; short label `V00124`) and
`visit_id` (one presence episode - Phase 4; see the section below). All endpoints below require the bearer token; **no response ever contains an embedding, an image path or a biometric template**.

## Worker → API: recognition observation (binary message)
Same envelope as a video frame (`[uint32 N][JSON header][payload]`), but the header has `"kind": "observation"` and the payload is an optional small JPEG crop (≤ 192 px) – **never relayed to browsers**.
```json
{"kind":"observation","session_id":"b7c3…","track_id":4,"seq":2,"model_version":"sface-2021dec","dim":128,
 "embedding_b64":"<512 bytes float32 LE, base64>","quality":0.71,"reasons":[],"timestamp_ms":4200,"frame_id":105}
```
`embedding_b64 = null` + `reasons:["blurry"]` = the face failed the quality gate (reported so the UI can say *Low quality*). `seq` is per track; `session_id:track_id:seq` is the idempotency key – a duplicate or
delayed message is ignored. The API validates every field (types, ranges, base64, exact byte length) and drops invalid messages without affecting the stream. The worker's `hello` additionally reports
`recognition:{key,enabled,available,detail}` (detail is generic – no server paths).

## API → browser: identity events (text, on the existing stream)
```json
{"type":"identity","session_id":"b7c3…","track_id":4,"state":"REGISTERED","event":"registered",
 "visitor":{"id":"6f1c…","number":124,"label":"V00124"},"similarity":null,"reason":null}
{"type":"identities","identities":[ {…same shape…} ]}        // sent once to a browser that (re)connects mid-session
```
`state` ∈ `UNRESOLVED | INSUFFICIENT_QUALITY | MATCH_PENDING | RECOGNIZED | AMBIGUOUS | REGISTRATION_PENDING | REGISTERED | REJECTED`. `event` is `registered | recognized | ambiguous | failed | conflict | state` –
the UI notifies only for the first four and only once per (event, track, visitor); `state` changes are silent. `visitor` is non-null only for RECOGNIZED/REGISTERED. A track's identity is meaningful only together with its `session_id`.

## REST
| Endpoint | Behaviour |
|---|---|
| `GET /visitors?limit=24&offset=0&q=&sort=last_seen\|first_seen\|number&order=desc\|asc` | `{items:[VisitorOut], total, limit, offset}`; `limit` 1–100; `q` = `V00012`, `12` or a hex id prefix (≥ 4 chars); invalid `sort`/`limit` → 422 |
| `GET /visitors/{id}` | `VisitorDetail`: identity, first/last seen, `recognition_count`, `sessions_count`, `tracks_count`, `associations[]` (session, source label, **session-scoped** `track_id`, state, similarity, observations, conflicts). `{id}` = 32-hex id or the label (`V00124`/`124`). 404 `visitor_not_found` |
| `GET /visitors/{id}/image` | the representative crop as `image/jpeg`, `Cache-Control: private, no-store`; 401 without a token; 404 `image_not_found`. The file path is resolved server-side and confined to the image store; there is no static route to the face directory |
| `DELETE /visitors/{id}` | right to erasure: removes embeddings, image files, associations and the in-memory index entry; `204`, repeat → 404 |
| `GET /recognition/status` | `{enabled, model, model_version, worker_available, worker_detail, registered_visitors, indexed_embeddings}` |
| `GET /recognition/stats` | `{registered_visitors, associations, runtime:{…counters, latencies…}, thresholds}` – counters only |
Recognition switched off → `503 recognition_disabled` on visitor endpoints. `VisitorOut` = `{id, number, label, status, first_seen_at, last_seen_at, recognition_count, has_image, model_version}`;
`recognition_count` = tracks recognised as this visitor, **not visits**. There is no unique-visitor count and no entry/exit event anywhere in the API.

## Session / stats additions
`SessionSettings.recognition_enabled`; `SessionStats` gains worker counters `recog_enabled, recog_submitted, recog_dropped, recog_processed, recog_usable, recog_errors, recog_avg_ms, recog_queue_peak`;
`GET /sources` → `worker.recognition_available / recognition_detail`; `GET /system/status` reports the tracker and recognizer components from the worker. Identity associations are persisted **before** a session is marked finished.

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

# Phase 4 – visits, entry/exit events, evidence images
Identifiers: **`visitor.id`** (person) ≠ **`visit_id`** (one presence episode) ≠ event **`id`**. A track id never appears here. All endpoints need the bearer token; **no response contains an embedding, a storage path or a credential**, and
`503 events_disabled` is returned when `visits.enabled` is false. There is no unique-visitor count in the API.

## Event / visit objects
```json
// EventOut
{"id":"…32hex","visit_id":"…","type":"entry|exit|interrupted","visitor":{"id":"…","number":7,"label":"V00007"} | null,
 "session_id":"…","source_id":"hall","source_label":"Hall camera",
 "event_time":"2026-11-11T10:03:05.120Z",      // first (entry) / LAST defensible (exit) observation, wall clock
 "video_ms":51000, "recorded_at":"…",            // recorded_at = when the event was confirmed and stored
 "image_status":"STORED|PENDING|UNAVAILABLE|MISSING|ERASED","has_image":true,
 "image_kind":"observation|last_good_observation|entry_representative|null",
 "persistence_status":"PENDING|DELIVERED|DEAD",   // events.log delivery
 "exit_basis":"absence|observation_ended|interrupted|null","closure_reason":"absence_timeout|end_of_stream|manual_stop|worker_lost|api_shutdown|api_restarted|source_error|null"}
// VisitOut: id, visitor|null, session_id, source_id/label, state (ACTIVE|TEMPORARILY_MISSING|EXIT_PENDING|CLOSED|INTERRUPTED),
//           entry_time, last_observed_time, exit_time|null, exit_confirmed_at|null, exit_basis, closure_reason, track_segments,
//           duration_seconds (video time; ONLY for state CLOSED, null for open/interrupted visits),
//           integrity: open|complete|pending|evidence_missing|delivery_failed
// VisitDetail = VisitOut + events[]
```
`visitor` is `null` after the visitor was erased (the row remains as an anonymous audit record). `exit` means **the last sighting**, not a measured departure; `observation_ended` = the video ended or monitoring stopped while the visitor was still in view.

## REST
| Endpoint | Behaviour |
|---|---|
| `GET /events?limit=25&offset=0&type=&visitor=&since=&until=&session_id=&visit_id=&persistence_status=&sort=event_time\|recorded_at&order=desc` | `{items:[EventOut], total, limit, offset}`; `limit` 1–100; `visitor` = `V00012`, `12` or an id prefix (≥ 4 hex); `since`/`until` ISO times on `event_time`; invalid values → 422 |
| `GET /events/{id}` | one event; 404 `event_not_found` (also for malformed ids) |
| `GET /events/{id}/image` | the evidence crop, `image/jpeg`, `Cache-Control: private, no-store`; 404 `image_not_found` when none/missing/erased; the path is resolved server-side and confined to the evidence root; **no static route** exists |
| `GET /visits?state=&visitor=&since=&until=&sort=entry_time\|exit_time&order=` | paged `VisitOut` |
| `GET /visits/{id}` · `GET /visits/{id}/events` | `VisitDetail` / its `EventOut[]`; 404 `visit_not_found` |
| `GET /events/health` | persistence health: events by persistence/image status, open visits, pending image retries, counters (duplicates ignored, conflicts, redeliveries, dead), latency (db/image/log), last reconciliation, whether `events.log` exists |
| `POST /events/reconcile` | run reconciliation now (deliver missing log lines, retry/flag evidence, remove orphans, complete erasures, interrupt visits of sessions that are not live) → `{result:{…counts}}` |
| `GET /events/log-verification` | `events.log` vs database report (see `scripts/verify_events_log.py`) |
`DELETE /visitors/{id}` now also deletes the visitor's evidence images and anonymises their visits/events.

## Stream additions (API → browser, text)
```json
{"type":"visit_event","event":{ "id":…,"visit_id":…,"type":"entry","visitor":{…},"event_time":…,"session_id":…,"source_label":…,"image_status":…,"persistence_status":…,"exit_basis":null,"closure_reason":null }}
{"type":"visit_state","visit_id":…,"visitor_id":…,"visitor_number":7,"state":"TEMPORARILY_MISSING"}
{"type":"visits","visits":[{"visit_id":…,"visitor_id":…,"visitor_number":7,"state":"ACTIVE"}]}   // sent once to a browser that (re)connects mid-session
```
The browser notifies once per event `id` (a replay is silent). `visit_event` is sent after the database commit.

## Worker → API
**Unchanged.** Presence is derived by the API from the per-frame header (`frame_id`, `timestamp_ms`, `tracks[].{track_id,state,observed,first_seen_ms}`) and the Phase 3 observations; the worker needs no database access and sends no visit messages. Frames with
a non-increasing `frame_id`, a time going backwards, a negative time or malformed tracks are ignored; the existing size limits apply. Session end reasons map onto visits as described in ADR 0014.

# Phase 5 – analytics
All routes are authenticated (`/analytics/*`), validated and free of paths, embeddings and secrets. **Definitions are part of the contract** (also returned in `overview.definitions`): *registered visitors* = identities in the database (global); *unique visitors* = `COUNT(DISTINCT visitor_id)` over visits in scope; *visits* = presence episodes; *entry/terminal events* = logical events (terminal kinds `absence`, `observation_ended`, `interrupted` are never merged and none is a verified departure); *returning* = seen in a later session than the registering one; *active visits* = open visits of the live session; *stale open visits* = open rows of non-live sessions (not counted); *unresolved tracks* = coverage diagnostic. **None is a count of physical people.**
Common query parameters: `since` (inclusive) / `until` (exclusive) ISO 8601 on the visit **entry time**, naive = UTC, `since ≥ until` → 422 `invalid_range`; `source_id`; `session_id` (32 hex).
| Endpoint | Returns |
|---|---|
| `GET /analytics/overview` | `registered_visitors` (global), `unique_visitors_in_scope`, `returning_visitors_in_scope`, `new_visitors_in_scope`, `visits`, `visits_without_identity` (erased), `entry_events`, `terminal_events`, `terminal_by_basis{absence,observation_ended,interrupted}`, `active_visits`, `active_visits_temporarily_missing`, `stale_open_visits`, `unresolved_tracks`, `generated_at`, `last_event_recorded_at`, `definitions` |
| `GET /analytics/timeline?bucket=hour\|day` | UTC buckets `{bucket_start, visits, entries, unique_visitors (not additive), exits_absence, exits_observation_ended, interrupted}`, empty buckets filled, `truncated` when more than 1000 |
| `GET /analytics/sources` | per source: visits, unique visitors, sessions, last entry |
| `GET /analytics/sessions?limit&offset` | per session (newest first): source, state, `live`, visits (all of the session), unique visitors, interrupted, visitors registered here; paged |
| `GET /analytics/coverage` | diagnostics: confirmed tracks, tracks evaluated / resolved / unresolved (+ breakdown), crops analysed / usable / rejected / dropped, errors, `resolved_share_of_evaluated_tracks` + a disclaimer (**not person-level recall**) |
| `GET /analytics/recent?limit` | the latest entry/exit events in scope (`EventOut`) |
| `GET /analytics/reconciliation` | read-only independent recount: `{consistent, errors, warnings, checks[{name, ok, expected, actual, detail, severity}], mode}` |
| `POST /analytics/stream-ticket` → `WS /analytics/stream?ticket=` | change notices `{"type":"analytics_changed","reason":"visit_event\|visitor_registered\|visit_state\|session_started\|session_ended"}` after a `hello`; **no counts**; the ticket is single-use, 30 s and bound to `analytics` (a session ticket does not open it) |

