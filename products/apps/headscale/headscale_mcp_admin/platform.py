"""Optional, fixed-target PaaS extension; no arbitrary URL, resource or shell access."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Annotated, Literal, Union
from uuid import UUID

import httpx
from pydantic import Field, StrictBool, StrictInt, TypeAdapter, create_model

from mcp_admin_core.operation_policy import OperationSpec
from headscale_mcp_server.client import redact
from headscale_mcp_server.errors import HeadscaleError
from headscale_mcp_server.http_policy import Destination, HeadscaleTransport
from headscale_mcp_server.models import Arguments, Digest, Id
from headscale_mcp_server.policy import SavedPolicy

NAME = "headscale_platform"
DESCRIPTION = "Manage the bound PaaS Headscale configuration and durable Tailscale service pairing. Workspace and control service are fixed in administrator connection settings. Writes require a stable UUID idempotency key; poll the returned operation."
_Uuid = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
_Version = Annotated[StrictInt, Field(ge=0)]
_Scalar = Union[str, StrictBool, StrictInt, None]
_FIELDS = {
    "tailscale_services": {"cursor": (Annotated[str, Field(max_length=2048)], None)},
    "configuration": {},
    "configure": {"values": (dict[str, _Scalar], ...), "expected_revision": (Digest, ...), "idempotency_key": (_Uuid, ...)},
    "restart": {"idempotency_key": (_Uuid, ...)},
    "operation": {"operation_id": (_Uuid, ...)},
    "pairing_options": {"tailscale_service_id": (Id, ...)},
    "pair": {"tailscale_service_id": (Id, ...), "headscale_user_id": (Id, None), "expected_binding_version": (_Version, ...), "idempotency_key": (_Uuid, ...)},
    "disconnect": {"tailscale_service_id": (Id, ...), "expected_binding_version": (_Version, ...), "idempotency_key": (_Uuid, ...)},
    "pairing_operation": {"tailscale_service_id": (Id, ...), "operation_id": (_Uuid, ...)},
}
OPERATIONS = tuple(_FIELDS)
POLICY = OperationSpec(OPERATIONS, parameter="operation")
MODELS = TypeAdapter(Annotated[Union[tuple(create_model("Platform_" + op, __base__=Arguments,
                              operation=(Literal[op], ...), **fields) for op, fields in _FIELDS.items())], Field(discriminator="operation")])
_ALLOWED_VALUES = frozenset({"ngrok.enabled", "ngrok.domain", "headscale.dns.magicDns",
                           "headscale.dns.baseDomain", "headscale.dns.nameservers", "headscale.nodeExpiry", "headscale.logLevel"})


def input_schema():
    # The MCP wire contract requires an object even when its shape is a union.
    return {**MODELS.json_schema(), "type": "object"}


def sanitize_response(result, credential):
    sanitized = redact(result, credential=credential)
    fields = result.get("fields")
    if isinstance(fields, list):
        for source, target in zip(fields, sanitized["fields"]):
            # In this DTO only, fields[].key is a configuration field identifier.
            # All other key fields retain the native fail-closed redaction rule.
            if isinstance(source, dict) and isinstance(target, dict) and isinstance(source.get("key"), str):
                target["key"] = redact(source["key"], credential=credential)
    return sanitized


def normalize_arguments(arguments):
    try:
        args = MODELS.validate_python(arguments).model_dump(exclude_none=True)
        if args["operation"] == "configure":
            values = args["values"]
            if not values or set(values) - _ALLOWED_VALUES or len(json.dumps(values)) > 16384:
                raise ValueError()
        return args
    except (ValueError, TypeError):
        raise HeadscaleError("PAAS_INVALID_ARGUMENTS") from None


def require_policy(config_path, args):
    disabled, operations = SavedPolicy(config_path).read()
    requested = POLICY.resolve(args)
    if NAME in disabled or requested is None or requested & POLICY.denied(operations.get(NAME, [])):
        raise HeadscaleError("HEADSCALE_POLICY_DENIED")


class PlatformClient:
    def __init__(self, connection, *, transport=None):
        try:
            self.workspace = str(connection["workspace_id"])
            self.service = str(connection["headscale_service_id"])
            if not all(re.fullmatch(r"[1-9][0-9]{0,9}", x) for x in (self.workspace, self.service)):
                raise ValueError()
            self._token = connection["paas_api_token"]
            if not isinstance(self._token, str) or not self._token.startswith("woow_pat_") or len(self._token) > 4096 or any(ord(c) <= 32 or ord(c) >= 127 for c in self._token):
                raise ValueError()
            self.destination = Destination(connection["paas_base_url"])
        except (KeyError, ValueError, TypeError):
            raise HeadscaleError("PAAS_SETUP_REQUIRED") from None
        self._client = httpx.AsyncClient(transport=transport or HeadscaleTransport(self.destination),
                                        follow_redirects=False, trust_env=False,
                                        timeout=httpx.Timeout(connect=5, read=25, write=10, pool=5))

    async def aclose(self):
        await self._client.aclose()

    async def request(self, method, path, *, body=None, params=None, key=None, revision=None):
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json", "Accept-Encoding": "identity"}
        if key:
            headers["Idempotency-Key"] = str(UUID(key))
        if revision:
            headers["If-Match"] = '"' + revision + '"'
        try:
            async with asyncio.timeout(35):
                async with self._client.stream(method, self.destination.base_url + path, json=body,
                                               params=params, headers=headers) as response:
                    data = bytearray()
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise HeadscaleError("PAAS_INVALID_RESPONSE", outcome="not_sent" if method == "GET" else "unknown")
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 1024 * 1024:
                            raise HeadscaleError("PAAS_RESPONSE_TOO_LARGE", outcome="not_sent" if method == "GET" else "unknown")
                    try:
                        result = json.loads(data)
                    except (ValueError, RecursionError):
                        raise HeadscaleError("PAAS_INVALID_RESPONSE", outcome="not_sent" if method == "GET" else "unknown") from None
                    if not isinstance(result, dict):
                        raise HeadscaleError("PAAS_INVALID_RESPONSE", outcome="not_sent" if method == "GET" else "unknown")
                    if response.status_code not in (200, 202):
                        code = result.get("error", {}).get("code", "request_failed") if isinstance(result.get("error", {}), dict) else "request_failed"
                        code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code) else "request_failed"
                        raise HeadscaleError("PAAS_" + code.upper(), http_status=response.status_code,
                                             outcome="unknown" if method != "GET" and response.status_code >= 500 else "rejected")
                    return sanitize_response(result, self._token)
        except (httpx.TransportError, TimeoutError):
            raise HeadscaleError("PAAS_CONNECTION_FAILED", outcome="not_sent" if method == "GET" else "unknown") from None


async def execute_platform_tool(arguments, connection, *, config_path, transport=None):
    args = normalize_arguments(arguments)
    require_policy(config_path, args)
    client = PlatformClient(connection, transport=transport)
    root = f"/api/woow/v1/workspaces/{client.workspace}/services/"
    target = root + client.service
    op = args["operation"]
    try:
        # Recheck after configuration/connection validation before each side effect.
        require_policy(config_path, args)
        if op == "tailscale_services":
            result = await client.request("GET", root.rstrip("/"), params={"limit": "100", **(
                {"cursor": args["cursor"]} if args.get("cursor") else {})})
            rows = result.get("services")
            if not isinstance(rows, list):
                raise HeadscaleError("PAAS_INVALID_RESPONSE")
            result = {"services": [{key: row.get(key) for key in ("id", "name", "state")} for row in rows
                      if isinstance(row, dict) and str(row.get("workspace_id")) == client.workspace
                      and isinstance(row.get("template"), dict) and row["template"].get("slug") == "tailscale"],
                      "next_cursor": result.get("next_cursor")}
        elif op == "configuration":
            result = await client.request("GET", target + "/headscale/configuration")
            if str(result.get("workspace_id")) != client.workspace or str(result.get("service_id")) != client.service:
                raise HeadscaleError("PAAS_TARGET_MISMATCH")
        elif op == "configure":
            result = await client.request("PATCH", target + "/headscale/configuration", body={"values": args["values"]},
                                          revision=args["expected_revision"], key=args["idempotency_key"])
        elif op == "restart":
            result = await client.request("POST", target + "/restart", body={}, key=args["idempotency_key"])
        elif op == "operation":
            result = await client.request("GET", "/api/woow/v1/operations/" + args["operation_id"])
        else:
            consumer = root + args["tailscale_service_id"] + "/vpn/"
            if op == "pairing_options":
                result = await client.request("GET", consumer + "options", params={"headscale_service_id": client.service})
            elif op == "pairing_operation":
                result = await client.request("GET", consumer + "operations/" + args["operation_id"],
                                              params={"headscale_service_id": client.service})
            else:
                body = {"headscale_service_id": int(client.service), "expected_binding_version": args["expected_binding_version"]}
                if op == "pair" and args.get("headscale_user_id"):
                    body["headscale_user_id"] = int(args["headscale_user_id"])
                result = await client.request("POST", consumer + op, body=body, key=args["idempotency_key"])
        if op in ("configure", "restart", "operation") and "operation" in result:
            operation = result["operation"]
            if not isinstance(operation, dict) or str(operation.get("workspace_id")) != client.workspace or str(operation.get("service_id")) != client.service:
                raise HeadscaleError("PAAS_TARGET_MISMATCH", outcome="not_sent" if op == "operation" else "unknown")
        return result
    finally:
        await client.aclose()


def add_platform_tool(server, connection, config_path):
    from fastmcp.exceptions import ToolError
    from fastmcp.tools import Tool
    from mcp.types import ToolAnnotations

    class PlatformTool(Tool):
        async def run(self, arguments):
            try:
                result = await execute_platform_tool(arguments, connection, config_path=config_path)
                return self.convert_result(result)
            except HeadscaleError as exc:
                raise ToolError(json.dumps(exc.as_dict())) from None

    server.add_tool(PlatformTool(name=NAME, description=DESCRIPTION, parameters=input_schema(),
                                output_schema={"type": "object"}, tags={"PaaS services"},
                                annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                                            idempotentHint=False, openWorldHint=True)))
