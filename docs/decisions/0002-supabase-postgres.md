# ADR 0002 – Supabase PostgreSQL (Neon fallback, local PostgreSQL for offline)
**Status:** accepted. **Decision:** `DATABASE_URL` selects any PostgreSQL; hosted default is Supabase with `sslmode=require`; Neon equivalent; Render free DB rejected (expires after 30 days). See `docs/deployment.md`.
**Consequences:** free projects pause after inactivity; first connect is slow (observed ≈5 s) so readiness uses a 5 s connect timeout with retries and the pool keeps connections warm. RLS is enabled on every table.
