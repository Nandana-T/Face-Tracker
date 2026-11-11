"""visitors, face_embeddings, identity_associations (Phase 3 recognition)

Purely additive: no existing table or row is touched, so applying it to a database that already
holds Phase 0-2 data cannot lose anything. Downgrade drops only the three new tables (and with them
any registered identities - destructive by nature; never run against hosted data without approval).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-11
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATES = (
    "'UNRESOLVED', 'INSUFFICIENT_QUALITY', 'MATCH_PENDING', 'RECOGNIZED', 'AMBIGUOUS', "
    "'REGISTRATION_PENDING', 'REGISTERED', 'REJECTED'"
)


def upgrade() -> None:
    op.create_table(
        "visitors",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("visitor_number", sa.Integer(), nullable=False),
        sa.Column("registration_key", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("model_version", sa.String(length=40), nullable=False),
        sa.Column("first_session_id", sa.String(length=32), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("recognition_count", sa.Integer(), nullable=False),
        sa.Column("image_path", sa.String(length=200), nullable=True),
        sa.Column("image_sha256", sa.String(length=64), nullable=True),
        sa.Column("image_quality", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('REGISTERED')", name=op.f("ck_visitors_status_valid")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_visitors")),
        sa.UniqueConstraint("visitor_number", name=op.f("uq_visitors_visitor_number")),
        sa.UniqueConstraint("registration_key", name=op.f("uq_visitors_registration_key")),
    )
    op.create_index("ix_visitors_first_seen_at", "visitors", ["first_seen_at"])
    op.create_index("ix_visitors_last_seen_at", "visitors", ["last_seen_at"])

    op.create_table(
        "face_embeddings",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("visitor_id", sa.String(length=32), nullable=False),
        sa.Column("model_version", sa.String(length=40), nullable=False),
        sa.Column("dim", sa.SmallInteger(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.Column("vector_sha256", sa.String(length=64), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["visitor_id"], ["visitors.id"], name=op.f("fk_face_embeddings_visitor_id_visitors"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_face_embeddings")),
        sa.UniqueConstraint("visitor_id", "vector_sha256", name=op.f("uq_face_embeddings_visitor_vector")),
    )
    op.create_index("ix_face_embeddings_visitor_id", "face_embeddings", ["visitor_id"])
    op.create_index("ix_face_embeddings_model_version", "face_embeddings", ["model_version"])

    op.create_table(
        "identity_associations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=False),
        sa.Column("visitor_id", sa.String(length=32), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("best_similarity", sa.Float(), nullable=True),
        sa.Column("observations", sa.Integer(), nullable=False),
        sa.Column("conflicts", sa.Integer(), nullable=False),
        sa.Column("registered_here", sa.SmallInteger(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"state IN ({_STATES})", name=op.f("ck_identity_associations_state_valid")),
        sa.ForeignKeyConstraint(
            ["session_id"], ["processing_sessions.id"],
            name=op.f("fk_identity_associations_session_id_processing_sessions"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["visitor_id"], ["visitors.id"],
            name=op.f("fk_identity_associations_visitor_id_visitors"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_identity_associations")),
        sa.UniqueConstraint("session_id", "track_id", name=op.f("uq_identity_associations_session_track")),
    )
    op.create_index("ix_identity_associations_visitor_id", "identity_associations", ["visitor_id"])
    # Biometric tables: deny direct access through Supabase's public API (the backend connects as owner).
    for table in ("visitors", "face_embeddings", "identity_associations"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("identity_associations")
    op.drop_table("face_embeddings")
    op.drop_table("visitors")
