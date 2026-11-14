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
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from fastapi import HTTPException
from starlette._utils import get_route_path
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import child_identity
from .config.store import ConfigStore, ConfigUnavailable, _active_store, fresh_config, get_default_config_store
from .configuration import OperationRefused
from .launch_policy import coerce_port, get_launch_policy
from .log_redaction import PathTokenRedactionFilter
from .owner_defaults import _StdoutHandler
from .process import McpProcessManager, _active_process, get_default_process_manager

DEFAULT_PROFILE = "default"
PROFILE_HEADER = "X-MCP-Profile"
# Six Odoo children measured about 513 MiB RSS. Keep enough room under the
# current 1 GiB PaaS pod limit for the admin process and product variation.
MAX_EXTRA_PROFILES = 5
FIRST_PROFILE_PORT = 18000
PROFILE_MEMORY_RESERVE_MIB = 256
#: The same "token on more than one profile" warning is written at most once per this many seconds per profile set
#: (every request with that URL hits it).
AMBIGUOUS_TOKEN_LOG_SECONDS = 60.0
_PROFILE_ID = re.compile(r"[a-f0-9]{24}\Z")
_PATH_TOKEN = re.compile(r"^/private_([^/]{1,256})(?:/|$)")
_current_profile: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_profile_id", default=DEFAULT_PROFILE)
_registry: ProfileRegistry | None = None
_ambiguous_token_logged: dict[tuple[str, ...], float] = {}
logger = logging.getLogger(__name__)
if not any(isinstance(handler, _StdoutHandler) for handler in logger.handlers):
    # Same as connection_apply: profile-index and duplicate-token warnings must reach kubectl logs, not only the
    # Logs page ring buffer (which another profile's traffic can evict). Masked like every other stdout line.
    _handler = _StdoutHandler()
    _handler.setLevel(logging.WARNING)
    _handler.addFilter(PathTokenRedactionFilter())
    logger.addHandler(_handler)


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


def _index_entries(snapshot: dict) -> dict[str, dict]:
    """The ``profile_index`` with every entry structurally valid (safe id, label, port); ports may still collide.

    Runtime reads use this: a port the index assigns twice, or the default profile's port, is not a reason to stop
    the whole service (2026-11-13 mp-odoo-res R6b: one hand-edited port made startup fail, CrashLoopBackOff for every
    profile). :func:`_port_collisions` names the profiles involved; they are refused at spawn
    (:meth:`ProfileRegistry.port_conflict`) and listed with ``disabled_reason`` ``port_conflict``; their tokens still
    select only themselves. Restore validation keeps the strict :func:`_validated_index`.
    """
    index = snapshot.get("profile_index", {})
    if not isinstance(index, dict) or len(index) > MAX_EXTRA_PROFILES:
        raise ConfigUnavailable("Profile index unavailable")
    labels = {"default"}
    for profile_id, item in index.items():
        if (not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id)
                or not isinstance(item, dict) or set(item) != {"label", "port"}
                or not isinstance(item["label"], str) or not item["label"].strip()
                or len(item["label"]) > 80 or not isinstance(item["port"], int)
                or isinstance(item["port"], bool) or not 1024 <= item["port"] <= 65535
                or item["label"].casefold() in labels):
            raise ConfigUnavailable("Profile index unavailable")
        labels.add(item["label"].casefold())
    return index


def _port_collisions(index: dict[str, dict], default_port: int) -> dict[int, list[str]]:
    """Ports the index does not assign uniquely → the additional profiles holding them, in index order.

    A port of two or more additional profiles, or the default profile's own port. The default profile is never
    listed: it is the pre-existing single profile and keeps its port; the additional profiles on it are refused.
    """
    holders: dict[int, list[str]] = {}
    for profile_id, item in index.items():
        holders.setdefault(item["port"], []).append(profile_id)
    return {port: ids for port, ids in holders.items() if len(ids) > 1 or port == default_port}


def _validated_index(snapshot: dict) -> dict[str, dict]:
    """Strict form for restore validation: entries valid, every port unique and not the default profile's port.

    Same outcome (value or exception type) as before the runtime form was split off, checks in the same order.
    """
    index = snapshot.get("profile_index", {})
    if not isinstance(index, dict) or len(index) > MAX_EXTRA_PROFILES:
        raise ConfigUnavailable("Profile index unavailable")
    default_port = int((snapshot.get("mcp_server") or {}).get("port", 8000))
    index = _index_entries(snapshot)
    if _port_collisions(index, default_port):
        raise ConfigUnavailable("Profile index unavailable")
    return index


def _log_ambiguous_token(profile_ids: list[str]) -> None:
    """One WARNING (rate limited per profile set) for a URL token saved on more than one profile; never the token."""
    key = tuple(sorted(profile_ids))
    now = time.monotonic()
    last = _ambiguous_token_logged.get(key)
    if last is not None and now - last < AMBIGUOUS_TOKEN_LOG_SECONDS:
        return
    if len(_ambiguous_token_logged) >= 64:
        _ambiguous_token_logged.clear()
    _ambiguous_token_logged[key] = now
    logger.warning("An MCP URL token matches the saved token of more than one profile (%s): the request is refused "
                   "with 503 and routed to none of them. Rotate or revoke the token on all but one of these profiles "
                   "(the files were edited by hand or restored inconsistently). Repeats are suppressed for %ds.",
                   ", ".join(key), int(AMBIGUOUS_TOKEN_LOG_SECONDS))


@dataclass(frozen=True)
class ProfileCapacity:
    """How many additional profiles may run: the product's limit and, under a cgroup memory limit, the memory budget.

    ``memory_extra_limit`` is None without a usable cgroup limit or launch policy (then only ``policy_limit`` applies).
    """
    policy_limit: int
    memory_limit_mib: int | None = None
    child_memory_mib: int | None = None
    memory_extra_limit: int | None = None

    @property
    def limit(self) -> int:
        if self.memory_extra_limit is None:
            return self.policy_limit
        return min(self.policy_limit, self.memory_extra_limit)

    def required_memory_mib(self, extras: int) -> int | None:
        """Smallest memory limit that runs the default profile plus *extras* additional profiles."""
        if self.child_memory_mib is None:
            return None
        return PROFILE_MEMORY_RESERVE_MIB + (extras + 1) * self.child_memory_mib

    def disabled_reason(self, position: int) -> str | None:
        """Why the additional profile at 0-based index *position* is not started, or None (index order decides)."""
        if position >= self.policy_limit:
            return "profile_limit"
        if self.memory_extra_limit is not None and position >= self.memory_extra_limit:
            return "memory_capacity"
        return None

    def memory_detail(self) -> str:
        return (f"this service's memory limit ({self.memory_limit_mib} MiB) runs {self.memory_extra_limit} additional "
                f"profile(s) at {self.child_memory_mib} MiB per child plus {PROFILE_MEMORY_RESERVE_MIB} MiB reserve")


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
        if profile_id not in self._managers:
            self._managers[profile_id] = McpProcessManager(profile_id=profile_id)
        return self._managers[profile_id]

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
        return copy.deepcopy(_index_entries(await self.default_store.load()))

    async def has(self, profile_id: str) -> bool:
        return profile_id == DEFAULT_PROFILE or profile_id in await self.index()

    def _start_blocks(self, snapshot: dict, index: dict[str, dict]) -> dict[str, dict]:
        """Additional profiles whose child is not started, by id → ``{"disabled_reason": ...}`` (+ memory needed).

        The order of :meth:`McpProcessManager.start`: the profile limits first (``profile_limit``,
        ``memory_capacity``), then an index port that is not unique (``port_conflict``). Not listed: may start.
        """
        capacity = self.capacity()
        blocks: dict[str, dict] = {}
        for position, profile_id in enumerate(index):
            reason = capacity.disabled_reason(position)
            if reason == "memory_capacity":
                blocks[profile_id] = {"disabled_reason": reason,
                                      "required_memory_mib": capacity.required_memory_mib(position + 1)}
            elif reason is not None:
                blocks[profile_id] = {"disabled_reason": reason}
        default_port = coerce_port((snapshot.get("mcp_server") or {}).get("port", 8000))
        for ids in _port_collisions(index, default_port).values():
            for profile_id in ids:
                blocks.setdefault(profile_id, {"disabled_reason": "port_conflict"})
        return blocks

    async def list(self) -> list[dict]:
        """Profile rows; ``disabled_reason`` says why a child is not started (``memory_capacity``: the pod's memory
        limit is too small, ``required_memory_mib`` would run it; ``profile_limit``; ``port_conflict``), else None."""
        snapshot = await self.default_store.load()
        index = _index_entries(snapshot)
        default_port = int((snapshot.get("mcp_server") or {}).get("port", 8000))
        blocks = self._start_blocks(snapshot, index)
        rows = [{"id": DEFAULT_PROFILE, "label": "Default", "port": default_port, "is_default": True,
                 "disabled_reason": None}]
        rows.extend({"id": key, **value, "is_default": False, **blocks.get(key, {"disabled_reason": None})}
                    for key, value in index.items())
        return rows

    def capacity(self) -> ProfileCapacity:
        policy = get_launch_policy()
        if policy is None:
            return ProfileCapacity(MAX_EXTRA_PROFILES)
        policy_limit = min(MAX_EXTRA_PROFILES, policy.max_extra_profiles)
        ceiling = _memory_limit_mib()
        if ceiling is None:
            return ProfileCapacity(policy_limit, child_memory_mib=policy.profile_child_memory_mib)
        children = max(1, (ceiling - PROFILE_MEMORY_RESERVE_MIB) // policy.profile_child_memory_mib)
        return ProfileCapacity(policy_limit, ceiling, policy.profile_child_memory_mib, max(0, children - 1))

    def profile_limit(self) -> int:
        return self.capacity().limit

    async def capacity_refusal(self, profile_id: str) -> str | None:
        """Why *profile_id*'s child must not start because of the profile limits (log text without secrets), or None."""
        if profile_id == DEFAULT_PROFILE:
            return None
        index = await self.index()
        if profile_id not in index:
            return "the profile is not in the profile index"
        position = list(index).index(profile_id)
        capacity = self.capacity()
        reason = capacity.disabled_reason(position)
        if reason == "memory_capacity":
            return (f"disabled for lack of memory: {capacity.memory_detail()}, and this is additional profile "
                    f"{position + 1}; raise the memory limit to at least "
                    f"{capacity.required_memory_mib(position + 1)} MiB or delete another profile")
        if reason == "profile_limit":
            return (f"disabled: this product runs at most {capacity.policy_limit} additional profile(s), and this is "
                    f"additional profile {position + 1}")
        return None

    async def may_start(self, profile_id: str) -> bool:
        return await self.capacity_refusal(profile_id) is None

    async def create(self, label: str) -> dict:
        label = label.strip()
        if not label or len(label) > 80:
            raise HTTPException(422, "Profile label must be 1–80 characters")
        async with self.default_store.transition():
            root = await self.default_store.load()
            index = _index_entries(root)
            policy = get_launch_policy()
            capacity = self.capacity()
            if len(index) >= capacity.policy_limit:
                raise HTTPException(409, detail={
                    "code": "profile_limit_reached", "limit": capacity.policy_limit,
                    "message": (f"Profile limit reached: this product runs at most {capacity.policy_limit} additional "
                                f"profile(s). 已達此產品的額外帳號上限（{capacity.policy_limit} 個）。")})
            if capacity.memory_extra_limit is not None and len(index) >= capacity.memory_extra_limit:
                needed = capacity.required_memory_mib(len(index) + 1)
                raise HTTPException(409, detail={
                    "code": "profile_memory_insufficient", "limit": capacity.memory_extra_limit,
                    "memory_limit_mib": capacity.memory_limit_mib, "required_memory_mib": needed,
                    "message": (f"Not enough memory for another profile: {capacity.memory_detail()}; adding one needs a "
                                f"memory limit of at least {needed} MiB. 記憶體不足，無法再新增帳號：此服務的記憶體上限 "
                                f"{capacity.memory_limit_mib} MiB 只夠 {capacity.memory_extra_limit} 個額外帳號（每個子程式 "
                                f"{capacity.child_memory_mib} MiB，另保留 {PROFILE_MEMORY_RESERVE_MIB} MiB）；要再新增一個，"
                                f"記憶體上限至少要 {needed} MiB。")})
            if label.casefold() in {"default", *(v["label"].casefold() for v in index.values())}:
                raise HTTPException(409, "Profile label already exists")
            occupied = {int((root.get("mcp_server") or {}).get("port", 8000)), *(v["port"] for v in index.values())}
            # Never assign the admin listener's port or one a running child (older configuration) still uses.
            occupied |= child_identity.admin_listener_ports() | self._active_child_ports()
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
                    **cfg, "profile_index": {**_index_entries(cfg), profile_id: {"label": label, "port": port}}
                })
            except BaseException:
                (directory / "config.json").unlink(missing_ok=True)
                directory.rmdir()
                raise
            self._stores[profile_id] = store
            # Admitted above: within the limits and on a port no other profile has.
            return {"id": profile_id, "label": label, "port": port, "is_default": False, "disabled_reason": None}

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
                **cfg, "profile_index": {**_index_entries(cfg), profile_id: {**index[profile_id], "label": label}}
            })
            return next(row for row in await self.list() if row["id"] == profile_id)

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
                **cfg, "profile_index": {k: v for k, v in _index_entries(cfg).items() if k != profile_id}
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
            # Only a hand edit or an inconsistent restore gets here (assign_token refuses a second owner). Still fail
            # closed (503, no profile), but say why in the log: ConfigAvailabilityMiddleware answers without one.
            _log_ambiguous_token(matches)
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
                # Refused before any write: the admin gets the reason, not "outcome unknown" (2026-11-13 iso S7,
                # n8n N3, products a3b). Neither the value nor the other profile is named.
                raise OperationRefused(409, detail={
                    "code": "token_in_use",
                    "message": ("This MCP token is already used by another profile of this service; choose a "
                                "different value, or rotate to generate a new one. 這個 token 已由同一服務的另一個帳號"
                                "使用；請改用其他值，或用輪替產生新的 token。")})
            await self.store_for(profile_id).change_mcp_token(token, action="set")

    def _active_child_ports(self, exclude: McpProcessManager | None = None) -> set[int]:
        managers = (get_default_process_manager(), *self._managers.values())
        return {manager._active_launch[1] for manager in managers
                if manager is not exclude and manager.is_running and manager._active_launch is not None}

    async def port_conflict(self, profile_id: str, port: int, manager: McpProcessManager) -> str | None:
        """Why *profile_id*'s child must not use *port* because another managed listener claims it, or None.

        The index can assign one port twice, or the default profile's port, after a hand edit or restore
        (``_port_collisions``): every additional profile on such a port is refused, the default profile keeps its
        own. A profile's own config.json can also diverge from its assignment, and a running child may still use
        a port from an older configuration; all would put two profiles' children behind one loopback address.
        """
        try:
            snapshot = await self.default_store.load()
            index = _index_entries(snapshot)
        except ConfigUnavailable:
            if profile_id != DEFAULT_PROFILE:
                return "the profile index is unavailable, so the port's owner cannot be checked"
            snapshot, index = {}, {}
        if profile_id != DEFAULT_PROFILE:
            if (index.get(profile_id) or {}).get("port") != port:
                return "the profile's configured port differs from the port assigned to it"
            if coerce_port((snapshot.get("mcp_server") or {}).get("port", 8000)) == port:
                return "the default profile is assigned this port"
            others = [other for other, item in index.items() if other != profile_id and item["port"] == port]
            if others:
                return "the profile index assigns this port to another profile as well (" + ", ".join(others) + ")"
        if port in self._active_child_ports(exclude=manager):
            return "another profile's child is running on this port"
        return None

    def reclaimer_for(self, profile_id: str, default):
        if profile_id == DEFAULT_PROFILE or default is None:
            return default
        if profile_id not in self._reclaimers:
            self._reclaimers[profile_id] = default.fork(label_suffix=profile_id[:8])
        return self._reclaimers[profile_id]

    async def start_all(self) -> None:
        """Start every indexed profile. Each child decides for itself: limits, required connection, port refusal.

        An index port that is not unique stops only the profiles on it (one ERROR here, then each refusal),
        never the service: the default and every other profile start as usual.
        """
        snapshot = await self.default_store.load()
        index = _index_entries(snapshot)
        default_port = coerce_port((snapshot.get("mcp_server") or {}).get("port", 8000))
        for port, ids in _port_collisions(index, default_port).items():
            if port == default_port:
                where = "the default profile's own port 127.0.0.1:%s to profile(s) %s (the default profile keeps it)"
            else:
                where = "127.0.0.1:%s to more than one profile (%s)"
            logger.error("Profile index assigns " + where + "; the MCP children of those profiles are not started "
                         "until profile_index in the default config.json is corrected or one of them is deleted. "
                         "Every other profile starts normally.", port, ", ".join(ids))
        for profile_id in index:
            try:
                with self.activate(profile_id):
                    await self.manager_for(profile_id).start()
            except ConfigUnavailable:
                logger.warning("Skipping an unavailable MCP profile at startup [profile=%s]", profile_id)

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


def existing_profile_ids() -> frozenset[str] | None:
    """Profiles that exist now, for code that can neither await nor read files (the Logs page buffers).

    Reads the default store's last loaded snapshot without its lock and never creates the registry or a
    ConfigStore (that could write a fresh config.json). None when unknown: no registry or snapshot yet,
    or an index that is not a mapping.
    """
    registry = _registry
    if registry is None:
        return None
    try:
        index = registry.default_store.peek("profile_index", {})
    except LookupError:
        return None
    if not isinstance(index, dict):
        return None
    return frozenset((DEFAULT_PROFILE, *index))


class ProfileContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = get_route_path(request.scope)
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
