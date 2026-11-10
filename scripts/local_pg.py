r"""Embedded local PostgreSQL (pgserver) stored under <repo>\storage\pgdata.

Development/test fallback while the hosted database credential is awaiting rotation, and for
offline work. NOT a production database.

    python scripts/local_pg.py start   -> starts (if needed) and prints the DATABASE_URL
    python scripts/local_pg.py stop    -> fast shutdown via pg_ctl
"""

import subprocess
import sys
from pathlib import Path

import pgserver

PGDATA = Path(__file__).resolve().parents[1] / "storage" / "pgdata"


def pg_ctl() -> Path:
    exe = "pg_ctl.exe" if sys.platform == "win32" else "pg_ctl"
    return Path(pgserver.__file__).resolve().parent / "pginstall" / "bin" / exe


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    if cmd == "start":
        PGDATA.parent.mkdir(parents=True, exist_ok=True)
        server = pgserver.get_server(PGDATA, cleanup_mode=None)  # keep running after we exit
        print(server.get_uri())
        return 0
    if cmd == "stop":
        if not (PGDATA / "postmaster.pid").exists():
            print("not running")
            return 0
        result = subprocess.run(  # noqa: S603
            [str(pg_ctl()), "stop", "-D", str(PGDATA), "-m", "fast", "-w"],
            capture_output=True,
            text=True,
        )
        print("stopped" if result.returncode == 0 else result.stdout + result.stderr)
        return result.returncode
    print("usage: local_pg.py start|stop", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
