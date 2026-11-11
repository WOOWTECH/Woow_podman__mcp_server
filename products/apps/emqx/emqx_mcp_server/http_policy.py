"""EMQX-only transport: fixed origin, validated DNS pinned to TCP, verified TLS.

The HTTPX/httpcore private pool seam is source-checked against the reviewed HA
transport versions. Address rules are the PaaS deployment's SSRF policy. No global
socket patch, environment proxy, redirect, transport retry, or credential logging.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import ipaddress
import logging
from pathlib import Path
import re
import socket
import threading
import time
from urllib.parse import unquote, unquote_to_bytes, urlsplit, urlunsplit

import httpcore
from httpcore._backends.anyio import AnyIOBackend
import httpx
from mcp_admin_core.util.ssrf import address_block_reason, check_url_syntax, current_mode

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_SOURCES = {
    'httpx/_transports/default.py': '03379a454c95c0271c4f2c8d25ec437f49f57457f3cf2769748420bf0ecf2e79',
    'httpcore/_async/connection_pool.py': '0ce210dacd9909ff6a7f0c61ccca6b4cf2ea08bf0ec465e2285eaa447c6f5726',
    'httpcore/_async/connection.py': 'e8e70f5ea3047dcd01537f3ffa21d4343775bca4937365154f4f57a8d6ff04e9',
    'httpcore/_backends/anyio.py': 'c7c3e01215d10bc6d5aac77393f609c7c63a77d4ee6f4e82b944b09db9adaa86',
}
_DNS_SLOTS = threading.BoundedSemaphore(4)
_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='emqx-dns')


class DestinationDenied(httpx.HTTPError):
    def __init__(self):
        super().__init__('EMQX_DESTINATION_DENIED')


class ResponseTooLarge(httpx.HTTPError):
    def __init__(self):
        super().__init__('EMQX_RESPONSE_TOO_LARGE')


# Request paths may keep a literal '%' (a client id or topic that contains one travels as '%25'): the rule of the HA
# add-on's apps/runtime/backend_policy.path_value, copied unchanged (Woow_ha_mcp_addons #70, two independent reviews).
_DECODE_LAYERS = 8  # a path that still changes after eight further decodes is refused
_BARE_PERCENT = re.compile(r'%(?![0-9A-Fa-f]{2})')  # on the wire every '%' must start a %XX escape
# Taken for a hex digit by some decoder: ASCII hex by all; any Unicode decimal digit by Java's Character.digit and
# Python's int(); fullwidth A-F/a-f by Java; ':;<=>?@`' and U+0010-U+001F by Jetty's
# (c & 0x1f) + (c >> 6) * 0x19 - 0x10.
_HEX_LIKE = r'[\d\x10-\x1f:;<=>?@`A-Fa-f\uff21-\uff26\uff41-\uff46]'
# A '%' that Python's unquote leaves alone but another decoder still decodes: %uXXXX (IIS, .NET, JavaScript's
# unescape), or two hex-like characters that are not both ASCII hex ('%' + fullwidth '2' + 'e' is '.' to Java's
# URLDecoder).
_ODD_ESCAPE = re.compile('%[uU]' + _HEX_LIKE + '{4}|%(?![0-9A-Fa-f]{2})' + _HEX_LIKE + '{2}')
# nginx's ngx_unescape_uri: valid escapes decoded; '%' + a non-hex character keeps that character; '%' + hex + non-hex,
# and a dangling '%' or '%X', disappear. So '.%.' and '.%2..' are '..' to it.
_NGINX_ESCAPE = re.compile(r'%(?:([0-9A-Fa-f]{2})|[0-9A-Fa-f](?:[^0-9A-Fa-f]|\Z)|([^0-9A-Fa-f])|\Z)')
# Overlong UTF-8, which lax decoders read as ASCII ('%c0%ae' as '.', '%c0%af' as '/'); no real name contains it.
_OVERLONG = re.compile(rb'[\xc0\xc1]|\xe0[\x80-\x9f]|\xf0[\x80-\x8f]')
_LINE_BREAKS = '\x00\n\r\x85\u2028\u2029'  # NUL and the line breaks a further decode must not add


def _path_shape_ok(path):
    return '\\' not in path and not any(p in ('.', '..') for p in path.split('/'))


def _structure(path):
    """What a further decode must not add: separators, query and fragment marks, NUL and line breaks."""
    return path.count('/'), path.count('?'), path.count('#'), sum(path.count(c) for c in _LINE_BREAKS)


def _decodes_alike(layer):
    """No escape left in this layer that another decoder reads where Python's unquote does not: an odd escape, an
    invalid one that nginx drops into a dot segment, or overlong UTF-8 in what the next decode gives."""
    if _ODD_ESCAPE.search(layer) or _OVERLONG.search(unquote_to_bytes(layer.encode('utf-8', 'surrogatepass'))):
        return False
    return not _BARE_PERCENT.search(layer) or _path_shape_ok(
        _NGINX_ESCAPE.sub(lambda m: chr(int(m[1], 16)) if m[1] else (m[2] or ''), layer))


def path_value(path, literal_percent=False):
    """The path after one percent-decode. Refused: a backslash, a '.'/'..' segment, and a '%' still present.

    literal_percent (request paths, not the configured base URL): a '%' may remain, because a file or calendar name can
    contain one ('100%-off.txt' travels as '100%25-off.txt', '%18 KDV.xlsx' as '%2518%20KDV.xlsx'), as long as no
    decoder in front of or inside the backend can read more into it than the first decode gave:
    - every '%' on the wire starts a %XX escape (httpx sends a bare '%' as it is);
    - decoding again with unquote, layer after layer, as a proxy or a backend that decodes twice would, never adds a
      '/', a backslash, a '.'/'..' segment, a '?', a '#', a NUL or a line break (CR, LF, NEL, U+2028, U+2029), and the
      path no longer changes after at most eight further decodes;
    - no layer holds an escape that other decoders read differently (_decodes_alike).
    So '%252e%252e', '%252f', '%2500' and '%25u002e' stay refused however deeply nested; what the first decode yields is
    checked exactly as before. Not modelled: a decoder reading a lone byte as Latin-1 (a second-decode '%85' is U+FFFD
    here, NEL there); a sign or space before one hex digit ('%+0', '% d'), which Java's URLDecoder or int() read as a
    control character, never as structure; and the byte-wrapping variant of Jetty's hex arithmetic.
    """
    raw, path = path, unquote(path)
    if not _path_shape_ok(path):
        raise DestinationDenied()
    if '%' in path:
        if not literal_percent or _BARE_PERCENT.search(raw):
            raise DestinationDenied()
        layer, shape = path, _structure(path)
        for _ in range(_DECODE_LAYERS + 1):
            if not _decodes_alike(layer):
                raise DestinationDenied()
            deeper = unquote(layer)
            if deeper == layer:
                break
            if not _path_shape_ok(deeper) or _structure(deeper) != shape:
                raise DestinationDenied()
            layer = deeper
        else:
            raise DestinationDenied()
    return path.rstrip('/') or '/'


def normalize_base_url(value):
    """Accept a root or a pasted /api/v5 root; never echo rejected input."""
    value = value.strip()
    try:
        u = urlsplit(value)
        if (not value or any(ord(c) <= 32 or ord(c) == 127 for c in value)
                or u.scheme not in ('http', 'https') or not u.hostname
                or u.username is not None or u.password is not None or '@' in u.netloc
                or '?' in value or '#' in value or '\\' in value or '%' in value
                or u.port == 0 or any(p in ('.', '..') for p in u.path.split('/'))):
            raise ValueError()
        path = u.path.rstrip('/')
        if path.endswith('/api/v5'):
            path = path[:-7]
        if '/api/v5' in path:
            raise ValueError()
        root = urlunsplit((u.scheme, u.netloc, path, '', ''))
        if not check_url_syntax(root).ok:
            raise ValueError()
        return root
    except (ValueError, TypeError):
        raise DestinationDenied() from None


class Destination:
    def __init__(self, root):
        self.base = normalize_base_url(root) + '/api/v5'
        u = urlsplit(self.base)
        self.scheme, self.host = u.scheme, u.hostname.lower()
        self.port = u.port or (443 if u.scheme == 'https' else 80)
        self.path = u.path
        self.mode = current_mode()  # Deployment-owned, never a management setting.

    def check_host(self, host, port):
        if host.lower() != self.host or port != self.port:
            raise DestinationDenied()

    def check_request(self, request):
        u = urlsplit(str(request.url))
        self.check_host(u.hostname or '', u.port or (443 if u.scheme == 'https' else 80))
        path = path_value(u.path, literal_percent=True)  # '\\', dot segments, a '%' a further decode could misread
        if (u.scheme != self.scheme or u.username is not None or u.password is not None
                or u.fragment
                or not (path == self.path or path.startswith(self.path + '/'))
                or 'sni_hostname' in request.extensions):
            raise DestinationDenied()

    def answers(self):
        try:
            ip = ipaddress.ip_address(self.host)
        except ValueError:
            rows = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        else:
            family = socket.AF_INET if ip.version == 4 else socket.AF_INET6
            rows = [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (str(ip), self.port))]
        if not rows:
            raise DestinationDenied()
        addresses = []
        for family, kind, proto, _, address in rows:
            if (family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM
                    or proto != socket.IPPROTO_TCP or address[1] != self.port
                    or '%' in address[0] or len(address) > 3 and address[3] != 0
                    or address_block_reason(address[0], self.mode, host=self.host) is not None):
                raise DestinationDenied()
            addresses.append(address[0])
        return list(dict.fromkeys(addresses))


class PinnedNetwork(AnyIOBackend):
    def __init__(self, destination):
        self.destination = destination

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.destination.check_host(host, port)
        budget = min(timeout if timeout is not None else 5.0, 5.0)
        deadline = time.monotonic() + budget
        if not _DNS_SLOTS.acquire(blocking=False):
            raise httpx.ConnectError('EMQX_DNS_BUSY')
        try:
            pending = _DNS_POOL.submit(self.destination.answers)
        except BaseException:
            _DNS_SLOTS.release()
            raise
        pending.add_done_callback(lambda _: _DNS_SLOTS.release())
        future = asyncio.wrap_future(pending)
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        try:
            addresses = await asyncio.wait_for(asyncio.shield(future), budget)
            for index, address in enumerate(addresses):
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError()
                try:
                    # Numeric address only. HTTPcore retains the original SNI
                    # hostname for TLS; no second hostname resolution at TCP.
                    return await super().connect_tcp(address, port, left / (len(addresses) - index),
                                                     None, socket_options)
                except (httpcore.ConnectError, httpcore.ConnectTimeout):
                    if index == len(addresses) - 1:
                        raise
        except TimeoutError:
            raise httpcore.ConnectTimeout('EMQX_TIMEOUT') from None
        except OSError:
            raise httpcore.ConnectError('EMQX_UNAVAILABLE') from None


class BrokerTransport(httpx.AsyncHTTPTransport):
    def __init__(self, destination):
        for name, expected in _SOURCES.items():
            module = httpx if name.startswith('httpx/') else httpcore
            path = Path(module.__file__).parent.parent / name
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError('EMQX transport dependency needs source review')
        super().__init__(verify=True, trust_env=False, retries=0,
                         # A brief network outage can leave an apparently live
                         # pooled socket that times out on the first request
                         # after the broker is reachable again. Never replay
                         # an ambiguous write: use a fresh pinned TCP connection
                         # for each request instead of retaining idle sockets.
                         limits=httpx.Limits(max_connections=4, max_keepalive_connections=0))
        self.destination = destination
        self._pool._network_backend = PinnedNetwork(destination)

    async def handle_async_request(self, request):
        self.destination.check_request(request)
        return await super().handle_async_request(request)


class BrokerClient:
    """One owned pool, bounded buffered responses, and immutable backend auth."""
    def __init__(self, base_url, api_key, api_secret, request_timeout=30):
        if not api_key or not api_secret or ':' in api_key or any(
                ord(c) < 32 or ord(c) == 127 for c in api_key + api_secret):
            raise ValueError('EMQX_API_CREDENTIALS_REQUIRED')
        destination = Destination(base_url)
        self._client = httpx.AsyncClient(base_url=destination.base,
            auth=(api_key, api_secret), transport=BrokerTransport(destination),
            trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(connect=5, read=min(float(request_timeout), 60), write=10, pool=5),
            headers={'Accept': 'application/json', 'Accept-Encoding': 'identity'})
        for name in ('httpx', 'httpcore'):
            logging.getLogger(name).setLevel(logging.WARNING)

    @property
    def is_closed(self):
        return self._client.is_closed

    @property
    def timeout(self):
        return self._client.timeout

    async def request(self, method, url, **kwargs):
        if set(kwargs) - {'params', 'json', 'content', 'data'}:
            raise DestinationDenied()
        # Validate before HTTPX normalizes dot segments; tools use only API paths.
        u = urlsplit(str(url))
        if u.scheme or u.netloc or u.query or u.fragment or not unquote(u.path).startswith('/'):
            raise DestinationDenied()
        path_value(u.path, literal_percent=True)  # '\\', dot segments, a '%' a further decode could misread
        request = self._client.build_request(method, url, **kwargs)
        response = await self._client.send(request, stream=True)
        try:
            if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                raise httpx.HTTPError('EMQX_UNSUPPORTED_ENCODING')
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ResponseTooLarge()
                body.extend(chunk)
            return httpx.Response(response.status_code, content=bytes(body),
                                  headers={'content-type': response.headers.get('content-type', '')}, request=request)
        finally:
            await response.aclose()

    async def aclose(self):
        await self._client.aclose()
