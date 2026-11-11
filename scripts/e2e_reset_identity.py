r"""Empty the recognition tables of the E2E database (never the hosted one).

Run by frontend/e2e/prepare.ts before the real-stack tests so every run starts without visitors.
Refuses to touch anything that is not the local embedded PostgreSQL (127.0.0.1 / localhost).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.settings import get_settings  # noqa: E402
from app.db.session import create_db_engine  # noqa: E402


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    host = make_url(url.replace("postgresql://", "postgresql+psycopg://", 1)).host
    if host not in ("127.0.0.1", "localhost"):
        print("refusing to reset a non-local database", file=sys.stderr)
        return 2
    engine = create_db_engine(get_settings())
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE identity_associations, face_embeddings, visitors CASCADE"))
    engine.dispose()
    print("identity tables emptied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
