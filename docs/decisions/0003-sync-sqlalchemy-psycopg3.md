# ADR 0003 – Synchronous SQLAlchemy 2.x with psycopg 3
**Status:** accepted. **Decision:** `def` endpoints running in FastAPI's threadpool with a pooled sync engine; Alembic uses the same engine factory.
**Why:** fewer moving parts, identical driver for API, worker and migrations, adequate for dashboard queries. Async I/O is not justified until WebSocket fan-out (Phase 6); psycopg 3 supports async, so switching is localised to `db/session.py`.
