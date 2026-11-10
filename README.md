# Katomaran AI – Intelligent Face Tracker & Visitor Analytics

Detects, recognises, registers, tracks and counts unique visitors from video files and RTSP streams, with a real-time dashboard.

> **Current state: Phase 2 (multi-face tracking) on top of Phase 1/1.5, awaiting approval.** Everything marked ✅ exists and is tested.
> The app **detects** faces and **tracks** them with *temporary, per-session* ids (`#7`). A track id is **not** a person's identity: nothing is recognised,
> registered or counted as a visitor yet, and the UI never shows an invented visitor number. Details and honest limitations:
> [docs/phase-2-report.md](docs/phase-2-report.md), [docs/phase-1-5-report.md](docs/phase-1-5-report.md), [docs/phase-1-report.md](docs/phase-1-report.md).

| Capability | Status |
|---|---|
| Monorepo, FastAPI + Alembic + PostgreSQL foundation, health/ready/status/config APIs, design-system dashboard | ✅ Phase 0 |
| Video file + RTSP source adapters (timeouts, bounded reconnect, credential redaction) | ✅ Phase 1 (RTSP: tested with fakes/failure paths only – **no real camera tested**) |
| YOLO11n-face detector (ONNX Runtime, CPU), configurable `frame_skip`, bounded queues / back-pressure | ✅ Phase 1 |
| Separate worker process ⇄ API over an authenticated WebSocket; session lifecycle (start/stop/status/events) | ✅ Phase 1 |
| Live Monitoring page: canvas video with aligned boxes, source controls, honest status states, detection summary | ✅ Phase 1 |
| Multi-face tracking (ByteTrack-style, in the worker): temporary session-scoped ids, TENTATIVE→CONFIRMED→LOST→REMOVED lifecycle, observed vs predicted boxes, tracking panel + track details | ✅ Phase 2 ([report](docs/phase-2-report.md), [benchmark](docs/benchmarks/phase2-tracking.md)) |
| Sign-in (admin password → signed token), single-use stream tickets, production start-up guards, test-DB safety | ✅ Phase 1.5 |
| Validation on the hackathon footage (23 videos) | ✅ [docs/real-video-validation.md](docs/real-video-validation.md) |
| Measured CPU benchmark | ✅ [docs/benchmarks/phase1-benchmark.md](docs/benchmarks/phase1-benchmark.md) (GPU: not measured – no usable GPU) |
| InsightFace embeddings, auto-registration, re-identification | Phase 3 |
| Visit lifecycle, entry/exit events + face image storage | Phase 4 |
| Analytics, real-time updates, RTSP hardening, per-user auth, deployment | Phases 5–7 (`docs/phase-roadmap.md`) |

## Architecture
React SPA ⇄ FastAPI (`/api/v1`) ⇄ PostgreSQL, plus an independent **video worker** process that dials the API over a WebSocket
(inference never runs inside the API; the worker needs no database credentials and no inbound port).
Details: [architecture](docs/architecture.md) · [API](docs/api-contracts.md) · [testing](docs/testing-strategy.md) ·
[deployment & D: policy](docs/deployment.md) · [privacy & security](docs/privacy-and-security.md) · [security runbook](docs/security-runbook.md) · [ADRs](docs/decisions/) · [AI/implementation notes](docs/ai-notes.md).

## Prerequisites (Windows 11 / PowerShell; Linux works with the equivalent commands)
* Node.js 20+ and npm
* [uv](https://docs.astral.sh/uv/) (installs Python **3.12** and the venv inside the project)
* PostgreSQL 15+: hosted (Supabase/Neon), **or** the embedded local one started by `scripts\local-db.ps1` (no Docker needed)
* Microsoft Edge or Chrome for E2E tests (`PW_CHANNEL=msedge`), or `npx playwright install chromium`

## Setup
```powershell
python -m pip install --target .tools\uv uv      # one-time, skip if uv exists
.\scripts\install.ps1            # Python 3.12 + .venv + npm ci; creates .env from .env.example
# edit .env: DATABASE_URL, WORKER_TOKEN (see below)
.\scripts\download-model.ps1     # explicit, checksum-verified model download into models\ (never automatic)
.\scripts\migrate.ps1            # apply Alembic migrations (0001, 0002)
.\scripts\make-sample-video.ps1  # optional: synthetic demo clip in datasets\samples
```
Everything (venv, caches, models, datasets, logs, temp) stays inside the project directory – see [docs/deployment.md](docs/deployment.md#5-d-drive-policy-this-machine).
Run `. .\scripts\env.ps1` first if you invoke tools by hand.

**Sign-in:** set `ADMIN_PASSWORD` in `.env` to require a password (REST, the dashboard and the frame stream are then protected; mandatory with `APP_ENV=production`, which also requires `AUTH_SECRET`, a 24+ character `WORKER_TOKEN` and https-only `CORS_ORIGINS`). Without it (local development) the API stays open.

**Secrets:** `WORKER_TOKEN` – generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`; the API and the worker read the same value from `.env`.
RTSP cameras: put the URL in an environment variable (e.g. `RTSP_FRONT_DOOR=rtsp://user:pass@host/stream`) and reference its *name* from
`config.json` (`{"id":"front-door","label":"Front door","type":"rtsp","url_env":"RTSP_FRONT_DOOR"}`). The browser can only choose a source id.

## Database
* **Hosted (Supabase):** use the **Session pooler** connection string (the direct host is IPv6-only):
  `DATABASE_URL=postgresql://postgres.<ref>:<url-encoded-password>@<region>.pooler.supabase.com:5432/postgres?sslmode=require` and keep `DB_USE_PGBOUNCER=false` (session mode).
  The credential was rotated and **migration `0002` is applied and verified** (revision `0002`, tables present, RLS on, TLS in use). Phase 2 needs **no new migration**.
  Procedure and the credential check: [docs/security-runbook.md](docs/security-runbook.md), `.\scripts\migrate-cloud.ps1` (idempotent; refuses to run with the old exposed password).
* **Local (no Docker, no credentials):** `. .\scripts\local-db.ps1 start` starts an embedded PostgreSQL in `storage\pgdata` and sets `DATABASE_URL`/`TEST_DATABASE_URL` for the shell; then `.\scripts\migrate.ps1`.
  Alternatively `docker compose up -d db`.
* The schema is only ever created by Alembic, never `create_all()` in the app.

## Run
```powershell
.\scripts\start-all.ps1          # API (127.0.0.1:8000, docs at /api/docs) + video worker, as two processes
.\scripts\start-frontend.ps1     # http://localhost:5173  -> Live Monitoring
# or separately:  .\scripts\start-backend.ps1   and   .\scripts\start-worker.ps1
```
Open **Live Monitoring**, pick a source, press **Start processing**. If no worker is connected or the model is missing the page says so and
disables Start. Leaving the page does not stop a session; Stop does.

## Quality checks / tests
```powershell
.\scripts\check.ps1              # model checksum, ruff, mypy, pytest, eslint, tsc, vitest, build, playwright (uses the local PostgreSQL)
.\scripts\check.ps1 -SkipE2E
.\scripts\test-backend.ps1 [-Unit]    .\scripts\test-frontend.ps1 [-E2E]
```
Last full run (this machine, 2026-11-10): backend **277 passed** · frontend **153 passed** · Playwright **23 passed** (authenticated real stack) · **453 total** (Phase 1.5 baseline 341) ·
ruff, mypy (strict), ESLint, `tsc`, production build clean. Breakdown and what the tests do *not* prove: [docs/testing-strategy.md](docs/testing-strategy.md).
Benchmark: `.venv\Scripts\python.exe backend\benchmarks\benchmark_detector.py` then `render_report.py`.

## Configuration reference
**Environment (`.env`)** – see `.env.example`: `APP_ENV`, `DATABASE_URL`, `TEST_DATABASE_URL` (tests never fall back to `DATABASE_URL`; unset = embedded local PostgreSQL), `ADMIN_PASSWORD`, `AUTH_SECRET`, `AUTH_TOKEN_TTL_MINUTES`, `DB_*`, `CORS_ORIGINS` (explicit list, no `*`), `WORKER_TOKEN`, `WORKER_API_URL`, `WORKER_ID`,
`CONFIG_FILE`, `LOG_DIR`, `STORAGE_DIR`, `MODELS_DIR`, `LOG_LEVEL`, `KATOMARAN_ROOT`, `RTSP_*` (referenced by `url_env`); frontend build-time `VITE_API_BASE_URL`, `VITE_DEV_PROXY_TARGET`.

**Tracking parameters** were set from sensitivity sweeps on the hackathon footage and a synthetic ground-truth benchmark, not copied from other projects; because the footage has no labels they are a
preliminary calibration ([benchmark](docs/benchmarks/phase2-tracking.md)). `detection.frame_skip` keeps its Phase 1 meaning; the tracker is updated only on analysed frames using their real timestamps. Values of `frame_skip` above ≈ 3 are not recommended with tracking in crowds.

**`config/config.json`** (validated at startup; unknown keys and incompatible combinations are rejected; **changing it requires restarting the API and worker** – there is no runtime mutation):

| Key | Default | Meaning |
|---|---|---|
| `video.sources[]` | `[]` | trusted sources: `{id,label,type:file,path}` or `{id,label,type:rtsp,url_env}` |
| `video.sample_dir` | `datasets/samples` | every video file here becomes source `sample-<name>` |
| `video.source_type` / `video.source` | `file` / `""` | legacy single source, exposed as id `default` |
| `video.reconnect_interval_seconds` | 5 | first RTSP reconnect delay (doubles up to `video.rtsp.max_backoff_seconds`) |
| `video.rtsp.*` | 10 / 10 / 0 / 30 | connect timeout, read timeout, max reconnect attempts (0 = unlimited), max back-off |
| `detection.frame_skip` | 2 | **frames skipped between detections**: detector runs on frames 0, N+1, 2(N+1)… (2 ⇒ every 3rd) |
| `detection.confidence_threshold` | 0.5 | min detector confidence (inclusive) |
| `detection.model` / `input_size` | `yolov11n-face` / 640 | pinned model; input size is fixed by the model |
| `detection.iou_threshold` / `max_faces` | 0.45 / 100 | NMS overlap, cap per frame |
| `detection.intra_op_threads` / `cpu_spin_wait` | 4 / false | ONNX Runtime threads (0 = all cores) and idle busy-wait (see benchmark: spin-wait cost ~60 % of the CPU at real-time rates) |
| `worker.queue_size` | 4 | bounded reader→inference queue |
| `worker.realtime_pacing` | true | play files at their frame rate (false = as fast as possible) |
| `worker.heartbeat_interval_seconds` | 5 | worker heartbeat; API declares it lost after 3 intervals |
| `worker.start_timeout_seconds` / `stop_timeout_seconds` | 30 / 10 | STARTING / STOPPING watchdogs |
| `stream.max_fps` / `max_width` / `jpeg_quality` | 15 / 960 / 70 | what is sent to browsers |
| `tracking.enabled` | true | run the tracker in the worker (false = raw detections only, Phase 1 behaviour) |
| `tracking.high_confidence` | 0.6 | only detections scoring ≥ this can **start** a track; weaker ones (down to `detection.confidence_threshold`) may only extend one |
| `tracking.confirm_hits` | 3 | observed updates for TENTATIVE → CONFIRMED |
| `tracking.tentative_timeout_seconds` | 0.5 | a TENTATIVE track not detected for this long is dropped ("rejected") |
| `tracking.max_missing_seconds` | 2 | a LOST track not re-detected for this long is REMOVED; its id is never reused. **Seconds, not frames**, so it does not depend on FPS or `frame_skip` |
| `tracking.match_iou` / `low_match_iou` / `lost_match_iou` | 0.2 / 0.3 / 0.1 | minimum box overlap to associate a track with a high-score / low-score detection / (for a LOST track) any detection |
| `tracking.box_buffer` | 0.1 | boxes are grown by this fraction per side before computing overlap (keeps 12–30 px faces matchable); 0 = plain IoU |
| `tracking.lost_velocity_decay_per_second` | 1.0 | damping of a LOST track's extrapolated velocity |
| `tracking.max_tracks` | 200 | cap on simultaneously live tracks (births beyond it are refused and counted) |
| `recognition`, `logging` | … | later phases / logging (`LOG_LEVEL` env overrides level) |

## Troubleshooting
* **Start is disabled / "Video worker not connected"** – start the worker (`scripts\start-worker.ps1` or `start-all.ps1`); `WORKER_TOKEN` must match on both sides.
* **"Face detection model unavailable"** – run `scripts\download-model.ps1` on the worker machine and restart the worker.
* **Database indicator "Unavailable"** – check `DATABASE_URL` (URL-encode `$`/`@`), `sslmode=require` for hosted DBs, that a Supabase free project is not paused; or use `scripts\local-db.ps1`.
* **"Backend not reachable"** – start the API; the dev proxy targets `http://127.0.0.1:8000` (`VITE_DEV_PROXY_TARGET`) and proxies WebSockets too.
* **`ConfigError` at startup** – the message names the offending key in `config/config.json`.
* **Playwright can't find a browser** – set `PW_CHANNEL=msedge` (or `chrome`) or install Chromium. E2E needs ports 8000 and 5173 free.
* **Orphaned processes after a crashed run** – `python scripts\stop_stack.py <pid>` kills a stack and all its descendants.

This project is a part of a hackathon run by https://katomaran.com
