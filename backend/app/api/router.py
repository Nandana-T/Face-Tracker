from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import processing, system, ws

API_V1_PREFIX = "/api/v1"

api_router = APIRouter(prefix=API_V1_PREFIX)
api_router.include_router(system.router, tags=["system"])
api_router.include_router(processing.router, tags=["processing"])
api_router.include_router(ws.router, tags=["streaming"])
