# Katomaran AI – Intelligent Face Tracker & Visitor Analytics

Detects, recognises, registers, tracks and counts unique visitors from video files and RTSP streams, with a real-time dashboard.

> **Current state: Phase 0 (foundation).** Everything below marked ✅ exists and is tested; everything else is planned.
> No face detection, recognition, tracking or analytics is implemented, and the UI shows no invented data.

| Capability | Status |
|---|---|
| Monorepo, FastAPI + Alembic + PostgreSQL foundation | ✅ Phase 0 |
| Health / readiness / system status / sanitised config APIs | ✅ Phase 0 |
| Validated `config.json`, structured logging incl. `logs/events.log` | ✅ Phase 0 |
| Dashboard shell (6 routes, dark/light, responsive, a11y) wired to real backend status | ✅ Phase 0 |
| Video ingestion + YOLO face detection | Phase 1 |
| InsightFace embeddings, auto-registration, re-identification | Phase 2 |
| Tracking + visit lifecycle | Phase 3 |
| Entry/exit events + face image storage | Phase 4 |
| Analytics, real-time updates, RTSP hardening, deployment | Phases 5–7 (`docs/phase-roadmap.md`) |

## Architecture
React SPA ⇄ FastAPI (`/api/v1`) ⇄ PostgreSQL, plus an independently runnable AI worker process (inference never runs inside the API).
Details: [docs/architecture.md](docs/architecture.md) · [API](docs/api-contracts.md) · [testing](docs/testing-strategy.md) · [deployment & D: policy](docs/deployment.md) · [ADRs](docs/decisions/).

## Prerequisites (Windows 11 / PowerShell; Linux works with the equivalent commands)
* Node.js 20+ and npm
* [uv](https://docs.astral.sh/uv/) (installs Python **3.12** and the venv inside the project)
* PostgreSQL 15+ – hosted (Supabase/Neon) **or** local (`docker compose up -d db`, or a native install)
* Microsoft Edge or Chrome for E2E tests (`PW_CHANNEL=msedge`), or `npx playwright install chromium`

## Setup
```powershell
# one-time: uv without touching global Python (skip if uv is already installed)
python -m pip install --target .tools\uv uv

.\scripts\install.ps1          # Python 3.12 + .venv + npm ci; creates .env from .env.example
# edit .env -> DATABASE_URL (see Database below)
.\scripts\migrate.ps1          # apply Alembic migrations
```
All caches/venv/models stay inside the project directory (see [docs/deployment.md](docs/deployment.md#5-d-drive-policy-this-machine)). Run `. .\scripts\env.ps1` in a shell first if you invoke tools by hand.

## Database
* **Hosted (Supabase):** `DATABASE_URL=postgresql://postgres:<url-encoded-password>@db.<ref>.supabase.co:5432/postgres?sslmode=require`. Behind a transaction pooler (port 6543) also set `DB_USE_PGBOUNCER=true`.
* **Local fallback:** `docker compose up -d db` then `DATABASE_URL=postgresql://katomaran:katomaran@localhost:5432/katomaran`.
* Migrations: `.\scripts\migrate.ps1` (= `alembic upgrade head`). New migration: `cd backend; ..\.venv\Scripts\python -m alembic revision --autogenerate -m "..."`. The schema is never created with `create_all()`.
* Integration tests use a throw-away schema `test_<hex>` in `TEST_DATABASE_URL` (defaults to `DATABASE_URL`) and drop it afterwards.

## Run
```powershell
.\scripts\start-backend.ps1    # http://127.0.0.1:8000  (docs: /api/docs)
.\scripts\start-frontend.ps1   # http://localhost:5173  (proxies /api to the backend)
.\scripts\start-worker.ps1     # Phase 0: validates config + DB and exits
```

## Quality checks / tests
```powershell
.\scripts\check.ps1             # everything: ruff, mypy, pytest, eslint, tsc, vitest, build, playwright
.\scripts\check.ps1 -SkipE2E
.\scripts\test-backend.ps1 [-Unit]
.\scripts\test-frontend.ps1 [-E2E]
```

## Configuration reference
**Environment (`.env`)** – see `.env.example`: `APP_ENV`, `DATABASE_URL`, `TEST_DATABASE_URL`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_CONNECT_TIMEOUT_SECONDS`, `DB_CONNECT_RETRIES`, `DB_USE_PGBOUNCER`, `CORS_ORIGINS` (explicit list, no `*`), `CONFIG_FILE`, `LOG_DIR`, `STORAGE_DIR`, `MODELS_DIR`, `LOG_LEVEL`, `KATOMARAN_ROOT`; frontend build-time `VITE_API_BASE_URL`, `VITE_DEV_PROXY_TARGET`.
**`config/config.json`** (validated at startup, unknown keys rejected; values are *preliminary defaults*, not validated production values):

| Key | Default | Meaning |
|---|---|---|
| `video.source_type` / `video.source` | `file` / `""` | file path or RTSP URL (never exposed by the API) |
| `video.reconnect_interval_seconds` | 5 | RTSP reconnect delay |
| `detection.frame_skip` | 2 | frames skipped between detections |
| `detection.confidence_threshold` | 0.5 | min detector confidence |
| `recognition.similarity_threshold` | 0.55 | min embedding similarity for a match |
| `tracking.max_missing_seconds` | 2 | tolerated gap before a track ends |
| `logging.level`, `logging.events_file` | `INFO`, `logs/events.log` | `LOG_LEVEL` env overrides level |

## Troubleshooting
* **Database indicator "Unavailable"** – check `DATABASE_URL` (URL-encode `$`/`@`), `sslmode=require` for hosted DBs, and that a Supabase free project is not paused. The first connection can take several seconds.
* **"Backend not reachable"** – start the backend; the dev proxy targets `http://127.0.0.1:8000` (`VITE_DEV_PROXY_TARGET`).
* **Hosted DB unreachable from a platform** – the direct Supabase host can be IPv6-only; use the pooler connection string.
* **`ConfigError` at startup** – the message names the offending key in `config/config.json`.
* **Playwright can't find a browser** – set `PW_CHANNEL=msedge` (or `chrome`), or install Chromium.
* **Vite exits immediately when launched by another tool** – it quits on a closed stdin; set `CI=true` (done in `playwright.config.ts`).

This project is a part of a hackathon run by https://katomaran.com
