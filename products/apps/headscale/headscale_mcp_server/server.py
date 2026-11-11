"""Shared task dispatcher and FastMCP tools; no shell, subprocess or EMQX imports."""
from __future__ import annotations

from contextlib import asynccontextmanager
import difflib
import hashlib
import hmac
import json

from .client import HeadscaleClient
from .errors import HeadscaleError
from .policy import SavedPolicy
from .registry import OPERATION_POLICIES, TASK_NATIVE, TOOL_REGISTRY, input_schema, normalize_arguments


def policy_digest(policy: str) -> str:
    """Digest the exact UTF-8 document, not an invented semantic normalization."""
    return hashlib.sha256(policy.encode("utf-8")).hexdigest()


# NUL-separated domain marker cannot be a valid policy document. In particular,
# absence must not have the digest of an existing empty file/database document.
ABSENT_POLICY_DIGEST = hashlib.sha256(b"\x00headscale-mcp:policy:not-set:v1\x00").hexdigest()


async def execute_tool(name: str, args: dict, client: HeadscaleClient, *, config_path=None) -> dict:
    """The GUI and MCP cross the same validated, saved-policy-enforced seam."""
    args = normalize_arguments(name, args)
    saved = SavedPolicy(config_path)
    saved.require(name, args)
    operation = args.get("operation", OPERATION_POLICIES[name].default)

    async def call(native, **parameters):
        # Composite operations recheck after every await, before the next I/O.
        saved.require(name, args)
        return await client.request(native, **parameters)

    if name == "headscale_policy":
        async def snapshot():
            try:
                result = await call("GetPolicy")
            except HeadscaleError as exc:
                if exc.code != "HEADSCALE_POLICY_NOT_SET":
                    raise
                return {"policy": "", "exists": False, "digest": ABSENT_POLICY_DIGEST}
            return {**result, "exists": True, "digest": policy_digest(result["policy"])}

        if operation == "get":
            return await snapshot()
        if operation == "check":
            await call("CheckPolicy", policy=args["policy"])
            return {"valid": True, "candidate_digest": policy_digest(args["policy"]), "persisted": False}
        if operation == "diff":
            current = await snapshot()
            # Bound CPU and returned output independently of the upstream cap.
            before, after = current["policy"].splitlines(keepends=True), args["policy"].splitlines(keepends=True)
            if len(before) > 5000 or len(after) > 5000:
                raise HeadscaleError("HEADSCALE_POLICY_DIFF_TOO_LARGE")
            chunks, size, truncated = [], 0, False
            for line in difflib.unified_diff(before, after, fromfile="current", tofile="candidate"):
                size += len(line.encode("utf-8"))
                if size > 65536:
                    truncated = True
                    break
                chunks.append(line)
            return {"digest": current["digest"], "exists": current["exists"], "candidate_digest": policy_digest(args["policy"]),
                    "diff": "".join(chunks), "truncated": truncated, "persisted": False}
        async with client.policy_lock:
            current = await snapshot()
            if not hmac.compare_digest(current["digest"], args["expected_digest"]):
                raise HeadscaleError("HEADSCALE_POLICY_CONFLICT")
            await call("CheckPolicy", policy=args["policy"])
            observed = await snapshot()
            if not hmac.compare_digest(observed["digest"], args["expected_digest"]):
                raise HeadscaleError("HEADSCALE_POLICY_CONFLICT")
            await call("SetPolicy", policy=args["policy"])
            verification = {"status": "unverified", "write_acknowledged": True,
                            "candidate_digest": policy_digest(args["policy"]), "atomic_cas": False}
            try:
                updated = await snapshot()
            except HeadscaleError as exc:
                # The PUT already succeeded. A failed GET or local permission
                # revocation must not be reported as an unsent/rejected write.
                raise HeadscaleError("HEADSCALE_POLICY_UNVERIFIED", outcome="unknown",
                                     policy_verification={**verification, "readback_error": exc.as_dict()}) from None
            if not hmac.compare_digest(updated["digest"], verification["candidate_digest"]):
                raise HeadscaleError("HEADSCALE_POLICY_CONFLICT", outcome="unknown",
                                     policy_verification={**verification, "status": "conflict",
                                                          "observed_digest": updated["digest"],
                                                          "observed_exists": updated["exists"]})
            return {**updated, "readback_verified": True, "write_acknowledged": True,
                    "candidate_digest": verification["candidate_digest"],
                    "previous_exists": current["exists"],
                    "previous_digest": args["expected_digest"], "atomic_cas": False,
                    "concurrency_note": "Observed digest comparison and readback only; other writers can race the PUT or change policy after readback. The lock covers only this client instance."}

    parameters = {k: v for k, v in args.items() if k not in ("operation", "confirm", "reveal_once")}
    native = TASK_NATIVE[(name, operation)]
    if name == "headscale_users":
        if "user_id" in parameters:
            parameters["oldId" if operation == "rename" else "id"] = parameters.pop("user_id")
        for old, new in (("display_name", "displayName"), ("picture_url", "pictureUrl"), ("new_name", "newName")):
            if old in parameters:
                parameters[new] = parameters.pop(old)
    if name in ("headscale_nodes", "headscale_routes_replace"):
        if "node_id" in parameters:
            parameters["nodeId"] = parameters.pop("node_id")
        if "new_name" in parameters:
            parameters["newName"] = parameters.pop("new_name")
        if name == "headscale_nodes" and operation == "expire":
            mode = parameters.pop("mode")
            if mode == "disable":
                parameters["disableExpiry"] = True
    if name in ("headscale_register", "headscale_auth", "headscale_debug_node"):
        parameters["key" if native in ("RegisterNode", "DebugCreateNode") else "authId"] = parameters.pop("auth_id")
    if name == "headscale_preauth_keys":
        if "user_id" in parameters:
            parameters["user"] = parameters.pop("user_id")
        if "tags" in parameters:
            parameters["aclTags"] = parameters.pop("tags")
    if "key_id" in parameters:
        parameters["id"] = parameters.pop("key_id")
    if name == "headscale_backfill_ips":
        parameters["confirmed"] = True
    if args.get("reveal_once"):
        parameters["reveal_created_secret"] = True
    result = await call(native, **parameters)
    if args.get("reveal_once"):
        result = {**result, "secret_disclosure": "created_once"}
    if name == "headscale_auth":
        result = {**result, "decision": operation, "registered": False}
    if name == "headscale_debug_node":
        result = {**result, "synthetic": True}
    if name == "headscale_routes_replace":
        result = {**result, "semantics": "replace_all_approved_routes"}
    return result


def build_server(client: HeadscaleClient, config_path=None, *, extra_tool_policies=None):
    """Register native tools and filter trusted extensions using saved policy.

    Extensions are registered by the caller and enforce their own call policy;
    extra_tool_policies only supplies their operation metadata for availability.
    """
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError
    from fastmcp.server.middleware import Middleware
    from fastmcp.tools import Tool
    from mcp.types import ToolAnnotations

    saved = SavedPolicy(config_path)
    policies = {**OPERATION_POLICIES, **(extra_tool_policies or {})}

    class NativeTool(Tool):
        async def run(self, arguments):
            try:
                result = await execute_tool(self.name, arguments, client, config_path=saved.path)
                return self.convert_result(result)
            except HeadscaleError as exc:
                raise ToolError(json.dumps(exc.as_dict(), separators=(",", ":"))) from None

    class Availability(Middleware):
        async def on_list_tools(self, context, call_next):
            try:
                disabled, denied = saved.read()
            except HeadscaleError as exc:
                raise ToolError(exc.code) from None
            listed = await call_next(context)
            return [tool for tool in listed if tool.name in policies and tool.name not in disabled
                    and any(not policies[tool.name].requirements(op) &
                            policies[tool.name].denied(denied.get(tool.name, []))
                            for op in policies[tool.name].operations)]

    @asynccontextmanager
    async def lifespan(server):
        try:
            yield {"headscale": client}
        finally:
            await client.aclose()

    server = FastMCP("Headscale MCP", version="0.1.1", lifespan=lifespan, mask_error_details=True,
                     instructions="Manage one Headscale v0.29.4 instance. Registration and SSH verdicts are distinct. Routes replace an entire approved set. Policy apply is not atomic CAS. DNS, OIDC, DERP, deployment and client-daemon configuration are not native v1 capabilities.")
    for spec in TOOL_REGISTRY:
        read_only = spec.access == "read"
        server.add_tool(NativeTool(
            name=spec.name, description=spec.description, parameters=input_schema(spec.name),
            output_schema={"type": "object"}, tags={spec.category.value},
            annotations=ToolAnnotations(readOnlyHint=read_only, destructiveHint=spec.dangerous,
                                        idempotentHint=read_only, openWorldHint=True)))
    server.add_middleware(Availability())
    return server

