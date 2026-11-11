"""visits, visit_events, event_delivery_outbox (Phase 4 visit lifecycle)

Purely additive: no existing table or row is touched. Downgrade drops only the three new tables (and
with them every recorded visit/event - destructive by nature; never run against hosted data without
approval). Requires revision 0003 (visitors).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-11
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")


def _in(*values: str) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.create_table(
        "visits",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("visit_key", sa.String(length=100), nullable=False),
        sa.Column("visitor_id", sa.String(length=32), nullable=True),
        sa.Column("visitor_number", sa.Integer(), nullable=True),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("source_label", sa.String(length=80), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_video_ms", sa.Integer(), nullable=False),
        sa.Column("last_observed_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_video_ms", sa.Integer(), nullable=False),
        sa.Column("exit_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_video_ms", sa.Integer(), nullable=True),
        sa.Column("exit_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_basis", sa.String(length=24), nullable=True),
        sa.Column("closure_reason", sa.String(length=40), nullable=True),
        sa.Column("track_segments", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.CheckConstraint(
            f"state IN ({_in('ACTIVE', 'TEMPORARILY_MISSING', 'EXIT_PENDING', 'CLOSED', 'INTERRUPTED')})",
            name=op.f("ck_visits_state_valid"),
        ),
        sa.CheckConstraint(
            f"exit_basis IS NULL OR exit_basis IN ({_in('absence', 'observation_ended', 'interrupted')})",
            name=op.f("ck_visits_exit_basis_valid"),
        ),
        sa.CheckConstraint("exit_time IS NULL OR exit_time >= entry_time", name=op.f("ck_visits_exit_after_entry")),
        sa.ForeignKeyConstraint(["visitor_id"], ["visitors.id"], name=op.f("fk_visits_visitor_id_visitors"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["processing_sessions.id"], name=op.f("fk_visits_session_id_processing_sessions"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_visits")),
        sa.UniqueConstraint("visit_key", name=op.f("uq_visits_visit_key")),
    )
    op.create_index("ix_visits_visitor_id", "visits", ["visitor_id"])
    op.create_index("ix_visits_session_id", "visits", ["session_id"])
    op.create_index("ix_visits_entry_time", "visits", ["entry_time"])
    op.create_index("ix_visits_state", "visits", ["state"])

    op.create_table(
        "visit_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("visit_id", sa.String(length=32), nullable=False),
        sa.Column("visitor_id", sa.String(length=32), nullable=True),
        sa.Column("visitor_number", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=12), nullable=False),
        sa.Column("slot", sa.String(length=12), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("video_ms", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("image_status", sa.String(length=12), nullable=False),
        sa.Column("image_path", sa.String(length=120), nullable=True),
        sa.Column("image_sha256", sa.String(length=64), nullable=True),
        sa.Column("image_kind", sa.String(length=24), nullable=True),
        sa.Column("image_video_ms", sa.Integer(), nullable=True),
        sa.Column("persistence_status", sa.String(length=12), nullable=False),
        sa.CheckConstraint(f"event_type IN ({_in('entry', 'exit', 'interrupted')})", name=op.f("ck_visit_events_type_valid")),
        sa.CheckConstraint("slot IN ('entry', 'terminal')", name=op.f("ck_visit_events_slot_valid")),
        sa.CheckConstraint("(slot = 'entry') = (event_type = 'entry')", name=op.f("ck_visit_events_slot_matches_type")),
        sa.CheckConstraint(
            f"image_status IN ({_in('PENDING', 'STORED', 'UNAVAILABLE', 'MISSING', 'ERASED')})",
            name=op.f("ck_visit_events_image_status_valid"),
        ),
        sa.CheckConstraint(
            f"persistence_status IN ({_in('PENDING', 'DELIVERED', 'DEAD')})",
            name=op.f("ck_visit_events_persistence_status_valid"),
        ),
        sa.ForeignKeyConstraint(["visit_id"], ["visits.id"], name=op.f("fk_visit_events_visit_id_visits"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["visitor_id"], ["visitors.id"], name=op.f("fk_visit_events_visitor_id_visitors"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_visit_events")),
        sa.UniqueConstraint("visit_id", "slot", name=op.f("uq_visit_events_visit_slot")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_visit_events_idempotency_key")),
    )
    op.create_index("ix_visit_events_visitor_id", "visit_events", ["visitor_id"])
    op.create_index("ix_visit_events_event_time", "visit_events", ["event_time"])
    op.create_index("ix_visit_events_persistence_status", "visit_events", ["persistence_status"])

    op.create_table(
        "event_delivery_outbox",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("event_id", sa.String(length=32), nullable=False),
        sa.Column("target", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=200), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.CheckConstraint(
            f"status IN ({_in('PENDING', 'DELIVERED', 'DEAD')})", name=op.f("ck_event_delivery_outbox_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["visit_events.id"], name=op.f("fk_event_delivery_outbox_event_id_visit_events"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_delivery_outbox")),
        sa.UniqueConstraint("event_id", "target", name=op.f("uq_event_delivery_outbox_event_target")),
    )
    op.create_index("ix_event_delivery_outbox_status_retry", "event_delivery_outbox", ["status", "next_retry_at"])
    # Same posture as the Phase 3 biometric tables: no access through Supabase's public API.
    for table in ("visits", "visit_events", "event_delivery_outbox"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("event_delivery_outbox")
    op.drop_table("visit_events")
    op.drop_table("visits")
