# Phase 1 technical report – video ingestion and real-time face detection

Status: implemented and verified on the development machine; **awaiting approval**. Nothing is committed or pushed.
Scope: detection only – no recognition, embeddings, identity, tracking, entry/exit logging, unique-visitor counts or analytics.

## 1. What was built
| Area | Delivered |
|---|---|
| Sources | `VideoSource` interface; `FileVideoSource` (EOF vs corrupt-frame detection, FPS/size extraction, idempotent close); `RtspVideoSource` (OpenCV/FFmpeg, TCP transport, connect/read timeouts, exponential back-off reconnect, bounded attempts, credential redaction, clean stop during reconnect); server-side source registry (ids only reach the browser) |
| Detector | Pinned **YOLO11n-face** (WIDERFACE) on **ONNX Runtime CPU**; letterbox pre-processing, decode + NMS, original-frame pixel boxes; `FaceDetector` interface for swapping/benchmarking; explicit checksum-verified download (`scripts\download-model.ps1`), actionable errors, no start-up downloads |
| Worker | `python -m app.workers.video_worker` – long-running agent: reader thread → bounded queue → frame skip → detector → JPEG + metadata → latest-wins slot → WebSocket. `--check` keeps the Phase 0 config/DB check |
| Transport | Worker dials the API (`/api/v1/worker/ws`, bearer token); binary frame messages relayed unchanged to browsers (`/sessions/{id}/stream`); heartbeats, watchdogs, bounded messages and queues (ADR 0008/0009) |
| API | `/sources`, `/sessions` (start/stop/status/stats/active/list), state machine `IDLE…ERROR`, idempotent start/stop, single active session guaranteed by the database, orphan recovery; `/system/status` reports real worker/detector state |
| Data | Migration `0002` `processing_sessions` (summary only; no frames/boxes); RLS on; one index justified by the only list query |
| Frontend | Live Monitoring console: canvas with letterbox-correct boxes + confidence labels, source radio-group, Start/Stop/Retry, 13 honest status states with actionable copy, detection summary + expandable details, Motion transitions (reduced-motion aware), `aria-live` announcements, tablet layout, re-attach after reload |
| Quality | 168 backend, 119 frontend, 18 Playwright tests; strict lint/type/build; reproducible benchmark |

## 2. Model (selected: YOLO11n-face)
Source `AdamCodd/YOLOv11n-face-detection` @ `0e97fea5…`, `model.onnx`, 10 583 429 B, SHA-256 `2dfe1417…`. Input `1×3×640×640`
RGB/255 letterboxed (grey 114); output `1×5×8400` (`cx,cy,w,h,conf`). Reported WIDERFACE val AP 0.942 / 0.921 / 0.810
(easy/medium/hard; model-card figures, **not re-measured**). Chosen for face-specific training, 10 MB size, 31 ms CPU inference,
no PyTorch dependency, and a clean hand-off to InsightFace crops in Phase 2. **Licence is ambiguous (card: Apache-2.0; file metadata
and upstream: AGPL-3.0; dataset: non-commercial) – treat as AGPL/non-commercial** (ADR 0007).

## 3. Conventions (also in `docs/api-contracts.md`)
Box `[x1,y1,x2,y2]` integer px in the **original frame**, top-left origin, clipped; confidence ∈ [0,1] (detector score, not identity);
`frame_id` 0-based among delivered frames; `timestamp_ms` = `frame_id/fps` for files, monotonic for live sources;
`frame_skip=N` ⇒ N frames skipped between detections (every (N+1)th analysed), skipped frames carry no detections.

## 4. Measured results (this machine: i5-1240P, 15.7 GB, no usable GPU)
Full tables: [`benchmarks/phase1-benchmark.md`](benchmarks/phase1-benchmark.md).
* Model init ≈ 85 ms. Inference ≈ 31 ms/frame (auto threads), 41 ms (4 threads), 85 ms (1 thread); whole detect step ≈ 39 ms (≈ 25 frames/s flat out).
* Source resolution barely matters (640×360 → 1920×1080: +2 ms), the model input is fixed.
* Pipeline on a 1000-frame video: `frame_skip=0` 23 fps (0.92× real time), `=2` 2.4× real time, `=5` 4.5×; RSS ≈ 240 MB, flat (bounded queues).
* **Real-time cost of one stream (25 fps, skip 2):** ORT default spin-wait used **71 %** of the machine; with spin-wait off and 4 threads **6.8 %**
  (the shipped defaults).
* Worker → API → browser-socket delivery on loopback: mean 6.6 ms, p95 10 ms, p99 14 ms.
* GPU: not measured. Capacity for several concurrent streams: not measured (extrapolation only).

## 5. Results from real sample video
No hackathon sample video was available. A **synthetic** clip (public-domain NASA portrait panned/zoomed over a plain background,
`app.vision.sample_video`) was processed end to end through the real worker, model, API, database and browser:
* shipped config, 8 s / 200 frames: 200 read, 67 analysed (8.4/s, real-time paced), 133 skipped, 0 dropped, 0 unreadable, `COMPLETED`;
  detections stop in the clip's deliberately face-free last 2 s.
* two-face clip: in every streamed frame that was checked (the face-free tail excluded) both faces were found and the streamed boxes matched the known face positions (IoU > 0.5, backend slice test).
* browser: the cyan stroke drawn on the canvas overlaps the known face location with IoU > 0.6 on the last analysed frame (Playwright pixel test).
* real photograph (not video): face found with confidence 0.87; blank and random-noise frames: no detections.
This demonstrates plumbing and coordinate alignment. It is **not** an accuracy evaluation on real footage.

## 6. RTSP – scope and limits
Tested: adapter logic with scripted captures (open failure, read failure → reconnect with exponential back-off, bounded attempts, stop during
back-off, resource release, credential redaction) and the **real OpenCV/FFmpeg code path against a closed local port** (fails with
`camera_unavailable` inside the configured timeout; bounded reconnect gives up). **No RTSP camera or RTSP server was used, so decoding,
real latency, codec support and network behaviour are untested.** Do this when a camera is available.

## 7. Known limitations / risks
1. **Database credential not rotated (blocker for cloud operations).** The Supabase password from Phase 0 is still in `.env`. Migration `0002` has been verified only on a local PostgreSQL; it has **not** been applied to Supabase. Tests no longer touch the hosted DB.
2. **No user authentication** on REST or the browser stream (Origin allow-list + unguessable session id only). Do not expose publicly with real footage.
3. **Model licence ambiguity** (ADR 0007). Accuracy on real footage unmeasured; model is weakest on small/profile/occluded faces.
4. One worker and one active session; live state is in the API process memory (single-instance API). Scale-out needs a shared bus.
5. Variable-frame-rate files use nominal fps for `timestamp_ms`. Live pacing of files is approximate (`threading.Event.wait`).
6. Docker, `render.yaml` and the Dockerfile are unverified (Docker is not installed here). The API image also pulls OpenCV because the source registry is shared.
7. Playwright's built-in `webServer` teardown hangs on this Windows setup, so the E2E suite manages processes itself (`global-setup.ts`, `stop_stack.py`). Harmless but non-standard.
8. The frontend shows nothing about *which* faces appear in a frame beyond boxes/confidence – by design.
9. Phase 0 tests were modified where behaviour intentionally changed: component states (`not_implemented` → real worker/detector state) and the `/live` placeholder page.

## 8. Defects found and fixed during this phase (kept as regression tests)
* Worker-disconnect cleanup ran in a cancelled scope and left sessions `RUNNING` with no worker (now a shielded task; test).
* Canvas never painted under React StrictMode (animation-frame flag not reset on cleanup; test fails without the fix).
* Overlay and result banner showed the same heading simultaneously (single `AnimatePresence` in `wait` mode; test).
* Finished sessions lost most stats (now a persisted `summary`); browser disconnects raised unhandled transport errors; ORT idle spin-wait burned ~60 % extra CPU.

## 9. Phase 2 readiness
Ready, provided the owner (a) rotates the DB credential and applies `0002`, (b) confirms the model licence stance, (c) supplies real consented footage.
Integration points: `DetectionResult`/`FaceBox` (add crops), `ProcessingPipeline._consume` (embed after detect), `vision/recognition` interfaces,
`ImageStore` for crops (Phase 4), `track_id` slot in `FaceBox` (Phase 3). Phase 2 must add: embedding model download/pin, visitor/embedding tables,
a threshold-validation dataset, and an updated privacy review (embeddings are biometric templates).
