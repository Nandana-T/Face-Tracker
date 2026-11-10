# Security runbook: rotate the database credential, migrate, deploy safely

## 1. Rotate the Supabase database password (you do this; I never need the password)
The password that was pasted into a chat during Phase 0 must be treated as compromised.

1. Open the Supabase dashboard → your project → **Project Settings → Database**.
2. Under *Database password* choose **Reset database password**. Generate a long random one (the dashboard has a generator) and copy it
   **to your clipboard only** – do not paste it into chat, tickets, commits or screenshots.
3. Edit `D:\Face-Tracker\.env` locally and replace the password inside `DATABASE_URL`. URL-encode special characters
   (`$`→`%24`, `@`→`%40`, `#`→`%23`, `/`→`%2F`, `:`→`%3A`). Keep `?sslmode=require`.
4. Save the file. It is git-ignored; never commit it.
5. Optional hardening in the same dashboard page: restrict database access to your IP(s) under *Network Restrictions*, and
   enforce SSL (*SSL Configuration*). If the password may have been used by someone else, also review *Logs → Postgres Logs* for
   unexpected connections around the exposure window, and rotate the project's API keys if they were ever shared.

## 2. Verify – without printing any secret
```powershell
.\scripts\migrate-cloud.ps1 -CheckOnly
```
Prints host (shortened), user, port, database, password *length*, `sslmode`, and whether the password is still the exposed one
(compared by SHA-256 fingerprint kept in git-ignored `storage\.exposed-credential.sha256`). Exit code `3` = still exposed.

## 3. Apply migration 0002 to the hosted database
```powershell
.\scripts\migrate-cloud.ps1
```
It refuses while the credential is exposed, opens one connection (`SELECT 1`), runs `alembic upgrade head`, then prints the revision and
which of `app_metadata`, `processing_sessions`, `alembic_version` exist. Expected: revision `0002`, no missing tables.
Safe to repeat: `upgrade head` is idempotent and `0002` only *creates* `processing_sessions`; it never drops or rewrites existing rows
(tested on PostgreSQL by `test_migrations_are_repeatable_and_never_destroy_existing_data`). Only a *downgrade* of `0002` drops that table.

## 4. Keep tests away from real data
* Tests no longer fall back to `DATABASE_URL`. `TEST_DATABASE_URL` is used if set, otherwise the **embedded local PostgreSQL**
  (`storage\pgdata`) starts automatically. A `TEST_DATABASE_URL` equal to `DATABASE_URL`, or pointing at a hosted provider, makes the
  run fail immediately (`backend/tests/db_safety.py`).
* If you want hosted CI/tests, create a **separate free Supabase project** used only for tests, set `TEST_DATABASE_URL` to it and
  `ALLOW_HOSTED_TEST_DB=1`. Tests additionally work inside a throw-away `test_<hex>` schema that they drop afterwards.

## 5. Production deployment checklist (API on Render, frontend on Vercel, worker on your own machine)
Required environment for the API (startup **fails** if any is missing/weak when `APP_ENV=production`):

| Variable | Rule |
|---|---|
| `APP_ENV` | `production` |
| `ADMIN_PASSWORD` | ≥ 12 characters, unique, from a password manager |
| `AUTH_SECRET` | ≥ 32 random characters (`python -c "import secrets; print(secrets.token_urlsafe(48))"`); keeps sign-ins valid across restarts |
| `WORKER_TOKEN` | ≥ 24 random characters; the same value on the worker |
| `CORS_ORIGINS` | only `https://…` origins (your Vercel domain) |
| `DATABASE_URL` | rotated credential, `?sslmode=require` |

Worker host: `WORKER_API_URL=wss://<api-host>`, `WORKER_TOKEN`, RTSP URLs in `RTSP_*` variables (never in `config.json`).
In production the interactive docs and OpenAPI schema are **not served**.
See `docs/privacy-and-security.md` for the threat model and remaining gaps.
