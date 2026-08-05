"""JWT authentication middleware and login router for MCP Admin GUI."""

from __future__ import annotations

import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from ..config import get_config_store

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JWT configuration
# ---------------------------------------------------------------------------

JWT_SECRET: str = os.environ.get("JWT_SECRET", "")
if not JWT_SECRET:
    JWT_SECRET = secrets.token_urlsafe(32)
    logger.warning(
        "JWT_SECRET not set in environment; generated a random secret. "
        "Tokens will not survive restarts."
    )

JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = int(os.environ.get("JWT_EXPIRY_HOURS", "24"))

COOKIE_NAME = "mcp-admin-token"

# ---------------------------------------------------------------------------
# Session revocation
# ---------------------------------------------------------------------------
#
# The JWT is stateless, so "log out" on the client cannot invalidate anything —
# and the HttpOnly cookie the login sets is not even reachable from JS.  Every
# token carries the session epoch current when it was minted; bumping the epoch
# refuses all of them at once.  A counter rather than an "issued before"
# timestamp because ``iat`` has one-second granularity: a logout and the login
# that follows it can land in the same second, and a timestamp floor would
# either keep the dead token alive or kill the fresh one.
#
# It is deliberately global (there is exactly one admin account), so logging
# out — or changing the password — ends every outstanding session, including a
# cookie left behind on a shared workstation.
_SESSION_EPOCH: int = 0


def revoke_all_sessions() -> None:
    """Refuse every JWT issued so far (logout / password change)."""
    global _SESSION_EPOCH  # noqa: PLW0603
    _SESSION_EPOCH += 1


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def passwords_match(stored: str, provided: str) -> bool:
    """Constant-time password comparison that accepts any Unicode password.

    ``secrets.compare_digest`` raises TypeError on ``str`` arguments holding
    non-ASCII code points, which turned a single accented character in the
    login form into an unauthenticated 500 on the only public POST endpoint —
    and would have made the console permanently unusable had the admin password
    itself contained one.  Comparing UTF-8 bytes is still constant time.
    """
    return secrets.compare_digest(
        str(stored or "").encode("utf-8"), str(provided or "").encode("utf-8")
    )


def create_token(admin_password: str, provided_password: str) -> str | None:
    """Create a JWT if *provided_password* matches *admin_password*."""
    if not passwords_match(admin_password, provided_password):
        return None

    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": "admin",
        "iat": now,
        "exp": now + timedelta(hours=JWT_EXPIRY_HOURS),
        # Session epoch — see revoke_all_sessions().
        "ver": _SESSION_EPOCH,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _verify_token(token: str) -> dict[str, Any] | None:
    """Decode and verify a JWT, honouring logout / password-change revocation."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

    try:
        # Tokens minted before this build carry no "ver" and are treated as
        # epoch 0 — valid until the first logout, refused after it.
        epoch = int(payload.get("ver", 0))
    except (TypeError, ValueError):
        return None
    if epoch != _SESSION_EPOCH:
        return None
    return payload


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


class AuthMiddleware(BaseHTTPMiddleware):
    """Enforce JWT auth on ``/api/*`` routes (except login and MCP proxy)."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        path = request.url.path

        # Public: non-API routes, auth endpoints, MCP proxy, healthz
        if not path.startswith("/api/") or path.startswith("/api/auth/"):
            return await call_next(request)

        # Also skip MCP proxy paths (token validated by proxy itself)
        if path.startswith("/private_"):
            return await call_next(request)

        # Extract JWT from header, cookie, or query parameter
        # (EventSource/SSE cannot set headers, so token may come as ?token=...)
        token: str | None = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        if not token:
            token = request.cookies.get(COOKIE_NAME)
        if not token:
            token = request.query_params.get("token")

        if not token:
            return JSONResponse({"detail": "Authentication required"}, status_code=401)

        payload = _verify_token(token)
        if payload is None:
            return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)

        request.state.user = payload
        return await call_next(request)


# ---------------------------------------------------------------------------
# Login throttling
# ---------------------------------------------------------------------------
#
# The login endpoint is the only credential check in front of the Podman
# socket and the connector token, and it is published on the internet
# through the Cloudflare tunnel.  Twelve wrong guesses used to complete in a
# quarter of a second with no trace beyond a 127.0.0.1 access-log line.

_LOGIN_MAX_FAILURES = int(os.environ.get("ADMIN_LOGIN_MAX_FAILURES", "5"))
_LOGIN_WINDOW_SECONDS = 300.0
_LOGIN_BASE_LOCKOUT = 30.0
_LOGIN_MAX_LOCKOUT = 900.0
_LOGIN_MAX_TRACKED_IPS = 1024

# ip -> [failures, first_failure_ts, locked_until_ts]
_login_failures: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    """Best-effort source address (the tunnel terminates in-pod, so trust XFF)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _login_retry_after(ip: str) -> float:
    """Seconds the caller must wait, or 0 if it may try now."""
    entry = _login_failures.get(ip)
    if not entry:
        return 0.0
    now = time.monotonic()
    if now < entry[2]:
        return entry[2] - now
    if now - entry[1] > _LOGIN_WINDOW_SECONDS:
        _login_failures.pop(ip, None)
    return 0.0


def _record_login_failure(ip: str) -> None:
    now = time.monotonic()
    entry = _login_failures.get(ip)
    if entry is None or now - entry[1] > _LOGIN_WINDOW_SECONDS:
        entry = [0.0, now, 0.0]
        # Bound the table: a spoofed X-Forwarded-For could otherwise grow it
        # without limit.  Dropping the oldest entry only forgives old failures.
        if len(_login_failures) >= _LOGIN_MAX_TRACKED_IPS:
            oldest = min(_login_failures, key=lambda key: _login_failures[key][1])
            _login_failures.pop(oldest, None)
        _login_failures[ip] = entry
    entry[0] += 1
    if entry[0] >= _LOGIN_MAX_FAILURES:
        over = entry[0] - _LOGIN_MAX_FAILURES
        lockout = min(_LOGIN_BASE_LOCKOUT * (2 ** over), _LOGIN_MAX_LOCKOUT)
        entry[2] = now + lockout
        logger.warning(
            "Admin login locked out for %.0fs after %d failed attempts from %s",
            lockout,
            int(entry[0]),
            ip,
        )
    else:
        logger.warning("Failed admin login attempt %d from %s", int(entry[0]), ip)


def _clear_login_failures(ip: str) -> None:
    _login_failures.pop(ip, None)


def _cookie_secure(request: Request) -> bool:
    """Whether to mark the session cookie Secure.

    The console is published over HTTPS through the tunnel, so the default is
    "yes unless this very request arrived over plain HTTP" — otherwise the
    browser would happily send a 12-hour admin credential over http:// after a
    scheme downgrade.  ``ADMIN_COOKIE_SECURE`` forces the issue either way for
    local development.
    """
    override = os.environ.get("ADMIN_COOKIE_SECURE", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return (proto or request.url.scheme) == "https"


# ---------------------------------------------------------------------------
# Login router
# ---------------------------------------------------------------------------


class _LoginRequest(BaseModel):
    password: str


login_router = APIRouter(prefix="/api/auth", tags=["auth"])


@login_router.post("/login")
async def login(body: _LoginRequest, request: Request) -> JSONResponse:
    """Authenticate with admin password from config store."""
    ip = _client_ip(request)
    retry_after = _login_retry_after(ip)
    if retry_after > 0:
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts — try again later",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    store = get_config_store()
    admin_password = await store.get("admin_password", "")

    if not admin_password:
        raise HTTPException(status_code=500, detail="Admin password not configured")

    token = create_token(admin_password, body.password)
    if token is None:
        _record_login_failure(ip)
        raise HTTPException(status_code=401, detail="Invalid password")

    _clear_login_failures(ip)
    response = JSONResponse({"token": token})
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="strict",
        path="/",
        max_age=JWT_EXPIRY_HOURS * 3600,
    )
    return response


def _request_token(request: Request) -> str | None:
    """The credential this request carries, wherever it put it."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return request.cookies.get(COOKIE_NAME) or request.query_params.get("token")


@login_router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """End the session: clear the HttpOnly cookie and revoke issued tokens.

    Clearing the cookie is the half the SPA cannot do for itself (HttpOnly);
    the revocation floor is the half that matters for a JWT already copied out
    of localStorage.  Without both, clicking Logout showed a fresh login screen
    while the browser still held a live 12-hour admin session.

    The cookie is cleared unconditionally (an expired session must still be
    able to tidy up), but the *global* revocation only happens for a caller
    that presents a valid token — this route sits in the public ``/api/auth/``
    prefix, so an unauthenticated caller must not be able to log the operator
    out over and over.
    """
    token = _request_token(request)
    if token and _verify_token(token) is not None:
        revoke_all_sessions()
    response = JSONResponse({"status": "ok", "message": "Logged out"})
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        httponly=True,
        secure=_cookie_secure(request),
        samesite="strict",
    )
    return response
