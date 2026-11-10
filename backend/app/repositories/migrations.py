"""Read-only access to the Alembic version table."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session


def current_revision(session: Session) -> str | None:
    """Return the applied Alembic revision, or ``None`` if migrations were never applied."""
    try:
        row = session.execute(text("SELECT version_num FROM alembic_version")).first()
    except ProgrammingError:
        session.rollback()
        return None
    return str(row[0]) if row else None
