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
* `DATABASE_URL`, `WORKER_TOKEN` and RTSP URLs are read from environment/`.env` (git-ignored). `config.json` holds only the
  *name* of the variable (`url_env`) for an RTSP source - never the URL. Validation rejects a raw URL there.
* The API never returns source locations; `GET /config` and `GET /sources` are sanitised (tested).
* **Outstanding blocker:** the Supabase database password from Phase 0 was shared in a chat and has **not been rotated**
  (`.env` still contains it). Anyone who saw that message can reach the hosted database. Rotate it in the Supabase dashboard,
  update `.env`, and only then run `scripts\migrate.ps1` against the hosted database (migration `0002` is pending there).
  Until then Phase 1 uses the embedded local PostgreSQL.

## Access control - what exists and what does not
| Surface | Control today | Gap |
|---|---|---|
| `/worker/ws` | Bearer `WORKER_TOKEN`, constant-time compare; refused when unset; one worker at a time | Token is a shared secret; no rotation/mTLS |
| `/sessions/*/stream` (browser frames) | `Origin` allow-list (= CORS origins) + unguessable 128-bit session id | **No user authentication.** Anyone who can reach the API and learns a session id can watch the stream. Origin is not enforced for non-browser clients |
| REST (`/sources`, `/sessions`) | CORS allow-list only | **No authentication/authorization** - anyone who can reach the API can start/stop processing |

**Do not expose this API to the internet with a real camera or real visitor footage until an authentication phase adds
user login, per-user authorization and audit logging.** On a trusted LAN/dev machine it is acceptable.

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
