"""Validate public bind settings before writing Quadlet units."""

from __future__ import annotations

import ipaddress
import sys


def validate(bind: str, port: str) -> None:
    try:
        address = ipaddress.IPv4Address(bind)
    except ipaddress.AddressValueError as exc:
        raise ValueError("MCP_ADMIN_BIND must be one IPv4 address") from exc
    if address.is_unspecified or address.is_multicast:
        raise ValueError("MCP_ADMIN_BIND cannot publish to every network")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("MCP_ADMIN_PORT must be 1..65535")


if __name__ == "__main__":
    validate(*sys.argv[1:])
