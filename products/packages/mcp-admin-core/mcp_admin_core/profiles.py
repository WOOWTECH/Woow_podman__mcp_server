"""Private connection profiles sharing one admin identity and product adapter.

The default profile is the pre-existing /data/config.json. Extra profiles have
their own ConfigStore and MCP child; the default store only indexes their safe
identifiers, display labels and loopback ports. The index is never a credential
store. An admin request selects a profile after JWT authentication, while MCP
requests select one solely from the validated URL token.
"""

from __future__ import annotations

import contextvars
import copy
import asyncio
import logging
import os
import re
import secrets
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config.store import ConfigStore, ConfigUnavailable, _active_store, fresh_config, get_default_config_store
from .launch_policy import get_launch_policy
from .process import McpProcessManager, _active_process, get_default_process_manager

DEFAULT_PROFILE = "default"
PROFILE_HEADER = "X-MCP-Profile"
# Six Odoo children measured about 513 MiB RSS. Keep enough room under the
# current 1 GiB PaaS pod limit for the admin process and product variation.
MAX_EXTRA_PROFILES = 5
FIRST_PROFILE_PORT = 18000
PROFILE_MEMORY_RESERVE_MIB = 256
_PROFILE_ID = re.compile(r"[a-f0-9]{24}\Z")
_PATH_TOKEN = re.compile(r"^/private_([^/]{1,256})(?:/|$)")
_current_profile: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_profile_id", default=DEFAULT_PROFILE)
_registry: ProfileRegistry | None = None
logger = logging.getLogger(__name__)


def current_profile_id() -> str:
    return _current_profile.get()


def _memory_limit_mib() -> int | None:
    """Read the container memory ceiling; None means no usable cgroup limit."""
    for path in (Path("/sys/fs/cgroup/memory.max"),
                 Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")):
        try:
            raw = path.read_text().strip()
            if raw == "max":
                return None
            mib = int(raw) // (1024 * 1024)
            return None if mib > 1024 * 1024 else mib
        except (OSError, ValueError):
            continue
    return None


def _validated_index(snapshot: dict) -> dict[str, dict]:
    index = snapshot.get("profile_index", {})
    if not isinstance(index, dict) or len(index) > MAX_EXTRA_PROFILES:
        raise ConfigUnavailable("Profile index unavailable")
    ports = {int((snapshot.get("mcp_server") or {}).get("port", 8000))}
    labels = {"default"}
    for profile_id, item in index.items():
        if (not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id)
                or not isinstance(item, dict) or set(item) != {"label", "port"}
                or not isinstance(item["label"], str) or not item["label"].strip()
                or len(item["label"]) > 80 or not isinstance(item["port"], int)
                or isinstance(item["port"], bool) or not 1024 <= item["port"] <= 65535
                or item["port"] in ports or item["label"].casefold() in labels):
            raise ConfigUnavailable("Profile index unavailable")
        ports.add(item["port"])
        labels.add(item["label"].casefold())
    return index


class ProfileRegistry:
    def __init__(self, default_store: ConfigStore):
        self.default_store = default_store
        self._token_assignment_lock = asyncio.Lock()
        self._stores: dict[str, ConfigStore] = {}
        self._managers: dict[str, McpProcessManager] = {}
        self._reclaimers: dict[str, object] = {}

    def _directory(self, profile_id: str | None = None) -> Path:
        base = self.default_store.path.parent / "profiles"
        for directory in (base, base / profile_id if profile_id else None):
            if directory is None:
                continue
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise ConfigUnavailable("Profile directory unavailable") from exc
            # Kubernetes fsGroup can widen directory permissions on PVC mount.
            # Tighten only an owned directory, through one no-follow fd, as
            # ConfigStore already does for 0600 files.
            try:
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    before = os.fstat(fd)
                    current = directory.lstat()
                    if (not stat.S_ISDIR(before.st_mode) or before.st_uid != os.geteuid()
                            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
                        raise ConfigUnavailable("Profile directory unavailable")
                    if stat.S_IMODE(before.st_mode) != 0o700:
                        os.fchmod(fd, 0o700)
                    after = os.fstat(fd)
                    if stat.S_IMODE(after.st_mode) != 0o700 or after.st_uid != before.st_uid:
                        raise ConfigUnavailable("Profile directory unavailable")
                finally:
                    os.close(fd)
            except OSError as exc:
                raise ConfigUnavailable("Profile directory unavailable") from exc
        return base if profile_id is None else base / profile_id

    def store_for(self, profile_id: str) -> ConfigStore:
        if profile_id == DEFAULT_PROFILE:
            return self.default_store
        if not _PROFILE_ID.fullmatch(profile_id):
            raise ConfigUnavailable("Profile index unavailable")
        if profile_id not in self._stores:
            path = self._directory(profile_id) / "config.json"
            # An indexed profile must already have a config. ConfigStore must not
            # silently seed a fresh, unprivileged file over missing state.
            if not path.is_file() or path.is_symlink():
                raise ConfigUnavailable("Profile configuration unavailable")
            self._stores[profile_id] = ConfigStore(path)
        return self._stores[profile_id]

    def manager_for(self, profile_id: str) -> McpProcessManager:
        if profile_id == DEFAULT_PROFILE:
            return get_default_process_manager()
        return self._managers.setdefault(profile_id, McpProcessManager())

    @contextmanager
    def activate(self, profile_id: str) -> Iterator[None]:
        store = self.store_for(profile_id)
        manager = self.manager_for(profile_id)
        store_token = _active_store.set(store)
        process_token = _active_process.set(manager)
        id_token = _current_profile.set(profile_id)
        try:
            yield
        finally:
            _current_profile.reset(id_token)
            _active_process.reset(process_token)
            _active_store.reset(store_token)

    async def index(self) -> dict[str, dict]:
        return copy.deepcopy(_validated_index(await self.default_store.load()))

    async def has(self, profile_id: str) -> bool:
        return profile_id == DEFAULT_PROFILE or profile_id in await self.index()

    async def list(self) -> list[dict]:
        snapshot = await self.default_store.load()
        index = _validated_index(snapshot)
        default_port = int((snapshot.get("mcp_server") or {}).get("port", 8000))
        rows = [{"id": DEFAULT_PROFILE, "label": "Default", "port": default_port, "is_default": True}]
        rows.extend({"id": key, **value, "is_default": False} for key, value in index.items())
        return rows

    def profile_limit(self) -> int:
        policy = get_launch_policy()
        if policy is None:
            return MAX_EXTRA_PROFILES
        limit = min(MAX_EXTRA_PROFILES, policy.max_extra_profiles)
        ceiling = _memory_limit_mib()
        if ceiling is not None:
            children = max(1, (ceiling - PROFILE_MEMORY_RESERVE_MIB) // policy.profile_child_memory_mib)
            limit = min(limit, max(0, children - 1))
        return limit

    async def may_start(self, profile_id: str) -> bool:
        if profile_id == DEFAULT_PROFILE:
            return True
        index = await self.index()
        return profile_id in list(index)[:self.profile_limit()]

    async def create(self, label: str) -> dict:
        label = label.strip()
        if not label or len(label) > 80:
            raise HTTPException(422, "Profile label must be 1–80 characters")
        async with self.default_store.transition():
            root = await self.default_store.load()
            index = _validated_index(root)
            policy = get_launch_policy()
            if len(index) >= self.profile_limit():
                raise HTTPException(409, "Profile limit reached")
            if label.casefold() in {"default", *(v["label"].casefold() for v in index.values())}:
                raise HTTPException(409, "Profile label already exists")
            occupied = {int((root.get("mcp_server") or {}).get("port", 8000)), *(v["port"] for v in index.values())}
            port = next((p for p in range(FIRST_PROFILE_PORT, 65536) if p not in occupied), None)
            if port is None:
                raise HTTPException(409, "No child port available")
            while True:
                profile_id = secrets.token_hex(12)
                path = self.default_store.path.parent / "profiles" / profile_id / "config.json"
                if profile_id not in index and not path.exists() and not path.is_symlink():
                    break
            directory = self._directory(profile_id)
            config = fresh_config()
            connection_keys = policy.connection_keys if policy else set((root.get("connection") or {}).keys())
            config["connection"] = {key: "" for key in connection_keys}
            config["mcp_server"] = copy.deepcopy(root.get("mcp_server") or {})
            config["mcp_server"]["port"] = port
            # A proxy bearer is a credential for the default child. New
            # profiles must not inherit it, even when they share the product.
            config["proxy"] = {"timeout": (root.get("proxy") or {}).get("timeout", 86400)}
            # n8n uses a second, loopback-only bearer credential between the
            # gateway and child. Give each child its own pair as well.
            child_env = config["mcp_server"].get("env") or {}
            if policy and "AUTH_TOKEN" in policy.env_keys and child_env.get("AUTH_TOKEN"):
                internal_token = secrets.token_urlsafe(32)
                child_env["AUTH_TOKEN"] = internal_token
                config["proxy"]["bearer_token"] = internal_token
            config["tools"] = copy.deepcopy(root.get("tools") or config["tools"])
            config["admin_password"] = secrets.token_urlsafe(32)
            config.pop("profile_index", None)
            store = ConfigStore(directory / "config.json")
            try:
                await store.update(config)
                from .routers.tokens import new_token
                await store.issue_initial_mcp_token(new_token)
                await self.default_store.transform(lambda cfg: {
                    **cfg, "profile_index": {**_validated_index(cfg), profile_id: {"label": label, "port": port}}
                })
            except BaseException:
                (directory / "config.json").unlink(missing_ok=True)
                directory.rmdir()
                raise
            self._stores[profile_id] = store
            return {"id": profile_id, "label": label, "port": port, "is_default": False}

    async def rename(self, profile_id: str, label: str) -> dict:
        label = label.strip()
        if profile_id == DEFAULT_PROFILE or not label or len(label) > 80:
            raise HTTPException(422, "Invalid profile or label")
        async with self.default_store.transition():
            index = await self.index()
            if profile_id not in index:
                raise HTTPException(404, "Profile not found")
            if label.casefold() in {"default", *(v["label"].casefold() for k, v in index.items() if k != profile_id)}:
                raise HTTPException(409, "Profile label already exists")
            await self.default_store.transform(lambda cfg: {
                **cfg, "profile_index": {**_validated_index(cfg), profile_id: {**index[profile_id], "label": label}}
            })
            return {"id": profile_id, "label": label, "port": index[profile_id]["port"], "is_default": False}

    async def delete(self, profile_id: str) -> None:
        if profile_id == DEFAULT_PROFILE:
            raise HTTPException(422, "Default profile cannot be deleted")
        async with self.default_store.transition():
            index = await self.index()
            if profile_id not in index:
                raise HTTPException(404, "Profile not found")
            try:
                with self.activate(profile_id):
                    try:
                        await self.store_for(profile_id).change_mcp_token(None, action="revoke")
                    except ConfigUnavailable:
                        # Index removal below is the authoritative revocation
                        # when this profile's file can no longer be read.
                        pass
                    await self.manager_for(profile_id).stop()
            except ConfigUnavailable:
                # Missing file: no token can resolve. Stop a child that may
                # still be running from an earlier in-memory configuration.
                await self.manager_for(profile_id).stop()
            await self.default_store.transform(lambda cfg: {
                **cfg, "profile_index": {k: v for k, v in _validated_index(cfg).items() if k != profile_id}
            })
            self._managers.pop(profile_id, None)
            self._stores.pop(profile_id, None)
            self._reclaimers.pop(profile_id, None)
            # n8n keeps a private SQLite catalog/history under this directory;
            # deleting only config.json would leave it behind and make DELETE
            # fail with "Directory not empty" after the index was removed.
            directory = self._directory(profile_id)
            try:
                shutil.rmtree(directory)
            except OSError:
                # The token and index are already gone. Preserve a successful
                # revocation response; an unreadable orphan cannot be routed.
                logger.warning("Could not remove an unindexed profile directory")

    async def resolve_token(self, supplied: str, *, strict: bool = False) -> str | None:
        index = await self.index()
        matches: list[str] = []
        for profile_id in (DEFAULT_PROFILE, *index):
            try:
                snapshot = await self.store_for(profile_id).load()
            except ConfigUnavailable:
                if profile_id == DEFAULT_PROFILE or strict:
                    raise
                # A damaged additional profile cannot authenticate. Preserve
                # the default and healthy profiles instead of failing all URLs.
                continue
            expected = snapshot.get("mcp_auth_token", "")
            revoked = snapshot.get("mcp_auth_revoked", False)
            if not revoked and expected and secrets.compare_digest(expected.encode(), supplied.encode()):
                matches.append(profile_id)
        if len(matches) > 1:
            raise ConfigUnavailable("Ambiguous profile credential")
        return matches[0] if matches else None

    async def assign_token(self, profile_id: str, token: str) -> None:
        """Serialize manual token claims across all profiles in this service."""
        async with self._token_assignment_lock:
            # A damaged profile may still own this token. Refuse assignments
            # until every indexed file can be checked, while public lookup
            # continues to serve healthy profiles.
            owner = await self.resolve_token(token, strict=True)
            if owner is not None and owner != profile_id:
                raise HTTPException(409, "MCP token is already assigned to another profile")
            await self.store_for(profile_id).change_mcp_token(token, action="set")

    def reclaimer_for(self, profile_id: str, default):
        if profile_id == DEFAULT_PROFILE or default is None:
            return default
        if profile_id not in self._reclaimers:
            self._reclaimers[profile_id] = default.fork(label_suffix=profile_id[:8])
        return self._reclaimers[profile_id]

    async def start_all(self) -> None:
        for profile_id in await self.index():
            try:
                with self.activate(profile_id):
                    if await self.ready_to_start(profile_id):
                        await self.manager_for(profile_id).start()
            except ConfigUnavailable:
                logger.warning("Skipping an unavailable MCP profile at startup")

    async def ready_to_start(self, profile_id: str) -> bool:
        """Do not spawn a child that the product policy says cannot start yet."""
        policy = get_launch_policy()
        if policy is None:
            return True
        connection = await self.store_for(profile_id).get("connection", {})
        return not policy.missing_connection(connection)

    async def stop_all(self) -> None:
        for profile_id in await self.index():
            try:
                with self.activate(profile_id):
                    await self.manager_for(profile_id).stop()
            except ConfigUnavailable:
                await self.manager_for(profile_id).stop()


def get_profile_registry() -> ProfileRegistry:
    global _registry  # noqa: PLW0603
    store = get_default_config_store()
    if _registry is None or _registry.default_store is not store:
        _registry = ProfileRegistry(store)
    return _registry


class ProfileContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/auth/") or path.startswith("/api/profiles"):
            return await call_next(request)
        registry = get_profile_registry()
        profile_id = DEFAULT_PROFILE
        if path.startswith("/api/"):
            profile_id = request.headers.get(PROFILE_HEADER, DEFAULT_PROFILE)
            if not await registry.has(profile_id):
                return JSONResponse({"detail": "Profile not found"}, status_code=404)
        elif path.startswith("/private_"):
            match = _PATH_TOKEN.match(path)
            if match:
                profile_id = await registry.resolve_token(match.group(1)) or DEFAULT_PROFILE
        else:
            return await call_next(request)
        with registry.activate(profile_id):
            request.state.profile_id = profile_id
            request.state.session_reclaimer = registry.reclaimer_for(
                profile_id, getattr(request.app.state, "session_reclaimer", None))
            return await call_next(request)
