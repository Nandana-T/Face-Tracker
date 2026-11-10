# API contracts (v1)

Base path `/api/v1`. OpenAPI: `/api/openapi.json`, Swagger UI: `/api/docs`. Source of truth:
`backend/app/schemas/system.py`; mirrored by `frontend/src/lib/api-types.ts` (kept in sync by hand in Phase 0; a
generated client is a candidate once the surface grows).

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
  "database":{"status":"ok","migration_revision":"0001","migrations_applied":true},
  "components":{
    "video_worker":{"state":"not_implemented","detail":"Planned for Phase 1."},
    "face_detector":{"state":"not_implemented","detail":"Planned for Phase 1."},
    "face_recognizer":{"state":"not_implemented","detail":"Planned for Phase 2."},
    "tracker":{"state":"not_implemented","detail":"Planned for Phase 3."}},
  "active_video_sources":0,"video_source_configured":false
}
```
Component `state` ∈ `not_implemented | not_configured | not_running`. No component is ever reported as running in Phase 0.
If the database is down this endpoint still returns `200` with `database.status = "unavailable"`.

## GET /config – sanitised configuration
```json
{"environment":"development",
 "video":{"source_type":"file","source_configured":false,"reconnect_interval_seconds":5.0},
 "detection":{"frame_skip":2,"confidence_threshold":0.5},"recognition":{"similarity_threshold":0.55},
 "tracking":{"max_missing_seconds":2.0},"logging":{"level":"INFO"}}
```
Never exposed: `DATABASE_URL`, the video `source` value (RTSP URLs can embed credentials), absolute paths, log file locations.

## Error convention
All errors: `{"error":{"code","message","request_id","details"}}`

| Status | code | When |
|---|---|---|
| 404 | `not_found` | unknown route/resource |
| 422 | `validation_error` | invalid request; `details` = `[{loc,message,type}]` |
| 503 | `database_unavailable` | unhandled SQLAlchemy error |
| 500 | `internal_error` | unexpected failure; message is generic, details are only in logs |
