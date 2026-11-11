"""Persistent per-login revocation for the shared MCP administration GUI."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from typing import Any

from fastapi import HTTPException

from ..config.store import ConfigUnavailable, get_default_config_store


_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{32,128}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


class ConfigAdminSessions:
    """Use the existing private config volume; store only session ID hashes."""

    async def issue(self) -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def _identity(payload: dict[str, Any]) -> tuple[str, int] | None:
        session_id = payload.get("jti")
        expiry = payload.get("exp")
        if (not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id)
                or type(expiry) is not int or expiry <= int(time.time())):
            return None
        return hashlib.sha256(session_id.encode()).hexdigest(), expiry

    async def validate(self, payload: dict[str, Any], *, allow_revoked: bool = False) -> bool:
        identity = self._identity(payload)
        if identity is None:
            return False
        revoked = await get_default_config_store().get("admin_session_revocations", {})
        if (not isinstance(revoked, dict) or any(
                not isinstance(key, str) or not _DIGEST.fullmatch(key)
                or type(expiry) is not int for key, expiry in revoked.items())):
            raise HTTPException(503, "ADMIN_SESSION_STORE_UNAVAILABLE")
        return allow_revoked or identity[0] not in revoked

    async def revoke(self, payload: dict[str, Any]) -> None:
        identity = self._identity(payload)
        if identity is None:
            raise HTTPException(401, "Invalid or expired token")
        try:
            await get_default_config_store().revoke_admin_session(*identity)
        except (ConfigUnavailable, OSError, ValueError):
            raise HTTPException(503, "ADMIN_SESSION_STORE_UNAVAILABLE") from None
