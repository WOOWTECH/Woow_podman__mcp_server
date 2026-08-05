"""Seed ``config.json`` so a bare container boots into a working console.

Why this exists
---------------
``mcp_admin_core``'s ``_DEFAULT_CONFIG`` is product-agnostic, so it seeds
``mcp_server.command = ""`` and ``mcp_auth_token = ""``.  A container starting
on an empty volume therefore came up with:

* **no MCP child at all** — ``McpProcessManager.start()`` logs "No MCP server
  command configured — skipping start" and returns False, so the console is
  supervising nothing;
* **a proxy that 403s every request** — the path token is the credential, and
  an empty stored token fails closed.

The console still *looked* healthy: uvicorn bound, ``/healthz`` answered 200,
the login page rendered.  Only the connector was dead.  That is the worst
possible failure shape for something an operator points claude.ai at, and it
is invisible until they try to use it.

So this module runs BEFORE the app is built and fills in whatever is missing.
It is the difference between "a repo you can deploy" and "a repo you can
deploy after reading the source to work out what to put in config.json".

Rules
-----
* **Only fills gaps.**  Every existing value wins — this is safe to run on
  every boot, which is exactly what happens with a PersistentVolumeClaim.
* **Writes atomically**, same write-then-rename as the runtime store, so a
  crash mid-seed cannot leave truncated JSON behind.
* **Runs synchronously, before** ``get_config_store()`` is ever called.  It
  deliberately does not touch the async store: ``asyncio.Lock`` binds to the
  first event loop that uses it, so seeding through the singleton with
  ``asyncio.run()`` would poison it for uvicorn's loop
  (``RuntimeError: ... is bound to a different event loop``).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from mcp_admin_core.config.store import _DEFAULT_CONFIG, ConfigStore

logger = logging.getLogger(__name__)

# The loopback port the supervised MCP child binds.  8000 matches
# mcp_admin_core's own default so the proxy and the child agree with no
# further configuration.
DEFAULT_CHILD_PORT = 8000

# Connection keys the child understands, and the environment variables the
# container is configured with.  ``mcp_admin_core.process`` upper-cases the
# whole ``connection`` section into the child's environment, so what lands
# here IS what the child reads.
_CONNECTION_ENV = {
    "podman_uri": "PODMAN_URI",
    "podman_api_version": "PODMAN_API_VERSION",
    "podman_tls_ca": "PODMAN_TLS_CA",
    "podman_tls_cert": "PODMAN_TLS_CERT",
    "podman_tls_key": "PODMAN_TLS_KEY",
}

# Gating/---limits the child reads directly from its environment.  These go in
# ``mcp_server.env`` rather than ``connection`` because they are not connection
# settings and the Tools page owns them from Phase 2 on.
_CHILD_ENV = ("PODMAN_MCP_PROFILE", "PODMAN_MCP_MAX_CHARS", "PODMAN_MCP_NAME_ALLOW")


def _child_argv(port: int) -> list[str]:
    """The command line for the supervised MCP child.

    ``-u`` is load-bearing: without unbuffered stdio the child's startup lines
    sit in a pipe buffer and the Logs page looks frozen for the first minute,
    which reads as "it didn't start".
    """
    return [
        "-u",
        "-m",
        "woow_podman_mcp_server.server",
        "--transport",
        "streamable-http",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--path",
        "/mcp",
    ]


def _seed_token() -> tuple[str, bool]:
    """The connector token: ``MCP_AUTH_TOKEN`` if given, else a fresh one.

    Returns ``(token, was_generated)``.  A generated token is logged once,
    because otherwise the operator has no way to reach the connector without
    opening the GUI and rotating — and rotating is exactly what they must NOT
    do if they are migrating an existing connector onto this deployment.
    """
    env = (os.environ.get("MCP_AUTH_TOKEN") or "").strip()
    if env:
        return env, False
    return secrets.token_urlsafe(32), True


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2, ensure_ascii=False))
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def build_seed(existing: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[str]]:
    """Return ``(config, filled)`` — *existing* with every gap filled in.

    ``filled`` names what this call had to supply, so the caller can log a
    one-line summary instead of a diff nobody reads.
    """
    cfg: dict[str, Any] = json.loads(json.dumps(_DEFAULT_CONFIG))  # deep copy
    for key, value in (existing or {}).items():
        cfg[key] = value
    filled: list[str] = []

    if not cfg.get("admin_password"):
        cfg["admin_password"] = ConfigStore._seed_admin_password()  # noqa: SLF001
        filled.append("admin_password")

    if not cfg.get("mcp_auth_token"):
        token, generated = _seed_token()
        cfg["mcp_auth_token"] = token
        filled.append("mcp_auth_token(generated)" if generated else "mcp_auth_token(env)")
        if generated:
            logger.warning(
                "MCP_AUTH_TOKEN not set — generated one: %s "
                "(the connector URL is /private_%s/mcp/ ; it is not logged again)",
                token,
                token,
            )

    connection = dict(cfg.get("connection") or {})
    for key, env_name in _CONNECTION_ENV.items():
        value = (os.environ.get(env_name) or "").strip()
        if value and not connection.get(key):
            connection[key] = value
            filled.append(f"connection.{key}")
    if not connection.get("podman_uri"):
        connection["podman_uri"] = "unix:///run/podman/podman.sock"
        filled.append("connection.podman_uri(default)")
    cfg["connection"] = connection

    mcp_server = dict(cfg.get("mcp_server") or {})
    try:
        port = int(mcp_server.get("port") or DEFAULT_CHILD_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_CHILD_PORT
    mcp_server["port"] = port
    if not mcp_server.get("command"):
        # sys.executable, not "python": in the image the console runs under a
        # specific interpreter and the child must be the same one, or it
        # imports a different site-packages and fails on `mcp`.
        mcp_server["command"] = sys.executable or "python3"
        mcp_server["args"] = _child_argv(port)
        filled.append("mcp_server.command")
    child_env = dict(mcp_server.get("env") or {})
    for name in _CHILD_ENV:
        value = (os.environ.get(name) or "").strip()
        if value and not child_env.get(name):
            child_env[name] = value
            filled.append(f"mcp_server.env.{name}")
    mcp_server["env"] = child_env
    cfg["mcp_server"] = mcp_server

    return cfg, filled


def ensure_seeded(path: str | Path | None = None) -> Path:
    """Fill in whatever ``config.json`` is missing.  Idempotent."""
    target = Path(path or os.environ.get("MCP_ADMIN_CONFIG", "/data/config.json"))

    existing: dict[str, Any] | None = None
    if target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # Do NOT overwrite: a corrupt file is a restore-from-backup
            # situation, and rewriting it here would destroy the only copy of
            # the admin password and the connector token.  The store raises
            # ConfigCorruptError for the same reason.
            logger.error("Config at %s exists but is unreadable (%s) — leaving it alone", target, exc)
            return target
        existing = loaded if isinstance(loaded, dict) else None
        if existing is None:
            logger.error("Config at %s is not a JSON object — leaving it alone", target)
            return target

    cfg, filled = build_seed(existing)
    if not filled and existing is not None:
        return target

    _atomic_write(target, cfg)
    logger.info(
        "Seeded %s (%s)", target, ", ".join(filled) if filled else "created with defaults"
    )
    return target
