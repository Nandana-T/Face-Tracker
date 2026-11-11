"""Persistent facial identities (Phase 3). BIOMETRIC DATA - see docs/privacy-and-security.md.

Identifiers are never interchangeable:
* ``Visitor.id``            - persistent identity (UUID hex). ``visitor_number`` is its short public label (V00124).
* ``track_id``              - temporary id of a face trajectory inside ONE processing session
                              (stored only in ``IdentityAssociation`` together with ``session_id``).
* ``visit_id``              - reserved for the future entry/exit lifecycle (Phase 4); not present here.

Raw embeddings live in ``face_embeddings.vector`` (float32 little-endian bytes) and are never
returned by any API or written to logs. A unique constraint cannot prevent duplicate *people*
(similarity is fuzzy); duplicate prevention is the IdentityManager's serialized match-then-register
transaction plus the idempotent ``registration_key``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

IDENTITY_STATES = (
    "UNRESOLVED",
    "INSUFFICIENT_QUALITY",
    "MATCH_PENDING",
    "RECOGNIZED",
    "AMBIGUOUS",
    "REGISTRATION_PENDING",
    "REGISTERED",
    "REJECTED",
)
_STATE_LIST = ", ".join(f"'{s}'" for s in IDENTITY_STATES)


class Visitor(Base):
    __tablename__ = "visitors"
    __table_args__ = (
        CheckConstraint("status IN ('REGISTERED')", name="status_valid"),
        UniqueConstraint("visitor_number", name="uq_visitors_visitor_number"),
        UniqueConstraint("registration_key", name="uq_visitors_registration_key"),
        Index("ix_visitors_first_seen_at", "first_seen_at"),
        Index("ix_visitors_last_seen_at", "last_seen_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    visitor_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Idempotency key of the registration decision ("<session_id>:<track_id>"): a retried request
    # finds the existing row instead of creating a second visitor.
    registration_key: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="REGISTERED")
    model_version: Mapped[str] = mapped_column(String(40), nullable=False)
    first_session_id: Mapped[str | None] = mapped_column(String(32))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    recognition_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Relative path inside the face-image store (never served statically); NULL = no image.
    image_path: Mapped[str | None] = mapped_column(String(200))
    image_sha256: Mapped[str | None] = mapped_column(String(64))
    image_quality: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class FaceEmbedding(Base):
    __tablename__ = "face_embeddings"
    __table_args__ = (
        UniqueConstraint("visitor_id", "vector_sha256", name="uq_face_embeddings_visitor_vector"),
        Index("ix_face_embeddings_visitor_id", "visitor_id"),
        Index("ix_face_embeddings_model_version", "model_version"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    visitor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("visitors.id", ondelete="CASCADE"), nullable=False
    )
    # Embeddings from different models are never comparable; every row records its model.
    model_version: Mapped[str] = mapped_column(String(40), nullable=False)
    dim: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    vector_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IdentityAssociation(Base):
    """What recognition concluded about one temporary track of one session (an audit trail,
    not a visit and not an entry/exit event)."""

    __tablename__ = "identity_associations"
    __table_args__ = (
        CheckConstraint(f"state IN ({_STATE_LIST})", name="state_valid"),
        UniqueConstraint("session_id", "track_id", name="uq_identity_associations_session_track"),
        Index("ix_identity_associations_visitor_id", "visitor_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("processing_sessions.id", ondelete="CASCADE"), nullable=False
    )
    track_id: Mapped[int] = mapped_column(Integer, nullable=False)
    visitor_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("visitors.id", ondelete="CASCADE")
    )
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    best_similarity: Mapped[float | None] = mapped_column(Float)
    observations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    conflicts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    registered_here: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
