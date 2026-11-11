# Phase 3 report – face recognition, persistent identity, automatic registration

Status: implemented and verified on this machine. **Nothing committed or pushed. Phase 4 not started. Hosted (Supabase) migration `0003` is NOT applied** (see §4).
Boundary kept: this phase creates *persistent visitor identities* from face embeddings. It does **not** implement visits, entry/exit events, `events.log` visit events or unique-visitor analytics, and nothing in the UI/API claims them:
`recognition_count` counts recognised **tracks**, a "registered here" count is per processing session, and a track id is never shown as, or mistaken for, a visitor id.

## 1. Recognition model and licensing assessment ([ADR 0013](decisions/0013-recognition-model-and-licensing.md))
* **Selected: SFace** (OpenCV Zoo, `face_recognition_sface_2021dec.onnx`, 38.7 MB, 128-d, ONNX Runtime CPU) + **YuNet** (MIT, 0.23 MB) for the 5 alignment landmarks. Files are fetched only by explicit command, pinned by URL, size and SHA-256, stored on D:, verified before every load, and never loaded in the FastAPI process.
* **Licence, from the primary sources:** the OpenCV Zoo states *"All files in this directory are licensed under Apache 2.0 License"* for SFace; it does **not** state the training data (the SFace paper reports CASIA-WebFace / VGGFace2 / MS-Celeb-1M experiments, whose own terms are research-oriented). InsightFace's pretrained weights (`buffalo_l` / `w600k_r50`) are documented as **non-commercial research only** (the MIT licence covers the code, not the weights); they were evaluated by licence only and not downloaded.
* **Verdict:** acceptable for the non-commercial hackathon with locally fetched weights; **not cleared for commercial use or for redistributing weights**; training-data provenance of the SFace file is an **unresolved restriction** that must be clarified with the model's publishers before any commercial deployment.
* **Correctness checks that mattered:** alignment equals OpenCV's `alignCrop` (mean pixel difference 0.0005); the embedder must be fed **RGB** 0–255 (BGR gives cosine ≈ 0.95 against the reference, RGB gives 1.0) – a convention bug found by comparing with OpenCV's own recogniser and pinned by a test.

## 2. Identity architecture
```
worker: confirmed track → scheduler → crop copy → [recognition thread] landmarks → align → quality gate → embedding ─► observation
API:    IdentityManager (one lock; PostgreSQL advisory lock): lookup → vote → track state machine → [register] → identity event → browser
```
Track id (temporary, per session) ≠ visitor id (persistent, label `V00124`) ≠ visit id (not implemented). The worker never touches the database; the API owns identities. Details: [architecture.md](architecture.md#identity-architecture-phase-3).

## 3. Registration and matching logic
* **Quality gate** (never stops detection/tracking; failure = `INSUFFICIENT_QUALITY` with reasons): size ≥ 40 px, detector confidence ≥ 0.6, not cut by the frame edge, landmarks found, yaw/roll/pitch limits, sharpness ≥ 15.
* **Per observation:** best similarity `s1`, best of a *different* visitor `s2`. `s1 ≥ 0.50` and `s1 − s2 ≥ 0.05` → **match vote**; `s1 ≥ 0.50` but margin small → **ambiguous vote**; `0.40 ≤ s1 < 0.50` → **grey vote** (neither recognise nor register); `s1 < 0.40` → **novel vote**.
* **Per track** (states `UNRESOLVED, INSUFFICIENT_QUALITY, MATCH_PENDING, RECOGNIZED, AMBIGUOUS, REGISTRATION_PENDING, REGISTERED, REJECTED`): RECOGNIZED needs 2 match votes in an 8-vote window. A recognised track that receives a conflicting match vote records a **conflict** and keeps its identity until another visitor has ≥ 2 more votes than the current one. The same visitor is never propagated to two tracks of one session seen within 1.5 s (the second becomes AMBIGUOUS – a tracker swap or a look-alike, either way no identity is invented).
* **Automatic registration on the first sufficiently reliable observations:** ≥ 3 consecutive novel, good-quality observations over ≥ 0.8 s, mean quality ≥ 0.35, mutually consistent (mean pairwise cosine ≥ 0.6; otherwise the track is REJECTED, not registered), and a final re-check of the aggregated embedding against the freshly synced index inside the lock. A low-quality or ambiguous observation stays pending; a single frame never creates an identity; no manual step is needed for a clear new face.
* **Not implemented (stated plainly):** adding embeddings to a visitor after registration (no drift/poisoning risk, but no adaptation either), mask detection, and an appearance-based tracker-swap detector other than the conflict/two-track rules.

## 4. Database schema and migration status
Migration `0003` (additive; no existing row touched): `visitors` (UUID id, UNIQUE `visitor_number`, UNIQUE `registration_key`, model version, first/last seen, image reference), `face_embeddings` (float32 BYTEA, model version, quality; UNIQUE per visitor+hash), `identity_associations` (session, **session-scoped** track id, visitor, state, similarity, conflicts; UNIQUE per session+track); RLS enabled; CHECK constraints on states.
Decision: an exact in-memory index rebuilt from PostgreSQL (watermark-synced), **not** pgvector – a few thousand 128-d vectors search in microseconds, and the availability of pgvector on the configured Supabase project was not assumed or tested.
* **Local PostgreSQL (embedded): verified** – upgrade, idempotent re-run, rows from `0001/0002` survive, `0002 ↔ 0003` down/up round trip (downgrade drops only the three new tables), model↔migration drift test clean; all identity tests run on it.
* **Hosted Supabase: `0003` NOT applied.** Revision there remains `0002` (verified in Phase 1.5). No cloud write was made this phase because none was authorised. To apply (deliberate, additive, idempotent): `.\scripts\migrate-cloud.ps1`, which refuses the exposed credential and prints revision/tables afterwards.

## 5. Worker integration
Recognition runs on its own thread behind a bounded queue (drops when busy, counted); the scheduler embeds a confirmed track every 0.5 s for its first 6 observations and every 5 s afterwards within an 80 ms/frame budget; observations travel as ordered binary messages (embedding + ≤ 192 px crop) in a bounded outbox separate from the latest-wins video slot; the pipeline shuts the thread down on every exit path. A recognition failure (missing model, exception) never affects detection or tracking and is reported to the UI. Observation ids (`session:track:seq`) are monotonic per track for its whole life (a bug where a briefly unobserved track restarted its numbering and had its evidence dropped as "duplicates" was found on real footage and fixed).

## 6. API changes
Observation protocol, identity events on the stream (and a replay for late-joining browsers), `GET/DELETE /visitors…`, authenticated image endpoint, recognition status/stats, session settings/stats additions, honest component states on `/system/status`; `GET /config` hides filesystem paths. **No response contains an embedding or an image path.** Contract: [api-contracts.md](api-contracts.md).

## 7. Frontend
Visitor gallery (responsive cards, authenticated thumbnails with placeholder/"image unavailable", search by id/number, sort, order, pagination, skeletons, empty/error/retry), URL-addressable details drawer (identity, timestamps, recognition history with *session-only* track numbers, erase with confirmation), live labels (`V00124 · #7`, `#7 · Recognizing`, `#15 · Low quality`, ambiguous/unverified), distinct box colours per identity state, a Recognition panel (recognised / registered here / undecided / flagged, per-face cost), notifications for registered / recognised / ambiguous / failed – once per (event, track, visitor), silent on reconnect replay, never applied from another session. Overlay labels are drawn imperatively (no per-frame React state); Motion animates cards, drawer and panels and respects reduced motion.

## 8. Duplicate prevention and concurrency
Layers, each tested (mutation check: removing any one of eight rules failed a test, except the grey-zone vote, whose job is also done by the registration re-check – the two layers are redundant by design):
1. one lock around every match **and** registration (threads) + a PostgreSQL advisory transaction lock (processes); 2. registration only after consistent multi-frame evidence; 3. re-check of the aggregated embedding against a freshly synced index inside the lock;
4. `registration_key = session:track` UNIQUE – a retry returns the existing visitor, and an integrity-error race returns the winner's row; 5. observation ids deduplicate retries and delayed messages, and messages for finished sessions are ignored; 6. the same visitor is not assigned to two simultaneous tracks; 7. failed registrations roll back visitor + embeddings + image file, back off, and retry on fresh evidence; 8. `reconcile()` repairs interrupted writes at start-up.
Tests: two tracks of one unseen person → one visitor; 8 racing threads → one visitor; 6 racing different people → numbers 1..6; two manager instances sharing a database → one visitor; session restart / process restart → recognised, not re-registered. A database constraint alone cannot prevent duplicate *people*; that is not claimed.

## 9. Recognition threshold evaluation ([benchmark](benchmarks/phase3-recognition.md))
* **LFW verification (labelled, 10 folds):** pair accuracy **99.4 %** at 0.34; impostor maximum **0.393** over 2,996 pairs (p99.9 = 0.348); genuine median 0.668, p5 0.456.
* **Identity simulation, 300 distinct people:** match < 0.45 caused wrong merges (2–4 %); a low novelty threshold left 54 % of new people undecided forever (a 1:N effect that pair statistics do not show). Chosen **novelty 0.40 / match 0.50 / margin 0.05**: 92 % registered on first appearance, 92 % of known returning people recognised from a *different* photo, **0 wrong identities**, **≈ 1 % duplicates** (3 of 297).
* **Degradation study:** small clean faces stay accurate down to 34 px (FNMR 6.8 → 10.7 %); defocus blur raises impostor similarities (0.36 at sharpness ≈ 15, 0.46 at ≈ 5) → `min_sharpness` 15 (from 25), footage-informed and data-checked.
* **Cannot be measured** (no labels): duplicate rate, wrong-merge rate and recognition recall on the hackathon footage; demographic performance differences; accuracy at registry sizes ≫ 300.

## 10. Real-video evaluation (qualitative; no identity labels)
Six clips through one registry (frame_skip 2): **9 visitors registered**, most faces rejected by the quality gate (blur, profile, small, looking down, masks); the 4K crowd registered 2 of 30 tracks (faces are 60–119 px but motion-blurred or non-frontal). The 9 saved crops look like nine different people; no cross-clip recognition happened, so re-identification on real footage is **unverified**. Seven tracks were refused registration because their own observations disagreed. Details and the clip table: [benchmark §4](benchmarks/phase3-recognition.md). Evidence stays in git-ignored `datasets/evidence/recognition/`.

## 11. CPU and memory
Per face 10–16 ms mean (p95 ≤ 38 ms) on the recognition thread; lookup 0.07 ms; registration 17–27 ms; model load 0.11 s. End-to-end A/B: hall clip wall +4 %, CPU +6 %, RSS +56 MB; crowded 4K wall +3 %, CPU +21 %, RSS +64 MB; no faces dropped. Baseline reference (Phase 2): ≈ 14 analysed frames/s, ≈ 520 MB RSS.

## 12. Automated test results (this machine, 2026-11-11, all executed)
| Check | Result |
|---|---|
| Backend pytest (embedded PostgreSQL, real models) | **389 passed** (was 277) |
| ruff check / format --check / mypy strict (100 files) | clean |
| Frontend vitest | **184 passed** (was 153) |
| ESLint, `tsc --noEmit`, Vite production build | clean / succeeded |
| Playwright (Edge), real stack, signed in | **25 passed** (was 23); no processes left behind |
| Total automated tests | **598** (baseline 453); no test removed or weakened |
New: real-model tests (OpenCV-reference equivalence, transformation robustness, quality rejection, full pipeline register-once / recognise-later / restart on PostgreSQL), 33 identity-manager tests on PostgreSQL, 43 recognition-unit tests, 12 relay tests, 18 visitor-API tests (incl. auth on every route, traversal, erasure), migration `0003` round trip, 31 frontend tests, 2 Playwright flows (register → gallery → details → second session recognised without a duplicate → application restart → erase; two different people + a different photo of one of them).
Existing tests changed because behaviour changed by design: migration head `0003`; the `/system/status` tracker/recognizer components are real (were "not implemented"); the Overview "start processing" step; the live-page disclaimer text; the e2e stroke detector accepts identity colours. An end-to-end harness defect was also fixed: an application *restart* inside a test left an orphaned API + worker (the restarted supervisor died before its children); teardown now also sweeps this repository's API/worker processes (`scripts/stop_stack.py --sweep`) and the suite leaves nothing running. A flaky `CancelledError` of Starlette's TestClient when closing a browser socket mid-processing is contained in one test helper and documented (harness artefact; not seen in any assertion or production path).
**Not covered by any test:** accuracy on labelled surveillance footage, masks, real RTSP, GPU, hosted-database load, multi-process deployment beyond two manager instances sharing one database.

## 13. Security and privacy limitations ([privacy-and-security.md](privacy-and-security.md))
Embeddings and face images are biometric data: authenticated visitor APIs, no embeddings/paths in responses or logs, image endpoint confined and non-cacheable, no static serving, input validation on every worker message, erasure and optional retention, no demographic inference. **Gaps:** one shared admin role (any signed-in user can view/erase any visitor), no audit trail, no encryption of the embedding columns beyond the provider's disk encryption, consent/permitted-use is an operator assumption, LFW and footage are research/evidence data kept locally, per-group fairness not evaluated, a false match or duplicate is possible.

## 14. Remaining blockers and limitations
1. **No identity labels for the hackathon footage** – real-footage accuracy, duplicate and wrong-merge rates are unknown; thresholds come from LFW (optimistic) and a 300-person simulation.
2. **Model licensing:** SFace file Apache-2.0 by declaration, training data undocumented → not cleared commercially; InsightFace weights non-commercial.
3. **Hosted migration `0003` not applied** (awaits your approval).
4. Quality gate is conservative on motion-blurred, non-frontal and masked faces; masks are not detected (a masked face was registered in the crowd clip) and masked faces embed poorly.
5. The 1:N effect: larger registries raise the chance of a wrong match; thresholds must be revalidated if the registry grows far beyond ≈ 300.
6. Single API instance with an in-memory index (synced from the database); multi-process behaviour is covered only by the shared-database test and the advisory lock on PostgreSQL.
7. Not exercised: real RTSP, GPU, Docker/Render/Vercel.

## 15. Definition-of-Done verdict
Every item that can be verified without labelled surveillance data is verified: a real embedding model runs; alignment/preprocessing verified against OpenCV; embeddings normalised and validated; unsuitable crops rejected without stopping detection/tracking; new identities registered after documented confirmation; later recognition across sessions and restarts; no duplicates under retries, races, restarts and two tracks; ambiguity explicit; track and visitor ids distinct everywhere; results reach the UI; the gallery shows real records with authorised images; thresholds have a documented, reproducible evaluation basis; biometric data protected as described; local migrations verified; a real-model integration path passes; all suites pass; performance and licensing documented; no visit/entry-exit/unique-visitor claims.
**Two items are met only partially and are stated as such:** hosted migration (reported, not applied) and "thresholds have an evaluation basis" (LFW + simulation, not the hackathon footage). **Verdict: accepted for Phase 3 as a hackathon-scope implementation, with limitations 1–3 above.**

## 16. Readiness for Phase 4 (visit lifecycle, entry/exit logging)
Ready: stable temporary tracks, persistent visitor ids, per-session associations with state and conflicts, session-scoped uniqueness, and a clean place to hang a visit (`visit_id`) on a *recognised* track. Before starting: (1) decide whether to apply `0003` to Supabase; (2) label a few clips (identity per track) so Phase 4's visit counting can be validated – visit logic must tolerate ambiguous, rejected and undecided tracks and tracker id changes, and must not turn an undecided face into a visit; (3) decide the licence stance for the recognition weights; (4) add per-user roles and an audit trail before exposing visit data. Not started.
