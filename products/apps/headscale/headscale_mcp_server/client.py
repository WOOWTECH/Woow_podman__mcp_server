"""Owned async HTTP pool and bounded, redacted native Headscale responses."""
from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import quote

import httpx

from .errors import HeadscaleError
from .http_policy import Destination, HeadscaleTransport, normalize_base_url
from .native import NATIVE_OPERATIONS, native_arguments

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 512 * 1024
# v0.29.4 GetPolicy wraps ErrPolicyNotFound only in database mode. Match the
# complete known envelope, never arbitrary HTTP 500 / gRPC Unknown errors.
_POLICY_NOT_SET = {"code": 2, "message": "loading ACL from database: acl policy not found", "details": []}


def _error_category(status, grpc_code):
    # Prefer HTTP status when a gateway envelope disagrees. An unrecognized
    # status may still carry a standard gRPC error, but never infer from prose.
    categories = {400: "invalid_arguments", 401: "authentication", 403: "permission",
                  404: "not_found", 408: "timeout", 409: "conflict", 429: "rate_limit",
                  503: "unavailable", 504: "timeout"}
    grpc_categories = {3: "invalid_arguments", 4: "timeout", 5: "not_found",
                       6: "conflict", 7: "permission", 8: "rate_limit",
                       9: "precondition", 10: "conflict", 12: "unsupported",
                       14: "unavailable", 16: "authentication"}
    if 300 <= status < 400:
        return "redirect"
    return categories.get(status, grpc_categories.get(grpc_code, "upstream"))


def _policy_diagnostic(operation_id, status, grpc_code, upstream):
    if operation_id != "CheckPolicy" or status != 400 or grpc_code != 3:
        return None
    diagnostic = {"reason": "policy_validation",
                  "hint": "Check policy syntax, references and embedded tests against current users and nodes."}
    message = upstream.get("message") if isinstance(upstream, dict) else None
    if not isinstance(message, str) or len(message) > 4096:
        return diagnostic
    # Fixed v0.29.4 policy/v2 error markers. Return only static hints: policy
    # errors can interpolate arbitrary secrets, aliases and entire documents.
    for marker, reason, hint in (
        ("references undefined tag", "undefined_tag", "Declare referenced tags in tagOwners."),
        ("tag names must start with a letter", "tag_name", "Start policy tag names with an ASCII letter after tag:."),
        ("group members cannot be recursive", "recursive_group", "Remove nested group membership."),
        ("checkPeriod is only valid with action", "ssh_check_period", "Use SSH checkPeriod only with the check action."),
    ):
        if marker in message:
            return {"reason": reason, "hint": hint}
    return diagnostic


def redact(value, *, credential="", reveal_path=None, path=(), depth=0):
    """All preauth `key` fields, including nested nodes and legacy plaintext."""
    if depth > 40:
        raise HeadscaleError("HEADSCALE_INVALID_RESPONSE")
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            here = path + (key,)
            sensitive = key.lower().replace("_", "") in {"key", "apikey", "secret", "hash", "authorization", "token"}
            if sensitive and here != reveal_path:
                out[key] = "[REDACTED]" if item else ""
            else:
                out[key] = redact(item, credential=credential, reveal_path=reveal_path, path=here, depth=depth+1)
        return out
    if isinstance(value, list):
        return [redact(x, credential=credential, reveal_path=reveal_path, path=path+(i,), depth=depth+1)
                for i, x in enumerate(value)]
    if isinstance(value, str) and credential:
        return value.replace(credential, "[REDACTED]")
    return value


class HeadscaleClient:
    def __init__(self, base_url, api_key, *, allowed_private_cidrs=(), transport=None):
        if (not isinstance(api_key, str) or not api_key or len(api_key) > 8192 or
                any(ord(c) <= 32 or ord(c) >= 127 for c in api_key)):
            raise HeadscaleError("HEADSCALE_INVALID_CREDENTIAL")
        self.destination = Destination(base_url, allowed_private_cidrs)
        self.base_url = self.destination.base_url
        self._api_key = api_key
        self.policy_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            transport=transport if transport is not None else HeadscaleTransport(self.destination),
            trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(connect=5, read=20, write=10, pool=5),
            headers={"Accept": "application/json", "Accept-Encoding": "identity"})
        # Query registration auth IDs must not reach HTTPX's INFO request logger.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    @property
    def is_closed(self):
        return self._client.is_closed

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self):
        await self._client.aclose()

    async def request(self, operation_id: str, *, reveal_created_secret=False, **parameters) -> dict:
        op, args = native_arguments(operation_id, parameters)
        if type(reveal_created_secret) is not bool or (reveal_created_secret and operation_id not in ("CreateApiKey", "CreatePreAuthKey")):
            raise HeadscaleError("HEADSCALE_INVALID_ARGUMENTS")
        path = op.path
        for key in op.path_fields:
            path = path.replace("{" + key + "}", quote(args.pop(key), safe=""))
        query = {k: (str(args[k]).lower() if isinstance(args[k], bool) else args[k]) for k in op.query_fields if k in args}
        body = {k: v for k, v in args.items() if k not in op.query_fields}
        has_body = op.method in ("POST", "PUT") and not op.query_fields and operation_id not in ("RenameUser", "RenameNode")
        encoded = json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode() if has_body else None
        if encoded and len(encoded) > MAX_REQUEST_BYTES:
            raise HeadscaleError("HEADSCALE_REQUEST_TOO_LARGE")
        request = self._client.build_request(op.method, self.base_url + path, params=query,
                                            content=encoded,
                                            headers={"Authorization": "Bearer " + self._api_key,
                                                     **({"Content-Type": "application/json"} if has_body else {})})
        response = None
        try:
            async with asyncio.timeout(30):
                response = await self._client.send(request, stream=True)
                try:
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise HeadscaleError("HEADSCALE_UNSUPPORTED_ENCODING", outcome="rejected" if op.read_only else "unknown")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            raise HeadscaleError("HEADSCALE_RESPONSE_TOO_LARGE", outcome="rejected" if op.read_only else "unknown")
                    if response.status_code != 200:
                        grpc_code = None
                        upstream = None
                        try:
                            upstream = json.loads(raw)
                            if isinstance(upstream, dict) and type(upstream.get("code")) is int:
                                grpc_code = upstream["code"] if 0 <= upstream["code"] <= 16 else None
                        except (ValueError, RecursionError):
                            pass
                        if (operation_id == "GetPolicy" and response.status_code == 500
                                and grpc_code == 2 and upstream == _POLICY_NOT_SET):
                            raise HeadscaleError("HEADSCALE_POLICY_NOT_SET", http_status=500,
                                                 grpc_code=2, outcome="rejected")
                        raise HeadscaleError("HEADSCALE_UPSTREAM_ERROR", http_status=response.status_code,
                                             grpc_code=grpc_code,
                                             category=_error_category(response.status_code, grpc_code),
                                             diagnostic=_policy_diagnostic(operation_id, response.status_code, grpc_code, upstream),
                                             outcome="unknown" if not op.read_only and (response.status_code >= 500 or response.status_code < 400 or response.status_code == 408) else "rejected")
                    try:
                        result = json.loads(raw)
                    except (ValueError, RecursionError):
                        raise HeadscaleError("HEADSCALE_INVALID_RESPONSE", outcome="rejected" if op.read_only else "unknown") from None
                    if not isinstance(result, dict) or op.response_field and op.response_field not in result:
                        raise HeadscaleError("HEADSCALE_INVALID_RESPONSE", outcome="rejected" if op.read_only else "unknown")
                    if op.response_field:
                        field = result[op.response_field]
                        expected = bool if operation_id == "Health" else str if op.response_field in ("policy", "apiKey") else list if op.response_field in ("users", "nodes", "apiKeys", "preAuthKeys", "changes") else dict
                        if not isinstance(field, expected):
                            raise HeadscaleError("HEADSCALE_INVALID_RESPONSE", outcome="rejected" if op.read_only else "unknown")
                    reveal = (("apiKey",) if operation_id == "CreateApiKey" else ("preAuthKey", "key")) if reveal_created_secret else None
                    try:
                        return redact(result, credential=self._api_key, reveal_path=reveal)
                    except HeadscaleError:
                        raise HeadscaleError("HEADSCALE_INVALID_RESPONSE", outcome="rejected" if op.read_only else "unknown") from None
                finally:
                    await response.aclose()
        except HeadscaleError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # These errors normally precede sending the request. If a custom
            # transport raises one after headers, the response proves otherwise.
            outcome = "not_sent" if response is None else "rejected" if op.read_only else "unknown"
            raise HeadscaleError("HEADSCALE_TRANSPORT_ERROR", outcome=outcome,
                                 category="timeout" if isinstance(exc, httpx.TimeoutException) else "connection") from None
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            # A disconnected request can have committed. No write is retried.
            category = ("timeout" if isinstance(exc, (httpx.TimeoutException, TimeoutError)) else
                        "protocol" if isinstance(exc, httpx.ProtocolError) else "transport")
            raise HeadscaleError("HEADSCALE_TRANSPORT_ERROR", outcome="rejected" if op.read_only else "unknown",
                                 category=category) from None

