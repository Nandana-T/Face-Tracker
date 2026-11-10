# Deployment, hosted database, storage and D: drive policy

Nothing in this repository provisions or deploys paid infrastructure. This document is the plan plus the exact manual steps.

## 1. Hosted PostgreSQL – provider evaluation
Figures come from web searches on 2026-11-10 (mostly third-party summaries; **verify on each provider's pricing page** before committing).

| | Supabase | Neon | Render Postgres |
|---|---|---|---|
| Free tier | 2 projects, 500 MB DB | 100 CU-h/project; storage 1 GB per Neon's FAQ (older pages say 0.5 GB) | 1 free DB, 1 GB |
| Idle behaviour | Project **paused after ~7 days of inactivity** (restorable, manual wake) | Compute suspends after 5 min idle (cold start of ~hundreds of ms–seconds), cannot be disabled on Free | **Expires 30 days after creation**, 14-day grace, then deleted |
| SQLAlchemy / psycopg | Standard PostgreSQL 17; direct (`:5432`) or pooler (`:6543`, set `DB_USE_PGBOUNCER=true`) | Standard; pooled endpoint available (`-pooler` host) | Standard |
| TLS | Supported (`sslmode=require`) | Required | Supported |
| Backups | Daily backups on paid plans; none guaranteed on Free | 6-hour restore window on Free | Not on Free |
| Regions | Choose at project creation | Choose at creation | Choose at creation |
| Fit | Good: stable always-on if kept active; Auth/Storage available for later phases | Good alternative; cold starts affect first request | **Poor for a hackathon that outlives 30 days** |
| Expected cost | $0 (Pro $25/mo if pausing is unacceptable) | $0 (Launch is usage-based) | $0 for 30 days, then paid |

**Decision: Supabase PostgreSQL** (user-supplied project), Neon as the fallback; Render's free DB is rejected because of the 30-day expiry.
Caveats actually observed: first TLS connection from the dev machine took ≈5–6 s (direct host); the pool then reuses
connections. The direct host (`db.<ref>.supabase.co`) can be IPv6-only on some networks/hosts - if a platform such as
Render cannot reach it, use the Supavisor session pooler string from the Supabase dashboard (port 5432 session mode,
or 6543 transaction mode with `DB_USE_PGBOUNCER=true`).

### Connection settings
`DATABASE_URL=postgresql://postgres:<URL-ENCODED-PASSWORD>@db.<ref>.supabase.co:5432/postgres?sslmode=require`
(`$` → `%24`, `@` → `%40`). The URL is held as a `SecretStr`, never returned by the API or logged. Pool: `pool_pre_ping`,
recycle 30 min, `DB_POOL_SIZE` + `DB_MAX_OVERFLOW` (keep below the provider's connection limit).
**Local fallback:** `docker compose up -d db` + `DATABASE_URL=postgresql://katomaran:katomaran@localhost:5432/katomaran`,
or any local PostgreSQL. The app runs fully locally without any cloud service.

### Security notes
* **BLOCKER:** the password was shared in a chat session and has **not** been rotated (checked 2026-11-10). Follow `docs/security-runbook.md` (rotate → `scripts\migrate-cloud.ps1 -CheckOnly` → `scripts\migrate-cloud.ps1`) before any cloud operation. Migration `0002` has not been applied to Supabase for this reason; Phase 1 development and tests use the embedded local PostgreSQL (`scripts\local-db.ps1`, ADR 0010).
* Migration `0001` enables RLS on its table so the Supabase Data API (anon key) cannot read it. Do the same for every future table.
* Never expose `DATABASE_URL` or the service-role key to the frontend (only `VITE_*` variables reach the browser, and none are secrets).

## 2. Target topology
| Component | Platform | Notes |
|---|---|---|
| Frontend | Vercel (`frontend/vercel.json`) | Build env: `VITE_API_BASE_URL=https://<api-host>/api/v1`. Project root = `frontend`. |
| API | Render web service (`render.yaml`, `backend/Dockerfile`) | `preDeployCommand: alembic upgrade head`; health check `/api/v1/health`; set `CORS_ORIGINS` to the Vercel origin(s); `$PORT` honoured; SIGTERM → graceful shutdown. |
| Database | Supabase | as above |
| AI worker | local machine first → dedicated CPU/GPU host | see §3 |
| Images | local `storage/` → object storage | see §4 |

Supported configurations: (1) all local + hosted DB; (2) hosted frontend + API, local worker + hosted DB; (3) everything hosted with a dedicated worker host.
HTTPS/WSS: terminated by Vercel/Render; the API uses `--proxy-headers`; no localhost URLs exist in production code (dev proxy target lives only in `vite.config.ts` / `VITE_DEV_PROXY_TARGET`).
Free web instances should **not** be assumed capable of real-time YOLO + InsightFace inference, and they spin down when idle.

### Production environment (the API refuses to start without these)
`APP_ENV=production`, `ADMIN_PASSWORD` (≥12), `AUTH_SECRET` (≥32), `WORKER_TOKEN` (≥24), `CORS_ORIGINS` (https only), `DATABASE_URL`. Interactive docs are off in production. **Verified here:** the API in production mode (start-up refusal, 401s, hidden docs, CORS) and the production frontend build (API URL baked in, no secrets in the bundle). **Not verified:** the Dockerfile build, `docker compose`, Render and Vercel deployments (no Docker/WSL here, nothing was deployed); `render.yaml` and `docker-compose.yml` were only checked to be valid YAML.

## 3. AI worker requirements (measured numbers: `docs/benchmarks/phase1-benchmark.md`)
* CPU-only: ≥ 4 vCPU, ≥ 4 GB RAM per 1–2 streams at frame_skip 2; GPU recommended for multiple streams.
* Persistent disk for model weights (`MODELS_DIR`, ~hundreds of MB) and temporary frames.
* Network: outbound access to PostgreSQL (and object storage). **RTSP**: the camera must be reachable from the worker - a worker on the camera LAN avoids exposing cameras to the internet; if the worker is remote use a VPN (WireGuard/Tailscale) and never port-forward RTSP.
* Cost: a GPU VM is the dominant cost; a local machine is free for the hackathon demo. Final choice deferred to Phase 7 after benchmarks.
* The Phase 1 worker needs **no database credentials and no inbound ports**: it dials the API
  (`WORKER_API_URL`, use `wss://` when hosted) with `WORKER_TOKEN`; the API owns all persistence. Tracking runs in the worker
  (read from the worker's `config.json`; the API only reads `tracking.enabled` to label sessions, so keep both files identical).
  It adds well under 1 ms per analysed frame and no dependency or database change, so no deployment step changes in Phase 2. Same `config.json`
  and models directory as the API host's config (sources are resolved on the worker, so RTSP URLs live only in the
  *worker's* environment as `RTSP_*` variables referenced by `url_env`).
* Start: `python -m app.workers.video_worker` (long-running agent; `--check` = Phase 0 config/DB check).
  Render has no free background-worker tier with the CPU this needs; run it locally or on a small VM.
* Model: `scripts\download-model.ps1` on the worker machine (explicit, checksum-verified, never automatic).
* Hosted API additionally needs `WORKER_TOKEN` (same value on both sides) and the migration `0002` applied
  (`preDeployCommand` already runs `alembic upgrade head`). The API is currently a **single process** (live state is
  in memory); keep Render at one instance until a shared bus is introduced.
* Security: no user authentication exists yet - do not expose a hosted API with real cameras (see `privacy-and-security.md`).

## 4. Image storage migration strategy
Development: files under `storage/` (D:). Rows store **relative** paths. Phase 4 introduces an `ImageStore` protocol
with local and S3-compatible/Supabase Storage implementations; migrating = copy objects preserving relative keys, set
`STORAGE_BACKEND`, done. Supabase Storage free tier is 1 GB - face crops can exceed that, so budget accordingly.

## 5. D: drive policy (this machine)
Project root is `D:\Face-Tracker` (chosen over `D:\KatomaranAI` because the existing git repository lives there).
Logical layout from the brief maps to: `.venv`, `.tools` (uv + managed Python 3.12), `models`, `datasets`, `storage`,
`logs`, `temp`, `cache\{pip,npm,uv,huggingface,insightface,torch,playwright}` - all inside the root and git-ignored.
`scripts\env.ps1` points every cache at these folders **for the current shell only** (no global settings changed):
`UV_CACHE_DIR, UV_PYTHON_INSTALL_DIR, UV_PROJECT_ENVIRONMENT, PIP_CACHE_DIR, npm_config_cache, HF_HOME, TORCH_HOME, INSIGHTFACE_HOME, PLAYWRIGHT_BROWSERS_PATH, TEMP/TMP`.
Pre-existing global settings were left alone: the machine already had `pip` configured to `D:\pipcache` and npm's default cache on C: (`%LOCALAPPDATA%\npm-cache`) - the scripts override it per session. System tools (Node, PostgreSQL 18 server) remain on C: by design.
Other paths are configurable (`LOG_DIR`, `STORAGE_DIR`, `MODELS_DIR`, `CONFIG_FILE`, `KATOMARAN_ROOT`) and relative by default, so Linux servers need no Windows paths.

**Docker Desktop (WSL2)** – not installed here. If used: Docker Desktop → Settings → Resources → Advanced → *Disk image location* (WSL2 backend) to a D: folder, or `wsl --export/--import` of the `docker-desktop` distro to D:. A compose file on D: does not move Docker's storage.
