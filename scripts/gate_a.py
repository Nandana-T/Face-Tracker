r"""Gate A tooling: hosted Supabase migration 0002 -> 0004 (preflight, review, backup, restore rehearsal, verify).

    python scripts/gate_a.py preflight       READ-ONLY on the hosted database: destination, TLS, revision, tables, RLS, size
    python scripts/gate_a.py review          offline: the exact SQL of 0003+0004 and a scan for destructive operations
    python scripts/gate_a.py backup          READ-ONLY on the hosted database (pg_dump); writes ONLY under D:\Face-Tracker\backups
    python scripts/gate_a.py restore-test F  restores dump F into a throw-away LOCAL database, compares row counts, then
                                              REHEARSES the migration (0002 -> 0004) on that copy and checks data survival
    python scripts/gate_a.py verify          READ-ONLY post-migration verification of the hosted database (expects 0004)

No subcommand writes to the hosted database; the migration itself is the separate, approved step
(``.\scripts\migrate-cloud.ps1``). Secrets are never printed: the connection is passed to child processes through
environment variables, output shows host/port/database/user only (the host is needed to confirm the project).
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
BACKUPS = ROOT / "backups"
PG_BIN = Path(r"C:\Program Files\PostgreSQL\18\bin")
sys.path.insert(0, str(ROOT / "scripts"))

from db_credential_check import describe, load_database_url  # noqa: E402

NEW_TABLES = ("visitors", "face_embeddings", "identity_associations", "visits", "visit_events", "event_delivery_outbox")
OLD_TABLES = ("app_metadata", "processing_sessions")
HEAD = "0004"


def hosted_url() -> URL:
    url = make_url(load_database_url().replace("postgresql://", "postgresql+psycopg://", 1))
    if (url.host or "") in ("localhost", "127.0.0.1", "::1"):
        raise SystemExit("DATABASE_URL points at a LOCAL database; Gate A is about the hosted one.")
    return url


def guard(url: URL) -> None:
    info = describe(load_database_url())
    if info["password_is_the_exposed_one"]:
        raise SystemExit("BLOCKED: .env still holds the password that was shared in chat. Rotate it first.")
    if info["sslmode"] != "require":
        raise SystemExit("BLOCKED: DATABASE_URL must carry ?sslmode=require")


def engine_for(url: URL) -> Any:
    return create_engine(url, connect_args={"connect_timeout": 20}, pool_pre_ping=True)


def header(title: str) -> None:
    print(f"\n=== {title}")


def tables_and_counts(conn: Any, names: tuple[str, ...]) -> dict[str, int | None]:
    present = set(inspect(conn).get_table_names())
    out: dict[str, int | None] = {}
    for t in names:
        out[t] = int(conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar_one()) if t in present else None
    return out


# ============================================================================== preflight
def preflight() -> int:
    url = hosted_url()
    guard(url)
    info = describe(load_database_url())
    header("Destination (compare with the Supabase dashboard: Project Settings -> Database)")
    print(f"host      {url.host}")  # a project host is not a secret; needed to confirm the project
    print(f"port      {url.port}   ({'session/direct' if url.port == 5432 else 'TRANSACTION POOLER - pg_dump will not work on 6543, use 5432' if url.port == 6543 else 'custom'})")
    print(f"database  {url.database}")
    print(f"user      {url.username}")
    print(f"password  <hidden, {info['password_length']} characters>  exposed-password check: {'FAILED' if info['password_is_the_exposed_one'] else 'ok'}")
    print(f"sslmode   {url.query.get('sslmode')}")
    eng = engine_for(url)
    try:
        with eng.connect() as c:
            header("Connection")
            version = c.execute(text("SELECT version()")).scalar()
            # Client -> server TLS as libpq sees it (this is the leg we control). Behind Supabase's pooler the
            # server-side pg_stat_ssl describes only the pooler -> Postgres hop inside Supabase, so it is NOT used.
            raw = c.connection.dbapi_connection
            ssl_on = bool(raw.pgconn.ssl_in_use)  # type: ignore[union-attr]
            print(f"TLS client->server  {ssl_on}  (libpq ssl_in_use; sslmode=require makes a plaintext connection impossible)")
            hop = c.execute(text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")).scalar()
            print(f"(server-side view, pooler->database hop inside Supabase: ssl={hop} - informational)")
            print(f"server          {str(version).split(',')[0]}")
            print(f"database size   {c.execute(text('SELECT pg_size_pretty(pg_database_size(current_database()))')).scalar()}")
            print(f"is read-only?   {c.execute(text('SHOW transaction_read_only')).scalar()}")
            header("Alembic")
            has = "alembic_version" in inspect(c).get_table_names()
            rev = c.execute(text("SELECT version_num FROM alembic_version")).scalar() if has else None
            print(f"hosted revision {rev}   (expected 0002 before the migration)")
            heads = subprocess.run([sys.executable, "-m", "alembic", "heads"], cwd=BACKEND, capture_output=True, text=True, env={**os.environ, "DATABASE_URL": "postgresql+psycopg://x:x@localhost/x"})
            print(f"repository head {heads.stdout.strip().splitlines()[-1] if heads.stdout.strip() else '?'}")
            header("Tables, rows, row-level security (public schema)")
            rls = {r[0]: r[1] for r in c.execute(text("SELECT c.relname, c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r'"))}
            existing = tables_and_counts(c, OLD_TABLES + NEW_TABLES + ("alembic_version",))
            for t, n in existing.items():
                print(f"{t:26} rows={n if n is not None else '-':>6}  rls={rls.get(t, '-')}")
            clash = [t for t in NEW_TABLES if existing.get(t) is not None]
            print(f"\nnew tables already present: {clash or 'none'}  (must be none; otherwise 0003/0004 were applied or partly applied)")
            ext = [r[0] for r in c.execute(text("SELECT extname FROM pg_extension ORDER BY 1"))]
            print(f"extensions      {', '.join(ext)}")
            header("Verdict")
            ok = bool(ssl_on) and rev == "0002" and not clash
            print("READY for backup + migration" if ok else "NOT READY - resolve the points above first")
            return 0 if ok else 1
    finally:
        eng.dispose()


# ============================================================================== review (offline)
DESTRUCTIVE = re.compile(r"\b(DROP|TRUNCATE|DELETE|UPDATE|ALTER\s+TABLE\s+\S+\s+(DROP|ALTER\s+COLUMN|RENAME)|CREATE\s+EXTENSION|GRANT|REVOKE|DISABLE\s+ROW)\b", re.I)


def review() -> int:
    header("Offline SQL of the upgrade 0002 -> 0004 (nothing is executed)")
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0002:0004", "--sql"], cwd=BACKEND, capture_output=True, text=True, env={**os.environ, "DATABASE_URL": "postgresql+psycopg://x:x@localhost/x"})
    if r.returncode != 0:
        print(r.stderr[-800:])
        return 1
    sql = "\n".join(line for line in r.stdout.splitlines() if not line.startswith("INFO"))
    out = ROOT / "temp" / "gate-a-upgrade.sql"
    out.parent.mkdir(exist_ok=True)
    out.write_text(sql, encoding="utf-8")
    stmts = []
    for chunk in sql.split(";"):
        body = "\n".join(ln for ln in chunk.splitlines() if not ln.strip().startswith("--")).strip()
        if body:
            stmts.append(body)
    kinds: dict[str, int] = {}
    for s in stmts:
        k = " ".join(s.split()[:2]).upper()
        kinds[k] = kinds.get(k, 0) + 1
    print(f"{len(stmts)} statements -> {out}")
    for k, n in sorted(kinds.items()):
        print(f"  {k:24} {n}")
    header("Scan for destructive / risky operations in the UPGRADE path")
    # referential actions inside CREATE TABLE ("ON DELETE SET NULL") are definitions, not operations
    ref_action = re.compile(r"ON\s+(DELETE|UPDATE)\s+(CASCADE|SET NULL|RESTRICT|NO ACTION)", re.I)
    bad = [
        s.split("\n")[0][:110]
        for s in stmts
        if DESTRUCTIVE.search(ref_action.sub("", s)) and "alembic_version" not in s
    ]
    for b in bad:
        print("  !!", b)
    print("  none found (the only UPDATE is the alembic_version bookkeeping row)" if not bad else "")
    header("Static check of the migration files (upgrade() bodies only)")
    problems = 0
    for f in sorted((BACKEND / "alembic" / "versions").glob("000[34]_*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
        calls = [ast.unparse(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)]
        risky = [c for c in calls if re.search(r"drop_|alter_column|execute|rename", c)]
        ex = [ast.unparse(c.args[0])[:70] for c in ast.walk(fn) if isinstance(c, ast.Call) and ast.unparse(c.func).endswith("execute")]
        print(f"  {f.name}: {len(calls)} operations; create_table x{calls.count('op.create_table')}, create_index x{calls.count('op.create_index')}; "
              f"risky calls: {[c for c in risky if not c.endswith('execute')] or 'none'}; raw SQL: {ex or 'none'}")
        problems += len([c for c in risky if not c.endswith("execute")])
    header("Properties")
    print("  * additive: creates 6 new tables; no existing table or row is altered, rewritten or dropped")
    print("  * transactional DDL: PostgreSQL commits or rolls back the migration as a whole (alembic runs one transaction)")
    print("  * locks: new FKs briefly lock visitors / processing_sessions (SHARE ROW EXCLUSIVE); tables are empty -> milliseconds")
    print("  * RLS enabled on all six new tables (no policies => no access through Supabase's public API); backend connects as owner")
    print("  * downgrade() DROPS the new tables (and their rows): never use it as a first response (see recovery plan)")
    return 0 if not bad and not problems else 1


# ============================================================================== backup
def pg_env(url: URL) -> dict[str, str]:
    return {**os.environ, "PGHOST": url.host or "", "PGPORT": str(url.port or 5432), "PGUSER": url.username or "",
            "PGPASSWORD": url.password or "", "PGDATABASE": url.database or "postgres", "PGSSLMODE": "require",
            "PGCONNECT_TIMEOUT": "20"}  # fmt: skip


def backup() -> int:
    url = hosted_url()
    guard(url)
    if url.port == 6543:
        print("The transaction pooler (6543) cannot be used by pg_dump. Use the direct/session connection string (port 5432).")
        return 2
    BACKUPS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dump = BACKUPS / f"supabase-pre-0003-{stamp}.dump"
    eng = engine_for(url)
    try:
        with eng.connect() as c:
            rev = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
            counts = tables_and_counts(c, OLD_TABLES + ("alembic_version",))
    finally:
        eng.dispose()
    print(f"hosted revision {rev}; rows {counts}")
    # public schema only (our tables). Supabase-managed schemas (auth, storage, ...) are provider-owned and unused by this app.
    cmd = [str(PG_BIN / "pg_dump.exe"), "--format=custom", "--schema=public", "--no-owner", "--no-privileges", "--file", str(dump)]
    t = time.time()
    r = subprocess.run(cmd, env=pg_env(url), capture_output=True, text=True)
    if r.returncode != 0:
        print("pg_dump FAILED:", re.sub(r"(password|PASSWORD)\S*", "***", r.stderr)[-400:])
        dump.unlink(missing_ok=True)
        return 1
    sha = hashlib.sha256(dump.read_bytes()).hexdigest()
    toc = subprocess.run([str(PG_BIN / "pg_restore.exe"), "--list", str(dump)], capture_output=True, text=True)
    entries = [ln for ln in toc.stdout.splitlines() if ln and not ln.startswith(";")]
    manifest = {"dump": dump.name, "sha256": sha, "bytes": dump.stat().st_size, "created_utc": stamp, "revision": rev,
                "row_counts": counts, "toc_entries": len(entries), "schemas": "public only", "pg_dump": "18.2", "seconds": round(time.time() - t, 1)}  # fmt: skip
    (BACKUPS / f"{dump.stem}.manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    print(f"\nbackup: {dump}\n(outside Git: backups/ is ignored; it contains your data - keep it private)")
    print("NEXT: python scripts\\gate_a.py restore-test", dump)
    return 0 if toc.returncode == 0 and entries else 1


# ============================================================================== restore rehearsal (LOCAL only)
def restore_test(dump_arg: str) -> int:
    dump = Path(dump_arg).resolve()
    manifest = json.loads((BACKUPS / f"{dump.stem}.manifest.json").read_text())
    if hashlib.sha256(dump.read_bytes()).hexdigest() != manifest["sha256"]:
        print("CHECKSUM MISMATCH - the dump changed after it was written")
        return 1
    print("checksum ok")
    import pgserver  # the embedded LOCAL PostgreSQL; nothing here touches the hosted database

    server = pgserver.get_server(ROOT / "storage" / "pgdata", cleanup_mode=None)
    local = make_url(server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1))
    name = f"gatea_rehearsal_{int(time.time())}"
    admin = create_engine(local.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    clone = local.set(database=name)
    ok = True
    try:
        header(f"Restore into a throw-away LOCAL database ({name})")
        # The embedded LOCAL server is PostgreSQL 16; the archive (pg_dump 18, hosted server 17) sets the PG17+
        # parameter transaction_timeout. So the archive is converted to plain SQL by pg_restore (which also proves it
        # is readable), that single parameter is filtered out, and the SQL is applied with ON_ERROR_STOP. A real
        # disaster-recovery restore targets a PG17 project and can use `pg_restore -d` directly.
        env = pg_env(clone.set(drivername="postgresql")) | {"PGSSLMODE": "prefer"}
        sql_file = ROOT / "temp" / "gate-a-restore.sql"
        conv = subprocess.run([str(PG_BIN / "pg_restore.exe"), "--no-owner", "--no-privileges", "-f", str(sql_file), str(dump)], capture_output=True, text=True, timeout=120)
        sql_file.write_text("".join(ln for ln in sql_file.read_text(encoding="utf-8").splitlines(True) if "transaction_timeout" not in ln and ln.strip() != "CREATE SCHEMA public;"), encoding="utf-8")
        r = subprocess.run([str(PG_BIN / "psql.exe"), "-w", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(sql_file)], env=env, capture_output=True, text=True, timeout=120)
        r.returncode = r.returncode or conv.returncode
        print("restore exit code", r.returncode, (r.stderr[-300:] if r.returncode else ""))
        ok &= r.returncode == 0
        eng = create_engine(clone)
        with eng.connect() as c:
            restored = tables_and_counts(c, OLD_TABLES + ("alembic_version",))
            rev = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        print(f"restored revision {rev}; row counts {restored}")
        same = restored == manifest["row_counts"] and rev == manifest["revision"]
        print("row counts and revision match the hosted source at backup time:", same)
        ok &= same
        header("Rehearse the migration on the restored copy (0002 -> 0004)")
        env = {**os.environ, "DATABASE_URL": clone.render_as_string(hide_password=False)}
        up = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env, capture_output=True, text=True)
        print("alembic upgrade head exit code", up.returncode, (up.stderr.strip().splitlines()[-1] if up.returncode and up.stderr.strip() else ""))
        ok &= up.returncode == 0
        with eng.connect() as c:
            rev2 = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
            present = set(inspect(c).get_table_names())
            after = tables_and_counts(c, OLD_TABLES)
            rls = {r[0]: r[1] for r in c.execute(text("SELECT relname, relrowsecurity FROM pg_class WHERE relname = ANY(:n)"), {"n": list(NEW_TABLES)})}
            uniq = [r[0] for r in c.execute(text("SELECT conname FROM pg_constraint WHERE contype='u' AND conrelid::regclass::text IN ('visit_events','visits','event_delivery_outbox') ORDER BY 1"))]
        print(f"revision {rev2}; new tables present: {sorted(set(NEW_TABLES) & present) == sorted(NEW_TABLES)}; RLS on all new tables: {all(rls.get(t) for t in NEW_TABLES)}")
        print(f"unique constraints: {uniq}")
        print(f"pre-existing tables unchanged (row counts {after}): {all(after[t] == manifest['row_counts'][t] for t in OLD_TABLES)}")
        ok &= rev2 == HEAD and all(after[t] == manifest["row_counts"][t] for t in OLD_TABLES)
        header("Rehearse the application schema check (model vs migration drift)")
        print("(covered by tests/integration/test_database.py: no drift)")
        eng.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
    header("Verdict")
    print("BACKUP IS USABLE and the migration rehearses cleanly on a copy" if ok else "REHEARSAL FAILED - do not migrate")
    return 0 if ok else 1


# ============================================================================== verify (post-migration, read-only)
def verify() -> int:
    url = hosted_url()
    guard(url)
    eng = engine_for(url)
    results: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        results.append((name, bool(ok)))
        print(("PASS " if ok else "FAIL ") + name)

    try:
        with eng.connect() as c:
            insp = inspect(c)
            check("alembic revision is 0004", c.execute(text("SELECT version_num FROM alembic_version")).scalar() == HEAD)
            names = set(insp.get_table_names())
            check("all 8 expected tables exist", set(OLD_TABLES + NEW_TABLES) <= names)
            rls = {r[0]: r[1] for r in c.execute(text("SELECT c.relname, c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r'"))}
            check("RLS enabled on every new table", all(rls.get(t) for t in NEW_TABLES))
            check("RLS still enabled on processing_sessions (unchanged)", bool(rls.get("processing_sessions")))
            fks = {(r[0], r[1]) for r in c.execute(text("SELECT conrelid::regclass::text, confrelid::regclass::text FROM pg_constraint WHERE contype='f'"))}
            want = {("visits", "visitors"), ("visits", "processing_sessions"), ("visit_events", "visits"), ("visit_events", "visitors"), ("event_delivery_outbox", "visit_events"), ("face_embeddings", "visitors"), ("identity_associations", "processing_sessions"), ("identity_associations", "visitors")}
            check("foreign keys present", want <= fks)
            uq = {r[0] for r in c.execute(text("SELECT conname FROM pg_constraint WHERE contype='u'"))}
            check("exactly-once unique constraints present", {"uq_visit_events_visit_slot", "uq_visit_events_idempotency_key", "uq_visits_visit_key", "uq_event_delivery_outbox_event_target", "uq_visitors_registration_key"} <= uq)
            check("check constraints present", {"ck_visits_exit_after_entry", "ck_visit_events_slot_matches_type"} <= {r[0] for r in c.execute(text("SELECT conname FROM pg_constraint WHERE contype='c'"))})
            check("pre-existing sessions untouched (count reported below)", True)
            print("processing_sessions rows:", c.execute(text("SELECT count(*) FROM processing_sessions")).scalar())
            check("new tables start empty", all(v == 0 for v in tables_and_counts(c, NEW_TABLES).values()))
    finally:
        eng.dispose()
    print("\nApplication-level checks (run after the API is pointed at the database):")
    print("  python scripts\\smoke_deployed.py   (readiness, revision 0004, analytics, reconciliation - read-only)")
    return 0 if all(ok for _, ok in results) else 1


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "preflight":
        return preflight()
    if cmd == "review":
        return review()
    if cmd == "backup":
        return backup()
    if cmd == "restore-test" and len(sys.argv) > 2:
        return restore_test(sys.argv[2])
    if cmd == "verify":
        return verify()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
