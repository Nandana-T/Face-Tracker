"""Sign-in endpoints (public by necessity)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from app.core.security import AuthService
from app.schemas.system import ErrorResponse

router = APIRouter(prefix="/auth")


class AuthStatus(BaseModel):
    required: bool
    authenticated: bool


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a secret
    expires_in: int


def _auth(request: Request) -> AuthService:
    auth: AuthService = request.app.state.auth
    return auth


@router.get(
    "/status", response_model=AuthStatus, summary="Is sign-in required / is this request signed in"
)
def auth_status(request: Request) -> AuthStatus:
    auth = _auth(request)
    return AuthStatus(required=auth.required, authenticated=auth.is_authenticated(request))


@router.post(
    "/login",
    response_model=TokenResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
    },
    summary="Exchange the admin password for a bearer token",
)
def login(body: LoginRequest, request: Request) -> TokenResponse:
    client = request.client.host if request.client else "unknown"
    token, ttl = _auth(request).login(body.password, client)
    return TokenResponse(access_token=token, expires_in=ttl)
