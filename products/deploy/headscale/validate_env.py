"""Validate Quadlet public settings using the same Origin grammar as Headscale."""

from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path
import runpy


def validate(bind: str, port: str, origins: str, secure_cookie: str, cidrs: str) -> None:
    try:
        address = ipaddress.IPv4Address(bind)
    except ipaddress.AddressValueError as exc:
        raise ValueError("MCP_ADMIN_BIND must be one IPv4 address") from exc
    if address.is_unspecified or address.is_multicast:
        raise ValueError("MCP_ADMIN_BIND cannot publish to every network")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("MCP_ADMIN_PORT must be 1..65535")
    if secure_cookie not in ("true", "false"):
        raise ValueError("HEADSCALE_MCP_SECURE_COOKIE must be true or false")
    if not origins:
        raise ValueError("HEADSCALE_MCP_ALLOWED_ORIGINS is required for the GUI")

    parser = runpy.run_path(str(Path(__file__).resolve().parents[2] /
                                "apps/headscale/headscale_mcp_admin/origin_security.py"))
    normalize_origin = parser["normalize_origin"]
    for raw in origins.split(","):
        if not raw or raw != raw.strip():
            raise ValueError("Origins must be exact, comma-separated values")
        scheme, host, origin_port = normalize_origin(raw)
        if secure_cookie == "true":
            if scheme != "https":
                raise ValueError("Secure cookies require HTTPS Origins")
        elif scheme != "http" or host not in ("127.0.0.1", "localhost") or origin_port != int(port):
            raise ValueError("insecure cookies are allowed only for the local HTTP GUI")

    for raw in cidrs.split(",") if cidrs else ():
        if not raw or raw != raw.strip():
            raise ValueError("CIDRs must be comma-separated without spaces")
        try:
            network = ipaddress.ip_network(raw, strict=True)
        except ValueError as exc:
            raise ValueError("invalid private destination CIDR") from exc
        if network.prefixlen == 0:
            raise ValueError("a default-route CIDR is not an allowed destination exception")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--origins", required=True)
    parser.add_argument("--secure-cookie", required=True)
    parser.add_argument("--cidrs", default="")
    args = parser.parse_args()
    validate(args.bind, args.port, args.origins, args.secure_cookie, args.cidrs)


if __name__ == "__main__":
    main()
