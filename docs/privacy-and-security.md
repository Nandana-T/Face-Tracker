# Privacy and security (Phase 1)

Faces are **biometric data**. Phase 1 only *detects* faces; it does not identify, store or track anyone, and the UI
says so. This document records what is stored, what is exposed, and what is still missing.

## Data handling
| Data | Where it lives | Retention |
|---|---|---|
| Video frames | RAM only: reader queue (≤ `worker.queue_size` frames), one JPEG slot per hop, one decoded bitmap in the browser | Seconds; overwritten continuously. **Never written to disk or PostgreSQL** |
| Face boxes / confidences | Inside the streamed frame message only | Not persisted |
| Session summary | `processing_sessions`: state, timestamps, counters, last stats snapshot, settings snapshot | Until deleted; contains **no images, no boxes, no identities** |
| Logs | `logs/app.log` (JSON). Per-frame activity is DEBUG-only; INFO has session milestones and summaries. No frames, no URLs with credentials (RTSP URLs are redacted to `scheme://host:port/path`) | Rotating files on D: |
| Test imagery | One public-domain NASA portrait (`backend/tests/fixtures/`); generated synthetic videos in `datasets/` (git-ignored) | Fixture only |

Only authorized, consented footage may be processed. The repository contains no footage of real visitors. The
hackathon sample video was **not available** to this phase.

Detection is not identification: a box means "a face-like region", not "a person", not "the same person as before".
`detections_total` sums faces over analysed frames and must never be presented as visitors; the UI labels it
"Face detections (sum)" and explains why.

## Credentials and secrets
* `DATABASE_URL`, `WORKER_TOKEN`, `ADMIN_PASSWORD`, `AUTH_SECRET` and RTSP URLs are read from environment/`.env` (git-ignored). `config.json` holds only
  the *name* of an RTSP variable (`url_env`); validation rejects a raw URL there. A repository scan finds none of these values outside `.env`.
* The API never returns source locations; `GET /config` and `GET /sources` are sanitised (tested).
* **Resolved:** the Supabase password shared in chat during Phase 0 has been **rotated** (fingerprint check passes; nothing printed); `.env` uses the Session-pooler string with `sslmode=require`
  (client→pooler TLS verified). The old password still appears in earlier chat history, which is harmless once rotated. `scripts\migrate-cloud.ps1` still refuses to run if the exposed password ever returns.
* **Phase 2:** tracks are temporary, per-session, held in memory and discarded when the session ends; only aggregate counters are stored. No face images, embeddings or identities are created or stored.

## Access control (Phase 1.5)
| Surface | Control | Notes |
|---|---|---|
| REST (`/sources`, `/sessions*`, `/system/status`, `/config`) | Bearer token from `POST /auth/login` (single shared admin password, HMAC-signed, 8 h, login throttled) | Mandatory in production: startup fails without `ADMIN_PASSWORD`/`AUTH_SECRET`. `/health`, `/ready` stay public and reveal nothing |
| Browser frame stream | Single-use, 30 s, session-bound ticket (browsers cannot set WebSocket headers) **plus** the Origin allow-list | The access token is never placed in a URL. Frames are therefore not reachable without signing in |
| Worker link | `WORKER_TOKEN` bearer, constant-time compare, refused when unset (≥ 24 chars in production); independent of the admin password (tested) | One worker at a time |
| CORS | explicit origins only; `https://` only in production; no credentials/cookies are used, so cross-site request forgery does not apply | |
| Interactive API docs / OpenAPI | not served in production | |
| Source configuration | server-side only; the browser can only pick an id | RTSP credentials never leave the worker environment |

**Security assumptions and remaining gaps** (deliberately minimal system, not a full IAM):
1. One shared password = one role. No per-user accounts, audit trail, password reset or MFA. Anyone with the password can start/stop sessions and watch frames.
2. Tokens cannot be revoked individually (they expire; changing `AUTH_SECRET` invalidates all). The token is kept in `sessionStorage`, so an XSS bug would expose it - the app renders no user-supplied HTML and ships a strict dependency set, but there is no CSP yet.
3. Login throttling and ticket replay protection are in API-process memory (fine for the single-instance design; use a shared store before scaling out).
4. TLS is expected from the platform (Render/Vercel). Without HTTPS the password and token travel in clear text - do not use sign-in over plain HTTP outside a trusted LAN.
5. The ticket is in the WebSocket query string and may appear in server access logs for ~30 s of validity; it is single use.
6. Live footage is still personal data: retention, consent and legal basis remain the operator's responsibility (see above).

## Network
* RTSP is credential-bearing and usually plaintext. Keep camera VLANs private; run the worker next to the camera (it
  dials *out* to the API, so no inbound port is needed). Use `wss://` between worker and a hosted API.
* The browser never receives or sends an RTSP URL; it can only select a configured source id.
* Message sizes are bounded (control 16 KiB, header 32 KiB, frame 2 MiB) and per-hop queues are bounded.

## Deployment risks to review before going beyond a hackathon
1. Model licence (AGPL-3.0 vs Apache-2.0 vs WIDERFACE terms) - see ADR 0007.
2. Legal basis, notice/consent and retention rules for processing faces in your jurisdiction (e.g. GDPR/BIPA-style laws).
3. The authentication gap above.
4. Hosted database: row-level security is enabled on all tables (no policies); the backend connects as the owner.
