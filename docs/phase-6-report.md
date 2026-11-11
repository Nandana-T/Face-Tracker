# Phase 6 report – deployment readiness, integration hardening and submission package

Status: **everything that can be done and verified without your accounts, credentials and approvals is done. No cloud resource was provisioned, no hosted database write was made, nothing was committed or pushed, and the hackathon form was not submitted.** Deadline: Friday 13 November 2026, 2:00 PM IST.
Deployment steps: [deployment-guide.md](deployment-guide.md). Final evaluation: [final-evaluation.md](final-evaluation.md). Security: [security-audit.md](security-audit.md). Submission: [submission/](submission/).

## 1. Deployment architecture implemented
Vercel (static SPA) → Render (single lightweight API, no models) → Supabase (PostgreSQL + **private** Storage) ◄── WSS ── local AI worker (D:), with the worker holding only the API URL and a token. Dependency graph, plans/limits/cost and failure/recovery table: [deployment-guide.md](deployment-guide.md). **Implemented in code:** private object storage (ADR 0016), production blueprint variables, `frontend/.env.example`, read-only smoke test for a deployed stack. **Not provisioned:** every external service.

## 2. Database migration status
Hosted Supabase: **`0002`** (unchanged; `0003`, `0004` pending). Local PostgreSQL: `0004`, single Alembic head, preflight and a recovery plan (`pg_dump` first; Free plan has no PITR) documented. **Gate A – awaiting your explicit approval.** No hosted write was attempted.

## 3. Frontend deployment
**Not deployed (needs your Vercel account).** Verified locally: production build clean (`tsc` + Vite), SPA rewrite config, bundle scanned – no server-side secret names, only the public `VITE_API_BASE_URL`. No verified URL exists.

## 4. Backend deployment
**Not deployed (needs your Render account).** Verified locally in **production mode behind TLS** (`scripts/verify_remote_path.py`): all production guards on, health/readiness, 401 for anonymous access, WSS, graceful restarts. `Dockerfile`/`render.yaml` were **never built** (Docker is not installed here). Measured: API RSS 115 MB (no model is loaded), start-to-healthy 3.7–6.5 s. Free-tier facts from current provider docs (15-min spin-down, ≈ 1 min cold start, 750 h/month, ephemeral disk, single instance) drive the design (storage, one instance, warm-up before the demo).

## 5. Worker connectivity (remote path, loopback TLS)
`scripts/verify_remote_path.py` – 19/19 steps passed: API in production mode on `https://localhost:8443`, worker on `wss://` with **no database credentials** in its environment; wrong worker token refused; worker connects (5.4 s); a sample video is processed (tracking + recognition in the worker), one identity registered, one visit with one entry + one terminal event, evidence image through the authenticated endpoint, analytics = database, read-only reconciliation and `events.log` verification consistent; **API restart mid-session** → session `ERROR`, worker reconnects by itself (4.3–6.5 s), counts intact, no open visit counted as present, open visits closed as INTERRUPTED; **worker killed mid-session** → `worker_lost`, nothing counted as present; same person again → recognised, unique identities unchanged, visits +1. Scope: **loopback**, not the internet (no NAT/latency/load balancer, not Render).

## 6. Cloud storage
Implemented: `BlobBackend` with local and Supabase private backends, `STORAGE_BACKEND` switch, deterministic idempotent keys, authenticated retrieval only, deletion on erasure, reconciliation of orphans/missing objects, retry of failed uploads, service-role key confined to the API ([ADR 0016](decisions/0016-private-object-storage.md)). **25 new tests** (`test_blob_store.py`) run both backends through one contract, plus: auth header on every request, no public endpoint, 401/5xx/outage → transient errors, retry stores exactly one object, evidence persists across a "redeployment" (new recorder on the same bucket), lost objects reported `MISSING` (never invented), orphan removal, erasure by prefix, settings validation, key never in logs/`repr`. **Limit of the evidence:** the Supabase client is tested against an in-memory fake of the Storage API. **Behaviour against the real service is unverified**; run `scripts/verify_storage.py` once you have a bucket.

## 7. RTSP test scope
* **Scripted adapter tests** (fakes): existing + a new stalled-stream test.
* **Local RTSP server integration** (`scripts/verify_rtsp_local.py`, MediaMTX + ffmpeg looping the sample video on loopback) – 12/12 passed: connection, decoding, detection, tracking, recognition (visitor registered), publisher killed → *reconnecting* reported in 0.4 s, session stays alive, processing resumed 3.7 s after the publisher returned, no duplicate identity, clean stop (worker threads 23 → 24), reconciliation consistent, RTSP URL never in the API.
* **Real network camera: NOT tested.** Do not describe any of this as real-camera verification.
Defects this found and fixed: the worker never forwarded the RTSP state (a lost stream looked "connected" forever), and a stalled stream could block the reader indefinitely (watchdog added).

## 8. Full-stack E2E
Controlled fixtures (public-domain portrait; LFW photographs for person B): Playwright, Edge, real stack and real models, **30/30 passed** including registration, recognition, visits, evidence, gallery, timeline, analytics = API, refresh, application restart, crash recovery and live dashboard updates. Production-like smoke (`smoke_deployed.py`, TLS, production guards): **16/16**. The 20-step production walkthrough of the brief is covered by these runs locally; it has **not** been executed against deployed URLs (none exist).

## 9. Automated test results (all executed)
| Check | Result |
|---|---|
| Backend pytest | **565 passed** (second full run; the first full run had 1 intermittent websocket-harness failure, see notes) |
| Frontend Vitest | **245 passed** |
| Playwright (Edge, real stack) | **30 passed** |
| ruff · ruff format · mypy strict · ESLint · `tsc` · Vite build | clean |
| Baseline (Phase 5) | 813 → **840** (565 + 245 + 30) |
Notes: `test_sessions_api` websocket tests (`malformed_track…`, `subscriber_is_released…`) fail intermittently (≈ 1 in 4–8 runs) from the known Starlette-TestClient `CancelledError` harness quirk; **the same flakiness reproduces on a clean checkout of HEAD** (2 of 8 runs), so it is not caused by Phase 6, and the files pass on re-run. No test was removed or weakened; `pip-audit`/`npm audit` were not run; skipped tests: none beyond the LFW-fixture guards.

## 10. Performance (local; hosted not measured)
HTTPS round trip 3.4 ms warm; API start-to-healthy 3.7–6.5 s; worker connect 5.4 s; reconnect after API restart 4.3–6.5 s; session start → running 0.7 s; RTSP connect → running 1.7–2.5 s; API RSS 115 MB, worker 171–181 MB; analytics 5–660 ms at 1 k–100 k synthetic visits; event persistence 14–18 ms. Dashboard first figure ≈ 1.0 s. **Not measured:** hosted API latency, worker-to-hosted-API latency, upload latency to real Supabase, Render cold-start time, real-network WebSocket reconnection. Bottlenecks to expect: Render cold start (~1 min), CPU-bound worker (≈ 55 ms/frame), free-tier storage quota (1 GB).

## 11. Security audit
See [security-audit.md](security-audit.md): no secrets tracked or in history, no biometric files tracked (one public-domain fixture), bundle clean, service-role key confined, production guards, private storage design. Open: one shared admin role, no audit trail, no encryption beyond the provider's, `npm/pip audit` not run, Docker/Render unverified.

## 12. Git status
`main` at `420c013` (Phases 0–5 committed). Phase 6 changes are **uncommitted** (≈ 9 modified, ≈ 15 new files); nothing pushed. Commit plan with three suggested commits and pre-push checks: [submission/github-commit-plan.md](submission/github-commit-plan.md).

## 13. Demo recording readiness
Script and checklist ready ([submission/demo-video-script.md](submission/demo-video-script.md)); **no video recorded; no link exists.** The brief requires a Loom/YouTube link in the README – the README has an explicit `TODO` for it.

## 14. Submission package completeness
README (objective, features, architecture, stack, setup, deployment architecture, config, testing, hardware, licensing, limitations, sample output; demo/deployment links as TODO), phase reports, ADRs 0001–0016, API contracts, deployment guide, security audit, final evaluation, safe sample outputs (`docs/sample-output/`), commit plan, checklist. **Incomplete:** demo link, deployed URLs, hosted-database state, repository push, form submission.

## 15. Outstanding blockers (all need you)
1. Approve/perform Gate A (backup → migrations `0003`,`0004` on Supabase). 2. Create the private bucket and give the API its service-role key; run `verify_storage.py`. 3. Create the Render and Vercel services (or accept a local-only submission); run `smoke_deployed.py`. 4. Record the demo video and paste the link. 5. Approve commit/push. 6. Submit the form.
Also unverified until then: Dockerfile/blueprint build, real Supabase Storage behaviour, hosted TLS/CORS, cold-start behaviour.

## 16. Final acceptance verdict
**The software is submission-ready as a verified local/production-mode deployment; the cloud deployment is prepared but not executed, and the submission is not complete.** Complete means: demo link in README + repository pushed + form submitted (+ cloud URLs if you want them). Counts remain registered face **identities**, never verified physical visitors. If cloud deployment hits a blocker before the deadline, the fully reproducible local demonstration (`scripts\start-all.ps1`, `start-frontend.ps1`, fixtures in `datasets/e2e`) is the fallback, and the README should say so plainly.
