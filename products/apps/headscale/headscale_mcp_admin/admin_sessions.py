"""Persistent, per-login revocation. Only hashes and expiry are retained."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager

import anyio
from fastapi import HTTPException
from starlette._utils import get_route_path
from starlette.requests import Request
from starlette.responses import JSONResponse
from mcp_admin_core.auth.middleware import authenticate_request
from mcp_admin_core.config.store import get_default_config_store
from mcp_admin_core.config.store import ConfigUnavailable

logger = logging.getLogger(__name__)
_JTI = re.compile(r"[0-9a-f]{32}\.[A-Za-z0-9_-]{43}")


class AdminSessions:
    def __init__(self, path: Path | None = None):
        self._path = path
        self._initialized: set[Path] = set()
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        # /data/admin-sessions.sqlite3 by default; custom config locations follow suit.
        return self._path or get_default_config_store().path.with_name("admin-sessions.sqlite3")

    @contextmanager
    def _connect(self, path):
        # Never silently recreate a database that vanished during serving.
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=1)
        try:
            with db:
                yield db
        finally:
            db.close()

    async def _run(self, operation, *args):
        try:
            return await asyncio.to_thread(operation, *args)
        except (OSError, sqlite3.Error, ValueError):
            raise HTTPException(503, "ADMIN_SESSION_STORE_UNAVAILABLE") from None

    def _epoch(self, db) -> str:
        row = db.execute("SELECT epoch FROM session_state WHERE id = 1").fetchone()
        if row is None or not isinstance(row[0], str) or not re.fullmatch(r"[0-9a-f]{32}", row[0]):
            raise ValueError("Invalid session database")
        return row[0]

    def _initialize(self, path):
        with self._lock:
            if path in self._initialized:
                return
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                created = False
            else:
                os.close(fd)
                created = True
            with self._connect(path) as db:
                if created:
                    db.execute("CREATE TABLE session_state (id INTEGER PRIMARY KEY CHECK (id = 1), epoch TEXT NOT NULL)")
                    db.execute("INSERT INTO session_state VALUES (1, ?)", (secrets.token_hex(16),))
                    db.execute("CREATE TABLE revoked_sessions (jti_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)")
                    db.execute("CREATE INDEX revoked_expiry ON revoked_sessions (expires_at)")
                self._epoch(db)
                db.execute("DELETE FROM revoked_sessions WHERE expires_at <= ?", (int(time.time()),))
            self._initialized.add(path)

    async def initialize(self):
        await self._run(self._initialize, self.path)

    def _issue(self, path):
        with self._connect(path) as db:
            epoch = self._epoch(db)
            db.execute("DELETE FROM revoked_sessions WHERE expires_at <= ?", (int(time.time()),))
        return epoch + "." + secrets.token_urlsafe(32)

    async def issue(self) -> str:
        await self.initialize()
        return await self._run(self._issue, self.path)

    def _identity(self, payload):
        jti, expiry = payload.get("jti"), payload.get("exp")
        if (not isinstance(jti, str) or not _JTI.fullmatch(jti)
                or type(expiry) is not int or expiry <= time.time()):
            return None
        return jti[:32], hashlib.sha256(jti.encode()).hexdigest(), expiry

    def _validate(self, path, identity, allow_revoked):
        epoch, digest, _ = identity
        with self._connect(path) as db:
            if not secrets.compare_digest(epoch, self._epoch(db)):
                return False
            return allow_revoked or db.execute(
                "SELECT 1 FROM revoked_sessions WHERE jti_hash = ?", (digest,)
            ).fetchone() is None

    async def validate(self, payload, *, allow_revoked=False) -> bool:
        identity = self._identity(payload)
        return identity is not None and await self._run(self._validate, self.path, identity, allow_revoked)

    def _revoke(self, path, identity):
        epoch, digest, expiry = identity
        with self._connect(path) as db:
            if not secrets.compare_digest(epoch, self._epoch(db)):
                raise HTTPException(401, "Invalid or expired token")
            db.execute("DELETE FROM revoked_sessions WHERE expires_at <= ?", (int(time.time()),))
            db.execute("INSERT OR IGNORE INTO revoked_sessions VALUES (?, ?)", (digest, expiry))

    async def revoke(self, payload):
        identity = self._identity(payload)
        if identity is None:
            raise HTTPException(401, "Invalid or expired token")
        await self._run(self._revoke, self.path, identity)

    def _prune(self, path):
        with self._connect(path) as db:
            db.execute("DELETE FROM revoked_sessions WHERE expires_at <= ?", (int(time.time()),))

    async def prune(self):
        await self._run(self._prune, self.path)

    async def maintain(self):
        while True:
            await asyncio.sleep(300)
            try:
                await self.prune()
            except HTTPException:
                # Do not include database paths, JWTs or identifiers in logs.
                logger.warning("Admin session maintenance unavailable; authentication remains fail-closed")


class AdminSessionStreams:
    """Headscale-only stream guard; buffered/in-flight writes are not rolled back.

    Check before each SSE send and poll idle streams every second (plus DB I/O).
    Cancelling the ASGI application also closes generators blocked on upstream I/O
    or downstream backpressure. MCP streams use their separate shared-token lease.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not get_route_path(scope).startswith("/api/"):
            return await self.app(scope, receive, send)
        stream = anyio.Event()
        streaming = started = ended = False
        refusal = None

        async with anyio.create_task_group() as tasks:
            async def check():
                nonlocal refusal
                try:
                    await authenticate_request(Request(scope))
                except (HTTPException, ConfigUnavailable) as exc:
                    refusal = exc if isinstance(exc, HTTPException) else HTTPException(503, "ADMIN_SESSION_STORE_UNAVAILABLE")
                    tasks.cancel_scope.cancel()
                    return False
                return True

            async def guarded_send(message):
                nonlocal streaming, started, ended
                if message["type"] == "http.response.start":
                    headers = dict(message.get("headers", []))
                    streaming = (headers.get(b"content-type", b"").split(b";", 1)[0] == b"text/event-stream"
                                 and "user" in scope.get("state", {}))
                    if streaming:
                        stream.set()
                        if not await check():
                            return
                    started = True
                elif streaming and message["type"] == "http.response.body":
                    if not await check():
                        return
                    ended = not message.get("more_body", False)
                await send(message)

            async def watch():
                await stream.wait()
                while True:
                    await anyio.sleep(1)
                    if not await check():
                        return

            tasks.start_soon(watch)
            try:
                await self.app(scope, receive, guarded_send)
            finally:
                tasks.cancel_scope.cancel()
        if refusal is not None:
            if not started:
                await JSONResponse({"detail": refusal.detail}, status_code=refusal.status_code)(scope, receive, send)
            elif not ended:
                # Headers have already been sent; finish without any further data.
                with anyio.move_on_after(1):
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
