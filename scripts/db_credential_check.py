r"""Safe checks for the hosted-database credential. Never prints a password or a full URL.

    python scripts/db_credential_check.py              # offline: is .env still using the exposed password?
    python scripts/db_credential_check.py --connect    # also open one connection (refused if exposed)
    python scripts/db_credential_check.py --migrate    # connect, then `alembic upgrade head`, then verify

The "exposed" password is recognised by SHA-256 fingerprint stored in
``storage/.exposed-credential.sha256`` (git-ignored) or ``KATOMARAN_EXPOSED_SHA256``, so the
password itself does not need to appear anywhere. Exit codes: 0 ok, 3 credential still exposed,
4 connection/migration failure, 2 usage/configuration error.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT_FILE = ROOT / "storage" / ".exposed-credential.sha256"
EXPECTED_TABLES = {"app_metadata", "processing_sessions", "alembic_version"}


def load_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    env_file = ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("DATABASE_URL is not set (.env or environment)")


def exposed_fingerprints() -> set[str]:
    values = {v.strip().lower() for v in os.environ.get("KATOMARAN_EXPOSED_SHA256", "").split(",") if v.strip()}
    if FINGERPRINT_FILE.is_file():
        values |= {FINGERPRINT_FILE.read_text(encoding="ascii").strip().lower()}
    return values


def describe(url_text: str) -> dict[str, object]:
    url = make_url(url_text.replace("postgresql://", "postgresql+psycopg://", 1))
    password = url.password or ""
    digest = hashlib.sha256(password.encode()).hexdigest()
    host = url.host or ""
    return {
        "driver": url.drivername,
        "user": url.username,
        "host": host[:4] + "…" + host[-12:] if len(host) > 20 else host,
        "port": url.port,
        "database": url.database,
        "password_length": len(password),
        "sslmode": url.query.get("sslmode"),
        "is_local": host in ("localhost", "127.0.0.1", "::1"),
        "password_is_the_exposed_one": digest in exposed_fingerprints(),
    }


def main() -> int:
    args = set(sys.argv[1:])
    info = describe(load_database_url())
    for key, value in info.items():
        print(f"{key:30} {value}")
    if info["is_local"]:
        print("\nLocal database: no credential concern.")
    elif info["password_is_the_exposed_one"]:
        print(
            "\nBLOCKED: .env still uses the password that was shared in chat. Rotate it in the Supabase "
            "dashboard, update DATABASE_URL in .env, then re-run. (docs/security-runbook.md)"
        )
        return 3
    elif info["sslmode"] != "require":
        print("\nWARNING: hosted databases must use ?sslmode=require")

    if not ({"--connect", "--migrate"} & args):
        return 0

    engine = create_engine(load_database_url().replace("postgresql://", "postgresql+psycopg://", 1),
                           connect_args={"connect_timeout": 20})
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            before = _revision(conn)
        print(f"\nconnection: ok   revision before: {before}")
    except Exception as exc:  # noqa: BLE001
        print(f"\nconnection FAILED: {type(exc).__name__}")  # class only - messages can contain hosts
        return 4

    if "--migrate" in args:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT / "backend",
            capture_output=True, text=True,
        )
        print(result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "(alembic printed nothing)")
        if result.returncode != 0:
            print("migration FAILED (see alembic output; no secrets were printed)")
            return 4

    with engine.connect() as conn:
        after = _revision(conn)
        tables = set(inspect(conn).get_table_names())
        print(f"revision now: {after}")
        print(f"tables: {sorted(tables & EXPECTED_TABLES)}   missing: {sorted(EXPECTED_TABLES - tables)}")
        if "processing_sessions" in tables:
            n = conn.execute(text("SELECT count(*) FROM processing_sessions")).scalar()
            print(f"processing_sessions rows: {n}")
    engine.dispose()
    return 0


def _revision(conn) -> str | None:  # type: ignore[no-untyped-def]
    if "alembic_version" not in inspect(conn).get_table_names():
        return None
    return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


if __name__ == "__main__":
    sys.exit(main())
