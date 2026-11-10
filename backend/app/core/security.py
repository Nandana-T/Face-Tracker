"""Minimal access control: one shared admin password -> signed, expiring bearer tokens.

Deliberately small (no user database): the goal of Phase 1.5 is that nobody who merely *reaches*
the API can start sessions, read sources or watch frames.

* ``POST /auth/login {password}`` -> ``access_token`` (HMAC-SHA256 signed, default 8 h).
* Every REST route except ``/health``, ``/ready``, ``/auth/*`` requires ``Authorization: Bearer``.
* Browsers cannot set headers on a WebSocket, so the live stream uses a single-use ticket
  (``POST /sessions/{id}/stream-ticket``, valid 30 s, bound to that session) in the query string.
* The worker keeps its own bearer secret (``WORKER_TOKEN``).
* Auth is enforced whenever ``ADMIN_PASSWORD`` is set, and is *mandatory* in production
  (see ``Settings`` validation). Without it (local development) the API stays open, as in Phase 1.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque

from fastapi import Request

from app.core.errors import AppError
from app.core.settings import Settings

TICKET_TTL_SECONDS = 30
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 300


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret: bytes, payload: dict[str, object]) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = _b64(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{mac}"


def verify(
    secret: bytes, token: str, kind: str, now: float | None = None
) -> dict[str, object] | None:
    """Return the payload if the signature, ``kind`` and expiry are valid, else None."""
    try:
        body, mac = token.split(".", 1)
        expected = _b64(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(mac, expected):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("kind") != kind:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int | float) or (now if now is not None else time.time()) >= exp:
        return None
    return payload


class AuthService:
    def __init__(self, settings: Settings) -> None:
        password = settings.admin_password.get_secret_value() if settings.admin_password else ""
        self.required = bool(password)
        self._password = password.encode()
        configured = settings.auth_secret.get_secret_value() if settings.auth_secret else ""
        # Without a configured secret, tokens simply stop working after a restart.
        self._secret = (configured or secrets.token_urlsafe(48)).encode()
        self.token_ttl = settings.auth_token_ttl_minutes * 60
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._used_tickets: dict[str, float] = {}

    # -- login ----------------------------------------------------------------------
    def login(self, password: str, client: str, now: float | None = None) -> tuple[str, int]:
        if not self.required:
            raise AppError(400, "auth_disabled", "Authentication is not enabled on this server.")
        t = now if now is not None else time.time()
        attempts = self._failures[client]
        while attempts and t - attempts[0] > LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) >= LOGIN_MAX_FAILURES:
            raise AppError(
                429, "too_many_attempts", "Too many failed sign-in attempts. Try again later."
            )
        if not hmac.compare_digest(password.encode(), self._password):
            attempts.append(t)
            raise AppError(401, "invalid_credentials", "Incorrect password.")
        attempts.clear()
        token = sign(self._secret, {"kind": "access", "sub": "admin", "exp": t + self.token_ttl})
        return token, self.token_ttl

    # -- bearer verification --------------------------------------------------------
    def is_authenticated(self, request: Request) -> bool:
        if not self.required:
            return True
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and verify(self._secret, token, "access") is not None

    # -- WebSocket tickets ----------------------------------------------------------
    def issue_ticket(self, session_id: str, now: float | None = None) -> tuple[str, int]:
        t = now if now is not None else time.time()
        ticket = sign(
            self._secret,
            {
                "kind": "stream",
                "sid": session_id,
                "jti": secrets.token_hex(8),
                "exp": t + TICKET_TTL_SECONDS,
            },
        )
        return ticket, TICKET_TTL_SECONDS

    def consume_ticket(self, ticket: str | None, session_id: str, now: float | None = None) -> bool:
        """True if the ticket is valid, belongs to ``session_id`` and was not used before."""
        if not self.required:
            return True
        t = now if now is not None else time.time()
        self._used_tickets = {j: e for j, e in self._used_tickets.items() if e > t}
        payload = verify(self._secret, ticket or "", "stream", t)
        if payload is None or payload.get("sid") != session_id:
            return False
        jti = str(payload.get("jti"))
        if jti in self._used_tickets:
            return False
        self._used_tickets[jti] = float(str(payload["exp"]))
        return True


def require_auth(request: Request) -> None:
    """FastAPI dependency: 401 unless the request carries a valid bearer token (when required)."""
    auth: AuthService = request.app.state.auth
    if not auth.is_authenticated(request):
        raise AppError(401, "unauthorized", "Sign in to use this API.")
