"""JWT authentication middleware and login router for MCP Admin GUI."""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import jwt
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette._utils import get_route_path
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from ..config.store import get_default_config_store
from .login_throttle import LoginThrottle, client_key

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


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def create_token(
    admin_password: str, provided_password: str, *, session_generation: str,
    session_id: str | None = None,
) -> str | None:
    """Create a JWT if *provided_password* matches *admin_password*."""
    if not secrets.compare_digest(admin_password, provided_password):
        return None

    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": "admin",
        "session_generation": session_generation,
        "iat": now,
        "exp": now + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    if session_id is not None:
        payload["jti"] = session_id
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _verify_token(token: str) -> dict[str, Any] | None:
    """Decode and verify a JWT."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
    except RecursionError:
        # PyJWT 在驗簽前解析 header／payload JSON；極深巢狀的惡意 token 在舊版會丟
        # RecursionError 而變成 HTTP 500（GHSA-8wjv-2p76-3863）。2.15.x 已修，這裡再兜底，
        # 任何剖析階段的遞迴爆炸一律視為無效 token（401），不讓請求走到 500。
        return None


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


class AdminSessionPolicy(Protocol):
    """Optional product policy. Storage failures must raise HTTPException(503)."""

    async def issue(self) -> str: ...

    async def validate(self, payload: dict[str, Any], *, allow_revoked: bool = False) -> bool: ...

    async def revoke(self, payload: dict[str, Any]) -> None: ...


def request_token(request: Request) -> str | None:
    """One credential precedence for API reads, writes, streams and logout."""
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else None
    return token or request.cookies.get("mcp-admin-token") or request.query_params.get("token")


async def authenticate_request(request: Request, *, allow_revoked: bool = False) -> dict[str, Any]:
    token = request_token(request)
    if not token:
        raise HTTPException(401, "Authentication required")
    payload = _verify_token(token)
    if payload is None:
        raise HTTPException(401, "Invalid or expired token")
    generation = payload.get("session_generation")
    current_generation = await get_default_config_store().get("admin_session_generation")
    if (not isinstance(generation, str) or not generation
            or not isinstance(current_generation, str) or not current_generation
            or not secrets.compare_digest(generation.encode(), current_generation.encode())):
        raise HTTPException(401, "Invalid or expired token")
    policy = getattr(request.app.state, "admin_session_policy", None)
    if policy is not None and not await policy.validate(payload, allow_revoked=allow_revoked):
        raise HTTPException(401, "Invalid or expired token")
    return payload


class AuthMiddleware(BaseHTTPMiddleware):
    """Enforce JWT auth on ``/api/*`` routes (except login and MCP proxy)."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        path = get_route_path(request.scope)

        # Public: non-API routes, auth endpoints, MCP proxy, healthz
        if not path.startswith("/api/") or path.startswith("/api/auth/"):
            return await call_next(request)

        # Also skip MCP proxy paths (token validated by proxy itself)
        if path.startswith("/private_"):
            return await call_next(request)

        try:
            payload = await authenticate_request(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

        request.state.user = payload
        return await call_next(request)


# ---------------------------------------------------------------------------
# Login router
# ---------------------------------------------------------------------------


class _LoginRequest(BaseModel):
    password: str


login_router = APIRouter(prefix="/api/auth", tags=["auth"])
logout_router = APIRouter(prefix="/api/auth", tags=["auth"])


def _throttle(request: Request) -> LoginThrottle:
    """app 層級的限流器（create_app 建立；沒經 create_app 組裝時退回環境變數預設並掛上去）。"""
    throttle = getattr(request.app.state, "login_throttle", None)
    if throttle is None:
        throttle = request.app.state.login_throttle = LoginThrottle.from_env()
    return throttle


@login_router.post("/login")
async def login(body: _LoginRequest, request: Request) -> JSONResponse:
    """Authenticate with admin password from config store."""
    throttle = _throttle(request)
    key = client_key(request)
    retry_after = throttle.begin(key)  # 先預記一次失敗再驗證（平行猛打也擋得住）
    if retry_after:
        # 鎖定期間連正確密碼也不放行，否則鎖定就拖不慢猜密碼的速度
        return JSONResponse(
            {"detail": "Too many failed login attempts. Try again later."},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    store = get_default_config_store()
    admin_password, generation = await store.get_admin_auth()

    if not admin_password:
        raise HTTPException(status_code=500, detail="Admin password not configured")

    token = create_token(
        admin_password, body.password, session_generation=generation
    )
    if token is None:
        raise HTTPException(status_code=401, detail="Invalid password")
    policy = getattr(request.app.state, "admin_session_policy", None)
    if policy is not None:
        token = create_token(
            admin_password, body.password, session_generation=generation,
            session_id=await policy.issue(),
        )
    throttle.succeed(key)

    response = JSONResponse({"token": token})
    response.set_cookie(
        key="mcp-admin-token",
        value=token,
        httponly=True,
        samesite="strict",
        max_age=JWT_EXPIRY_HOURS * 3600,
    )
    return response


@logout_router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """Revoke this login on the server and remove its browser cookie."""
    payload = await authenticate_request(request, allow_revoked=True)
    policy = getattr(request.app.state, "admin_session_policy", None)
    if policy is None:
        raise HTTPException(503, "ADMIN_SESSION_STORE_UNAVAILABLE")
    await policy.revoke(payload)
    response = JSONResponse({"logged_out": True})
    response.delete_cookie("mcp-admin-token")
    return response
