"""Deployment-owned destinations; DNS validation occurs at the TCP seam.

HEADSCALE_MCP_ALLOWED_PRIVATE_CIDRS permits exact deployment-declared private
networks (prefer a service's /32 or /128). Loopback and link-local destinations
remain denied even if someone tries to put them in that allowlist. Local tests
use an injected transport or a reachable sandbox service IP, never a bypass.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import os
import re
import socket
import threading
from urllib.parse import urlsplit, urlunsplit

import httpcore
from httpcore._backends.anyio import AnyIOBackend
import httpx
from mcp_admin_core.util.ssrf import address_block_reason

from .errors import HeadscaleError

_PRIVATE = tuple(ipaddress.ip_network(x) for x in
                 ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "100.64.0.0/10"))
_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="headscale-dns")
_DNS_SLOTS = threading.BoundedSemaphore(4)


def normalize_base_url(value: str) -> str:
    """Accept only an origin or that origin's /api/v1 root, never a URL prefix."""
    try:
        if not isinstance(value, str) or value != value.strip() or not value or len(value) > 2048:
            raise ValueError()
        u = urlsplit(value)
        if (u.scheme not in ("http", "https") or not u.hostname or
                u.username is not None or u.password is not None or
                any(ord(c) <= 32 or ord(c) >= 127 for c in value) or
                any(c in value for c in ("%", "\\", "?", "#", "@")) or
                u.path not in ("", "/", "/api/v1", "/api/v1/") or u.port == 0):
            raise ValueError()
        host = u.hostname.lower()
        try:
            address = ipaddress.ip_address(host)
            host = f"[{address}]" if address.version == 6 else str(address)
        except ValueError:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host):
                raise ValueError()
            if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                   for label in host.split(".")):
                raise ValueError()
        return urlunsplit((u.scheme, host + (f":{u.port}" if u.port else ""), "", "", ""))
    except (ValueError, TypeError):
        raise HeadscaleError("HEADSCALE_INVALID_BASE_URL") from None


def deployment_private_cidrs() -> tuple[str, ...]:
    value = os.environ.get("HEADSCALE_MCP_ALLOWED_PRIVATE_CIDRS", "")
    if len(value) > 4096:
        raise HeadscaleError("HEADSCALE_INVALID_PRIVATE_ALLOWLIST")
    result = []
    try:
        for item in value.split(","):
            if not item.strip():
                continue
            network = ipaddress.ip_network(item.strip(), strict=True)
            if not any(network.version == parent.version and network.subnet_of(parent) for parent in _PRIVATE):
                raise ValueError()
            result.append(str(network))
    except ValueError:
        raise HeadscaleError("HEADSCALE_INVALID_PRIVATE_ALLOWLIST") from None
    return tuple(result)


class Destination:
    def __init__(self, base_url, allowed_private_cidrs=()):
        self.base_url = normalize_base_url(base_url)
        u = urlsplit(self.base_url)
        self.scheme, self.host = u.scheme, u.hostname
        self.port = u.port or (443 if u.scheme == "https" else 80)
        authorized = deployment_private_cidrs()
        # The constructor can narrow the deployment list; it cannot widen it.
        requested = tuple(allowed_private_cidrs) if allowed_private_cidrs else authorized
        if any(cidr not in authorized for cidr in requested):
            raise HeadscaleError("HEADSCALE_PRIVATE_ALLOWLIST_NOT_AUTHORIZED")
        self.networks = tuple(ipaddress.ip_network(cidr) for cidr in requested)
        try:
            ip = ipaddress.ip_address(self.host)
        except ValueError:
            pass
        else:
            self.check_address(ip)

    def check_address(self, address):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise HeadscaleError("HEADSCALE_DESTINATION_DENIED") from None
        # The permanent metadata/tunnel deny rules precede the deployment allowlist.
        # This calls only the shared immutable address classifier; it does not change
        # the deployment's global SSRF mode or weaken the checks below.
        if address_block_reason(str(ip), "permissive", host=self.host) is not None:
            raise HeadscaleError("HEADSCALE_DESTINATION_DENIED")
        if (getattr(ip, "ipv4_mapped", None) or ip.is_loopback or ip.is_link_local or
                ip.is_multicast or ip.is_unspecified or ip.is_reserved):
            raise HeadscaleError("HEADSCALE_DESTINATION_DENIED")
        if not ip.is_global and not any(ip.version == n.version and ip in n for n in self.networks):
            raise HeadscaleError("HEADSCALE_DESTINATION_DENIED")
        return str(ip)

    def check_host(self, host, port):
        if host != self.host or port != self.port:
            raise HeadscaleError("HEADSCALE_DESTINATION_DENIED")

    def answers(self):
        try:
            return [self.check_address(ipaddress.ip_address(self.host))]
        except ValueError as exc:
            if isinstance(exc, HeadscaleError):
                raise
        rows = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        if not rows or len(rows) > 64:
            raise HeadscaleError("HEADSCALE_DESTINATION_DENIED")
        addresses = []
        for family, kind, proto, _, sockaddr in rows:
            if (family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM or
                    proto != socket.IPPROTO_TCP or sockaddr[1] != self.port or
                    "%" in sockaddr[0] or len(sockaddr) > 3 and sockaddr[3] != 0):
                raise HeadscaleError("HEADSCALE_DESTINATION_DENIED")
            addresses.append(self.check_address(sockaddr[0]))
        return list(dict.fromkeys(addresses))


class PinnedNetwork(AnyIOBackend):
    def __init__(self, destination):
        self.destination = destination

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.destination.check_host(host, port)
        if local_address is not None or not _DNS_SLOTS.acquire(blocking=False):
            raise httpcore.ConnectError("HEADSCALE_DNS_UNAVAILABLE")
        try:
            pending = _DNS_POOL.submit(self.destination.answers)
        except BaseException:
            _DNS_SLOTS.release()
            raise
        pending.add_done_callback(lambda _: _DNS_SLOTS.release())
        future = asyncio.wrap_future(pending)
        # A timed-out lookup must not free its slot while its worker is still running.
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        budget = min(timeout if timeout is not None else 5.0, 5.0)
        try:
            async with asyncio.timeout(budget):
                addresses = await asyncio.shield(future)
                # Numeric IP only, with TLS SNI left as the original host by httpcore.
                return await super().connect_tcp(addresses[0], port, timeout=budget,
                                                 socket_options=socket_options)
        except TimeoutError:
            raise httpcore.ConnectTimeout("HEADSCALE_CONNECT_TIMEOUT") from None
        except OSError:
            raise httpcore.ConnectError("HEADSCALE_CONNECT_FAILED") from None


class HeadscaleTransport(httpx.AsyncHTTPTransport):
    def __init__(self, destination):
        # This private injection seam is reviewed for the explicitly pinned versions.
        if httpx.__version__ != "0.28.1" or httpcore.__version__ != "1.0.9":
            raise RuntimeError("Headscale transport dependency needs review")
        super().__init__(verify=True, trust_env=False, retries=0,
                         limits=httpx.Limits(max_connections=4, max_keepalive_connections=4))
        self._pool._network_backend = PinnedNetwork(destination)

