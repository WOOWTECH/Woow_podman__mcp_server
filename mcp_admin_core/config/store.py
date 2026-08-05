"""File-based configuration store for MCP Admin.

Reads and writes ``/data/config.json`` (path configurable via
``MCP_ADMIN_CONFIG`` env var).  All Admin API routers use this
store instead of K8s Secrets/ConfigMaps, making the application
portable to Podman, Docker, or bare-metal environments.

Plaintext at rest: there is no at-rest crypto.  The file is protected
by filesystem perms (chmod 600, seeded by the initContainer) and the
settings router masks any secret before echoing it to the browser.

The config file has this structure::

    {
      "admin_password": "...",
      "mcp_auth_token": "...",
      "connection": {
        "podman_uri": "unix:///run/podman/podman.sock",
        "podman_api_version": "v5.0.0"
      },
      "tools": {
        "readonly": false,
        "disabled_categories": [], "disabled_tools": [],
        "disabled_operations": {},
        "permissions": {"allowed_tools": ["*"], "denied_tools": []}
      },
      "mcp_server": { "command": "...", "port": 8000, "env": { ... } },
      "proxy": { "timeout": 86400 },
      "token_history": [{"masked": "abcd…", "rotated_at": "<ISO8601 UTC>"}]
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = "/data/config.json"


class ConfigCorruptError(RuntimeError):
    """Raised when the on-disk config exists but cannot be parsed.

    Deliberately fatal.  The litellm original logged a warning and fell back to
    ``_DEFAULT_CONFIG``, which silently reset ``admin_password`` to a known
    value and blanked ``mcp_auth_token`` — every connector breaks AND the
    console opens up, at the exact moment an operator is least likely to be
    watching.  Refusing to start is the safe failure.
    """


def mask_secret(value: str | None) -> str:
    """Mask a secret for display, leaking at most four leading characters.

    The canonical masking implementation for everything this package echoes
    back to the browser (``podman_mcp_admin.store.mask_secret`` is the same
    function for the product layer — the two MUST stay byte-identical, a GET
    masked one way and a PUT that compares against the other way would silently
    overwrite the stored secret with a mask).  A longer prefix — or any
    trailing fragment — is enough to recognise a key in a screenshot or a
    pasted support ticket, and the old ``a1****…**yz`` form additionally leaked
    the exact length.  Values of 8 characters or fewer are hidden entirely.
    """
    text = str(value or "")
    if not text:
        return ""
    return f"{text[:4]}…" if len(text) > 8 else "…"


# Default config seeded on first run.
#
# The ``tools`` section is the canonical shape the tool gate actually reads
# (``disabled_tools``/``disabled_categories``/``readonly``).  It used to be
# seeded as ``{"disabled": [], …}``, a key nothing reads, so an operator who
# copied the on-disk shape switched tools off that stayed fully callable.
#
# ``admin_password`` is deliberately absent here.  A default password baked
# into the shape is a default password in production; ``_ensure_file()``
# generates one instead (see ``_seed_admin_password``).
_DEFAULT_CONFIG: dict[str, Any] = {
    "admin_password": "",
    "mcp_auth_token": "",
    "connection": {},
    "tools": {
        "readonly": False,
        "disabled_categories": [],
        "disabled_tools": [],
        "disabled_operations": {},
        # ``allowed_tools`` is an ALLOWLIST: ``["*"]`` (or a missing key) means
        # "no allowlist"; ``[]`` means "allow nothing" and must fail closed.
        "permissions": {"allowed_tools": ["*"], "denied_tools": []},
    },
    "mcp_server": {
        "command": "",
        "args": [],
        "port": 8000,
        "env": {},
    },
    "proxy": {
        "timeout": 86400,
    },
    "token_history": [],
}


class ConfigStore:
    """Thread-safe, file-backed configuration store.

    All public methods are async so callers don't need to know
    whether the backing store is a file or a remote API.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path or os.environ.get("MCP_ADMIN_CONFIG", _DEFAULT_CONFIG_PATH))
        self._lock = asyncio.Lock()
        self._cache: dict[str, Any] | None = None
        self._ensure_file()

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _seed_admin_password() -> str:
        """Pick the initial admin password: ``ADMIN_PASSWORD`` env, else random.

        A generated password is logged exactly once, here, at creation time.
        It is never recoverable afterwards — the operator either reads it out
        of the first-boot logs or resets it by deleting the config file.
        """
        env = (os.environ.get("ADMIN_PASSWORD") or "").strip()
        if env:
            return env
        generated = secrets.token_urlsafe(16)
        logger.warning(
            "ADMIN_PASSWORD not set — generated one for first boot: %s "
            "(save it now; it is not logged again)",
            generated,
        )
        return generated

    def _ensure_file(self) -> None:
        """Create config file with defaults if it doesn't exist."""
        if self._path.exists():
            return
        seed = dict(_DEFAULT_CONFIG)
        seed["admin_password"] = self._seed_admin_password()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(seed, indent=2))
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        logger.info("Created default config at %s", self._path)

    def _read_sync(self) -> dict[str, Any]:
        """Read and parse the config file (synchronous).

        A missing file is recoverable (defaults).  A *corrupt* file is not:
        see ``ConfigCorruptError``.
        """
        try:
            raw = self._path.read_text()
        except FileNotFoundError as exc:
            logger.warning("Config read failed (%s), using defaults", exc)
            return dict(_DEFAULT_CONFIG)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error(
                "Config at %s is corrupt (%s). Refusing to fall back to defaults — "
                "that would reset admin_password and blank mcp_auth_token. "
                "Restore the file from backup, or delete it to re-seed.",
                self._path,
                exc,
            )
            raise ConfigCorruptError(f"{self._path}: {exc}") from exc
        if not isinstance(data, dict):
            logger.error("Config at %s is not a JSON object", self._path)
            raise ConfigCorruptError(f"{self._path}: top level is not an object")
        # Merge with defaults so new keys are always present
        merged = {**_DEFAULT_CONFIG, **data}
        for key in _DEFAULT_CONFIG:
            if isinstance(_DEFAULT_CONFIG[key], dict) and key in data and isinstance(data[key], dict):
                merged[key] = {**_DEFAULT_CONFIG[key], **data[key]}
        return merged

    def _write_sync(self, data: dict[str, Any]) -> None:
        """Write config to file atomically (synchronous).

        Write-then-rename via a sibling temp file.  A direct ``write_text`` can
        be interrupted (SIGKILL, OOM, full disk) with the file half-written;
        the next read then sees truncated JSON.  ``os.replace`` is atomic on
        the same filesystem, so a reader sees either the old file or the new
        one, never a fragment.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self._path)
        # Durability of the rename itself: fsync the containing directory.
        try:
            dir_fd = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def load(self) -> dict[str, Any]:
        """Load the full config, using cache if available."""
        if self._cache is not None:
            return self._cache
        async with self._lock:
            self._cache = await asyncio.to_thread(self._read_sync)
            return self._cache

    async def save(self, data: dict[str, Any]) -> None:
        """Write the full config and update cache."""
        async with self._lock:
            await asyncio.to_thread(self._write_sync, data)
            self._cache = data

    async def get(self, key: str, default: Any = None) -> Any:
        """Get a top-level config value."""
        cfg = await self.load()
        return cfg.get(key, default)

    async def put(self, key: str, value: Any) -> None:
        """Set a top-level config value and persist."""
        cfg = await self.load()
        cfg[key] = value
        await self.save(cfg)

    async def patch(self, key: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Merge *updates* into a top-level dict value and persist.

        Returns the merged value.
        """
        cfg = await self.load()
        current = cfg.get(key, {})
        if not isinstance(current, dict):
            current = {}
        current.update(updates)
        cfg[key] = current
        await self.save(cfg)
        return current

    async def reload(self) -> dict[str, Any]:
        """Force re-read from disk, clearing cache."""
        self._cache = None
        return await self.load()

    @property
    def path(self) -> Path:
        return self._path


# ------------------------------------------------------------------
# Singleton accessor
# ------------------------------------------------------------------

_instance: ConfigStore | None = None


def get_config_store() -> ConfigStore:
    """Return the global ConfigStore singleton."""
    global _instance  # noqa: PLW0603
    if _instance is None:
        _instance = ConfigStore()
    return _instance
