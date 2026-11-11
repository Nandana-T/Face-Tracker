"""Long-running worker agent: keeps an outbound WebSocket to the API and runs one session at a time.

The asyncio loop here only does I/O. All video decoding and inference run on a separate thread
(see ProcessingPipeline), so a slow model never stalls heartbeats or commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from app.core.config import AppConfig
from app.core.context import session_id_var
from app.core.settings import Settings
from app.schemas.detection import DetectionResult
from app.vision.detection.interfaces import FaceDetector, ModelUnavailable
from app.vision.detection.model_registry import (
    MODELS,
    ModelFetchError,
    model_path,
    verify_model_file,
)
from app.vision.detection.yolo_onnx import YoloOnnxFaceDetector
from app.vision.recognition.registry import recognition_status as recognition_models_status
from app.vision.recognition.service import (
    FaceRecognizer,
    FaceResult,
    RecognitionRunner,
    RecognitionScheduler,
)
from app.vision.sources.interfaces import SourceError, VideoSource
from app.vision.sources.registry import build_source
from app.vision.tracking.tracker import FaceByteTracker, TrackerConfig
from app.workers import protocol as P
from app.workers.pipeline import Outcome, PipelineSettings, ProcessingPipeline

logger = logging.getLogger("katomaran.worker")

DetectorFactory = Callable[[Settings, AppConfig], FaceDetector]
SourceBuilder = Callable[..., VideoSource]


def model_status(settings: Settings, config: AppConfig) -> tuple[bool, str | None]:
    spec = MODELS[config.detection.model]
    path = model_path(settings.models_path, spec)
    try:
        verify_model_file(path, spec)
    except ModelFetchError:
        # Deliberately generic: this text reaches the browser, so no server paths.
        return False, (
            "The face detection model is missing or corrupt on the worker machine. "
            "Run scripts\\download-model.ps1 there."
        )
    return True, None


def recognition_status(settings: Settings, config: AppConfig) -> tuple[bool, str | None]:
    rc = config.recognition
    if not rc.enabled:
        return False, "Face recognition is switched off in the configuration."
    ok, detail = recognition_models_status(settings.models_path)
    if not ok:
        # Generic text: it reaches the browser, so no server paths.
        return False, (
            "The face recognition model is missing or corrupt on the worker machine. "
            "Run scripts\\download-model.ps1 -Recognition there."
        )
    return True, None


def default_detector_factory(settings: Settings, config: AppConfig) -> FaceDetector:
    d = config.detection
    spec = MODELS[d.model]
    path = model_path(settings.models_path, spec)
    if d.model_path:
        from app.core.paths import resolve_path

        path = resolve_path(d.model_path, settings.models_path)
    else:
        verify_model_file(path, spec)  # raises ModelFetchError
    return YoloOnnxFaceDetector(
        path,
        input_size=d.input_size,
        conf_threshold=d.confidence_threshold,
        iou_threshold=d.iou_threshold,
        max_faces=d.max_faces,
        intra_op_threads=d.intra_op_threads,
        allow_spinning=d.cpu_spin_wait,
        name=d.model,
    )


class Outbox:
    """Bounded outgoing queue: control messages are kept in order, frames are latest-wins."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._control: deque[str] = deque(maxlen=500)
        self._frame: bytes | None = None
        self._observations: deque[bytes] = deque(maxlen=256)
        self._wake = asyncio.Event()
        self._lock = threading.Lock()
        self.frames_overwritten = 0

    def _signal(self) -> None:
        self._loop.call_soon_threadsafe(self._wake.set)

    def put_control(self, message: dict[str, Any]) -> None:
        with self._lock:
            self._control.append(P.encode_control(message))
        self._signal()

    def put_frame(self, data: bytes) -> None:
        with self._lock:
            if self._frame is not None:
                self.frames_overwritten += 1
            self._frame = data
        self._signal()

    def put_observation(self, data: bytes) -> None:
        """Recognition observations keep their order (bounded; the oldest is dropped when full)."""
        with self._lock:
            self._observations.append(data)
        self._signal()

    def drain(self) -> tuple[list[str], bytes | None, list[bytes]]:
        with self._lock:
            control, self._control = list(self._control), deque(maxlen=500)
            frame, self._frame = self._frame, None
            observations, self._observations = list(self._observations), deque(maxlen=256)
        self._wake.clear()
        return control, frame, observations

    async def wait(self) -> None:
        await self._wake.wait()


class ActiveSession:
    def __init__(self, session_id: str, pipeline: ProcessingPipeline | None = None) -> None:
        self.session_id = session_id
        self.pipeline = pipeline
        self.thread: threading.Thread | None = None
        self.stop_requested = False


class WorkerAgent:
    def __init__(
        self,
        settings: Settings,
        config: AppConfig,
        detector_factory: DetectorFactory = default_detector_factory,
        source_builder: SourceBuilder = build_source,
    ) -> None:
        self.settings = settings
        self.config = config
        self._detector_factory = detector_factory
        self._source_builder = source_builder
        self._active: ActiveSession | None = None
        self._outbox: Outbox | None = None
        self._stop_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- public ---------------------------------------------------------------------
    def request_shutdown(self) -> None:
        """Thread-safe: may be called from signal handlers or other threads."""
        loop, event = self._loop, self._stop_event
        if loop is not None and event is not None:
            with contextlib.suppress(
                RuntimeError
            ):  # the loop already ended: shutdown is idempotent
                loop.call_soon_threadsafe(event.set)

    async def run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        token = self.settings.worker_token
        if token is None or not token.get_secret_value():
            logger.error("WORKER_TOKEN is not set; the API would reject this worker.")
            return
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                await self._connection(token.get_secret_value())
                backoff = 1.0
            except (OSError, ConnectionError) as exc:
                logger.warning(
                    "API connection failed (%s); retrying in %.0fs", type(exc).__name__, backoff
                )
            except Exception as exc:  # websockets raises its own hierarchy
                logger.warning(
                    "API connection lost (%s); retrying in %.0fs", type(exc).__name__, backoff
                )
            self._abort_active("api_disconnected")
            if self._stop_event.is_set():
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
            backoff = min(backoff * 2, 15.0)
        self._abort_active("worker_shutdown")
        await self._join_active()

    # -- connection -----------------------------------------------------------------
    async def _connection(self, token: str) -> None:
        from websockets.asyncio.client import connect

        url = self.settings.worker_api_url.rstrip("/") + "/api/v1/worker/ws"
        async with connect(
            url,
            additional_headers={"Authorization": f"Bearer {token}"},
            max_size=P.MAX_CONTROL_BYTES,
            open_timeout=10,
            ping_interval=20,
        ) as ws:
            loop = asyncio.get_running_loop()
            self._outbox = Outbox(loop)
            ok, detail = model_status(self.settings, self.config)
            rok, rdetail = recognition_status(self.settings, self.config)
            self._outbox.put_control(
                {
                    "type": P.MSG_HELLO,
                    "worker_id": self.settings.worker_id,
                    "version": self.settings.app_version,
                    "model": {
                        "key": self.config.detection.model,
                        "available": ok,
                        "detail": detail,
                    },
                    "recognition": {
                        "key": self.config.recognition.model,
                        "enabled": self.config.recognition.enabled,
                        "available": rok,
                        "detail": rdetail,
                    },
                }
            )
            logger.info("Connected to API", extra={"model_available": ok})
            tasks = [
                asyncio.create_task(self._sender(ws)),
                asyncio.create_task(self._receiver(ws)),
                asyncio.create_task(self._heartbeat()),
            ]
            assert self._stop_event is not None
            stopper = asyncio.create_task(self._stop_event.wait())
            try:
                done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t is not stopper and t.exception():
                        raise t.exception()  # type: ignore[misc]
            finally:
                for t in [*tasks, stopper]:
                    t.cancel()
                await asyncio.gather(*tasks, stopper, return_exceptions=True)

    async def _sender(self, ws: Any) -> None:
        assert self._outbox is not None
        while True:
            await self._outbox.wait()
            control, frame, observations = self._outbox.drain()
            for text in control:
                await ws.send(text)
            for data in observations:
                await ws.send(data)
            if frame is not None:
                await ws.send(frame)

    async def _heartbeat(self) -> None:
        assert self._outbox is not None
        while True:
            active = self._active
            self._outbox.put_control(
                {
                    "type": P.MSG_HEARTBEAT,
                    "active_session_id": active.session_id if active else None,
                }
            )
            await asyncio.sleep(self.config.worker.heartbeat_interval_seconds)

    async def _receiver(self, ws: Any) -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                continue  # the API never sends binary
            try:
                message = P.decode_control(raw)
            except P.ProtocolError as exc:
                logger.warning("Ignoring malformed API message: %s", exc)
                continue
            kind = message["type"]
            sid = message.get("session_id")
            if (
                kind == P.MSG_START
                and isinstance(sid, str)
                and isinstance(message.get("source_id"), str)
            ):
                self._start_session(sid, message["source_id"])
            elif kind == P.MSG_STOP and isinstance(sid, str):
                self._stop_session(sid)
            else:
                logger.debug("Ignoring unknown message type %s", kind)

    # -- sessions -------------------------------------------------------------------
    def _send(self, message: dict[str, Any]) -> None:
        if self._outbox is not None:
            self._outbox.put_control(message)

    def _start_session(self, session_id: str, source_id: str) -> None:
        if self._active is not None:
            self._send(
                {
                    "type": P.MSG_ENDED,
                    "session_id": session_id,
                    "state": "ERROR",
                    "code": "worker_busy",
                    "message": "The worker is already processing a session.",
                    "stats": {},
                }
            )
            return
        active = ActiveSession(session_id)
        self._active = active
        self._send({"type": P.MSG_ACCEPTED, "session_id": session_id})
        thread = threading.Thread(
            target=self._session_thread,
            args=(active, source_id),
            name=f"session-{session_id[:8]}",
            daemon=True,
        )
        active.thread = thread
        thread.start()

    def _stop_session(self, session_id: str) -> None:
        active = self._active
        if active is None or active.session_id != session_id:
            # Idempotent: tell the API it is already over.
            self._send(
                {
                    "type": P.MSG_ENDED,
                    "session_id": session_id,
                    "state": "STOPPED",
                    "code": None,
                    "message": None,
                    "stats": {},
                }
            )
            return
        active.stop_requested = True
        if active.pipeline is not None:
            active.pipeline.request_stop()

    def _abort_active(self, reason: str) -> None:
        active = self._active
        if active is not None:
            logger.warning("Aborting session: %s", reason)
            active.stop_requested = True
            if active.pipeline is not None:
                active.pipeline.request_stop()

    async def _join_active(self) -> None:
        active = self._active
        if active is not None and active.thread is not None:
            await asyncio.to_thread(active.thread.join, 10)

    def _session_thread(self, active: ActiveSession, source_id: str) -> None:
        session_id_var.set(active.session_id)
        outbox = self._outbox
        outcome = Outcome("ERROR", "processing_error", "Processing failed to start.")
        stats: dict[str, Any] = {}
        try:
            try:
                source = self._source_builder(
                    self.config,
                    source_id,
                    # RTSP reconnecting/connected must reach the API and the UI (found by the local RTSP test:
                    # this callback was never wired, so a lost stream looked "connected" forever)
                    on_state=lambda s: self._send(
                        {
                            "type": P.MSG_STATE,
                            "session_id": active.session_id,
                            "state": s,
                            "detail": None,
                        }
                    ),
                )
                detector = self._detector_factory(self.settings, self.config)
            except SourceError as exc:
                logger.warning("Source rejected: %s", exc.code)
                outcome = Outcome("ERROR", exc.code, str(exc))
                return
            except (ModelUnavailable, ModelFetchError) as exc:
                logger.error("Model unavailable: %s", exc)
                outcome = Outcome("ERROR", "model_unavailable", str(exc))
                return
            cfg, st = self.config, self.config.stream
            tr = cfg.tracking
            # One tracker per session: track ids restart and nothing leaks between sessions.
            tracker = (
                FaceByteTracker(
                    TrackerConfig(
                        high_confidence=tr.high_confidence,
                        confirm_hits=tr.confirm_hits,
                        tentative_timeout_seconds=tr.tentative_timeout_seconds,
                        max_missing_seconds=tr.max_missing_seconds,
                        match_iou=tr.match_iou,
                        low_match_iou=tr.low_match_iou,
                        lost_match_iou=tr.lost_match_iou,
                        box_buffer=tr.box_buffer,
                        lost_velocity_decay_per_second=tr.lost_velocity_decay_per_second,
                        max_tracks=tr.max_tracks,
                    )
                )
                if tr.enabled
                else None
            )
            runner = self._build_recognition(active.session_id) if tracker is not None else None
            pipeline = ProcessingPipeline(
                source,
                detector,
                PipelineSettings(
                    frame_skip=cfg.detection.frame_skip,
                    queue_size=cfg.worker.queue_size,
                    realtime_pacing=cfg.worker.realtime_pacing,
                    stream_max_fps=st.max_fps,
                    stream_max_width=st.max_width,
                    jpeg_quality=st.jpeg_quality,
                    stats_interval_seconds=cfg.worker.stats_interval_seconds,
                ),
                on_result=lambda r, j: self._publish(active.session_id, r, j),
                on_state=lambda s, d: self._send(
                    {"type": P.MSG_STATE, "session_id": active.session_id, "state": s, "detail": d}
                ),
                on_stats=lambda s: self._send(
                    {"type": P.MSG_STATS, "session_id": active.session_id, "stats": s}
                ),
                tracker=tracker,
                recognition=runner,
                recognition_pad=cfg.recognition.landmark_pad,
            )
            active.pipeline = pipeline
            if active.stop_requested:
                pipeline.request_stop()
            outcome = pipeline.run()
            stats = pipeline.final_snapshot()
        except Exception:
            logger.exception("Session thread crashed")
            outcome = Outcome("ERROR", "processing_error", "An internal processing error occurred.")
        finally:
            # Free the slot BEFORE announcing the end so an immediate new start is not "busy".
            self._active = None
            if outbox is not None:
                self._send(
                    {
                        "type": P.MSG_ENDED,
                        "session_id": active.session_id,
                        "state": outcome.state,
                        "code": outcome.code,
                        "message": outcome.message,
                        "stats": stats,
                    }
                )

    def _build_recognition(self, session_id: str) -> RecognitionRunner | None:
        rc = self.config.recognition
        if not rc.enabled:
            return None
        try:
            recognizer = FaceRecognizer(self.settings.models_path, rc)
        except Exception as exc:  # noqa: BLE001 - the session continues without recognition
            logger.warning("Recognition unavailable for this session: %s", type(exc).__name__)
            return None
        logger.info(
            "Recognition ready",
            extra={"model": recognizer.model_version, "load_s": round(recognizer.load_seconds, 2)},
        )
        return RecognitionRunner(
            recognizer,
            lambda r: self._publish_observation(session_id, recognizer, r),
            RecognitionScheduler(rc),
            rc.queue_size,
        )

    def _publish_observation(
        self, session_id: str, recognizer: FaceRecognizer, r: FaceResult
    ) -> None:
        if self._outbox is None:
            return
        import base64

        header = {
            "kind": P.KIND_OBSERVATION,
            "session_id": session_id,
            "track_id": r.track_id,
            "seq": r.seq,
            "model_version": recognizer.model_version,
            "dim": recognizer.dim,
            "embedding_b64": None
            if r.embedding is None
            else base64.b64encode(r.embedding.astype("<f4").tobytes()).decode("ascii"),
            "quality": r.quality,
            "reasons": list(r.reasons),
            "timestamp_ms": r.timestamp_ms,
            "frame_id": r.frame_id,
        }
        try:
            self._outbox.put_observation(P.pack_frame(header, r.crop_jpeg or b""))
        except P.ProtocolError as exc:
            logger.warning("Dropping oversized observation: %s", exc)

    def _publish(self, session_id: str, result: DetectionResult, jpeg: bytes | None) -> None:
        if jpeg is None or self._outbox is None:
            return
        header = {
            "session_id": session_id,
            **result.model_dump(mode="json"),
            # Wall-clock publish time; lets a same-host client measure end-to-end delivery latency.
            "published_at_ms": int(time.time() * 1000),
        }
        try:
            self._outbox.put_frame(P.pack_frame(header, jpeg))
        except P.ProtocolError as exc:
            logger.warning("Dropping oversized frame: %s", exc)
