"""Centralized identity resolution: recognise, defer, or register - one serialized decision point.

Three identifiers, never interchangeable:
  track_id   temporary, per processing session (from the tracker)
  visitor_id persistent facial identity (UUID hex; ``visitor_number`` -> label "V00124")
  visit_id   reserved for Phase 4 (entry/exit lifecycle) - not used here

Pipeline per observation (an embedding the worker computed from one face of one track):
  index lookup -> VOTE (match / ambiguous / grey / novel) -> per-track state machine -> maybe register

Per-track states: UNRESOLVED, INSUFFICIENT_QUALITY, MATCH_PENDING, RECOGNIZED, AMBIGUOUS,
REGISTRATION_PENDING, REGISTERED, REJECTED. A single observation never decides anything on its own.

Duplicate prevention (see docs/phase-3-report.md for the full argument):
  * every match and every registration runs under ONE lock, so two tracks of the same unseen person
    cannot both be "novel" - the second one finds the first one's embeddings in the index;
  * a registration re-checks the aggregated embedding against the (freshly synced) index INSIDE the
    lock and only registers if it is still novel (similarity < novelty_threshold);
  * registration needs several consistent, good-quality, novel observations (not one frame);
  * ``registration_key = "<session>:<track>"`` is unique in the database: a retried request returns
    the existing visitor instead of creating a second one; delayed/duplicate worker messages are
    dropped by observation id;
  * on PostgreSQL a transaction-scoped advisory lock additionally serializes registrations across
    processes (a later multi-worker setup), and the index is re-synced from the database first.
A database constraint cannot prevent duplicate *people* (similarity is fuzzy) - that is what the
points above are for.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
import uuid
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import RecognitionConfig
from app.models.visitor import FaceEmbedding, IdentityAssociation, Visitor
from app.services.image_store import ImageStoreError, LocalImageStore
from app.vision.recognition.matching import EmbeddingIndex, MatchResult

logger = logging.getLogger("katomaran.identity")

# Registration advisory-lock key (arbitrary constant).
_ADVISORY_KEY = 0x4B41544F


def parse_observation(header: dict[str, Any], crop: bytes, dim: int) -> Observation:
    """Validate an untrusted worker observation. Raises ValueError on anything malformed."""
    import base64

    try:
        raw_session, raw_model = header["session_id"], header["model_version"]
        if not isinstance(raw_session, str) or not isinstance(raw_model, str):
            raise ValueError("session_id and model_version must be strings")
        session_id, model_version = raw_session, raw_model[:40]
        track_id = int(header["track_id"])
        seq = int(header["seq"])
        quality = float(header["quality"])
        timestamp_ms = int(header.get("timestamp_ms", 0))
        frame_id = int(header.get("frame_id", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("missing or invalid observation fields") from exc
    if track_id < 1 or seq < 0 or not 0.0 <= quality <= 1.0:
        raise ValueError("observation field out of range")
    reasons = tuple(str(r)[:40] for r in (header.get("reasons") or [])[:8])
    embedding: np.ndarray | None = None
    raw = header.get("embedding_b64")
    if raw is not None:
        try:
            blob = base64.b64decode(str(raw), validate=True)
        except ValueError as exc:
            raise ValueError("embedding is not valid base64") from exc
        if len(blob) != dim * 4:
            raise ValueError("embedding has the wrong size")
        embedding = np.frombuffer(blob, dtype="<f4").astype(np.float32)
    return Observation(
        session_id, track_id, seq, model_version, embedding, quality, reasons, timestamp_ms, frame_id,
        crop or None,
    )  # fmt: skip


def visitor_label(number: int) -> str:
    return f"V{number:05d}"


@dataclass(frozen=True)
class Observation:
    session_id: str
    track_id: int
    seq: int
    model_version: str
    embedding: np.ndarray | None  # None = the face was not usable (quality)
    quality: float
    reasons: tuple[str, ...] = ()
    timestamp_ms: int = 0
    frame_id: int = 0
    crop_jpeg: bytes | None = None

    @property
    def obs_id(self) -> str:
        return f"{self.session_id}:{self.track_id}:{self.seq}"


@dataclass(frozen=True)
class IdentityEvent:
    """What changed for one track. ``kind`` drives notifications; the UI never invents these."""

    kind: str  # state | registered | recognized | ambiguous | failed | conflict
    session_id: str
    track_id: int
    state: str
    visitor_id: str | None = None
    visitor_number: int | None = None
    similarity: float | None = None
    reason: str | None = None

    def to_message(self) -> dict[str, Any]:
        return {
            "type": "identity",
            "session_id": self.session_id,
            "track_id": self.track_id,
            "state": self.state,
            "event": self.kind,
            "visitor": None
            if self.visitor_id is None
            else {
                "id": self.visitor_id,
                "number": self.visitor_number,
                "label": visitor_label(self.visitor_number or 0),
            },
            "similarity": None if self.similarity is None else round(self.similarity, 3),
            "reason": self.reason,
        }


@dataclass
class _Vote:
    kind: str  # match | ambiguous | grey | novel
    visitor_id: str | None
    similarity: float


@dataclass
class _Novel:
    vector: np.ndarray
    quality: float
    timestamp_ms: int
    crop: bytes | None


@dataclass
class TrackIdentity:
    state: str = "UNRESOLVED"
    visitor_id: str | None = None
    visitor_number: int | None = None
    similarity: float | None = None
    votes: deque[_Vote] = field(default_factory=lambda: deque(maxlen=8))
    novel: list[_Novel] = field(default_factory=list)
    observations: int = 0
    conflicts: int = 0
    registered_here: bool = False
    reg_failures: int = 0
    retry_after: int = 0  # observation count before which registration is not retried
    first_ms: int = 0
    last_ms: int = 0
    last_touch: float = 0.0
    dirty: bool = True
    notified: set[str] = field(default_factory=set)


class IdentityManager:
    def __init__(
        self,
        factory: sessionmaker[Session],
        store: LocalImageStore,
        cfg: RecognitionConfig,
        model_version: str,
        dim: int = 128,
    ) -> None:
        self._factory = factory
        self._store = store
        self.cfg = cfg
        self.model_version = model_version
        self.dim = dim
        self._index = EmbeddingIndex()
        self._lock = threading.RLock()
        self._tracks: dict[tuple[str, int], TrackIdentity] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._watermark: datetime | None = None
        self._last_sync = 0.0
        self.stats: Counter[str] = Counter()
        self.lookup_ms_total = 0.0
        self.register_ms_total = 0.0
        self.loaded = False

    # ------------------------------------------------------------------ index / start-up
    def load(self) -> int:
        """Rebuild the in-memory index from the authoritative database."""
        with self._lock:
            self._index.clear()
            self._watermark = None
            added = self._sync_index(force=True)
            self.loaded = True
            return added

    def _sync_index(self, force: bool = False) -> int:
        now = time.monotonic()
        if not force and now - self._last_sync < self.cfg.index_sync_seconds:
            return 0
        self._last_sync = now
        added = 0
        with self._factory() as db:
            stmt = select(
                FaceEmbedding.id,
                FaceEmbedding.visitor_id,
                FaceEmbedding.model_version,
                FaceEmbedding.dim,
                FaceEmbedding.vector,
                FaceEmbedding.created_at,
            )
            if self._watermark is not None:
                stmt = stmt.where(
                    FaceEmbedding.created_at >= self._watermark - timedelta(seconds=5)
                )
            newest = self._watermark
            for eid, vid, version, dim, blob, created in db.execute(stmt):
                vec = np.frombuffer(blob, dtype=np.float32)
                if vec.shape[0] != dim or not np.isfinite(vec).all():
                    self.stats["corrupt_embeddings_skipped"] += 1
                    continue
                if self._index.add(version, vid, vec, eid):
                    added += 1
                aware = created if created.tzinfo else created.replace(tzinfo=UTC)
                if newest is None or aware > newest:
                    newest = aware
            self._watermark = newest
        return added

    def visitor_count(self) -> int:
        with self._lock:
            return self._index.visitor_count(self.model_version)

    # ------------------------------------------------------------------ observations
    def observe(self, obs: Observation) -> list[IdentityEvent]:
        with self._lock:
            return self._observe(obs)

    def _observe(self, obs: Observation) -> list[IdentityEvent]:
        if obs.obs_id in self._seen:
            self.stats["duplicate_observations_ignored"] += 1
            return []
        self._seen[obs.obs_id] = None
        if len(self._seen) > 8192:
            self._seen.popitem(last=False)
        self.stats["observations"] += 1
        ts = self._track(obs.session_id, obs.track_id)
        ts.last_ms = max(ts.last_ms, obs.timestamp_ms)
        if not ts.first_ms:
            ts.first_ms = obs.timestamp_ms
        events: list[IdentityEvent] = []
        if obs.model_version != self.model_version:
            # Vectors from different models are not comparable: never match them.
            self.stats["incompatible_model_observations"] += 1
            return events
        usable = obs.embedding is not None and obs.quality >= self.cfg.min_quality_score
        if not usable:
            self.stats["insufficient_quality"] += 1
            if ts.state in ("UNRESOLVED", "INSUFFICIENT_QUALITY"):
                self._set_state(
                    ts,
                    "INSUFFICIENT_QUALITY",
                    obs,
                    events,
                    "state",
                    reason=",".join(obs.reasons) or None,
                )
            return events
        assert obs.embedding is not None
        vec = np.asarray(obs.embedding, dtype=np.float32).reshape(-1)
        if vec.shape[0] != self.dim or not np.isfinite(vec).all():
            self.stats["invalid_embeddings"] += 1
            return events
        norm = float(np.linalg.norm(vec))
        if norm < 1e-6:
            self.stats["invalid_embeddings"] += 1
            return events
        vec = vec / norm
        self.stats["usable_observations"] += 1
        ts.observations += 1
        started = time.perf_counter()
        if not self.loaded:  # the database was unavailable at start-up
            try:
                self.load()
            except Exception:  # noqa: BLE001
                self.stats["index_load_failures"] += 1
                return events
        self._sync_index()
        result = self._index.search(self.model_version, vec)
        self.lookup_ms_total += (time.perf_counter() - started) * 1000
        ts.votes.append(self._vote(result))
        if ts.votes[-1].kind == "novel":
            ts.novel.append(_Novel(vec, obs.quality, obs.timestamp_ms, obs.crop_jpeg))
            ts.novel = ts.novel[-self.cfg.vote_window :]
        else:
            ts.novel.clear()  # any evidence of a known/uncertain person restarts registration
        self._evaluate(ts, obs, events)
        return events

    def _vote(self, result: MatchResult) -> _Vote:
        cfg = self.cfg
        best = result.best
        if best is None:
            return _Vote("novel", None, -1.0)
        if best.similarity >= cfg.similarity_threshold:
            if result.margin >= cfg.ambiguity_margin:
                return _Vote("match", best.visitor_id, best.similarity)
            return _Vote("ambiguous", best.visitor_id, best.similarity)
        if best.similarity >= cfg.novelty_threshold:
            return _Vote("grey", best.visitor_id, best.similarity)
        return _Vote("novel", None, best.similarity)

    def _track(self, session_id: str, track_id: int) -> TrackIdentity:
        key = (session_id, track_id)
        ts = self._tracks.get(key)
        if ts is None:
            ts = TrackIdentity(votes=deque(maxlen=self.cfg.vote_window))
            self._tracks[key] = ts
        return ts

    # ------------------------------------------------------------------ per-track state machine
    def _evaluate(self, ts: TrackIdentity, obs: Observation, events: list[IdentityEvent]) -> None:
        cfg = self.cfg
        matches = Counter(v.visitor_id for v in ts.votes if v.kind == "match")
        if ts.state in ("RECOGNIZED", "REGISTERED") and ts.visitor_id:
            own = matches.get(ts.visitor_id, 0)
            others = {k: c for k, c in matches.items() if k != ts.visitor_id}
            if others:
                latest = ts.votes[-1]
                if latest.kind == "match" and latest.visitor_id != ts.visitor_id:
                    ts.conflicts += 1
                    ts.dirty = True
                    self.stats["identity_conflicts"] += 1
                    if "conflict" not in ts.notified:
                        ts.notified.add("conflict")
                        events.append(
                            self._event("conflict", obs, ts, reason="conflicting_observation")
                        )
                challenger, count = max(others.items(), key=lambda kv: kv[1])
                if (
                    challenger is not None
                    and count >= cfg.recognize_votes
                    and count >= own + cfg.switch_hysteresis
                ):
                    self._assign(ts, obs, challenger, events, reason="reassigned_after_conflict")
            else:
                self._touch_visitor(ts.visitor_id, ts, 0)  # throttled "last seen" bookkeeping
            return
        if matches:
            visitor, count = matches.most_common(1)[0]
            if count >= cfg.recognize_votes and visitor is not None:
                self._assign(ts, obs, visitor, events, reason=None)
                return
        ambiguous = sum(1 for v in ts.votes if v.kind == "ambiguous")
        if ambiguous >= cfg.recognize_votes and not matches:
            self._set_state(
                ts, "AMBIGUOUS", obs, events, "ambiguous", reason="similar_to_several_visitors"
            )
            return
        if any(v.kind in ("match", "grey", "ambiguous") for v in ts.votes):
            if ts.state not in ("AMBIGUOUS",):
                self._set_state(ts, "MATCH_PENDING", obs, events, "state")
            return
        # only novel votes so far
        if ts.state in ("INSUFFICIENT_QUALITY", "UNRESOLVED", "MATCH_PENDING"):
            self._set_state(ts, "UNRESOLVED", obs, events, "state")
        self._maybe_register(ts, obs, events)

    def _assign(
        self,
        ts: TrackIdentity,
        obs: Observation,
        visitor_id: str,
        events: list[IdentityEvent],
        reason: str | None,
    ) -> None:
        number = self._visitor_number(visitor_id)
        # One person cannot be on two tracks of the same frame sequence: tracker switch or look-alike -
        # either way, do not propagate the identity to the second track.
        for (sid, tid), other in self._tracks.items():
            if (
                sid == obs.session_id
                and tid != obs.track_id
                and other.visitor_id == visitor_id
                and other.state in ("RECOGNIZED", "REGISTERED")
                and abs(other.last_ms - obs.timestamp_ms) <= 1500
            ):
                self.stats["same_visitor_on_two_tracks"] += 1
                self._set_state(
                    ts, "AMBIGUOUS", obs, events, "ambiguous", reason="same_visitor_on_two_tracks"
                )
                return
        sims = [v.similarity for v in ts.votes if v.kind == "match" and v.visitor_id == visitor_id]
        first = ts.visitor_id != visitor_id or "recognized" not in ts.notified
        ts.visitor_id, ts.visitor_number = visitor_id, number
        ts.similarity = max(sims) if sims else None
        ts.notified.add("recognized")
        self.stats["tracks_recognized"] += 1
        self._set_state(
            ts, "RECOGNIZED", obs, events, "recognized" if first else "state", reason=reason
        )
        self._touch_visitor(visitor_id, ts, count=1)

    # ------------------------------------------------------------------ registration
    def _maybe_register(
        self, ts: TrackIdentity, obs: Observation, events: list[IdentityEvent]
    ) -> None:
        cfg = self.cfg
        if (
            ts.state in ("REGISTERED", "RECOGNIZED", "REJECTED")
            or len(ts.novel) < cfg.register_min_observations
        ):
            return
        if ts.observations < ts.retry_after:
            return  # backing off after a failed registration (e.g. database outage)
        # all of the recent votes must be novel (a single match/grey vote already cleared the buffer)
        recent = list(ts.votes)[-cfg.register_min_observations :]
        if len(recent) < cfg.register_min_observations or any(v.kind != "novel" for v in recent):
            return
        span = (ts.novel[-1].timestamp_ms - ts.novel[0].timestamp_ms) / 1000.0
        if span < cfg.register_min_seconds:
            return
        quality = float(np.mean([n.quality for n in ts.novel]))
        if quality < cfg.register_min_quality:
            return
        consistency = self._self_consistency([n.vector for n in ts.novel])
        if consistency < cfg.register_self_consistency:
            # The "same track" shows embeddings that disagree with each other: probably two people
            # (a tracker swap) or unusable faces. Do not turn that into a permanent identity.
            self.stats["registrations_rejected_inconsistent"] += 1
            self._set_state(
                ts, "REJECTED", obs, events, "ambiguous", reason="inconsistent_observations"
            )
            ts.novel.clear()
            return
        self._set_state(ts, "REGISTRATION_PENDING", obs, events, "state")
        self._register(ts, obs, events)

    @staticmethod
    def _self_consistency(vectors: list[np.ndarray]) -> float:
        if len(vectors) < 2:
            return 1.0
        m = np.vstack(vectors)
        sims = m @ m.T
        n = len(vectors)
        return float((sims.sum() - n) / (n * (n - 1)))

    def _register(self, ts: TrackIdentity, obs: Observation, events: list[IdentityEvent]) -> None:
        cfg = self.cfg
        started = time.perf_counter()
        # Fresh view of the registry (another process may have registered this person a moment ago).
        self._sync_index(force=True)
        aggregate = np.mean([n.vector for n in ts.novel], axis=0)
        aggregate = aggregate / max(float(np.linalg.norm(aggregate)), 1e-9)
        result = self._index.search(self.model_version, aggregate)
        if result.best is not None and result.best.similarity >= cfg.novelty_threshold:
            # Not novel after all: never register, fall back to matching.
            self.stats["registrations_prevented_by_recheck"] += 1
            ts.novel.clear()
            if (
                result.best.similarity >= cfg.similarity_threshold
                and result.margin >= cfg.ambiguity_margin
            ):
                self._assign(
                    ts, obs, result.best.visitor_id, events, reason="matched_at_registration_check"
                )
            else:
                self._set_state(ts, "MATCH_PENDING", obs, events, "state")
            return
        ranked = sorted(ts.novel, key=lambda n: n.quality, reverse=True)
        chosen: list[_Novel] = []
        for cand in ranked:
            if all(float(cand.vector @ c.vector) < 0.98 for c in chosen):
                chosen.append(cand)
            if len(chosen) >= cfg.max_embeddings_per_visitor:
                break
        crop_src = next((n for n in ranked if n.crop), None)
        key = f"{obs.session_id}:{obs.track_id}"
        visitor_id = uuid.uuid4().hex
        image_path: str | None = None
        image_sha: str | None = None
        try:
            if crop_src is not None and crop_src.crop is not None:
                try:
                    image_path, image_sha = self._store.save(visitor_id, crop_src.crop)
                except ImageStoreError as exc:
                    logger.warning("Representative image rejected: %s", exc)
                    image_path = None
            row = self._insert_visitor(
                key, visitor_id, obs, chosen, image_path, image_sha, crop_src
            )
        except Exception as exc:  # noqa: BLE001 - registration must never take the API down
            if image_path:
                self._store.delete_visitor(visitor_id)
            ts.reg_failures += 1
            ts.retry_after = ts.observations + cfg.register_min_observations
            self.stats["registration_failures"] += 1
            logger.error(
                "Registration failed (%s)", type(exc).__name__, extra={"track_id": obs.track_id}
            )
            state = "REJECTED" if ts.reg_failures >= 5 else "UNRESOLVED"
            self._set_state(ts, state, obs, events, "failed", reason="registration_failed")
            return
        if row.id != visitor_id:  # idempotent retry: a visitor already exists for this key
            if image_path:
                self._store.delete_visitor(visitor_id)
            self.stats["registration_retries_deduplicated"] += 1
        for emb_id, vec in row.embeddings:
            self._index.add(self.model_version, row.id, vec, emb_id)
        ts.visitor_id, ts.visitor_number = row.id, row.visitor_number
        ts.similarity = None
        ts.registered_here = True
        ts.novel.clear()
        ts.notified.add("registered")
        self.stats["registrations"] += 1
        self.register_ms_total += (time.perf_counter() - started) * 1000
        self._set_state(ts, "REGISTERED", obs, events, "registered")

    def _insert_visitor(
        self,
        key: str,
        visitor_id: str,
        obs: Observation,
        chosen: list[_Novel],
        image_path: str | None,
        image_sha: str | None,
        crop_src: _Novel | None,
    ) -> _RegisteredRow:
        import hashlib

        with self._factory() as db:
            try:
                if db.bind is not None and db.bind.dialect.name == "postgresql":
                    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADVISORY_KEY})
                existing = db.execute(
                    select(Visitor).where(Visitor.registration_key == key)
                ).scalar_one_or_none()
                if existing is not None:
                    return _RegisteredRow(existing.id, existing.visitor_number, [])
                number = (
                    int(
                        db.execute(
                            select(func.coalesce(func.max(Visitor.visitor_number), 0))
                        ).scalar_one()
                    )
                    + 1
                )
                now = datetime.now(UTC)
                visitor = Visitor(
                    id=visitor_id,
                    visitor_number=number,
                    registration_key=key,
                    status="REGISTERED",
                    model_version=self.model_version,
                    first_session_id=obs.session_id,
                    first_seen_at=now,
                    last_seen_at=now,
                    recognition_count=0,
                    image_path=image_path,
                    image_sha256=image_sha,
                    image_quality=None if crop_src is None else round(crop_src.quality, 3),
                )
                db.add(visitor)
                db.flush()
                embeddings: list[tuple[str, np.ndarray]] = []
                for n in chosen:
                    blob = n.vector.astype(np.float32).tobytes()
                    eid = uuid.uuid4().hex
                    db.add(
                        FaceEmbedding(
                            id=eid,
                            visitor_id=visitor_id,
                            model_version=self.model_version,
                            dim=self.dim,
                            vector=blob,
                            vector_sha256=hashlib.sha256(blob).hexdigest(),
                            quality_score=round(n.quality, 3),
                        )
                    )
                    embeddings.append((eid, n.vector))
                db.commit()
                return _RegisteredRow(visitor_id, number, embeddings)
            except IntegrityError:
                db.rollback()
                # Lost a race on the unique key: the other transaction's visitor is the answer.
                existing = db.execute(
                    select(Visitor).where(Visitor.registration_key == key)
                ).scalar_one_or_none()
                if existing is None:
                    raise
                return _RegisteredRow(existing.id, existing.visitor_number, [])
            except Exception:
                db.rollback()
                raise

    # ------------------------------------------------------------------ helpers
    def _visitor_number(self, visitor_id: str) -> int:
        with self._factory() as db:
            return int(
                db.execute(
                    select(Visitor.visitor_number).where(Visitor.id == visitor_id)
                ).scalar_one_or_none()
                or 0
            )

    def _touch_visitor(self, visitor_id: str, ts: TrackIdentity, count: int) -> None:
        now = time.monotonic()
        if now - ts.last_touch < 30 and count == 0:
            return
        ts.last_touch = now
        try:
            with self._factory() as db:
                row = db.get(Visitor, visitor_id)
                if row is not None:
                    row.last_seen_at = datetime.now(UTC)
                    row.recognition_count = (row.recognition_count or 0) + count
                    db.commit()
        except Exception:  # noqa: BLE001 - a bookkeeping failure must not block recognition
            logger.warning("Could not update last_seen for a visitor")

    def _set_state(
        self,
        ts: TrackIdentity,
        state: str,
        obs: Observation,
        events: list[IdentityEvent],
        kind: str,
        reason: str | None = None,
    ) -> None:
        changed = state != ts.state
        ts.state = state
        if changed or kind not in ("state",):
            ts.dirty = True
        if kind == "state" and not changed:
            return
        if kind == "ambiguous":
            if "ambiguous" in ts.notified and not changed:
                return
            ts.notified.add("ambiguous")
            self.stats["ambiguous_tracks"] += 1
        events.append(self._event(kind, obs, ts, reason=reason))

    def _event(
        self, kind: str, obs: Observation, ts: TrackIdentity, reason: str | None = None
    ) -> IdentityEvent:
        return IdentityEvent(
            kind=kind,
            session_id=obs.session_id,
            track_id=obs.track_id,
            state=ts.state,
            visitor_id=ts.visitor_id if ts.state in ("RECOGNIZED", "REGISTERED") else None,
            visitor_number=ts.visitor_number if ts.state in ("RECOGNIZED", "REGISTERED") else None,
            similarity=ts.similarity,
            reason=reason,
        )

    # ------------------------------------------------------------------ session end / persistence
    def snapshot(self, session_id: str) -> list[IdentityEvent]:
        """Current identity of every track of a session (sent to a browser that connects later)."""
        with self._lock:
            out = []
            for (sid, tid), ts in self._tracks.items():
                if sid == session_id and ts.state != "UNRESOLVED":
                    out.append(
                        IdentityEvent(
                            "state",
                            sid,
                            tid,
                            ts.state,
                            ts.visitor_id if ts.state in ("RECOGNIZED", "REGISTERED") else None,
                            ts.visitor_number if ts.state in ("RECOGNIZED", "REGISTERED") else None,
                            ts.similarity,
                        )
                    )
            return out

    def end_session(self, session_id: str) -> None:
        """Persist the final identity of every track of a session and release its memory."""
        with self._lock:
            keys = [k for k in self._tracks if k[0] == session_id]
            try:
                with self._factory() as db:
                    for sid, tid in keys:
                        ts = self._tracks[(sid, tid)]
                        if ts.observations == 0 and ts.state == "UNRESOLVED":
                            continue
                        self._upsert_association(db, sid, tid, ts)
                    db.commit()
            except Exception:  # noqa: BLE001
                logger.warning("Could not persist the identity associations of a session")
            for k in keys:
                self._tracks.pop(k, None)

    @staticmethod
    def _upsert_association(db: Session, session_id: str, track_id: int, ts: TrackIdentity) -> None:
        row = db.execute(
            select(IdentityAssociation).where(
                IdentityAssociation.session_id == session_id,
                IdentityAssociation.track_id == track_id,
            )
        ).scalar_one_or_none()
        now = datetime.now(UTC)
        visitor = ts.visitor_id if ts.state in ("RECOGNIZED", "REGISTERED") else None
        if row is None:
            row = IdentityAssociation(
                id=uuid.uuid4().hex,
                session_id=session_id,
                track_id=track_id,
                first_seen_at=now,
                state=ts.state,
            )
            db.add(row)
        row.state = ts.state
        row.visitor_id = visitor
        row.best_similarity = ts.similarity
        row.observations = ts.observations
        row.conflicts = ts.conflicts
        row.registered_here = 1 if ts.registered_here else 0
        row.last_seen_at = now

    # ------------------------------------------------------------------ maintenance
    def delete_visitor(self, visitor_id: str) -> bool:
        """Erase a visitor: embeddings, associations, images and the index entry (right to erasure)."""
        with self._lock:
            with self._factory() as db:
                row = db.get(Visitor, visitor_id)
                if row is None:
                    return False
                db.delete(row)
                db.commit()
            self._index.remove_visitor(visitor_id)
            with contextlib.suppress(ImageStoreError):
                self._store.delete_visitor(visitor_id)
            for ts in self._tracks.values():
                if ts.visitor_id == visitor_id:
                    ts.visitor_id, ts.visitor_number, ts.state = None, None, "UNRESOLVED"
            return True

    def purge_expired(self, now: datetime | None = None) -> int:
        """Delete visitors not seen for ``retention_days`` (0 = retention disabled)."""
        days = self.cfg.retention_days
        if days <= 0:
            return 0
        cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
        with self._factory() as db:
            ids = [
                i for (i,) in db.execute(select(Visitor.id).where(Visitor.last_seen_at < cutoff))
            ]
        return sum(1 for i in ids if self.delete_visitor(i))

    def reconcile(self, grace_seconds: float = 300) -> dict[str, int]:
        """Repair inconsistencies left by an interrupted write.

        * image files that no visitor references (crash between file write and DB commit) are deleted
          once older than ``grace_seconds``;
        * visitors whose image file is missing keep their identity but lose the image reference;
        * embeddings are never removed here.
        """
        removed = cleared = 0
        with self._lock, self._factory() as db:
            referenced = {p for (p,) in db.execute(select(Visitor.image_path)) if p}
            for rel, mtime in self._store.all_files():
                if rel not in referenced and self._store.age_seconds(mtime) > grace_seconds:
                    self._store.delete(rel)
                    removed += 1
            for vid, path in db.execute(
                select(Visitor.id, Visitor.image_path).where(Visitor.image_path.is_not(None))
            ).all():
                if path and not self._store.exists(path):
                    row = db.get(Visitor, vid)
                    if row is not None:
                        row.image_path = None
                        row.image_sha256 = None
                        cleared += 1
            db.commit()
        return {"orphan_images_removed": removed, "missing_image_references_cleared": cleared}

    def runtime_stats(self) -> dict[str, Any]:
        with self._lock:
            n = max(1, self.stats["usable_observations"])
            r = max(1, self.stats["registrations"])
            return {
                "model_version": self.model_version,
                "indexed_visitors": self._index.visitor_count(self.model_version),
                "indexed_embeddings": len(self._index),
                "active_tracks": len(self._tracks),
                **{k: int(v) for k, v in self.stats.items()},
                "avg_lookup_ms": round(self.lookup_ms_total / n, 4),
                "avg_registration_ms": round(self.register_ms_total / r, 2),
            }


@dataclass
class _RegisteredRow:
    id: str
    visitor_number: int
    embeddings: list[tuple[str, np.ndarray]]
