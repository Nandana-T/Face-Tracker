"""create app_metadata

Revision ID: 0001
Revises:
Create Date: 2026-11-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_metadata",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_metadata")),
    )
    # Supabase exposes the public schema through its Data API (PostgREST). The backend connects
    # as the table owner (which bypasses RLS); enabling RLS with no policies denies anon/authenticated
    # roles by default. Harmless on plain PostgreSQL.
    op.execute("ALTER TABLE app_metadata ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("app_metadata")
