"""Persistence for the tool switches.

Reads and writes the same ``tools`` section of ``config.json`` that
``mcp_admin_core.ConfigStore`` owns, so the Admin GUI and the MCP server
subprocess always agree on what is switched off.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

_DEFAULT: dict[str, Any] = {
    "disabled_categories": [],
    "disabled_tools": [],
    "disabled_operations": {},
    "readonly": False,
}

# The allow/deny blob the PermissionEditor page speaks. ``allowed_tools`` is an
# ALLOWLIST: missing/``None`` means "no allowlist at all", ``["*"]`` is the same
# thing spelled explicitly, and ``[]`` means "allow nothing" — which must fail
# closed, never fall back to the wildcard.
DEFAULT_PERMISSIONS: dict[str, Any] = {"allowed_tools": ["*"], "denied_tools": []}

# Pre-existing config files stored the tool switches under this key; nothing
# reads it any more. ``load()`` migrates it away so a hand-edited config.json
# cannot keep a second, dead copy of the disabled set next to the live one.
_LEGACY_DISABLED_KEY = "disabled"


def mask_secret(value: str | None) -> str:
    """Mask a secret for display, leaking at most four leading characters.

    Every path that echoes a stored secret back to the browser goes through
    here. A longer prefix — or any trailing fragment — is enough to recognise a
    key in a screenshot or a pasted support ticket, which is the whole point of
    masking. Values of 8 characters or fewer are hidden entirely.
    """
    text = str(value or "")
    if not text:
        return ""
    return f"{text[:4]}…" if len(text) > 8 else "…"


def _as_str_list(value: Any) -> list[str]:
    """Normalise a stored switch list. Never raises, never yields ``None``."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Mapping):
        return [str(key).strip() for key in value if str(key).strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _as_operations(value: Any) -> Any:
    """Normalise the operation gates into the mapping shape the GUI stores.

    The flat ``["tool:op", "op"]`` form is passed through untouched: an older
    config file may still carry it and
    ``woow_podman_mcp_server`` accepts both (Phase 2 adds the gate).
    """
    if not value:
        return {}
    if isinstance(value, Mapping):
        return {
            str(tool): [str(op).strip() for op in (ops or []) if str(op).strip()]
            for tool, ops in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return {}


def env_from_tool_settings(tools: dict[str, Any]) -> dict[str, str]:
    """Translate stored switches into the subprocess environment.

    ``mcp_admin_core.process`` only forwards the ``connection`` section, so the
    MCP server would otherwise never learn what the operator switched off.
    Merge this into the child environment when starting or restarting it.

    The keys use the ``PODMAN_MCP_`` prefix so they line up with
    ``woow_podman_mcp_server.server`` (which reads ``PODMAN_MCP_*`` directly in
    Phase 1, and a pydantic ``Settings`` with ``env_prefix="PODMAN_MCP_"`` from
    Phase 2 on).

    Every collection is serialised as JSON, never as CSV. pydantic-settings
    treats a ``list``/``dict`` field as *complex* and json-decodes the raw env
    value before any before-validator can run, so ``""`` (nothing disabled) and
    ``"podman_container_exec"`` both raised ``SettingsError`` at import time and
    the MCP child exited 1 — one toggle on the Tools page killed the operator's
    live connector. ``json.dumps([])`` is ``"[]"``, which always decodes.
    """
    settings = {**_DEFAULT, **(tools or {})}
    return {
        "PODMAN_MCP_READONLY": "true" if settings.get("readonly") else "false",
        "PODMAN_MCP_DISABLED_CATEGORIES": json.dumps(
            _as_str_list(settings.get("disabled_categories"))
        ),
        "PODMAN_MCP_DISABLED_TOOLS": json.dumps(
            _as_str_list(settings.get("disabled_tools"))
        ),
        "PODMAN_MCP_DISABLED_OPERATIONS": json.dumps(
            _as_operations(settings.get("disabled_operations"))
        ),
    }


class ToolConfigStore:
    """File-backed store for the ``tools`` section."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def _read_all(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def load(self) -> dict[str, Any]:
        """Current tool settings, merged over defaults so new keys appear.

        Values are coerced on the way out (a CSV string left behind by an older
        build, ``None`` where a list belongs) so neither the gate nor the env
        serialiser ever sees a shape it cannot handle.
        """
        stored = dict(self._read_all().get("tools", {}) or {})

        legacy = stored.pop(_LEGACY_DISABLED_KEY, None)
        if legacy and not stored.get("disabled_tools"):
            stored["disabled_tools"] = _as_str_list(legacy)

        settings = {**_DEFAULT, **stored}
        settings["disabled_categories"] = _as_str_list(settings.get("disabled_categories"))
        settings["disabled_tools"] = _as_str_list(settings.get("disabled_tools"))
        settings["disabled_operations"] = _as_operations(settings.get("disabled_operations"))
        settings["readonly"] = bool(settings.get("readonly"))
        return settings

    def save(self, tools: dict[str, Any]) -> dict[str, Any]:
        """Persist tool settings, leaving every other config section intact."""
        config = self._read_all()
        merged = {**self.load(), **tools}
        config["tools"] = merged
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(config, indent=2), "utf-8")
        return merged
