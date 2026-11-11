# Security audit (Phase 6 pre-submission review)

Scope: repository, configuration, API/worker/browser trust boundaries, storage, build artifacts. Performed by reading code and running the checks below; it is **not** an external penetration test.

## 1. Findings
| # | Area | Finding | Status |
|---|---|---|---|
| 1 | Secrets in git | Scanned all tracked files and the history for connection strings with passwords, JWT-like tokens, private keys and cloud key patterns: only placeholders (`<ref>`), local-development defaults, `.env.example` and test fixtures match. `.env` is git-ignored and was never committed (history contains only `.env.example`). | ✅ clean |
| 2 | Binary / biometric data in git | Only one tracked image: the **NASA public-domain portrait** test fixture. `datasets/`, `models/`, `storage/`, `logs/`, `temp/`, `*.onnx`, `*.mp4` are git-ignored. Real hackathon faces and evidence stay local. | ✅ clean |
| 3 | Frontend bundle | A production build was scanned for `SERVICE_ROLE`, `WORKER_TOKEN`, `ADMIN_PASSWORD`, `AUTH_SECRET`, `postgresql://`, `supabase.co`: none. The single `DATABASE_URL` hit is a UI help string. Only `VITE_API_BASE_URL` (public) is embedded. `scripts/smoke_deployed.py` repeats the scan on the deployed bundle. | ✅ |
| 3b | Service-role key placement | Exists only in the API process (`Settings.supabase_service_role_key`, a `SecretStr`, never logged, absent from `repr`/`model_dump` – tested). The worker environment is built without database or storage keys (asserted in `verify_remote_path.py`). | ✅ |
| 4 | Biometric storage | Supabase bucket must be **private**; images are reachable only through authenticated API endpoints (`private, no-store`); no public URL is ever generated; `verify_private()` and `scripts/verify_storage.py` check the bucket and that the public URL is refused (real-service check pending credentials). Deterministic keys; erasure deletes objects. | ✅ code / ⏳ real bucket unverified |
| 5 | Authentication | Production start-up refuses: missing/short admin password, auth secret, worker token, non-https CORS. Login throttling, signed expiring tokens, single-use 30 s stream tickets bound per resource (a *session* ticket cannot open the analytics stream); worker token compared in constant time; wrong worker token refused (verified over WSS). | ✅ |
| 6 | Transport | WSS/HTTPS exercised with a self-signed certificate on loopback; Origin allow-list on browser sockets. Real certificates are provided by Render/Vercel (unverified until deployed). | ✅ local / ⏳ hosted |
| 7 | Data exposure | No embeddings, storage paths, credentials or server filesystem paths in any API response (asserted per endpoint); RTSP URLs (may contain credentials) never returned and logged redacted. | ✅ |
| 8 | Database | RLS enabled on all biometric/event tables (`0003`, `0004`); backend connects as owner; tests refuse hosted URLs. Not weakened. | ✅ |
| 9 | Authorization model | **One shared admin role**: any signed-in user can view and erase any visitor. No audit trail of who viewed an image. | ⚠ known limitation |
| 10 | Encryption at rest | Embedding columns/evidence rely on the provider's disk encryption; no application-level encryption. | ⚠ known limitation |
| 11 | Supply chain | Dependencies pinned by lockfiles (`uv.lock`, `package-lock.json`); model weights pinned by SHA-256. `npm audit` / `pip-audit` were **not** run in this phase. | ⚠ not run |
| 12 | Ephemeral hosting disk | On Render's free tier local files vanish on redeploy. The API warns at start-up when production runs with local storage; durable evidence requires `STORAGE_BACKEND=supabase`. | ✅ mitigated |
| 13 | Log hygiene | `events.log` holds pseudonymous visitor ids/labels (no images/embeddings); app logs hold no credentials; storage errors never include the key (tested). Rotate/delete `events.log` per your retention policy. | ✅ |
| 14 | Docker/Render blueprint | Never built (no Docker on the dev machine). | ⚠ unverified |

## 2. Defects found by Phase 6 verification (fixed)
* **RTSP connection state never reached the API/UI** – the worker did not pass the source's state callback, so a lost stream looked "connected" forever. Fixed and unit-tested.
* **A stalled RTSP stream (server connected, no data) could block the reader indefinitely** – added a stall watchdog that abandons the capture and reconnects. Unit-tested; verified against a real local RTSP server.
* **Evidence could only be written to the local disk** – unusable on ephemeral hosting; storage backends added (local + Supabase private).

## 3. Residual risks to state in the submission
Single shared admin credential; no per-user audit; model-licence stance (ADR 0013, hackathon scope); recognition errors and look-alikes; counts are identities not people; consent/legal basis for biometric processing is the operator's responsibility; hosted components unverified until you deploy them.
