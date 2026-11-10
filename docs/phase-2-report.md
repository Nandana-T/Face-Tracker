# Phase 2 report – production-oriented multi-face tracking

Status: implemented and verified on this machine. **Nothing committed or pushed. Phase 3 not started.**
Boundary kept: a *track* is a **temporary, session-scoped** hypothesis. There is no recognition, no persistent visitor, no visitor
count and no entry/exit event; the UI and API never call a track count a visitor count.

## 1. Tracker selected and why
A **ByteTrack-style tracker implemented in this repository** (`backend/app/vision/tracking/`, `numpy` only, no new dependency): IoU association of
Kalman-predicted boxes with detections in two stages (high-score detections first; low-score ones may only extend existing tracks), plus
tentative-track handling. SORT was rejected (no weak-detection handling; GPL-3.0 reference code), OC-SORT is a later option, appearance-based trackers were
rejected because an appearance network is recognition and out of scope. Full comparison, dependencies, licensing and limitations:
[ADR 0012](decisions/0012-tracker-selection.md).

## 2. Architecture and data flow
```
source → reader thread → bounded queue → [detector every (N+1)th frame] → FaceByteTracker.update(dets, timestamp_ms, frame_id)
       → DetectionResult(faces, tracks, tracking) → binary frame header → API (validates tracks) → browser (canvas overlay + panel)
```
* The tracker runs in the worker's inference thread (one instance per session); the API never tracks and the queues stay bounded; no extra frame copies.
* **Frame skipping:** updated **only on analysed frames** with the real timestamp ⇒ `dt` is 120 ms at `frame_skip=2` on 25 fps footage; age, expiry and
  velocity are in seconds and independent of `frame_skip`/FPS. Skipped frames publish nothing, so no stale box is ever shown as a fresh detection.
  `frame_skip` semantics are unchanged (0, N+1, 2(N+1)…).
* **Observed vs predicted:** every published box says whether a detection was matched in that frame (`observed`); predicted boxes carry `detection_confidence = null`
  (never invented) and only `last_observed_confidence`.
* Failure containment: a tracker exception is counted (`tracks_errors`), the tracker is reset and detection continues (tested).

## 3. Track lifecycle (deterministic, time-based)
| From → to | Rule (defaults) |
|---|---|
| (new) → TENTATIVE | unmatched detection with score ≥ `high_confidence` (0.6); weak detections never start a track; refused beyond `max_tracks` (200) |
| TENTATIVE → CONFIRMED | `confirm_hits` (3) observed updates |
| TENTATIVE → REMOVED ("rejected") | no detection for `tentative_timeout_seconds` (0.5 s) |
| CONFIRMED → LOST | an analysed frame without a matching detection |
| LOST → CONFIRMED ("recovered") | a matching detection (IoU of buffered boxes ≥ `lost_match_iou` 0.1); the id is kept |
| LOST → REMOVED | no detection for `max_missing_seconds` (2 s); the id is never reused |
| any → (reset) | the tracker is discarded/reset when the session ends; the next session starts at id 1 |
Repeated updates with the same data are idempotent apart from time advancing; a backwards timestamp counts as "no time passed" (`timestamp_anomalies`); invalid boxes (NaN, inverted, zero-size) are dropped and counted.
**When a new id is expected:** a face hidden or out of frame longer than 2 s; re-entering at the frame edge as a thin sliver; reappearing far from its predicted position
after a long gap; two overlapping faces whose motion changes during the overlap (identity can swap). Motion-only tracking does not promise identity through these.

## 4. Files
**New:** `backend/app/vision/tracking/{tracker,kalman}.py`, `backend/app/schemas/tracking.py`, `backend/benchmarks/{evaluate_tracking,tracking_metrics,synthetic_tracking_benchmark}.py`,
`backend/tests/unit/{test_tracker,test_tracking_pipeline,test_tracking_metrics}.py`, `frontend/src/lib/tracks.ts`, `frontend/src/features/monitoring/tracking-panel.tsx`,
`frontend/src/test/tracking.test.tsx`, `docs/decisions/0012-tracker-selection.md`, `docs/benchmarks/phase2-tracking.{md,json}`, this report.
**Modified:** `vision/tracking/interfaces.py` (contract), `workers/pipeline.py` (tracker hook, tracking stats), `workers/agent.py` (tracker per session), `schemas/{detection,processing}.py`,
`services/session_manager.py` (validation, settings snapshot), `core/config.py` + `config/config*.json` (tracking section), frontend `video-canvas`, `use-monitoring`, `monitoring-page`, `api-types`, `protocol`, test mocks/setup,
e2e spec, and the docs listed in §12.

## 5. API changes (additive; Phase 1 consumers keep working)
Frame header: `tracks[]` + `tracking{}` (session id already in every header); `SessionStats`: flat `tracks_*` counters; `SessionSettings.tracking_enabled`; `GET /config` exposes the tracking section.
The API validates track metadata and drops invalid frames. Reconnecting clients need no history: each frame carries the complete current track set. Contract: [api-contracts.md](api-contracts.md#tracking-metadata-phase-2-additive--everything-above-is-unchanged).
Authentication and single-use stream tickets are untouched (the Phase 1.5 live access-control tests still pass).

## 6. Database changes and migration status
**None needed – no migration `0003`.** The tracking counters are flat numbers inside the existing `processing_sessions.summary` JSON column (added by `0002`); per-track rows were deliberately not
created (a track has no value after its session, and Phase 3/4 will decide what must be stored once identity and visits exist). No row per frame. Verified on real PostgreSQL
(`test_tracking_counters_round_trip_through_postgresql_summary`, incl. a JSON `->>` query) and via the API on SQLite. **Supabase was not touched this phase** (still revision `0002`, verified in Phase 1.5); no cloud write was needed.

## 7. Frontend
Live Monitoring now has: stable `#id` labels on the canvas, state colours (green confirmed, yellow tentative, grey lost), dashed boxes for predicted positions, crowding-aware labels (bare ids above 8 tracks), a **Tracking** panel
(confirmed / tentative / lost now; diagnostics: created, reached CONFIRMED, rejected, expired, recoveries, tracker ms, errors; ID-switch metrics shown as "not measurable without ground truth"),
a clickable track list and canvas click-to-select, a **track details** panel (id, state, first/last seen, age, last observed confidence, box, detected-vs-predicted, missed updates), **Boxes** and **Labels** toggles, selection highlight (white box),
an "ended" state for a track that disappears while selected, and a note when tracking is disabled server-side. Overlay changes are pushed imperatively to the canvas (no per-frame React state). Motion for React drives the details panel and the collapsible diagnostics
(`MotionConfig reducedMotion="user"` already applies; global CSS also disables transitions under `prefers-reduced-motion`). Copy states that a track id is not an identity and totals are not visitor counts.

## 8. Real-video tracking results (details: [benchmarks/phase2-tracking.md](benchmarks/phase2-tracking.md))
7-clip subset of the hackathon footage (60 s hall clips + the crowded 4K sample), real YOLO detections, `frame_skip=2` (3,086 analysed frames): **116 tracks created, 77 reached CONFIRMED, 36 rejected as tentative flicker, 53 confirmed tracks expired, 72 lost→confirmed recoveries**;
median confirmed-track life 2.5–11.9 s (hall) / 3.4 s (crowd). These are track counts, **not people**. The sparse small-face clip is the weak spot (median 0.48 s). No ground truth exists for this footage, so none of this is accuracy.

## 9. ID switches and fragmentation
* **Real footage: not measurable** (no labels). The only automatic indicator is a proxy ("suspected fragmentation"): 3 across all 7 clips at the default skip – a hint, not a count of errors.
* **Manual review (one AI reviewer; ≈ 10 annotated frames/strips, not a substitute for labels):** ids stayed on the same face across consecutive analysed frames in the crowd (e.g. `#1 #4 #5 #10 #14 #18 #19 #20 #22` over six frames) and for a walking pair (`#1`, `#2`); tentative → confirmed
  promotion looked right; a first review exposed stacked flickering tentative boxes at one spot (a tiny face-like detection), which led to hiding unobserved tentative tracks and enabling a 10 % box buffer. Remaining visual weakness: LOST boxes keep a static grey "pred" box
  for up to 2 s at the spot where a turned-away head vanished (`#9`, `#11` in the crowd) – honest but noisy. No ID swap was *seen* in the reviewed frames; that is not proof there were none.
* **Synthetic ground truth** (known trajectories; validates the algorithm, not the footage): 0 ID switches for clean detections in every scene at every step; 0 over ten seeds for single/parallel/enter-leave scenes with 15 % misses + jitter + false positives; separated crossing 0; overlapping crossing 0 (constant velocity) but 8 switches summed over ten noisy seeds/steps;
  a 0.8 s detector failure keeps the id; a 4 s gap gives a new id (as designed); `frame_skip=5` or harsh noise degrade it. IDF1/MOTA there are from my own implementation, validated against hand-computed cases; HOTA is not computed.

## 10. CPU and memory
Tracker cost measured directly: **0.01–0.06 ms** per analysed frame (hall), **0.42 ms mean / 0.93 ms p99** (20 faces, 4K) ≈ 0.1–1 % of detector time. End-to-end A/B (real pipeline, 3 interleaved runs): wall +2 % at most, process CPU +0–0.5 %, RSS ±3 MB – **below the run-to-run noise**
(historical: 14.3 analysed frames/s, 55 ms inference, ≈ 520 MB RSS; unchanged). No extra thread, bounded memory. GPU and real RTSP not measured.

## 11. Test results (this machine, 2026-11-10 – all executed)
| Check | Result |
|---|---|
| Backend pytest (embedded PostgreSQL, real model) | **277 passed** (was 192) |
| ruff check / ruff format --check / mypy strict (83 files) | clean |
| Frontend vitest | **153 passed** (was 128) |
| ESLint (src + e2e), `tsc --noEmit`, Vite production build | clean / succeeded |
| Playwright (Edge), real stack, signed in | **23 passed** (was 21) |
| Total automated tests | **453** (baseline 341); no existing test removed or weakened |
Existing tests intentionally changed (behaviour changed by design, nothing weakened): the disclaimer copy assertion (`pages.test.tsx`, e2e) now expects "Face detection and temporary tracking"; the pixel-level alignment e2e now measures the green *confirmed-track* stroke instead of the cyan detection stroke
(labels use neutral text so they do not disturb the measurement); the test mocks/recording canvas gained `setLineDash`/colour recording.
New: 37 tracker lifecycle/geometry/logging tests, 9 pipeline-integration tests, 23 metric/synthetic-benchmark tests, 10 API relay/persistence tests (7 malformed-metadata cases), 5 config-validation cases, 1 PostgreSQL persistence test, 25 frontend tracking tests (mutation-checked: removing the dashed-prediction logic fails 2), 2 Playwright tracking tests (real model: stable id, details, overlay toggles, counters, reload re-attach, clean session numbering, cleanup).
What the tests do **not** prove: accuracy on real footage (no labels), RTSP, GPU, Docker/Render/Vercel, multi-user behaviour.

## 12. Documentation updated
README (status, capabilities, tracking configuration reference), architecture (diagram, tracking section, persistence note), API contracts, phase roadmap (renumbered: 3 = recognition, 4 = visit lifecycle/events), testing strategy, deployment, benchmark report, ADR 0012, Phase 1.5 report/README/privacy notes (Supabase status corrected).

## 13. Remaining limitations
1. **No ground truth for real footage**: real ID-switch/fragmentation/false-track/miss rates are unknown; defaults were calibrated on proxies plus synthetic data.
2. Motion-only association: overlapping crossings, group walking, long occlusions (> 2 s) and edge re-entries can swap or renew ids. By design a returning person gets a new track – **a track is never an identity**.
3. `frame_skip` ≥ ≈ 4 degrades tracking in crowds (measured: 4K crowd created 78 tracks at skip 5 vs 38 at skip 0). The default (2) is a compromise, not an optimum.
4. Sparse scenes with tiny faces (21–30 px) confirm slowly (3 hits needed) and flicker.
5. Predicted (grey) boxes can linger up to 2 s where a face vanished.
6. In-memory live state (single API instance), single shared admin login, no per-track persistence – all as before.
7. Not exercised: real RTSP camera, GPU, Docker/Render/Vercel, Supabase under load.
8. Pre-existing and unchanged: model licensing (ADR 0011) – decide before commercial use or before adopting InsightFace in Phase 3.

## 14. Acceptance verdict
**Accepted for the Phase 2 definition of done as implemented**, with limitation 1 stated plainly: every checklist item that can be verified without labelled data is verified (real YOLO detections feed the tracker, distinct ids, stable ids for continuous movement, missed detections handled,
expiry, explicit tested lifecycle, frame-skip handling, tracker in the worker, metadata at the API and in the UI, aligned boxes, selection, real diagnostics, session isolation, authenticated streaming intact, real footage evaluated, cost measured, all gates green, no identity claims).
"Track IDs remain stable during straightforward continuous movement" is shown on synthetic trajectories, in the e2e run, and visually on real clips, **not measured on labelled real footage**.

## 15. Readiness for Phase 3 (recognition & automatic registration)
Ready on the tracking side: stable temporary ids with a documented failure profile, per-track first/last-seen/age, a clean per-session reset, and metadata that an embedding stage can attach to (one embedding per confirmed track, not per frame – cuts cost).
Recommended before/while starting Phase 3: (1) decide the commercial/licence stance for face-recognition models (ADR 0011); (2) label ≈ 300 frames or a few 20-second clips (identities per track) so ID switches can be measured and Phase 3 thresholds validated; (3) treat any track id change as a possible new person only after
embedding comparison, never on tracker output alone. Not started.
