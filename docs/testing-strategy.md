# Testing strategy

## Pyramid
| Layer | Tooling | What | Needs |
|---|---|---|---|
| Backend unit | pytest, FastAPI TestClient | config validation (incl. Phase 1 sources/RTSP/model cross-checks), settings, logging, error envelope, **wire protocol**, **detector geometry/NMS/thresholds on synthetic model output**, **file + RTSP source adapters (scripted fakes)**, **pipeline (frame skip, back-pressure, shutdown, cleanup, failure containment)**, **session lifecycle through the real API and worker WebSocket with a scripted fake worker** (idempotent start/stop, competing starts, timeouts, worker loss, relay, bounded queues, orphan recovery) | nothing; session tests use file-backed SQLite |
| Backend integration | pytest (`-m integration`) | Alembic upgrade/downgrade/round-trip incl. migration `0002`, model↔migration drift, ready/status against real PostgreSQL, pool hygiene | PostgreSQL (`TEST_DATABASE_URL`; `scripts\local-db.ps1` provides one) |
| Real model (`-m model`) | pytest | the downloaded YOLO face model on a public-domain photograph: finds the face, letterbox/resize alignment (IoU), two faces, no false positives on blank/noise frames, thresholds, determinism | the model file (`scripts\download-model.ps1`); **fails, never skips, when missing** |
| Live backend slice (`-m e2e`) | pytest | real uvicorn server + `WorkerAgent` + real model + real generated video over real sockets: full processing with exact frame/skip/processed counts, streamed boxes vs known face positions (IoU > 0.5, two faces), stop, unreadable video, worker death, model failure, missing model; real-OpenCV RTSP failure paths | model + OpenCV |
| Frontend unit/component | Vitest + React Testing Library | geometry (letterboxing, resize invariants), wire protocol, `LiveStream` (latest-wins decode, reconnect/back-off, no reconnect after end, cleanup), canvas drawing (recorded 2D calls incl. StrictMode regression), state copy (every state), monitoring page (preconditions, start/confirm/RUNNING, streamed frames, stats, stop, failures, polling convergence, re-attach, keyboard) | `fetch` and `WebSocket` faked per test |
| E2E | Playwright (Edge channel locally, Chromium in CI) | Phase 0 dashboard tests (stubbed API) + stubbed Live Monitoring states + **five `@live` tests against the real stack** (API + worker process + real model + embedded PostgreSQL): process a real video with pixel-level alignment of the drawn box, stop/restart, unreadable source, reload re-attach, consistency with the API | `e2e/global-setup.ts` starts and stops everything |

## What the mocked tests do and do not prove
Scripted workers, fake sockets and synthetic model outputs prove *logic and contracts*. They say nothing about
detection accuracy. Accuracy-relevant claims rest only on the `model`/`e2e` tests, and even those use one portrait
photograph (and a synthetic video made from it) - **no accuracy was measured on real footage**.

## Fixtures
* `make_settings` / `make_config` / `make_full_config` build isolated `Settings`/`AppConfig` (no `.env` read, tmp dirs).
* `sqlite_engine` - file-backed SQLite with the ORM schema for session-logic tests (per-thread pooled connections).
* `isolated_schema` - throw-away PostgreSQL schema `test_<hex>` via `search_path`; Alembic runs inside it; dropped afterwards.
* `api()` (session tests) closes every fake worker on exit, even when an assertion fails, so a failing test cannot hang the run.
* Test data: `backend/tests/fixtures/astronaut_nasa_public_domain.png` (NASA, public domain) and videos generated from it
  by `app.vision.sample_video` at test time. No other face imagery exists in the repository.

## Process hygiene
`pytest-timeout` (120 s) guards every test. Playwright's own `webServer` could not terminate process trees on this
Windows setup (it hung after the run), so `e2e/global-setup.ts` starts API + worker (`scripts/dev_stack.py`) and Vite
with output redirected to files and `global-teardown.ts` kills the whole tree via `scripts/stop_stack.py` (psutil).
The suite fails fast if ports 8000/5173 are already in use and leaves no processes behind.

## Determinism and external services
Tests never contact the hosted database: `scripts\check.ps1` starts the embedded local PostgreSQL and sets
`TEST_DATABASE_URL`. No internet is needed after dependencies and the model are downloaded.

## Quality gates (all must pass) - run with `scripts\check.ps1`
Model checksum, `ruff check`, `ruff format --check`, `mypy` (strict, tests included), `pytest` (all markers),
`eslint`, `tsc --noEmit`, `vitest`, `vite build`, `playwright test`.
