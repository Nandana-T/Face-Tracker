# Privacy and security (updated for Phase 4)

Faces are **biometric data**. Phases 1-2 only detected and tracked faces (nothing identifying was stored). **From Phase 3 the system creates and stores biometric templates**
(face embeddings) and representative face images of people it registers automatically. This document records what is stored, what is exposed, who may see it, and what is still missing.
Section *Biometric data (Phase 3)* below is the authoritative description of the new data; the table that follows still describes Phases 1-2 and is unchanged.

## Biometric data (Phase 3)
| Data | Where | Who can read it | Retention / deletion |
|---|---|---|---|
| Face **embedding** (128 float32) of a registered visitor, ≤ 5 per visitor, tagged with model version | PostgreSQL `face_embeddings` (RLS enabled); the identity manager's in-memory index | Only the API/worker processes. **No API response, log line or event contains an embedding**; the frontend never receives one | Until the visitor is erased (`DELETE /visitors/{id}`: embeddings, image, associations and index entry) or `recognition.retention_days` (0 = keep) has passed since last seen |
| Representative **face image** (≤ 192 px crop, one or a few per visitor) | `recognition.faces_dir` (default `storage/faces`, on D:), content-addressed `<visitor>/<hash>.jpg` | Authenticated `GET /visitors/{id}/image` only (`Cache-Control: private, no-store`); no static route, no directory listing; the path is validated and confined to the store | Same as above; orphan files from interrupted writes are removed by `reconcile()` |
| Observation embeddings in transit (worker → API) | One WebSocket message each, in RAM | Worker/API | Not stored unless they become part of a registration |
| Track → visitor association (`identity_associations`) | PostgreSQL | Authenticated visitor API | Cascade-deleted with the visitor or the session |
| Video frames | RAM only, as before. Crops sent for registration are small face regions, not frames | - | - |

* **Consent and permitted use (assumption to confirm):** only footage the operator is authorised to process, of people informed under the applicable law (e.g. GDPR/local biometric statutes), may be used. The repository ships no footage of real people;
  evaluation datasets (LFW, the hackathon clips) stay on D: in git-ignored folders and are not redistributed. Registering visitors from public cameras may be unlawful without notice/consent - this is a deployment decision, not a software feature.
* **Authorization:** all visitor/recognition endpoints sit behind the same signed bearer token as the rest of the API (401 without it); the image endpoint is covered by tests (`test_visitors_api.py`). There is still **one shared admin role**: any signed-in user can view and erase any visitor.
  Per-user roles, an audit trail of who viewed or erased what, and encryption of the biometric columns at rest (beyond the database provider's disk encryption) are **not implemented**.
* **No inference of demographics:** the system computes similarity between embeddings only; it does not estimate age, gender or ethnicity and has no code path for it.
* **Limits of face recognition:** false matches and false non-matches happen. The thresholds are conservative and an uncertain face stays *undecided* instead of being forced into an identity, but a wrong merge or a duplicate is possible
  (measured rates: docs/phase-3-report.md). Do not use these identities for decisions about people without human review. Performance may differ across demographic groups and image conditions; no per-group evaluation was performed.
* **Logs:** identity logging contains visitor numbers, track ids and states - never vectors or images. `events.log` entries `identity.registered|recognized|ambiguous|failed` carry session id, track id and visitor number.

## Visit evidence (Phase 4)
| Data | Where | Who can read it | Retention / deletion |
|---|---|---|---|
| Entry / exit **face crops** (≤ 192 px) | `visits.evidence_dir` (default `logs/`, on D:): `entries/YYYY-MM-DD/<event_id>.jpg`, `exits/...` | Authenticated `GET /events/{id}/image` only (`private, no-store`); no static route; names are validated hex ids and confined to the root | Deleted when the visitor is erased; orphans (no database row) removed by reconciliation after a grace period |
| `visits`, `visit_events`, outbox (PostgreSQL, RLS enabled) | database | Authenticated events/visits API (no paths, no embeddings) | **Kept** when the visitor is erased, but de-identified: `visitor_id`/`visitor_number` set to NULL, evidence deleted (`image_status = ERASED`). They hold times, source, outcome only |
| `events.log` lines | `logging.events_file` (rotating, 10 MB × 5) | Operators with file access | Append-only: lines written before an erasure keep the pseudonymous visitor id/label (no image, no embedding). An erasure writes a `visitor.erased` line. Delete/rotate the file per your retention policy |
* A crop is evidence only if it passed the recognition quality gate, is a valid non-blank image and is attributed to the visitor's own track; a track whose identity changed loses its earlier crops. A black frame, an empty crop or an unusable image is **never stored** - the event says `UNAVAILABLE`.
* **Exit images are not new captures.** They are the visitor's last good observation, or - when none exists - the entry image labelled `entry_representative`. The UI states which one it is.
* **What these events do not prove.** An exit is the last sighting, not proof of leaving the premises; a stream ending is not an exit; the number of visits is not a number of people (recognition errors, look-alikes and the quality gate make it unreliable without identity ground truth).
* No new roles, no audit trail of who viewed an image, no encryption of evidence files at rest beyond the disk: the Phase 3 limitations still apply.

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
