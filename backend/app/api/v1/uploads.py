"""Video upload (authenticated). The body is the raw video: ``PUT /uploads?filename=clip.mp4``.

A raw streamed body (no multipart parser) keeps the hot path simple and lets us enforce the size cap
while streaming. The response is the new source (no path), ready to be started in Live Monitoring.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from app.core.errors import AppError
from app.schemas.processing import SourceOut
from app.services import uploads as U
from app.services.session_manager import SessionManager

router = APIRouter()


@router.put(
    "/uploads", response_model=SourceOut, status_code=201, summary="Upload a video to process"
)
async def upload_video(
    request: Request, filename: Annotated[str, Query(min_length=1, max_length=255)]
) -> SourceOut:
    config = request.app.state.config
    if not config.video.uploads_enabled:
        raise AppError(404, "uploads_disabled", "Video upload is switched off on this server.")
    limit = config.video.max_upload_mb * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise AppError(
            413, "too_large", f"The video exceeds the {config.video.max_upload_mb} MB limit."
        )
    try:
        uid, part, final = U.new_upload_paths(config, filename)
    except U.UploadError as exc:
        raise AppError(exc.status, exc.code, exc.message) from exc
    size = 0
    try:
        with part.open("wb") as handle:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise AppError(
                        413,
                        "too_large",
                        f"The video exceeds the {config.video.max_upload_mb} MB limit.",
                    )
                await asyncio.to_thread(handle.write, chunk)
        if size == 0:
            raise AppError(422, "empty_upload", "The upload was empty.")
        info = await asyncio.to_thread(U.validate_video, part)
        part.replace(final)
        U.write_sidecar(final, uid, filename, size, info)
    except U.UploadError as exc:
        part.unlink(missing_ok=True)
        raise AppError(exc.status, exc.code, exc.message) from exc
    except BaseException:
        part.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        raise
    mgr_: SessionManager = request.app.state.sessions
    out = mgr_.source_out(f"upload-{uid}")
    if out is None:  # cannot happen: the registry lists the folder
        raise AppError(500, "upload_not_listed", "The upload was stored but could not be listed.")
    return out


@router.delete("/uploads/{upload_id}", status_code=204, summary="Delete an uploaded video")
async def delete_upload(upload_id: str, request: Request) -> Response:
    config = request.app.state.config
    video = U.find_upload(config, upload_id)
    if video is None:
        raise AppError(404, "upload_not_found", "No such upload.")
    mgr: SessionManager = request.app.state.sessions
    live = mgr.live
    if live is not None and live.row.source_id == f"upload-{upload_id}":
        raise AppError(409, "upload_in_use", "Stop the session that is using this video first.")
    with contextlib.suppress(OSError):
        await asyncio.to_thread(U.delete_upload, video)
    return Response(status_code=204)
