"""WebSockets: the worker's control/frame link and the browser's live stream.

Authorization boundary
* /worker/ws  - requires ``Authorization: Bearer <WORKER_TOKEN>`` (constant-time comparison).
  With no token configured every connection is refused. Only one worker is active; a new one
  replaces the old.
* /sessions/{id}/stream - browsers. There are no user accounts in Phase 1, so access is limited
  by (a) an Origin allow-list (the configured CORS origins) and (b) an unguessable 128-bit session
  id. This is NOT user authentication; do not expose the API publicly with a real camera until
  an auth phase exists (documented in docs/phase-1-report.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.core.errors import AppError
from app.services.session_manager import SessionManager, WorkerLink
from app.workers import protocol as P

logger = logging.getLogger("katomaran.ws")
router = APIRouter()


def _token_ok(websocket: WebSocket) -> bool:
    expected = websocket.app.state.settings.worker_token
    if expected is None or not expected.get_secret_value():
        return False
    header = websocket.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    return hmac.compare_digest(presented.encode(), expected.get_secret_value().encode())


class _SocketAdapter:
    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def send_text(self, data: str) -> None:
        await self._ws.send_text(data)

    async def close(self, code: int = 1000) -> None:
        if self._ws.application_state == WebSocketState.CONNECTED:
            await self._ws.close(code)


@router.websocket("/worker/ws")
async def worker_socket(websocket: WebSocket) -> None:
    if not _token_ok(websocket):
        await websocket.close(code=1008)  # policy violation; rejected before accept -> HTTP 403
        logger.warning("Rejected worker connection (bad or missing token)")
        return
    await websocket.accept()
    mgr: SessionManager = websocket.app.state.sessions
    link = WorkerLink(ws=_SocketAdapter(websocket))
    await mgr.attach_worker(link)
    logger.info("Worker connected")
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if (text := message.get("text")) is not None:
                try:
                    await mgr.on_worker_message(link, P.decode_control(text))
                except P.ProtocolError as exc:
                    logger.warning("Malformed worker message: %s", exc)
            elif (data := message.get("bytes")) is not None:
                await mgr.on_worker_frame(link, data)
    except (WebSocketDisconnect, OSError):
        pass  # worker went away; handled in `finally`
    except Exception:
        logger.exception("Worker socket error")
    finally:
        await mgr.run_shielded(mgr.detach_worker(link))
        logger.info("Worker disconnected")


def _origin_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if origin is None:
        return True  # non-browser client (tests, curl); browsers always send Origin
    return origin in websocket.app.state.settings.cors_origins


@router.websocket("/sessions/{session_id}/stream")
async def session_stream(websocket: WebSocket, session_id: str) -> None:
    if not _origin_allowed(websocket):
        await websocket.close(code=1008)
        return
    mgr: SessionManager = websocket.app.state.sessions
    try:
        sub, snapshot = await mgr.subscribe(session_id)
    except AppError:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    try:
        await websocket.send_text(
            P.encode_control({"type": "snapshot", "session": snapshot.model_dump(mode="json")})
        )
        if sub is None:  # finished session: nothing live to stream
            await websocket.close(code=1000)
            return
    except (WebSocketDisconnect, OSError, RuntimeError):
        if sub is not None:
            mgr.unsubscribe(session_id, sub)
        return

    async def writer() -> None:
        while True:
            controls, frame = await sub.next_batch()
            for text in controls:
                await websocket.send_text(text)
            if frame is not None:
                await websocket.send_bytes(frame)

    async def reader() -> None:
        # Clients send nothing meaningful; receiving detects disconnects.
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return

    tasks = [asyncio.create_task(writer()), asyncio.create_task(reader())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        mgr.unsubscribe(session_id, sub)
        for r in results:
            # A vanished browser surfaces as WebSocketDisconnect / ClientDisconnected (an
            # OSError); that is a normal end of the stream, not a server error.
            if isinstance(r, Exception) and not isinstance(
                r, WebSocketDisconnect | OSError | RuntimeError
            ):
                logger.warning("Stream task failed: %s", type(r).__name__)
        if websocket.application_state == WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close(code=1000)
