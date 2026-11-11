"""Dashboard session cache scoped to one profile and credential set."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time

import httpx

from mcp_admin_core.profiles import current_profile_id

_CACHE_SECRET = secrets.token_bytes(32)
_cookie_cache: dict[tuple[str, str], tuple[str, float]] = {}


def _cache_key(url: str, username: str, password: str) -> tuple[str, str]:
    # A keyed digest keeps credentials out of the long-lived dictionary keys.
    identity = json.dumps([url, username, password], ensure_ascii=False, separators=(",", ":")).encode()
    fingerprint = hmac.new(_CACHE_SECRET, identity, hashlib.sha256).hexdigest()
    return current_profile_id(), fingerprint


async def dashboard_cookie(url: str, username: str, password: str) -> str:
    """Return this profile's Dashboard cookie, logging in when it is absent or expired."""
    key = _cache_key(url, username, password)
    now = time.time()
    cached = _cookie_cache.get(key)
    if cached and cached[1] > now:
        return cached[0]
    if cached:
        _cookie_cache.pop(key, None)

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{url.rstrip('/')}/auth/password-login",
            json={"provider": "basic", "username": username, "password": password},
        )
        response.raise_for_status()
        for header in response.headers.get_list("set-cookie"):
            if "hermes_session_at=" in header:
                cookie = header.split("hermes_session_at=")[1].split(";")[0].strip('"')
                _cookie_cache[key] = (cookie, time.time() + 600)
                return cookie
    return ""


def remove_profile(profile_id: str) -> None:
    """Discard saved Dashboard sessions when a profile is deleted."""
    for key in [key for key in _cookie_cache if key[0] == profile_id]:
        _cookie_cache.pop(key, None)
