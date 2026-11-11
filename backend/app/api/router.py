from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.v1 import analytics, auth, events, processing, system, uploads, visitors, ws
from app.core.security import require_auth

API_V1_PREFIX = "/api/v1"

api_router = APIRouter(prefix=API_V1_PREFIX)
api_router.include_router(system.router, tags=["system"])
api_router.include_router(auth.router, tags=["auth"])
api_router.include_router(
    processing.router, tags=["processing"], dependencies=[Depends(require_auth)]
)
api_router.include_router(visitors.router, tags=["visitors"], dependencies=[Depends(require_auth)])
api_router.include_router(events.router, tags=["events"], dependencies=[Depends(require_auth)])
api_router.include_router(uploads.router, tags=["uploads"], dependencies=[Depends(require_auth)])
api_router.include_router(
    analytics.router, tags=["analytics"], dependencies=[Depends(require_auth)]
)
api_router.include_router(analytics.ws_router, tags=["streaming"])
api_router.include_router(ws.router, tags=["streaming"])
