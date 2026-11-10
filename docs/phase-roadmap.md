# Phase roadmap

Each phase is a vertical slice (frontend + backend + DB + tests) and needs explicit approval to start.

| Phase | Scope | Acceptance criteria | Risks / dependencies |
|---|---|---|---|
| **0 – Foundation** (this) | Monorepo, FastAPI, Alembic, config, logging, design system, shell UI, health/ready/status/config, tests, docs | See README "Phase 0 status" | Hosted DB latency; Windows tooling |
| **1 – Video ingestion & detection** | File/RTSP source adapters, frame skipping, YOLO face detector in worker, `video_sources`/`processing_sessions` tables, Live Monitoring shows annotated frames/stream status | Process a sample video end-to-end; detections per frame logged; frame_skip honoured; worker survives source loss (reconnect) | Model weights on D:, CPU speed, RTSP reachability, codec support |
| **2 – Embeddings & identity** | InsightFace embeddings, `visitors`/`embeddings` tables, matching + auto-registration, Visitors page | Same person re-identified across videos; unseen person registered once; threshold tunable | False merges/splits; threshold needs data-driven validation; privacy of biometric data |
| **3 – Tracking & visit lifecycle** | ByteTrack (or equivalent), `tracks`, `visits`, `track_segments`; lifecycle rules | No duplicate visit for one continuous presence; occlusion within `max_missing_seconds` keeps visit | ID switches, crowded scenes |
| **4 – Events & image storage** | Entry/exit events (exactly one each per visit), face-crop storage via `ImageStore`, `events.log`, Event Logs page | Unique constraint prevents duplicates; images on disk with timestamped paths | Idempotency under worker restart; storage migration |
| **5 – Analytics** | Counts, trends (Recharts), Overview metrics, Analytics page | Unique-visitor counts match DB ground truth in tests | Query performance on large event tables |
| **6 – Real-time & RTSP hardening** | WebSocket/SSE updates, RTSP reconnect, notifications, multi-source | UI updates < 2 s after event; stream drop/recovery handled | Network security, NAT, auth for stream endpoints |
| **7 – Hardening & deployment** | Auth, benchmarks (measured), load tests, CI/CD, deployment of API/frontend/worker, backups, docs | Documented benchmarks; reproducible deploy; security review | Worker hosting cost (CPU/GPU) |

Phase 0 deliberately contains no detection, recognition, tracking or analytics. Recharts is added in Phase 5 when it is first used.
