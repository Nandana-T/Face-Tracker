from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.v1 import auth, processing, system, ws
from app.core.security import require_auth

API_V1_PREFIX = "/api/v1"

api_router = APIRouter(prefix=API_V1_PREFIX)
api_router.include_router(system.router, tags=["system"])
api_router.include_router(auth.router, tags=["auth"])
api_router.include_router(
    processing.router, tags=["processing"], dependencies=[Depends(require_auth)]
)
api_router.include_router(ws.router, tags=["streaming"])
