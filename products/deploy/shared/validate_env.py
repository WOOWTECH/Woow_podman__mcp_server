"""Validate public bind and runtime settings before writing Quadlet units.

usage: validate_env.py BIND PORT
       validate_env.py --runtime KEY=VALUE...
"""

from __future__ import annotations

import ipaddress
import re
import sys

# Same grammar as mcp_admin_core.origin_security (_ORIGIN, _LABEL, normalize_origin): an
# invalid MCP_ADMIN_ALLOWED_ORIGINS or MCP_ADMIN_ORIGIN_MODE stops the core at startup.
# tests/test_deploy.py checks that both patterns still match the vendored core.
_ORIGIN = re.compile(r"(https?)://(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(?::([0-9]+))?", re.I | re.ASCII)
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.I | re.ASCII)
ORIGIN_MODES = ("report-only", "enforce")
_MEMORY = re.compile(r"([1-9][0-9]{0,5})([mg])")
MIN_MEMORY_MIB = 512


def validate(bind: str, port: str) -> None:
    try:
        address = ipaddress.IPv4Address(bind)
    except ipaddress.AddressValueError as exc:
        raise ValueError("MCP_ADMIN_BIND must be one IPv4 address") from exc
    if address.is_unspecified or address.is_multicast:
        raise ValueError("MCP_ADMIN_BIND cannot publish to every network")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("MCP_ADMIN_PORT must be 1..65535")


def memory_mib(value: str) -> int:
    match = _MEMORY.fullmatch(value)
    if match is None:
        raise ValueError("MCP_MEMORY_LIMIT must be a size such as 1g or 768m")
    mib = int(match[1]) * (1024 if match[2] == "g" else 1)
    if mib < MIN_MEMORY_MIB:
        raise ValueError(f"MCP_MEMORY_LIMIT must be at least {MIN_MEMORY_MIB}m")
    return mib


def _origin(value: str) -> None:
    match = _ORIGIN.fullmatch(value)
    if match is None:
        raise ValueError("MCP_ADMIN_ALLOWED_ORIGINS must list exact http(s) origins, comma-separated")
    scheme, host, port = match.groups()
    host = host.lower()
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError as exc:
            raise ValueError("MCP_ADMIN_ALLOWED_ORIGINS has an invalid IPv6 origin") from exc
    elif len(host) > 253 or any(not _LABEL.fullmatch(label) for label in host.split(".")):
        raise ValueError("MCP_ADMIN_ALLOWED_ORIGINS has an invalid host name")
    number = int(port) if port else (443 if scheme.lower() == "https" else 80)
    if not 1 <= number <= 65535:
        raise ValueError("MCP_ADMIN_ALLOWED_ORIGINS has an invalid port")


def _integer(values: dict[str, str], key: str, low: int, high: int) -> int | None:
    raw = values.get(key, "").strip()
    if not raw:
        return None
    if not raw.isdecimal() or not low <= int(raw) <= high:
        raise ValueError(f"{key} must be an integer from {low} to {high}")
    return int(raw)


def validate_runtime(values: dict[str, str]) -> None:
    memory_mib(values.get("MCP_MEMORY_LIMIT", "1g"))
    if values.get("MCP_ADMIN_ORIGIN_MODE", "") not in ("", *ORIGIN_MODES):
        raise ValueError("MCP_ADMIN_ORIGIN_MODE must be exactly report-only or enforce")
    origins = values.get("MCP_ADMIN_ALLOWED_ORIGINS", "")
    if origins.strip():
        for item in origins.split(","):
            _origin(item.strip())
    connections = _integer(values, "MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE", 2, 1024)
    _integer(values, "MCP_GATEWAY_MAX_STREAMS_PER_PROFILE", 1, (connections or 64) - 1)
    timeout = values.get("MCP_GATEWAY_POOL_TIMEOUT_SECONDS", "").strip()
    if timeout:
        try:
            seconds = float(timeout)
        except ValueError:
            seconds = 0.0
        if not 0 < seconds <= 300:
            raise ValueError("MCP_GATEWAY_POOL_TIMEOUT_SECONDS must be greater than 0 and at most 300")


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["--runtime"]:
            pairs = [item.split("=", 1) for item in argv[1:]]
            if any(len(pair) != 2 for pair in pairs):
                raise ValueError("--runtime takes KEY=VALUE arguments")
            validate_runtime(dict(pairs))
        else:
            validate(*argv)
    except (TypeError, ValueError) as exc:
        print(f"invalid setting: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
