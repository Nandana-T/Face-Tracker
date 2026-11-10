"""create processing_sessions

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "processing_sessions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("source_label", sa.String(length=80), nullable=False),
        sa.Column("source_type", sa.String(length=8), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("active_slot", sa.SmallInteger(), nullable=True),
        sa.Column("stop_reason", sa.String(length=40), nullable=True),
        sa.Column("error_code", sa.String(length=60), nullable=True),
        sa.Column("error_message", sa.String(length=300), nullable=True),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("frames_read", sa.Integer(), nullable=False),
        sa.Column("frames_processed", sa.Integer(), nullable=False),
        sa.Column("frames_skipped", sa.Integer(), nullable=False),
        sa.Column("frames_dropped", sa.Integer(), nullable=False),
        sa.Column("corrupt_frames", sa.Integer(), nullable=False),
        sa.Column("detections_total", sa.Integer(), nullable=False),
        sa.Column("max_faces_in_frame", sa.Integer(), nullable=False),
        sa.Column("avg_inference_ms", sa.Float(), nullable=True),
        sa.Column("avg_processing_fps", sa.Float(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("settings_snapshot", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "state IN ('STARTING', 'RUNNING', 'STOPPING', 'STOPPED', 'COMPLETED', 'ERROR')",
            name=op.f("ck_processing_sessions_state_valid"),
        ),
        sa.CheckConstraint(
            "active_slot IS NULL OR active_slot = 1",
            name=op.f("ck_processing_sessions_active_slot_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_sessions")),
        sa.UniqueConstraint("active_slot", name=op.f("uq_processing_sessions_active_slot")),
    )
    # Query pattern: "most recent sessions" (history list / latest status).
    op.create_index("ix_processing_sessions_created_at", "processing_sessions", ["created_at"])
    op.execute("ALTER TABLE processing_sessions ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index("ix_processing_sessions_created_at", table_name="processing_sessions")
    op.drop_table("processing_sessions")
