# Deployment guide – Vercel (frontend) · Render (API) · Supabase (database + private storage) · local AI worker

**Status: nothing has been deployed or provisioned by this repository's tooling.** Every step below that touches an external account is a *gate* that needs your explicit approval and your credentials. What was verified locally is listed in [phase-6-report.md](phase-6-report.md). Nothing here claims a deployed URL exists.

## 1. Dependency graph
```
Browser (Vercel static SPA, HTTPS)
   │  REST + WSS (single-use tickets)            VITE_API_BASE_URL = https://<api>/api/v1
   ▼
API on Render (ONE instance; lightweight - no models loaded; RSS ≈ 115 MB measured)
   ├─ PostgreSQL (Supabase, TLS)   ← migrations 0001…0004 (hosted is at 0002 today)
   ├─ Supabase Storage PRIVATE bucket (service-role key, server-side only)  ← faces/ + evidence/
   └─ WSS endpoint /api/v1/worker/ws  ◄── dials out ── AI worker (YOUR machine, D:\Face-Tracker)
                                                       ├─ detector + tracker + recognition models (CPU, local)
                                                       ├─ video file / local RTSP camera (never reachable from the cloud)
                                                       └─ holds ONLY WORKER_TOKEN and the API URL (no DB, no storage keys)
```
Build order: Supabase project → backup → migrations → private bucket → Render API → Vercel → worker → smoke test.

## 2. Plans, limits and cost (checked against current provider docs on 2026-11-11 – re-check before relying on them)
| Service | Plan used | What the docs say (fetched) | Consequence for this project |
|---|---|---|---|
| Render web service | **Free** | spins down after 15 min without inbound HTTP/WebSocket traffic; ≈ 1 min cold start; 750 instance-hours/month per workspace; **ephemeral filesystem**; single instance only; WebSockets supported; RAM/CPU not stated on the page fetched – check the dashboard | cold start before a demo: open the app and wait ≈ 1 min; a connected worker's heartbeat keeps it awake; **local files are lost on redeploy → use Supabase Storage** (this is why Phase 6 added it); one instance matches the design (live state is in memory) |
| Supabase | **Free** | Storage quota **1 GB** on the Free plan (other limits not on the page fetched) | face crops are ≈ 7–15 KB: thousands of events fit; free projects may pause when idle – check the project before the demo |
| Vercel | **Hobby** | static SPA, rewrites supported | Hobby is for personal/non-commercial use – fine for a hackathon submission; confirm the current terms |
| Worker | your PC | – | no hosting cost; CPU only |
**Expected cost: 0 on the free tiers.** Anything paid (a Render Starter instance to avoid cold starts, Supabase Pro for backups/point-in-time recovery) is **not provisioned and needs your approval**.

## 3. Gate A – Supabase migrations (explicit authorization required)
Not run. Preconditions (all must be true before `scripts\migrate-cloud.ps1`):
1. Destination: open the Supabase dashboard; confirm project ref/region match the `DATABASE_URL` host you will use.
2. TLS: URL ends with `?sslmode=require` (the script refuses otherwise).
3. Alembic: `alembic heads` → exactly one head `0004`; hosted `alembic current` → `0002`.
4. Review [migration-0004-preflight.md](migration-0004-preflight.md) (additive, no destructive statements, RLS enabled; `0003` is also additive).
5. **Backup:** the Free plan has no point-in-time recovery. Take `pg_dump "<DATABASE_URL>" -Fc -f D:\Face-Tracker\backups\pre-0003.dump` first (the folder is git-ignored) and keep it; restore plan = `pg_restore` into a fresh project. Alternatively export from the dashboard.
6. Your explicit "apply" instruction. Then `.\scripts\migrate-cloud.ps1`; verify: `alembic current` = `0004`, tables `visitors, face_embeddings, identity_associations, visits, visit_events, event_delivery_outbox` exist, RLS on (`relrowsecurity`), `GET /api/v1/system/status` shows `0004`. **Never disable RLS** to make a check pass.

## 4. Supabase private storage (after Gate A; needs your project)
1. Storage → New bucket → name `katomaran-evidence` → **Public: OFF** (private). Add no policies (the service role bypasses RLS; the browser gets no direct access).
2. Settings → API → copy the **service_role** key. It is a server secret: set it **only** as `SUPABASE_SERVICE_ROLE_KEY` on the Render service. Never in Vercel, never in the worker's environment, never in git.
3. API env: `STORAGE_BACKEND=supabase`, `SUPABASE_URL=https://<ref>.supabase.co`, `SUPABASE_BUCKET=katomaran-evidence`.
4. Verify against the real bucket (read-only except one throw-away object): `python scripts\verify_storage.py` (needs the three variables) – checks the bucket is private, an upload/read/duplicate/delete round trip with a synthetic image, and that the public URL is refused.
Objects: `faces/<visitor_id>/<sha>.jpg`, `evidence/entries|exits/YYYY-MM-DD/<event_id>.jpg`; deterministic keys make uploads idempotent; erasure deletes the objects; reconciliation lists the bucket to remove orphans and flag missing objects. **The Supabase REST client logic is proven against a faithful in-memory fake only (`tests/unit/test_blob_store.py`); real-service behaviour is unverified until step 4 is run with credentials.**

## 5. Render API (after Gates A and 4; needs your account)
`render.yaml` is a blueprint (unapplied). Dashboard → New → Blueprint (or Web Service from Docker) → repo `…` → `render.yaml`. Set the `sync: false` variables:
`DATABASE_URL` (Supabase, `sslmode=require`; if you use the **pooler** (port 6543) set `DB_USE_PGBOUNCER=true`), `CORS_ORIGINS=https://<your-app>.vercel.app` (https only), `WORKER_TOKEN` (≥ 24 chars: `python -c "import secrets;print(secrets.token_urlsafe(32))"`), `ADMIN_PASSWORD` (≥ 12), `AUTH_SECRET` (≥ 32), `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`. `APP_ENV=production` makes the API refuse to start when any of these is weak or CORS is not https. Health check `/api/v1/health`; `preDeployCommand: alembic upgrade head` **would run migrations automatically – delete that line if you want migrations to stay an explicit, approved step** (recommended until Gate A is done). Keep `numInstances: 1`.
**Docker is not installed on the development machine, so `backend/Dockerfile` and the blueprint have never been built here (unverified).** The API process itself was run in production mode behind TLS locally (below). The image installs `onnxruntime`/OpenCV wheels but the API never imports `onnxruntime` and loads no model (measured RSS 115 MB).
Without Docker the same service can be created as a *native Python* web service: build `pip install uv && uv sync --frozen --no-dev`, start `uv run uvicorn app.main:app_factory --factory --host 0.0.0.0 --port $PORT`, root `backend/`, Python 3.12.

## 6. Vercel frontend (needs your account)
Import the repo, **Root Directory `frontend`**, framework Vite (detected), build `npm run build` (runs `tsc` then Vite), output `dist`, install `npm ci`. Environment variable **`VITE_API_BASE_URL=https://<api>.onrender.com/api/v1`** (public by design – the build embeds it; see `frontend/.env.example`). `vercel.json` already rewrites every non-asset path to `index.html` (deep links like `/analytics` and refresh work; verified by `scripts/smoke_deployed.py`). Nothing server-side may be put in a `VITE_*` variable. Verified locally: a production build contains no secret names (scan in [security-audit.md](security-audit.md)).
Then add the Vercel domain to the API's `CORS_ORIGINS` and redeploy the API.

## 7. Worker (your machine)
`.env` for the worker only needs: `WORKER_API_URL=wss://<api>.onrender.com`, `WORKER_TOKEN=<same token>`, `WORKER_ID=…`, `CONFIG_FILE`/`MODELS_DIR` (D:). **No `DATABASE_URL`, no storage keys.** Start: `scripts\start-worker.ps1`. Sources are defined in `config/config.json` on the worker host (`video.sources`, RTSP via `url_env`); the API only knows source ids. A hosted API cannot reach a local camera – the worker dials out. Crops travel over the same authenticated WSS and the API stores them.
Reconnection: automatic backoff 1 → 15 s; an API restart or a free-tier spin-down ends the live session consistently (`ERROR api_restarted` / `worker_lost`) and the worker reconnects by itself (measured 4–6 s locally; allow ≈ 60 s after a Render cold start).

## 8. Verify (read-only)
```powershell
$env:KATOMARAN_API="https://<api>.onrender.com/api/v1"; $env:KATOMARAN_ADMIN_PASSWORD="…"; $env:KATOMARAN_FRONTEND="https://<app>.vercel.app"
python scripts\smoke_deployed.py --require-worker      # 20+ checks, never starts a session or writes data
python scripts\reconcile_counts.py                      # against the hosted DB: report-only
```
Then run one controlled video (astronaut fixture or an authorised clip) from the UI and follow the 20-step walkthrough in [submission/demo-video-script.md](submission/demo-video-script.md).

## 9. Where live state can be lost (single API instance) and how it recovers
| Event | What is lost | Recovery |
|---|---|---|
| API restart / Render spin-down or redeploy | the live session view and in-memory visit state | session → `ERROR api_restarted`; startup reconciliation closes open visits as `INTERRUPTED` at their last persisted observation, delivers pending log lines, flags evidence; analytics are rebuilt from PostgreSQL; the worker reconnects |
| Worker disconnect | frames in flight | session → `worker_lost`; visits `INTERRUPTED`; start a new session |
| Browser refresh/reconnect | nothing | re-attaches to the active session; analytics refetch |
| Database outage | events decided in memory but not yet committed (bounded queue, retried with back-off, dead-lettered when exhausted) | retries; reconciliation |
| Storage outage | evidence for events recorded meanwhile | event stored with `image_status=PENDING`, retried; if the API dies first it is reported `MISSING`, never fabricated |
No high availability is claimed.

## 10. Rollback
Frontend: Vercel → previous deployment → Promote. API: Render → previous deploy. Database: migrations are additive; `alembic downgrade 0003`/`0002` drops only the new tables (destructive for their rows) – restore from the `pg_dump` taken in §3 if needed. Storage: delete objects by prefix.
