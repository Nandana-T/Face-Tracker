# Phase roadmap

Each phase is a vertical slice (frontend + backend + DB + tests) and needs explicit approval to start.

| Phase | Scope | Acceptance criteria | Risks / dependencies |
|---|---|---|---|
| **0 – Foundation** (done) | Monorepo, FastAPI, Alembic, config, logging, design system, shell UI, health/ready/status/config, tests, docs | README "Current state" table | Hosted DB latency; Windows tooling |
| **1 – Video ingestion & detection** (implemented, awaiting approval; `docs/phase-1-report.md`) | File/RTSP source adapters, frame skipping, YOLO face detector in worker, worker⇄API WebSocket, `processing_sessions` table (sources live in config), Live Monitoring shows annotated frames/stream status | Process a sample video end-to-end; detections per frame logged; frame_skip honoured; worker survives source loss (reconnect) | Model weights on D:, CPU speed, RTSP reachability, codec support |
| **2 – Multi-face tracking** (implemented, awaiting approval; `phase-2-report.md`) | ByteTrack-style tracker in the worker, temporary session-scoped track ids, TENTATIVE→CONFIRMED→LOST→REMOVED lifecycle (time-based), frame-skip-aware updates, observed/predicted boxes, tracking overlay + diagnostics panel, session counters | Stable ids for continuous movement; short misses recovered; expiry; session isolation; measured cost on real footage | No appearance model: crossings/long occlusions can switch or renew ids; unlabelled real footage |
| **3 – Recognition & automatic registration** (implemented, awaiting approval; `phase-3-report.md`) | SFace embeddings via ONNX Runtime, landmark alignment, quality gate, identity manager (recognise / defer / register), `visitors` / `face_embeddings` / `identity_associations` (migration 0003), visitor gallery + details, live recognition labels and notifications | Same person recognised across sessions and restarts; new person registered once; no duplicate under retries/concurrency; thresholds from a documented evaluation | Weights' training-data licence (ADR 0013); small-model accuracy on hard footage; no identity labels for the hackathon footage |
| **4 – Visit lifecycle, events & image storage** | `visits`, `track_segments` built on Phase 2 tracks; entry/exit events (exactly one each per visit), face-crop storage via `ImageStore`, `events.log`, Event Logs page | No duplicate visit for one continuous presence; occlusion within `max_missing_seconds` keeps the visit; unique constraint prevents duplicate events | Idempotency under worker restart; ID switches feeding wrong visits; storage migration |
| **5 – Analytics** | Counts, trends (Recharts), Overview metrics, Analytics page | Unique-visitor counts match DB ground truth in tests | Query performance on large event tables |
| **6 – Real-time & RTSP hardening** | WebSocket/SSE updates, RTSP reconnect, notifications, multi-source | UI updates < 2 s after event; stream drop/recovery handled | Network security, NAT, auth for stream endpoints |
| **7 – Hardening & deployment** | Auth, benchmarks (measured), load tests, CI/CD, deployment of API/frontend/worker, backups, docs | Documented benchmarks; reproducible deploy; security review | Worker hosting cost (CPU/GPU) |

Phase 0 deliberately contains no detection, recognition, tracking or analytics. Recharts is added in Phase 5 when it is first used.

Phase 1 adds detection only. It deliberately has no recognition, embeddings, identity matching, tracking, visits, entry/exit
events, unique-visitor counts or analytics, and the UI never labels a detection count as visitors. Open items carried into
Phase 2 are listed in `phase-1-report.md` (database credential rotation, real-footage accuracy, RTSP camera test, GPU, auth).

Phase 2 adds *temporary* tracking only. A track id is not a visitor identity, tracks are not stored, nothing is counted as a
visitor and no entry/exit events exist. The roadmap numbering was updated at the Phase 2 request: tracking moved before
recognition, and the visit lifecycle moved after it because visits need both tracks and identities.

Phase 3 adds *recognition* and *persistent identity* only. There are still no visits, no entry/exit events and no unique-visitor analytics: `recognition_count` counts recognised tracks, not people entering a place.
