# Phase 1.5 report – security, real-video validation, cloud readiness

Status: stabilisation work done and verified. **Update 2026-11-10 (after the report):** the database credential was rotated by the owner, migration `0002` was applied to Supabase and verified (see §2/§3). Phase 2 (tracking) has since been implemented: [phase-2-report.md](phase-2-report.md).

## 1. Resolved
| Item | Outcome |
|---|---|
| Real hackathon footage | Downloaded 23 files (229 MB) from the Drive link via `gdown`, no sign-in required; stored on D: (`datasets/hackathon`). Processed all of them with the real pipeline → [real-video-validation.md](real-video-validation.md) |
| Model licence | Full inspection (card, embedded metadata, Ultralytics terms, AGPL text, dataset card) → [ADR 0011](decisions/0011-model-licence-assessment.md). Decision: acceptable for the non-commercial hackathon, **not cleared commercially**; YuNet (MIT) recommended as the replacement if clearance is needed. Model **not** changed |
| Access control | Sign-in (shared admin password → signed 8 h token), every REST route except `/health`, `/ready`, `/auth/*` protected, single-use session-bound WebSocket tickets, login throttling, production start-up guards, docs hidden in production |
| Test-DB safety | Tests no longer fall back to `DATABASE_URL`; they use `TEST_DATABASE_URL` or the embedded PostgreSQL, and refuse the dev DB or any hosted provider without an explicit opt-in |
| Migration safety | `0002` proven repeatable and non-destructive on real PostgreSQL (rows survive re-runs and a down/up cycle); invariants (one active session, state check) proven on PostgreSQL |
| Credential tooling | `scripts\migrate-cloud.ps1` / `db_credential_check.py`: fingerprint check without printing secrets; refuses to touch the hosted DB while the exposed password is in use |

## 2. Outstanding blockers
1. ~~Database password not rotated~~ **Resolved after this report was written:** the owner rotated it; the fingerprint check passes; the connection uses the Supabase *Session pooler* (the direct host is IPv6-only and unreachable from this machine) with `sslmode=require` (client→pooler TLS verified through libpq).
2. **Model licensing decision** for any commercial use (ADR 0011) – owner's call.
3. Not testable here: a real RTSP camera, a GPU, Docker/Render/Vercel deployments, a labelled ground-truth set for accuracy.

## 3. Database migration status
* **Hosted (Supabase): migration `0002` APPLIED and verified** (after the credential rotation): revision `0001 → 0002`; tables `alembic_version`, `app_metadata`, `processing_sessions` present, none missing; `processing_sessions` has 0 rows; row-level security enabled on both application tables; the application's readiness check passes (≈ 5 s first call, as in Phase 0); server PostgreSQL 17.11. Tests still use the embedded local database by design.
* **Local PostgreSQL (embedded, `storage/pgdata`): at `0002`**; tables `app_metadata`, `processing_sessions`, `alembic_version` present; model↔migration drift test clean.
* `scripts\migrate-cloud.ps1` is idempotent and can be re-run at any time.

## 4. Real-video validation (summary – details and caveats in [real-video-validation.md](real-video-validation.md))
* 23 videos, 22.1 min, 33,284 frames (22 × 2688×1520 @ 25 fps ≈ 60 s; 1 × 3840×2160 @ 29.97 fps, 8 s), 0 unreadable frames.
* 11,102 frames analysed (`frame_skip=2`), **4,942 face detections** (not visitors); mean 0.34 per analysed frame in the hall clips, 14.4 in the crowded 4K sample (max 20).
* Throughput 14.3 analysed frames/s overall (≈ 12–16/s for 2.7K, 9.4/s for 4K); mean inference 55 ms; mean CPU 24 % of the machine (33 % at 4K); RSS ≈ 520 MB (881 MB at 4K). 12.9 min to process 22.1 min of video. The 4K sample was consumed at about real time with no headroom.
* Independent cross-check (YuNet): 98 % of YOLO's detections were also found by it; its many extra boxes are mostly **its** false positives.
* Manual review of 25 frames (one AI reviewer, indicative only): boxes tightly aligned; no boxes on non-head objects; ≈ 6 of ≈ 40 boxes questionable (backs of heads, tiny seated figures at 0.50–0.60); misses on masked, edge-cropped and very small/averted faces (≈ 3 of ≈ 24 faces in the crowd frame). Raising the threshold would trade misses for fewer false positives; not tuned without labels.
* Evidence: 92 annotated frames + 16 review sheets in `datasets/evidence/hackathon/` (git-ignored: real people).

## 5. Model licensing decision (ADR 0011)
Card says Apache-2.0; the file's embedded metadata and Ultralytics' published terms say AGPL-3.0 (all models trained with its tooling); the training data (WIDER FACE) is listed CC BY-NC-ND 4.0. Treat as **AGPL-3.0 + non-commercial**. Hackathon/internal use with locally downloaded weights: acceptable. Bundling/hosting for third parties or commercial use: not cleared. Replacement if needed: **YuNet** (MIT; 0.23 MiB; lower reported AP, protocols differ; training-data provenance to verify).

## 6. Security improvements
Details in [privacy-and-security.md](privacy-and-security.md). Verified by tests (18 backend security tests, 9 frontend, 3 live E2E) **and** a production-mode smoke run: unsafe production config refuses to start; with safe config `/health` and `/ready` public (200), `/api/docs` and `/api/openapi.json` 404, `/sources` and `/system/status` 401 without a token, wrong password 401 (and throttled after 5), right password → token → 200, unlisted CORS origin gets no CORS header. WebSocket: no ticket / forged ticket / reused ticket refused in a real browser; a valid ticket opens exactly once. Mutation check: removing the auth dependency fails 3 tests.
Assumptions and gaps (single shared role, no revocation/audit/MFA, in-memory throttling, token in `sessionStorage`, no CSP, TLS expected from the platform): see the security section of that document.

## 7. Test execution (this machine, 2026-11-10; all actually run)
| Check | Result |
|---|---|
| Backend pytest (TEST_DATABASE_URL unset → embedded PostgreSQL) | **192 passed** (was 168) |
| ruff check / ruff format --check / mypy strict (83 files) | clean |
| Frontend vitest | **128 passed** (was 119) |
| ESLint, `tsc --noEmit` | clean |
| Vite production build (hosted API URL baked in; bundle scanned for secrets) | succeeded, no secrets found |
| Playwright (Edge), real stack with sign-in on | **21 passed, twice in a row** (was 18), no processes left behind |
| Total automated tests | **341** (was 305): all previous tests still pass; none weakened. Intentionally updated: the live E2E tests now sign in; the stream URL now carries a ticket |

## 8. Remaining deployment limitations
* Docker, `docker compose`, Render and Vercel were **not** exercised (no Docker/WSL on this machine); `render.yaml`/`docker-compose.yml` were only parsed as YAML.
* Single API instance (live state, throttling and tickets are in memory); one worker, one active session.
* Frame stream and passwords require HTTPS in production (platform-provided); plain HTTP is only acceptable on a trusted LAN.
* Real RTSP cameras and GPUs untested; footage-specific accuracy unmeasured without labels.
* The API image includes OpenCV and ONNX Runtime although only the worker needs inference.

## 9. Phase 2 readiness verdict
**Conditionally ready (item 1 below was met afterwards; Phase 2 tracking was then built).** Remaining recommendations: (1) ~~credential rotation / migration~~ done; (2) you decide the commercial stance on model licences (it recurs with InsightFace, whose pretrained models are published for non-commercial research); (3) someone labels a few hundred frames of the hackathon footage so detector quality — and any threshold change — can be measured rather than eyeballed.
