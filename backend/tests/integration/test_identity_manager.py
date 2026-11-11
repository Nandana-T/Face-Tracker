"""IdentityManager against a real PostgreSQL database (embedded, isolated schema).

Embeddings here are synthetic unit vectors with controlled cosine similarity, so the *decision
logic* (thresholds, voting, registration, duplicate prevention, concurrency, persistence) is tested
deterministically. Whether the real model separates real faces is tested elsewhere (model tests and
the LFW evaluation) - never inferred from these.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import cv2
import numpy as np
import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import RecognitionConfig
from app.db.base import Base
from app.models import FaceEmbedding, IdentityAssociation, ProcessingSession, Visitor
from app.services.identity_manager import IdentityEvent, IdentityManager, Observation
from app.services.image_store import LocalImageStore

pytestmark = pytest.mark.integration
DIM = 128
MODEL = "test-model-v1"


# ---------------------------------------------------------------------------------- helpers
_Q, _ = np.linalg.qr(np.random.default_rng(0).normal(size=(DIM, DIM)))


def unit(seed: int) -> np.ndarray:
    """Persona ``seed``: persons are exactly orthogonal (cosine 0), so tests control similarity."""
    out: np.ndarray = np.asarray(_Q[:, seed % DIM], dtype=np.float32)
    return out


def with_cosine(base: np.ndarray, cos: float, seed: int = 99) -> np.ndarray:
    """A unit vector whose cosine similarity with ``base`` is exactly ``cos``."""
    other = unit(seed)
    ortho = other - (other @ base) * base
    ortho /= np.linalg.norm(ortho)
    out: np.ndarray = (cos * base + np.sqrt(1 - cos**2) * ortho).astype(np.float32)
    return out


def jpeg() -> bytes:
    img = np.full((96, 96, 3), 127, dtype=np.uint8)
    cv2.circle(img, (48, 48), 20, (200, 180, 160), -1)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return bytes(buf.tobytes())


class Rig:
    def __init__(self, engine: Engine, tmp_path: Path, **cfg: object) -> None:
        self.engine = engine
        self.factory = sessionmaker(engine, expire_on_commit=False)
        self.store = LocalImageStore(tmp_path / "faces")
        self.cfg = RecognitionConfig(**cfg)
        self.mgr = self.new_manager()
        self.seq: dict[tuple[str, int], int] = {}
        self.clock = 0

    def new_manager(self) -> IdentityManager:
        mgr = IdentityManager(self.factory, self.store, self.cfg, MODEL, DIM)
        mgr.load()
        return mgr

    def session(self, sid: str) -> str:
        with self.factory() as db:
            if db.get(ProcessingSession, sid) is None:
                db.add(
                    ProcessingSession(
                        id=sid,
                        source_id="x",
                        source_label="X",
                        source_type="file",
                        state="COMPLETED",
                        frames_read=0,
                        frames_processed=0,
                        frames_skipped=0,
                        frames_dropped=0,
                        corrupt_frames=0,
                        detections_total=0,
                        max_faces_in_frame=0,
                    )
                )
                db.commit()
        return sid

    def see(
        self,
        sid: str,
        track: int,
        vec: np.ndarray | None,
        quality: float = 0.8,
        step_ms: int = 300,
        model: str = MODEL,
        mgr: IdentityManager | None = None,
        crop: bool = True,
    ) -> list[IdentityEvent]:
        n = self.seq.get((sid, track), 0)
        self.seq[(sid, track)] = n + 1
        self.clock += step_ms
        obs = Observation(
            session_id=sid,
            track_id=track,
            seq=n,
            model_version=model,
            embedding=vec,
            quality=quality,
            reasons=() if vec is not None else ("blurry",),
            timestamp_ms=self.clock,
            frame_id=n,
            crop_jpeg=jpeg() if crop and vec is not None else None,
        )
        return (mgr or self.mgr).observe(obs)

    def feed(
        self, sid: str, track: int, base: np.ndarray, n: int, noise: float = 0.9, **kw: object
    ) -> list[IdentityEvent]:
        events: list[IdentityEvent] = []
        for i in range(n):
            events += self.see(sid, track, with_cosine(base, noise, seed=1000 + i + track), **kw)  # type: ignore[arg-type]
        return events

    def visitors(self) -> int:
        with self.factory() as db:
            return int(db.execute(select(func.count()).select_from(Visitor)).scalar_one())

    def state(self, sid: str, track: int) -> str:
        return self.mgr._tracks[(sid, track)].state  # noqa: SLF001


@pytest.fixture
def rig(isolated_schema: tuple[object, Engine], tmp_path: Path) -> Iterator[Rig]:
    _, engine = isolated_schema
    Base.metadata.create_all(engine)
    yield Rig(engine, tmp_path)


def kinds(events: list[IdentityEvent]) -> list[str]:
    return [e.kind for e in events]


# ---------------------------------------------------------------------------------- registration
def test_registration_needs_several_consistent_good_observations(rig: Rig) -> None:
    s = rig.session("s" * 32)
    person = unit(1)
    first = rig.feed(s, 1, person, 2)
    assert rig.visitors() == 0 and "registered" not in kinds(first)  # 2 observations are not enough
    assert rig.state(s, 1) == "UNRESOLVED"
    more = rig.feed(s, 1, person, 3)
    assert kinds(more).count("registered") == 1
    assert rig.visitors() == 1 and rig.state(s, 1) == "REGISTERED"
    ev = next(e for e in more if e.kind == "registered")
    assert ev.visitor_number == 1 and ev.visitor_id and ev.track_id == 1


def test_too_short_in_time_does_not_register(
    isolated_schema: tuple[object, Engine], tmp_path: Path
) -> None:
    _, engine = isolated_schema
    Base.metadata.create_all(engine)
    r = Rig(engine, tmp_path, register_min_seconds=2.0)
    s = r.session("s" * 32)
    r.feed(s, 1, unit(1), 4, step_ms=100)  # 0.3 s
    assert r.visitors() == 0
    r.feed(s, 1, unit(1), 8, step_ms=400)  # the evidence now spans > 2 s
    assert r.visitors() == 1


def test_low_quality_observations_never_register_and_report_status(rig: Rig) -> None:
    s = rig.session("s" * 32)
    events: list[IdentityEvent] = []
    for _ in range(10):
        events += rig.see(s, 1, None)  # unusable faces
    assert rig.visitors() == 0
    assert rig.state(s, 1) == "INSUFFICIENT_QUALITY"
    assert kinds(events) == ["state"]  # one transition, not one event per frame
    # a usable-but-poor embedding (below the quality score floor) is treated the same way
    rig.feed(s, 2, unit(2), 6, quality=0.1)
    assert rig.visitors() == 0 and rig.state(s, 2) == "INSUFFICIENT_QUALITY"


def test_quality_failures_do_not_downgrade_a_recognised_track(rig: Rig) -> None:
    s = rig.session("s" * 32)
    rig.feed(s, 1, unit(1), 5)
    assert rig.state(s, 1) == "REGISTERED"
    rig.see(s, 1, None)
    assert rig.state(s, 1) == "REGISTERED"


def test_repeated_detections_do_not_create_repeated_registrations(rig: Rig) -> None:
    s = rig.session("s" * 32)
    rig.feed(s, 1, unit(1), 60)
    assert rig.visitors() == 1
    with rig.factory() as db:
        assert (
            db.execute(select(func.count()).select_from(FaceEmbedding)).scalar_one()
            <= rig.cfg.max_embeddings_per_visitor
        )


def test_inconsistent_observations_on_one_track_are_rejected_not_registered(rig: Rig) -> None:
    s = rig.session("s" * 32)
    # a tracker swap: the same track alternates between two unrelated faces
    events: list[IdentityEvent] = []
    for i in range(6):
        events += rig.see(s, 1, unit(10 if i % 2 else 20))
    assert rig.visitors() == 0
    assert rig.state(s, 1) == "REJECTED"
    assert "ambiguous" in kinds(events)


# ---------------------------------------------------------------------------------- recognition
def test_returning_person_in_another_session_is_recognised_without_a_duplicate(rig: Rig) -> None:
    person = unit(1)
    a, b = rig.session("a" * 32), rig.session("b" * 32)
    rig.feed(a, 1, person, 5)
    rig.mgr.end_session(a)
    events = rig.feed(b, 7, person, 4, noise=0.85)
    assert "recognized" in kinds(events) and "registered" not in kinds(events)
    assert rig.visitors() == 1 and rig.state(b, 7) == "RECOGNIZED"
    rec = next(e for e in events if e.kind == "recognized")
    assert (
        rec.visitor_number == 1 and rec.track_id == 7
    )  # track ids and visitor ids are distinct things


def test_recognition_needs_more_than_one_observation(rig: Rig) -> None:
    person = unit(1)
    a, b = rig.session("a" * 32), rig.session("b" * 32)
    rig.feed(a, 1, person, 5)
    events = rig.feed(b, 1, person, 1, noise=0.9)
    assert "recognized" not in kinds(events)
    assert rig.state(b, 1) == "MATCH_PENDING"


def test_recognition_survives_restart_and_registration_persists(rig: Rig) -> None:
    person = unit(1)
    a, b = rig.session("a" * 32), rig.session("b" * 32)
    rig.feed(a, 1, person, 5)
    rig.mgr.end_session(a)
    restarted = rig.new_manager()  # a process restart: only the database remains
    assert restarted.visitor_count() == 1
    events: list[IdentityEvent] = []
    for i in range(4):
        events += rig.see(b, 3, with_cosine(person, 0.9, seed=i), mgr=restarted)
    assert "recognized" in kinds(events) and rig.visitors() == 1


def test_different_people_with_moderately_similar_embeddings_are_not_merged_or_registered(
    rig: Rig,
) -> None:
    person = unit(1)
    a, b = rig.session("a" * 32), rig.session("b" * 32)
    rig.feed(a, 1, person, 5)
    look_alike = with_cosine(person, 0.45)  # between novelty (0.40) and match (0.50): the grey zone
    events = rig.feed(b, 1, look_alike, 8, noise=0.995)
    assert "recognized" not in kinds(events) and "registered" not in kinds(events)
    assert rig.visitors() == 1 and rig.state(b, 1) == "MATCH_PENDING"


def test_clearly_different_person_is_registered_as_a_new_visitor(rig: Rig) -> None:
    a, b = rig.session("a" * 32), rig.session("b" * 32)
    rig.feed(a, 1, unit(1), 5)
    events = rig.feed(b, 1, unit(2), 5)
    assert "registered" in kinds(events)
    assert rig.visitors() == 2
    with rig.factory() as db:
        numbers = sorted(n for (n,) in db.execute(select(Visitor.visitor_number)))
    assert numbers == [1, 2]


def test_ambiguous_match_between_two_visitors_is_explicit(
    isolated_schema: tuple[object, Engine], tmp_path: Path
) -> None:
    _, engine = isolated_schema
    Base.metadata.create_all(engine)
    r = Rig(engine, tmp_path)
    p1 = unit(1)
    p2 = unit(2)  # two registered, clearly different people
    r.feed(r.session("a" * 32), 1, p1, 5)
    r.feed(r.session("b" * 32), 1, p2, 5)
    assert r.visitors() == 2
    # a face lying midway between both (similar to each at ~0.7)
    mid = (p1 + p2) / np.linalg.norm(p1 + p2)
    events = r.feed(r.session("c" * 32), 1, mid, 5, noise=0.99)
    assert "ambiguous" in kinds(events)
    assert r.state("c" * 32, 1) == "AMBIGUOUS"
    assert r.visitors() == 2  # never forced into a recognised or new identity


def test_model_versions_are_never_compared(rig: Rig) -> None:
    s = rig.session("s" * 32)
    rig.feed(s, 1, unit(1), 5)
    events = rig.feed(rig.session("t" * 32), 1, unit(1), 5, model="some-other-model")
    assert events == [] and rig.visitors() == 1
    assert rig.mgr.runtime_stats()["incompatible_model_observations"] == 5


def test_embeddings_of_another_model_in_the_database_do_not_match(rig: Rig) -> None:
    s = rig.session("s" * 32)
    rig.feed(s, 1, unit(1), 5)
    other = IdentityManager(rig.factory, rig.store, rig.cfg, "other-model", DIM)
    other.load()
    assert other.visitor_count() == 0  # the stored vectors belong to another partition


def test_conflicting_observations_are_recorded_and_need_evidence_to_override(rig: Rig) -> None:
    p1, p2 = unit(1), unit(2)
    rig.feed(rig.session("a" * 32), 1, p1, 5)
    rig.feed(rig.session("b" * 32), 1, p2, 5)
    s = rig.session("c" * 32)
    rig.feed(s, 4, p1, 4)  # recognised as visitor 1
    assert rig.state(s, 4) == "RECOGNIZED"
    first = rig.mgr._tracks[(s, 4)].visitor_number  # noqa: SLF001
    events = rig.feed(s, 4, p2, 1)  # a single contradicting observation
    assert rig.mgr._tracks[(s, 4)].visitor_number == first  # noqa: SLF001
    assert "conflict" in kinds(events) and rig.mgr._tracks[(s, 4)].conflicts == 1  # noqa: SLF001
    later = rig.feed(s, 4, p2, 6)  # sustained evidence for the other visitor re-assigns
    assert rig.mgr._tracks[(s, 4)].visitor_number != first  # noqa: SLF001
    assert any(e.reason == "reassigned_after_conflict" for e in later)


def test_the_same_visitor_is_not_propagated_to_two_simultaneous_tracks(rig: Rig) -> None:
    person = unit(1)
    rig.feed(rig.session("a" * 32), 1, person, 5)
    s = rig.session("b" * 32)
    for i in range(4):  # interleaved observations: two tracks, same face, same time
        rig.see(s, 1, with_cosine(person, 0.9, seed=i), step_ms=40)
        rig.see(s, 2, with_cosine(person, 0.9, seed=50 + i), step_ms=40)
    states = {rig.state(s, 1), rig.state(s, 2)}
    assert (
        "AMBIGUOUS" in states and "RECOGNIZED" in states
    )  # one keeps the identity, one is flagged
    assert rig.visitors() == 1


# ---------------------------------------------------------------------------------- duplicates / races
def test_two_tracks_of_the_same_unseen_person_register_exactly_once(rig: Rig) -> None:
    person = unit(3)
    s = rig.session("s" * 32)
    events: list[IdentityEvent] = []
    for i in range(8):  # both tracks collect novel observations of the same person simultaneously
        events += rig.see(s, 1, with_cosine(person, 0.92, seed=i), step_ms=150)
        events += rig.see(s, 2, with_cosine(person, 0.92, seed=100 + i), step_ms=150)
    assert rig.visitors() == 1
    assert kinds(events).count("registered") == 1


def test_concurrent_registrations_of_the_same_person_create_one_visitor(rig: Rig) -> None:
    person = unit(4)
    s = rig.session("s" * 32)
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(track: int) -> None:
        try:
            barrier.wait()
            for i in range(6):
                rig.see(s, track, with_cosine(person, 0.92, seed=track * 31 + i), step_ms=300)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors
    assert rig.visitors() == 1


def test_concurrent_registrations_of_different_people_get_unique_numbers(rig: Rig) -> None:
    s = rig.session("s" * 32)
    barrier = threading.Barrier(6)

    def worker(track: int) -> None:
        barrier.wait()
        for i in range(6):
            rig.see(s, track, with_cosine(unit(500 + track), 0.93, seed=track * 7 + i), step_ms=400)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(1, 7)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    with rig.factory() as db:
        numbers = sorted(n for (n,) in db.execute(select(Visitor.visitor_number)))
    assert numbers == list(range(1, len(numbers) + 1)) and len(numbers) == 6


def test_two_manager_instances_do_not_both_register_the_same_person(rig: Rig) -> None:
    """Simulates two API processes sharing a database (the later multi-worker setup)."""
    person = unit(5)
    other = rig.new_manager()
    s1, s2 = rig.session("a" * 32), rig.session("b" * 32)
    for i in range(5):
        rig.see(s1, 1, with_cosine(person, 0.92, seed=i), mgr=rig.mgr)
    # the second instance's index was loaded BEFORE that registration; it must resync before deciding
    for i in range(5):
        rig.see(s2, 1, with_cosine(person, 0.92, seed=40 + i), mgr=other)
    assert rig.visitors() == 1


def test_registration_retry_with_the_same_key_returns_the_same_visitor(rig: Rig) -> None:
    s = rig.session("s" * 32)
    rig.feed(s, 1, unit(1), 5)
    with rig.factory() as db:
        first = db.execute(select(Visitor)).scalar_one()
    mgr = rig.new_manager()
    obs = Observation(s, 1, 99, MODEL, unit(1), 0.9, timestamp_ms=1)
    from app.services.identity_manager import _Novel  # noqa: PLC0415

    row = mgr._insert_visitor(  # noqa: SLF001
        f"{s}:1", "f" * 32, obs, [_Novel(unit(1), 0.9, 1, None)], None, None, None
    )
    assert row.id == first.id and rig.visitors() == 1  # the retry found the existing identity


def test_duplicate_and_delayed_worker_messages_are_ignored(rig: Rig) -> None:
    s = rig.session("s" * 32)
    obs = Observation(s, 1, 0, MODEL, unit(1), 0.9, timestamp_ms=1)
    assert rig.mgr.observe(obs) == []
    rig.mgr.observe(obs)
    rig.mgr.observe(obs)
    assert rig.mgr.runtime_stats()["duplicate_observations_ignored"] == 2
    assert rig.mgr.runtime_stats()["usable_observations"] == 1


# ---------------------------------------------------------------------------------- failures / validation
def test_failed_registration_rolls_back_completely_and_leaves_no_image(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = rig.session("s" * 32)
    real = rig.mgr._insert_visitor  # noqa: SLF001

    def boom(*args: object, **kwargs: object) -> object:
        # do the real work up to the commit, then fail: simulate a database failure mid-transaction
        from sqlalchemy.exc import OperationalError

        raise OperationalError("INSERT", {}, Exception("connection lost"))

    monkeypatch.setattr(rig.mgr, "_insert_visitor", boom)
    events = rig.feed(s, 1, unit(1), 6)
    assert "failed" in kinds(events)
    assert rig.visitors() == 0
    assert (
        rig.store.all_files() == []
    )  # the image written before the failed transaction was removed
    monkeypatch.setattr(rig.mgr, "_insert_visitor", real)
    rig.feed(s, 1, unit(1), 8)  # the database is back: the same track retries and registers
    assert rig.visitors() == 1


def test_partial_database_failure_inside_the_transaction_leaves_no_rows(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = rig.session("s" * 32)
    import uuid as uuid_module

    import app.services.identity_manager as im  # noqa: F401

    real_uuid4 = uuid_module.uuid4
    calls = {"n": 0}

    def flaky() -> object:
        calls["n"] += 1
        if (
            calls["n"] % 2 == 0
        ):  # the visitor id (odd call) works, the embedding row id (even) fails
            raise ValueError("simulated failure while inserting an embedding row")
        return real_uuid4()

    monkeypatch.setattr(uuid_module, "uuid4", flaky)
    events = rig.feed(s, 1, unit(1), 4)
    monkeypatch.undo()
    assert "failed" in kinds(events)
    assert rig.visitors() == 0  # visitor row was rolled back together with the failed embedding
    with rig.factory() as db:
        assert db.execute(select(func.count()).select_from(FaceEmbedding)).scalar_one() == 0


@pytest.mark.parametrize(
    "vec",
    [
        np.full(DIM, np.nan, dtype=np.float32),
        np.full(DIM, np.inf, dtype=np.float32),
        np.zeros(DIM, dtype=np.float32),
        np.ones(DIM - 1, dtype=np.float32),
    ],
)
def test_invalid_vectors_are_ignored_without_side_effects(rig: Rig, vec: np.ndarray) -> None:
    s = rig.session("s" * 32)
    for _ in range(6):
        assert rig.see(s, 1, vec) == []
    assert rig.visitors() == 0
    assert rig.mgr.runtime_stats()["invalid_embeddings"] == 6


# ---------------------------------------------------------------------------------- persistence / maintenance
def test_end_of_session_persists_one_association_per_track(rig: Rig) -> None:
    a, b = rig.session("a" * 32), rig.session("b" * 32)
    rig.feed(a, 1, unit(1), 5)
    rig.feed(b, 5, unit(1), 4, noise=0.9)
    rig.see(b, 6, None)
    rig.mgr.end_session(a)
    rig.mgr.end_session(b)
    rig.mgr.end_session(b)  # idempotent
    with rig.factory() as db:
        rows = {
            (r.session_id, r.track_id): r for r in db.execute(select(IdentityAssociation)).scalars()
        }
    assert rows[(a, 1)].state == "REGISTERED" and rows[(a, 1)].registered_here == 1
    assert rows[(b, 5)].state == "RECOGNIZED" and rows[(b, 5)].visitor_id == rows[(a, 1)].visitor_id
    assert rows[(b, 6)].state == "INSUFFICIENT_QUALITY" and rows[(b, 6)].visitor_id is None
    assert rig.mgr.runtime_stats()["active_tracks"] == 0  # memory released


def test_visitor_erasure_removes_embeddings_associations_images_and_index_entry(rig: Rig) -> None:
    a = rig.session("a" * 32)
    rig.feed(a, 1, unit(1), 5)
    rig.mgr.end_session(a)
    with rig.factory() as db:
        v = db.execute(select(Visitor)).scalar_one()
        vid = v.id
    assert rig.store.all_files()
    assert rig.mgr.delete_visitor(vid) is True
    assert rig.mgr.delete_visitor(vid) is False
    with rig.factory() as db:
        assert db.execute(select(func.count()).select_from(Visitor)).scalar_one() == 0
        assert db.execute(select(func.count()).select_from(FaceEmbedding)).scalar_one() == 0
        assert db.execute(select(func.count()).select_from(IdentityAssociation)).scalar_one() == 0
    assert rig.store.all_files() == [] and rig.mgr.visitor_count() == 0
    # and the person is "new" again afterwards
    assert "registered" in kinds(rig.feed(rig.session("b" * 32), 1, unit(1), 5))


def test_retention_purges_only_visitors_not_seen_for_the_configured_time(
    isolated_schema: tuple[object, Engine], tmp_path: Path
) -> None:
    from datetime import UTC, datetime, timedelta

    _, engine = isolated_schema
    Base.metadata.create_all(engine)
    r = Rig(engine, tmp_path, retention_days=30)
    r.feed(r.session("a" * 32), 1, unit(1), 5)
    r.feed(r.session("b" * 32), 1, unit(2), 5)
    with r.factory() as db:
        old = db.execute(select(Visitor).where(Visitor.visitor_number == 1)).scalar_one()
        old.last_seen_at = datetime.now(UTC) - timedelta(days=45)
        db.commit()
    assert r.mgr.purge_expired() == 1 and r.visitors() == 1
    assert Rig(engine, tmp_path).mgr.purge_expired() == 0  # retention disabled (0 days) by default


def test_reconcile_removes_orphan_images_and_clears_dangling_references(rig: Rig) -> None:
    rig.feed(rig.session("a" * 32), 1, unit(1), 5)
    orphan_dir = "e" * 32
    path, _ = rig.store.save(orphan_dir, jpeg())  # a file whose DB transaction never committed
    with rig.factory() as db:
        v = db.execute(select(Visitor)).scalar_one()
        real_path = v.image_path
    assert real_path and rig.store.exists(real_path)
    assert rig.mgr.reconcile(grace_seconds=10_000) == {
        "orphan_images_removed": 0,
        "missing_image_references_cleared": 0,
    }  # too young to judge: a write may be in flight
    assert rig.mgr.reconcile(grace_seconds=-1)["orphan_images_removed"] == 1
    assert not rig.store.exists(path) and rig.store.exists(real_path)
    rig.store.delete(real_path)  # the image disappears behind the database's back
    assert rig.mgr.reconcile(grace_seconds=-1)["missing_image_references_cleared"] == 1
    with rig.factory() as db:
        v = db.execute(select(Visitor)).scalar_one()
        assert v.image_path is None  # the identity survives, only the image reference is cleared


def test_runtime_stats_expose_no_embeddings(rig: Rig) -> None:
    rig.feed(rig.session("a" * 32), 1, unit(1), 5)
    stats = rig.mgr.runtime_stats()
    assert stats["registrations"] == 1 and stats["indexed_visitors"] == 1
    assert "vector" not in str(stats).lower() and "embedding_values" not in stats
