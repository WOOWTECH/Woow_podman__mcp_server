"""File-based configuration store for MCP Admin.

Reads and writes ``/data/config.json`` (path configurable via
``MCP_ADMIN_CONFIG`` env var).  All Admin API routers use this
store instead of K8s Secrets/ConfigMaps, making the application
portable to Podman, Docker, or bare-metal environments.

The config file has this structure::

    {
      "admin_password": "...",
      "mcp_auth_token": "...",
      "connection": { ... },
      "tools": { "disabled": [], "disabled_operations": {} },
      "mcp_server": { "command": "...", "port": 8000, "env": { ... } },
      "proxy": { "timeout": 86400 },
      "token_history": []
    }
"""

from __future__ import annotations

import asyncio
import copy
import contextvars
import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import time
import weakref
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = "/data/config.json"

# Default config seeded on first run
_DEFAULT_CONFIG: dict[str, Any] = {
    "admin_password": "admin",
    "mcp_auth_token": "",
    "mcp_auth_revoked": False,
    "mcp_auth_generation": "",
    "connection": {},
    "tools": {
        "disabled": [],
        "disabled_operations": {},
    },
    "mcp_server": {
        "command": "",
        "args": [],
        "port": 8000,
        "env": {},
        "env_prefix": "",
    },
    "proxy": {
        "timeout": 86400,
    },
    "token_history": [],
    "admin_session_revocations": {},
    "profile_index": {},
}


def fresh_config() -> dict[str, Any]:
    """本行程新建 config.json 的內容：共用預設＋app 宣告的全新安裝 env（``LaunchPolicy.fresh_env``）＋全開標記。

    負責人 2026-10-25 決定 MCP「預設全開」；app 在 LaunchPolicy 宣告寫入／破壞性／Secret 等開關的開啟值，
    這裡只在建立新檔時寫進 ``mcp_server.env``，並直接寫上 ``owner_defaults`` 標記（全新安裝不需遷移）。
    既有檔（含部署 seed 建立的）不經過這裡，由 ``owner_defaults.apply_owner_defaults`` 處理。
    """
    from ..launch_policy import get_launch_policy  # 延後 import：launch_policy 不依賴 config
    from ..owner_defaults import APPLIED_FRESH, MARKER, SOURCE_NEW_CONFIG, marker_value

    config = copy.deepcopy(_DEFAULT_CONFIG)
    policy = get_launch_policy()
    if policy is not None and policy.fresh_env:
        config["mcp_server"]["env"].update(dict(policy.fresh_env))
    config["tools"][MARKER] = marker_value(APPLIED_FRESH, SOURCE_NEW_CONFIG)
    return config


def never_had_mcp_token(cfg: dict[str, Any]) -> bool:
    """這份設定從來沒有過 MCP token：token 是空的、沒有被撤銷、也沒有任何 token 變更紀錄。

    ``change_mcp_token``（管理頁 rotate／set／revoke，2026-10-23 起）每一次都寫一筆 ``token_history`` 並換一個新的
    ``mcp_auth_generation``，所以兩者都空＝從來沒有發過、換過或撤銷過 token（本行程新建的 config.json、PaaS chart seed
    剛寫好的新服務——平台只 seed 管理密碼——或像正式站 266 那樣開通後從沒有人設過 token 的服務）。撤銷過的
    （``mcp_auth_revoked``）或留有任何 token 紀錄的（含舊版只在第二次輪替才記的紀錄）都不算，不會自動復活。

    撤銷旗標用真值判斷，與閘道（``mcp_access.AccessLease``）一致：閘道把任何真值都當成已撤銷，這裡就不能把它算成
    「從沒有過 token」再自動發一個、把旗標改回 False（審查第三輪）。``ConfigStore`` 讀檔時本來就拒絕非布林的
    ``mcp_auth_revoked``（整份設定 ConfigUnavailable），這是給直接拿 dict 呼叫的地方的防呆。
    """
    if cfg.get("mcp_auth_revoked"):
        return False
    if str(cfg.get("mcp_auth_token") or "").strip():
        return False
    return not cfg.get("token_history") and not str(cfg.get("mcp_auth_generation") or "").strip()


class ConfigUnavailable(RuntimeError):
    """Existing state cannot be read; never substitute factory credentials/policy."""


class ConfigAvailabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        try:
            return await call_next(request)
        except ConfigUnavailable:
            return JSONResponse({"detail": "Configuration state unavailable", "code": "state_unavailable"}, status_code=503)


class ConfigStore:
    """Thread-safe, file-backed configuration store.

    All public methods are async so callers don't need to know
    whether the backing store is a file or a remote API.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path or os.environ.get("MCP_ADMIN_CONFIG", _DEFAULT_CONFIG_PATH))
        self._lock = asyncio.Lock()
        # One application transition owns save + child replacement. Separate
        # from the short disk lock: child startup itself reads this store.
        self._transition_lock = asyncio.Lock()
        self._transition_owner = None
        self._revision_key = secrets.token_bytes(32)
        self._cache: dict[str, Any] | None = None
        self._mcp_access_events = weakref.WeakSet()
        self._ensure_file()

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------

    def _ensure_file(self) -> None:
        """不存在時以預設值建立 config 檔（權限 0600）。

        新建的檔＝全新安裝：除了共用預設，也帶上 app 宣告的 ``LaunchPolicy.fresh_env``（見 ``fresh_config``）。
        """
        try:
            # A dangling symlink is unavailable state, not a fresh installation.
            self._path.lstat()
            return
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ConfigUnavailable("Configuration state unavailable") from exc
        # 還原進行中（restore_marker）：設定目錄可能剛被清空、正要放進還原結果；不可補一份出廠設定（admin／admin）。
        # 看自己所在目錄的標記；profile 的 store（<設定目錄>/profiles/<id>/config.json）另看設定目錄的標記。
        from ..restore_marker import marker_path, restore_in_progress
        if restore_in_progress(self._path) or (
                marker_path().parent in self._path.parents and restore_in_progress()):
            raise ConfigUnavailable("Configuration state unavailable")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._atomic_write(json.dumps(fresh_config(), indent=2), create_only=True):
            logger.info("Created default config at %s", self._path)

    def _read_sync(self) -> dict[str, Any]:
        """Read and parse the config file (synchronous)."""
        try:
            data = json.loads(self._read_private_text())
            if not isinstance(data, dict):
                raise ValueError("config must be an object")
            for key, default in _DEFAULT_CONFIG.items():
                if key in data and not isinstance(data[key], type(default)):
                    raise ValueError("invalid config section")
            # Validate before product startup migrations can normalize/write it.
            # Local import avoids the policy module's ConfigUnavailable import cycle.
            from ..operation_policy import read_policy
            read_policy(data.get("tools", {}))
            if any(not isinstance(event, dict) or any(
                    not isinstance(value, str) for value in event.values())
                    for event in data.get("token_history", [])):
                raise ValueError("invalid token history")
            # Merge with defaults so new keys are always present
            merged = {**_DEFAULT_CONFIG, **data}
            for key in _DEFAULT_CONFIG:
                if isinstance(_DEFAULT_CONFIG[key], dict) and key in data and isinstance(data[key], dict):
                    merged[key] = {**_DEFAULT_CONFIG[key], **data[key]}
            return merged
        except (ValueError, OSError, ConfigUnavailable) as exc:
            logger.error("Configuration state unavailable (%s)", type(exc).__name__)
            raise ConfigUnavailable("Configuration state unavailable") from exc

    def _read_private_text(self) -> str:
        """掛載的 fsGroup 可能把 0600 擴成 0660；讀取前僅收緊本行程擁有的普通單連結檔。

        不改 bytes／owner，不跟隨最終路徑的 symlink；FIFO 不等待。檢查、chmod、讀取使用
        同一 fd，避免依路徑 chmod 到被換入的檔案。無法確認或收緊時交由 _read_sync 拒絕載入。
        """
        fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_uid != os.geteuid()):
                raise ConfigUnavailable("Configuration state unavailable")
            current = self._path.lstat()
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise ConfigUnavailable("Configuration state unavailable")
            if stat.S_IMODE(before.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            after = os.fstat(fd)
            if (stat.S_IMODE(after.st_mode) != 0o600 or after.st_nlink != 1
                    or (after.st_uid, after.st_gid) != (before.st_uid, before.st_gid)):
                raise ConfigUnavailable("Configuration state unavailable")
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as fh:
                return fh.read()
        finally:
            os.close(fd)

    def _write_sync(self, data: dict[str, Any]) -> None:
        """把 config 原子寫回檔案（權限 0600，同步）。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(json.dumps(data, indent=2, ensure_ascii=False))

    def _atomic_write(self, text: str, *, create_only: bool = False) -> bool:
        """以 0600 建立同目錄暫存檔寫入後 os.replace 原子換名。

        用 ``os.open(..., O_WRONLY|O_CREAT|O_EXCL, 0o600)`` 建檔，讓檔案
        自建立瞬間即為 0600（避免 ``write_text`` 後補 chmod 期間以 umask
        0644 落盤的窗口），並順帶取得原子寫語意（config.json 內含 kubeconfig
        全文，權限底線必須是 0600）。以 fd 設定精確權限，不在換名後對路徑 chmod。
        """
        tmp_path = self._path.with_name(f".{self._path.name}.tmp.{os.getpid()}.{os.urandom(4).hex()}")
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                # 即使 umask 拿掉 owner bits，寫入第一個 byte 前仍為精確 0600。
                os.fchmod(fh.fileno(), 0o600)
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            if create_only:
                try:
                    # Publish a complete seed only if the path is still absent.
                    os.link(tmp_path, self._path)
                except FileExistsError:
                    return False
            else:
                os.replace(str(tmp_path), str(self._path))
        finally:
            # link 成功或任何寫入失敗都不殘留暫存檔；replace 後已不存在。
            try:
                os.unlink(str(tmp_path))
            except OSError:
                pass
        # 換名本身也要落盤：fsync 所在目錄（best-effort；不支援目錄 fsync 的檔案系統略過）。
        try:
            dir_fd = os.open(str(self._path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
            finally:
                os.close(dir_fd)
        return True

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    def revision(self, snapshot: dict[str, Any], *, domain: str = "editable") -> str:
        """Opaque, process-local snapshot identity; never publish a credential hash.

        A new admin process deliberately invalidates old open editors. No schema
        migration, token rotation, or write occurs when computing a revision.
        """
        if domain == "editable":
            snapshot = {key: snapshot.get(key) for key in (
                "connection", "mcp_server", "tools", "proxy",
                "mcp_auth_token", "mcp_auth_revoked", "mcp_auth_generation",
                "admin_password", "admin_session_generation",
            )}
        payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        return hmac.new(self._revision_key, domain.encode() + b"\0" + payload, hashlib.sha256).hexdigest()

    @asynccontextmanager
    async def transition(self):
        """Serialize management transitions and child/watchdog lifecycle calls.

        Reentrant only for the owning asyncio task, never for its child tasks.
        This permits a save route to call restart(), which calls stop/start().
        """
        task = asyncio.current_task()
        if self._transition_owner is task:
            yield
            return
        async with self._transition_lock:
            self._transition_owner = task
            try:
                yield
            finally:
                self._transition_owner = None


    async def _load_locked(self) -> dict[str, Any]:
        """Load the cache while the caller holds ``_lock``."""
        if self._cache is None:
            self._cache = await asyncio.to_thread(self._read_sync)
        return self._cache

    async def _commit_locked(self, data: dict[str, Any]) -> None:
        """Finish disk/cache publication before a cancelled caller releases the lock."""
        writer = asyncio.create_task(asyncio.to_thread(self._write_sync, data))
        cancelled = False
        while True:
            try:
                await asyncio.shield(writer)
                break
            except asyncio.CancelledError:
                if writer.cancelled():
                    raise
                # The thread keeps running. Even repeated caller cancellation must
                # not release the lock while an older file write can still finish.
                cancelled = True
        previous = self._cache
        self._cache = data
        if previous is None or self._auth_identity(previous) != self._auth_identity(data):
            for event in self._mcp_access_events:
                event.set()
            self._mcp_access_events.clear()
        if cancelled:
            raise asyncio.CancelledError

    async def load(self) -> dict[str, Any]:
        """Return a snapshot; callers cannot mutate the live auth state."""
        async with self._lock:
            return copy.deepcopy(await self._load_locked())

    @staticmethod
    def _auth_identity(data):
        return (data.get("mcp_auth_token", ""), data.get("mcp_auth_revoked", False),
                data.get("mcp_auth_generation", ""))

    async def get_mcp_access(self):
        """Snapshot auth with the event invalidated only after durable publication."""
        async with self._lock:
            cfg = await self._load_locked()
            changed = asyncio.Event()
            self._mcp_access_events.add(changed)
            return self._auth_identity(cfg), changed

    @staticmethod
    def _token_transition(cfg: dict[str, Any], token: str | None, action: str) -> dict[str, Any]:
        """一次 token 轉換後的設定：token、撤銷旗標、新的 generation 與一筆（遮罩過的）紀錄。"""
        from mcp_admin_core.util.mask import mask_secret
        history = list(cfg.get("token_history", []))
        previous = cfg.get("mcp_auth_token", "")
        history.insert(0, {"action": action, "rotated_at": datetime.now(timezone.utc).isoformat(),
            "previous_token_masked": mask_secret(previous, prefix=4, suffix=4, min_len=8, max_stars=20) if previous else ""})
        return {**cfg, "mcp_auth_token": token or "",
            "mcp_auth_revoked": token is None, "mcp_auth_generation": secrets.token_urlsafe(32),
            "token_history": history[:5]}

    async def change_mcp_token(self, token: str | None, *, action: str) -> None:
        """One atomic token, revocation, generation and masked-history transition."""
        async with self._lock:
            cfg = await self._load_locked()
            await self._commit_locked(self._token_transition(cfg, token, action))

    async def issue_initial_mcp_token(self, make_token, *, action: str = "auto_issue") -> bool:
        """從來沒有過 MCP token 的設定（:func:`never_had_mcp_token`）發第一個 token；回傳有沒有發。

        判斷與寫入在同一把鎖內；寫入與管理頁輪替是同一個轉換（token_history 一筆、新的 generation）。*make_token*
        只在要發的時候才呼叫。撤銷過、設過或輪替過 token 的設定不動。
        """
        async with self._lock:
            cfg = await self._load_locked()
            if not never_had_mcp_token(cfg):
                return False
            await self._commit_locked(self._token_transition(cfg, make_token(), action))
            return True

    async def save(self, data: dict[str, Any]) -> None:
        """Restore general settings while retaining current auth and tool policy.

        Product migrations use patch() to add defaults/markers to their owned
        section; a stale whole-config restore must not remove those markers.
        """
        async with self._lock:
            current = await self._load_locked()
            updated = copy.deepcopy(data)
            # A generic whole-config save cannot undo a deliberate revocation or
            # reset auth generations or policy (including one-time migration
            # markers). Explicit management routes change tokens/policy separately.
            for key in ("mcp_auth_token", "mcp_auth_revoked", "mcp_auth_generation", "token_history", "tools",
                        "profile_index", "admin_session_revocations"):
                if key in current:
                    updated[key] = copy.deepcopy(current[key])
                else:
                    updated.pop(key, None)
            if updated.get("admin_password") != current.get("admin_password"):
                updated["admin_session_generation"] = secrets.token_urlsafe(32)
            elif current.get("admin_session_generation"):
                updated["admin_session_generation"] = current["admin_session_generation"]
            else:
                updated.pop("admin_session_generation", None)
            await self._commit_locked(updated)

    async def get(self, key: str, default: Any = None) -> Any:
        """Get a top-level config value from a snapshot."""
        cfg = await self.load()
        return cfg.get(key, default)

    def peek(self, key: str, default: Any = None) -> Any:
        """不拿鎖、不讀檔：最後一次載入或寫入完成的設定中 *key* 的複本；還沒載入過時拋 :class:`LookupError`。

        給不能等鎖的讀者用：``/healthz``（k8s probe 有逾時），而 :meth:`_commit_locked` 寫檔與 fsync 的整段時間都拿著鎖，
        Longhorn／iSCSI 卡住時可能卡很久。寫檔進行中時拿到的是寫入前的版本——快取在寫入完成後才換。要和其他讀寫序列化、
        要讀到最新的地方照舊用 :meth:`get`／:meth:`load`。
        """
        cfg = self._cache
        if cfg is None:
            raise LookupError("configuration has not been loaded yet")
        return copy.deepcopy(cfg.get(key, default))

    async def get_admin_auth(self) -> tuple[str, str]:
        """Atomically snapshot the password and its persisted session generation."""
        async with self._lock:
            cfg = await self._load_locked()
            generation = cfg.get("admin_session_generation")
            if not isinstance(generation, str) or not generation:
                generation = secrets.token_urlsafe(32)
                updated = {**cfg, "admin_session_generation": generation}
                await self._commit_locked(updated)
                cfg = updated
            return cfg.get("admin_password", ""), generation

    async def revoke_admin_session(self, digest: str, expires_at: int) -> None:
        """Persist one login revocation without affecting other admin sessions.

        Retain only unexpired hashes. The JWT itself never enters the config.
        This lock also prevents a concurrent management save from undoing logout.
        """
        if (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                or type(expires_at) is not int or expires_at <= int(time.time())):
            raise ValueError("invalid admin session identity")
        async with self._lock:
            cfg = await self._load_locked()
            previous = cfg.get("admin_session_revocations", {})
            if (not isinstance(previous, dict) or any(
                    not isinstance(k, str) or len(k) != 64 or any(c not in "0123456789abcdef" for c in k)
                    or type(v) is not int for k, v in previous.items())):
                raise ConfigUnavailable("Configuration state unavailable")
            now = int(time.time())
            current = {key: expiry for key, expiry in previous.items() if expiry > now}
            current[digest] = expires_at
            await self._commit_locked({**cfg, "admin_session_revocations": current})

    async def update(self, updates: dict[str, Any]) -> None:
        """Atomically update top-level values; a password write revokes sessions."""
        async with self._lock:
            cfg = await self._load_locked()
            updated = {**cfg, **copy.deepcopy(updates)}
            if "admin_password" in updates:
                updated["admin_session_generation"] = secrets.token_urlsafe(32)
            await self._commit_locked(updated)

    async def put(self, key: str, value: Any) -> None:
        """Set one top-level value without overwriting concurrent auth updates."""
        await self.update({key: value})

    async def transform(self, fn) -> bool:
        """在鎖內以 ``fn(目前設定的複本)`` 的回傳值原子取代整份設定；回 None 表示不改。回傳是否有寫入。

        給一次性的產品遷移用（例如 ``owner_defaults``）：讀、算、寫在同一把鎖內完成，寫入走同目錄暫存檔＋replace。
        讀不到現有設定時照舊拋 ``ConfigUnavailable``，``fn`` 不會被呼叫。
        """
        async with self._lock:
            cfg = await self._load_locked()
            updated = fn(copy.deepcopy(cfg))
            if updated is None:
                return False
            await self._commit_locked(updated)
            return True

    async def patch(self, key: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Atomically merge into a section while preserving current auth state."""
        async with self._lock:
            cfg = await self._load_locked()
            value = cfg.get(key, {})
            current = copy.deepcopy(value) if isinstance(value, dict) else {}
            current.update(copy.deepcopy(updates))
            updated = {**cfg, key: current}
            await self._commit_locked(updated)
            return copy.deepcopy(current)

    async def patch_connection_bound(self, updates: dict[str, Any], *,
                                     identity_keys: frozenset[str], make_token,
                                     before_rotate=None) -> tuple[dict[str, Any], bool]:
        """Atomically save a backend identity and revoke its old capability URL.

        Initial setup has no prior complete identity. A secret refresh for the
        same identity keeps the URL; an account/database/server rebind rotates
        an active URL in the same durable write as the new connection.
        """
        async with self._lock:
            cfg = await self._load_locked()
            previous = cfg.get("connection", {})
            previous = previous if isinstance(previous, dict) else {}
            merged = copy.deepcopy(previous)
            merged.update(copy.deepcopy(updates))
            rotated = bool(
                identity_keys
                and all(isinstance(previous.get(key), str) and previous[key].strip()
                        for key in identity_keys)
                and any(previous.get(key) != merged.get(key) for key in identity_keys)
                and cfg.get("mcp_auth_token") and not cfg.get("mcp_auth_revoked", False)
            )
            updated = {**cfg, "connection": merged}
            if rotated:
                replacement = make_token()
                if before_rotate is not None:
                    # Keep old-token lookups behind this lock until the old
                    # child is stopped. The new token cannot reach old creds.
                    await before_rotate()
                updated = self._token_transition(updated, replacement, "backend_identity_change")
            await self._commit_locked(updated)
            return copy.deepcopy(merged), rotated

    async def reload(self) -> dict[str, Any]:
        """Force re-read from disk, clearing cache."""
        async with self._lock:
            self._cache = None
            for event in self._mcp_access_events:
                event.set()
            self._mcp_access_events.clear()
            return copy.deepcopy(await self._load_locked())

    @property
    def path(self) -> Path:
        return self._path


# ------------------------------------------------------------------
# Singleton accessor
# ------------------------------------------------------------------

_instance: ConfigStore | None = None
_active_store: contextvars.ContextVar[ConfigStore | None] = contextvars.ContextVar("mcp_profile_store", default=None)


def get_default_config_store() -> ConfigStore:
    """Return the admin identity and legacy profile store, regardless of request context."""
    global _instance  # noqa: PLW0603
    if _instance is None:
        _instance = ConfigStore()
    return _instance


def get_config_store() -> ConfigStore:
    """Return the request's selected profile or the legacy default store."""
    return _active_store.get() or get_default_config_store()
