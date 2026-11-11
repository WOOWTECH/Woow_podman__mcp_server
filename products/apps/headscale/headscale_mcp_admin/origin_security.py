"""Deployment-owned Origin policy; request Host/forwarding headers confer no trust."""
import ipaddress
import os
import re
from functools import lru_cache

ENV = "HEADSCALE_MCP_ALLOWED_ORIGINS"
_ORIGIN = re.compile(r"(https?)://(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(?::([0-9]+))?", re.I)
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.I)


def normalize_origin(value: str) -> tuple[str, str, int]:
    match = _ORIGIN.fullmatch(value)
    if match is None:
        raise ValueError("Invalid Origin")
    scheme, host, port = match.groups()
    scheme, host = scheme.lower(), host.lower()
    if host.startswith("["):
        host = str(ipaddress.IPv6Address(host[1:-1]))
    elif len(host) > 253 or any(not _LABEL.fullmatch(label) for label in host.rstrip(".").split(".")):
        raise ValueError("Invalid Origin hostname")
    effective_port = int(port) if port else (443 if scheme == "https" else 80)
    if not 1 <= effective_port <= 65535:
        raise ValueError("Invalid Origin port")
    return scheme, host, effective_port


@lru_cache(maxsize=16)
def _parse_allowed(value: str) -> frozenset[tuple[str, str, int]]:
    if not value.strip():
        return frozenset()
    try:
        return frozenset(normalize_origin(item.strip()) for item in value.split(","))
    except ValueError:
        raise RuntimeError(f"{ENV} must contain comma-separated exact HTTP(S) origins") from None


def allowed_origins() -> frozenset[tuple[str, str, int]]:
    return _parse_allowed(os.environ.get(ENV, ""))


def origin_allowed(request) -> bool:
    allowed = allowed_origins()
    origins = request.headers.getlist("origin")
    if not origins:
        return True  # Native clients still require their normal authentication.
    if len(origins) != 1:
        return False
    try:
        return normalize_origin(origins[0]) in allowed
    except ValueError:
        return False
