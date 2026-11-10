"""Guards that keep the test-suite away from real data.

Integration tests create and drop a throw-away schema, but they must still never be pointed at the
development/production database by accident (for example because ``TEST_DATABASE_URL`` was left
unset and the old fallback silently reused ``DATABASE_URL``).
"""

from __future__ import annotations

from sqlalchemy.engine import make_url

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
HOSTED_MARKERS = ("supabase.co", "supabase.com", "neon.tech", "render.com", "amazonaws.com")


def _identity(url: str) -> tuple[str, int, str]:
    u = make_url(url.replace("postgresql://", "postgresql+psycopg://", 1))
    return (u.host or "").lower(), u.port or 5432, u.database or ""


def check_test_database(test_url: str, dev_url: str | None, allow_hosted: bool = False) -> None:
    """Raise RuntimeError unless ``test_url`` is a database tests may safely use."""
    host, port, name = _identity(test_url)
    if dev_url and _identity(dev_url) == (host, port, name):
        raise RuntimeError(
            "TEST_DATABASE_URL points at the same database as DATABASE_URL. Tests refuse to run "
            "against the development/production database. Use the embedded local PostgreSQL "
            "(leave TEST_DATABASE_URL unset or run scripts\\local-db.ps1) or a dedicated "
            "test project."
        )
    if host not in LOCAL_HOSTS and any(m in host for m in HOSTED_MARKERS) and not allow_hosted:
        raise RuntimeError(
            f"TEST_DATABASE_URL targets a hosted provider ({host.split('.')[-2]}). Set "
            "ALLOW_HOSTED_TEST_DB=1 only for a DEDICATED test project that holds no real data."
        )
