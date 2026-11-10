# Testing strategy

## Pyramid
| Layer | Tooling | What | Needs |
|---|---|---|---|
| Backend unit | pytest, FastAPI TestClient | config validation, settings, logging, error envelope, redaction, CORS, retry logic, session lifecycle | nothing (DB outage is simulated with an unreachable port) |
| Backend integration | pytest (`-m integration`) | Alembic upgrade/downgrade/round-trip, model↔migration drift, ready/status against real PostgreSQL, TLS, pool hygiene | PostgreSQL (`TEST_DATABASE_URL`, else `DATABASE_URL`) |
| Frontend unit/component | Vitest + React Testing Library | shell, routing, active nav, collapse, tooltips, mobile drawer, theme, connection states, empty/loading/error states, settings + confirm dialog, API client, retry policy | `fetch` mocked per test (`src/test/render.tsx`) |
| E2E | Playwright (Edge channel locally, Chromium in CI) | dashboard, navigation, status display, DB-down vs API-down, offline resilience, mobile drawer, one `@live` test through the Vite proxy to the real API | stubbed API except the `@live` test |

## Fixtures
* `make_settings` / `make_config` build isolated `Settings`/`AppConfig` (no `.env` read, tmp dirs).
* `isolated_schema` creates a throw-away schema `test_<hex>` (via `search_path`), runs Alembic inside it and drops it.
  Tests never touch `public` data. **Caveat:** with only one hosted database available this is schema-level, not
  database-level isolation. Use a separate database via `TEST_DATABASE_URL` in CI.
* Integration tests *fail* (never silently skip) when the database is unreachable.

## Determinism
No internet services are required except the database named by `DATABASE_URL`/`TEST_DATABASE_URL` for integration
tests (local PostgreSQL works equally). Frontend and E2E mocked tests are fully offline; fonts are bundled.

## CI quality gates (all must pass)
`ruff check`, `ruff format --check`, `mypy` (strict), `pytest`, `eslint`, `tsc --noEmit`, `vitest`, `vite build`, `playwright test`.
Run locally with `scripts\check.ps1`. Suggested CI: PostgreSQL service container + `TEST_DATABASE_URL`, `npx playwright install --with-deps chromium`.
