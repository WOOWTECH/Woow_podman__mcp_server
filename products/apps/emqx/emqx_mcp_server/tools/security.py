"""Access control: authentication, authorization and bans."""

from __future__ import annotations

from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..deps import emqx_client
from ..errors import emqx_request, json_body
from ..gating import ToolGate
from ..models import AuthnUser, AuthnUserResult
from ._common import destructive, page_of, path_segment, read_only

BUILT_IN = "password_based:built_in_database"
BanSubject = Literal["clientid", "username", "peerhost"]


def register(mcp: FastMCP, gate: ToolGate) -> None:
    on = gate.is_tool_enabled

    if on("emqx_list_authn"):

        @mcp.tool(name="emqx_list_authn", tags={"emqx", "read", "security"},
                  annotations=read_only("List EMQX Authenticators"))
        async def list_authn(
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """The authenticator chain and each authenticator's status.

            Check this first when devices cannot connect: an empty chain means
            EMQX has no user table, so every credential is rejected.
            """
            body = json_body(await emqx_request(emqx, "GET", "/authentication"))
            rows = body if isinstance(body, list) else body.get("data", [])
            return {"count": len(rows or []), "authenticators": rows or []}

    if on("emqx_manage_authn_users"):
        allowed = gate.allowed_operations("emqx_manage_authn_users")
        allowed_list = ", ".join(sorted(allowed))

        @mcp.tool(
            name="emqx_manage_authn_users",
            description=(
                "Manage MQTT accounts in the EMQX built-in authentication "
                "database.\n\nThese are the credentials devices use to connect "
                "to the broker — not the dashboard login. If no authenticator "
                "exists yet, create one in the EMQX dashboard first; without it "
                "there is no user table to write to.\n\n"
                f"Operations enabled on this server: {allowed_list}."
            ),
            tags={"emqx", "write", "destructive", "security"},
            annotations=destructive("Manage EMQX MQTT Users", idempotent=False),
        )
        async def manage_authn_users(
            operation: Annotated[str, Field(
                description="Action to perform. See the tool description for "
                            "which operations this server allows.")],
            user_id: Annotated[str | None, Field(
                description="MQTT username. Required for create and delete.")] = None,
            password: Annotated[str | None, Field(
                description="Password. Required for create.")] = None,
            is_superuser: Annotated[bool, Field(
                description="Grant ACL-bypassing superuser rights.")] = False,
            authenticator_id: Annotated[str, Field(
                description="Authenticator chain id.")] = BUILT_IN,
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> AuthnUserResult:
            if operation not in allowed:
                raise ToolError(
                    f"Operation {operation!r} is switched off on this server. "
                    f"Allowed: {allowed_list}."
                )
            base = f"/authentication/{path_segment(authenticator_id, 'authenticator_id')}/users"

            if operation == "read":
                rows, _ = page_of(json_body(await emqx_request(emqx, "GET", base)))
                return AuthnUserResult(
                    operation=operation,
                    users=[AuthnUser(user_id=r.get("user_id", ""),
                                     is_superuser=bool(r.get("is_superuser", False)))
                           for r in rows],
                )
            if operation == "create":
                if not user_id or not password:
                    raise ToolError("create requires both `user_id` and `password`.")
                await emqx_request(emqx, "POST", base, json={
                    "user_id": user_id, "password": password,
                    "is_superuser": is_superuser})
                return AuthnUserResult(operation=operation, user_id=user_id)
            if operation == "delete":
                if not user_id:
                    raise ToolError("delete requires `user_id`.")
                await emqx_request(emqx, "DELETE", f"{base}/{path_segment(user_id, 'user_id')}")
                return AuthnUserResult(operation=operation, user_id=user_id)

            raise ToolError(
                f"Unknown operation {operation!r}. Allowed: {allowed_list}.")

    if on("emqx_list_authz_sources"):

        @mcp.tool(name="emqx_list_authz_sources", tags={"emqx", "read", "security"},
                  annotations=read_only("List Authorization Sources"))
        async def list_authz_sources(
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Authorization sources in evaluation order.

            Order matters: the first source that returns allow or deny wins.
            """
            body = json_body(await emqx_request(emqx, "GET", "/authorization/sources"))
            rows = body.get("sources", body) if isinstance(body, dict) else body
            rows = rows or []

            # A file source returns the whole acl.conf, and EMQX ships ~6 KB of
            # explanatory comments in it. Keep the rules, drop the essay.
            cleaned = []
            for row in rows:
                row = dict(row) if isinstance(row, dict) else row
                text = row.get("rules") if isinstance(row, dict) else None
                if isinstance(text, str):
                    live = [line for line in text.splitlines()
                            if line.strip() and not line.lstrip().startswith("%")]
                    row["rules"] = "\n".join(live)
                    row["rule_count"] = len(live)
                cleaned.append(row)
            return {"count": len(cleaned), "sources": cleaned}

    if on("emqx_authz_settings"):

        @mcp.tool(name="emqx_authz_settings", tags={"emqx", "read", "security"},
                  annotations=read_only("Authorization Settings"))
        async def authz_settings(
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Global authorization behaviour: what happens when no rule matches,
            what a denial does to the connection, and cache settings."""
            return json_body(
                await emqx_request(emqx, "GET", "/authorization/settings"))

    if on("emqx_manage_authz_rules"):
        allowed_z = gate.allowed_operations("emqx_manage_authz_rules")
        allowed_z_list = ", ".join(sorted(allowed_z))

        @mcp.tool(
            name="emqx_manage_authz_rules",
            description=(
                "Read and write built-in-database ACL rules that decide which "
                "topics a user or client may publish to and subscribe to.\n\n"
                f"Operations enabled on this server: {allowed_z_list}."
            ),
            tags={"emqx", "write", "destructive", "security"},
            annotations=destructive("Manage EMQX ACL Rules", idempotent=False),
        )
        async def manage_authz_rules(
            operation: Annotated[str, Field(
                description="Action to perform; see the tool description.")],
            subject_type: Annotated[Literal["username", "clientid"], Field(
                description="Whether the rules attach to a username or a client id."
            )] = "username",
            subject: Annotated[str | None, Field(
                description="The username or client id the rules apply to.")] = None,
            rules: Annotated[list[dict] | None, Field(
                description="Rules for create, e.g. "
                            '[{"topic":"sensors/#","action":"subscribe",'
                            '"permission":"allow"}].')] = None,
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            if operation not in allowed_z:
                raise ToolError(
                    f"Operation {operation!r} is switched off. "
                    f"Allowed: {allowed_z_list}."
                )
            bucket = "users" if subject_type == "username" else "clients"
            base = f"/authorization/sources/built_in_database/rules/{bucket}"

            # Without this check EMQX answers a bare 404, which reads as "wrong
            # identifier" when the real problem is that the broker has no
            # built-in ACL table at all. That is a common setup: the default
            # install ships a file source only.
            sources = json_body(await emqx_request(
                emqx, "GET", "/authorization/sources"))
            source_rows = (sources.get("sources", sources)
                           if isinstance(sources, dict) else sources) or []
            kinds = [r.get("type") for r in source_rows if isinstance(r, dict)]
            if "built_in_database" not in kinds:
                raise ToolError(
                    "This broker has no built_in_database authorization source, "
                    "so there is no ACL table to read or write. Add one in the EMQX "
                    "Dashboard under Access Control -> Authorization -> Create "
                    "-> Built-in Database, then retry. Use "
                    "emqx_list_authz_sources to inspect what is in place."
                )

            if operation == "read":
                rows, _ = page_of(json_body(await emqx_request(emqx, "GET", base)))
                return {"subject_type": subject_type, "count": len(rows),
                        "rules": rows}
            if operation == "create":
                if not subject or not rules:
                    raise ToolError("create requires both `subject` and `rules`.")
                key = "username" if subject_type == "username" else "clientid"
                await emqx_request(emqx, "POST", base,
                                   json=[{key: subject, "rules": rules}])
                return {"subject": subject, "rules": rules, "created": True}
            if operation == "delete":
                if not subject:
                    raise ToolError("delete requires `subject`.")
                await emqx_request(emqx, "DELETE", f"{base}/{path_segment(subject, 'subject')}")
                return {"subject": subject, "deleted": True}

            raise ToolError(f"Unknown operation {operation!r}.")

    if on("emqx_list_banned"):

        @mcp.tool(name="emqx_list_banned", tags={"emqx", "read", "security"},
                  annotations=read_only("List Banned Clients"))
        async def list_banned(
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Everything currently barred from connecting, with expiry times."""
            rows, meta = page_of(json_body(await emqx_request(emqx, "GET", "/banned")))
            return {"count": len(rows), "total": meta.get("count", len(rows)),
                    "banned": rows}

    if on("emqx_ban"):

        @mcp.tool(name="emqx_ban", tags={"emqx", "write", "destructive", "security"},
                  annotations=destructive("Ban MQTT Client"))
        async def ban(
            subject_type: Annotated[BanSubject, Field(
                description="What to match on: client id, username or peer IP.")],
            who: Annotated[str, Field(
                description="The exact value to ban.")],
            reason: Annotated[str | None, Field(
                description="Free-text note kept with the ban entry.")] = None,
            until: Annotated[int | None, Field(
                description="Unix seconds when the ban lifts. Omit for permanent."
            )] = None,
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """[DESTRUCTIVE] Block a client id, username or IP from connecting.

            Unlike emqx_kick_client this persists — the target stays out until
            the ban expires or is lifted with emqx_unban.
            """
            payload: dict[str, Any] = {"as": subject_type, "who": who,
                                       "by": "emqx-mcp"}
            if reason:
                payload["reason"] = reason
            if until:
                payload["until"] = until
            await emqx_request(emqx, "POST", "/banned", json=payload)
            return {"as": subject_type, "who": who, "until": until, "banned": True}

    if on("emqx_unban"):

        @mcp.tool(name="emqx_unban", tags={"emqx", "write", "destructive", "security"},
                  annotations=destructive("Unban MQTT Client"))
        async def unban(
            subject_type: Annotated[BanSubject, Field(
                description="Must match the type used when the ban was created.")],
            who: Annotated[str, Field(description="The exact banned value.")],
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """[DESTRUCTIVE] Lift a ban so the client may connect again."""
            await emqx_request(
                emqx, "DELETE", f"/banned/{path_segment(subject_type, 'subject_type')}/{path_segment(who, 'who')}")
            return {"as": subject_type, "who": who, "unbanned": True}
