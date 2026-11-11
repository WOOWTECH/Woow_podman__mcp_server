"""Executable 29-operation contract pinned to Headscale v0.29.4.

Source: juanfont/headscale@8106636c7f8d0cf8c9d4fb0beae94415069b768b,
proto/headscale/v1/headscale.proto and gen/openapiv2/headscale/v1/headscale.swagger.json.
No arbitrary method, path, headers, origin or proxy can be selected by a caller.
"""
from dataclasses import dataclass
from types import MappingProxyType

from pydantic import create_model, ValidationError

from .errors import HeadscaleError
from .models import Arguments, AuthId, Bool, Id, NonemptyTags, NodeName, PolicyText, Prefix, Routes, Tags, Text, Timestamp, Username


@dataclass(frozen=True)
class NativeOperation:
    method: str
    path: str
    model: type[Arguments]
    path_fields: tuple[str, ...] = ()
    query_fields: tuple[str, ...] = ()
    response_field: str | None = None
    read_only: bool = False


def _op(name, method, path, fields=None, *, path_fields=(), query=(), response=None, read=False):
    return NativeOperation(method, "/api/v1" + path,
                           create_model(name + "Request", __base__=Arguments, **(fields or {})),
                           tuple(path_fields), tuple(query), response, read)


NATIVE_OPERATIONS = MappingProxyType({
    "CreateUser": _op("CreateUser", "POST", "/user", {"name": (Username, ...), "displayName": (Text, ""), "email": (Text, ""), "pictureUrl": (Text, "")}, response="user"),
    "RenameUser": _op("RenameUser", "POST", "/user/{oldId}/rename/{newName}", {"oldId": (Id, ...), "newName": (Username, ...)}, path_fields=("oldId", "newName"), response="user"),
    "DeleteUser": _op("DeleteUser", "DELETE", "/user/{id}", {"id": (Id, ...)}, path_fields=("id",)),
    "ListUsers": _op("ListUsers", "GET", "/user", {"id": (Id, None), "name": (Username, None), "email": (Text, None)}, query=("id", "name", "email"), response="users", read=True),
    "CreatePreAuthKey": _op("CreatePreAuthKey", "POST", "/preauthkey", {"user": (Id, None), "reusable": (Bool, False), "ephemeral": (Bool, False), "expiration": (Timestamp, ...), "aclTags": (Tags, [])}, response="preAuthKey"),
    "ExpirePreAuthKey": _op("ExpirePreAuthKey", "POST", "/preauthkey/expire", {"id": (Id, ...)}),
    "DeletePreAuthKey": _op("DeletePreAuthKey", "DELETE", "/preauthkey", {"id": (Id, ...)}, query=("id",)),
    "ListPreAuthKeys": _op("ListPreAuthKeys", "GET", "/preauthkey", response="preAuthKeys", read=True),
    "DebugCreateNode": _op("DebugCreateNode", "POST", "/debug/node", {"user": (Username, ...), "key": (AuthId, ...), "name": (NodeName, ...), "routes": (Routes, [])}, response="node"),
    "GetNode": _op("GetNode", "GET", "/node/{nodeId}", {"nodeId": (Id, ...)}, path_fields=("nodeId",), response="node", read=True),
    "SetTags": _op("SetTags", "POST", "/node/{nodeId}/tags", {"nodeId": (Id, ...), "tags": (NonemptyTags, ...)}, path_fields=("nodeId",), response="node"),
    "SetApprovedRoutes": _op("SetApprovedRoutes", "POST", "/node/{nodeId}/approve_routes", {"nodeId": (Id, ...), "routes": (Routes, ...)}, path_fields=("nodeId",), response="node"),
    "RegisterNode": _op("RegisterNode", "POST", "/node/register", {"user": (Username, ...), "key": (AuthId, ...)}, query=("user", "key"), response="node"),
    "DeleteNode": _op("DeleteNode", "DELETE", "/node/{nodeId}", {"nodeId": (Id, ...)}, path_fields=("nodeId",)),
    "ExpireNode": _op("ExpireNode", "POST", "/node/{nodeId}/expire", {"nodeId": (Id, ...), "expiry": (Timestamp, None), "disableExpiry": (Bool, False)}, path_fields=("nodeId",), query=("expiry", "disableExpiry"), response="node"),
    "RenameNode": _op("RenameNode", "POST", "/node/{nodeId}/rename/{newName}", {"nodeId": (Id, ...), "newName": (NodeName, ...)}, path_fields=("nodeId", "newName"), response="node"),
    "ListNodes": _op("ListNodes", "GET", "/node", {"user": (Username, None)}, query=("user",), response="nodes", read=True),
    "BackfillNodeIPs": _op("BackfillNodeIPs", "POST", "/node/backfillips", {"confirmed": (Bool, ...)}, query=("confirmed",), response="changes"),
    "AuthRegister": _op("AuthRegister", "POST", "/auth/register", {"user": (Username, ...), "authId": (AuthId, ...)}, response="node"),
    "AuthApprove": _op("AuthApprove", "POST", "/auth/approve", {"authId": (AuthId, ...)}),
    "AuthReject": _op("AuthReject", "POST", "/auth/reject", {"authId": (AuthId, ...)}),
    "CreateApiKey": _op("CreateApiKey", "POST", "/apikey", {"expiration": (Timestamp, ...)}, response="apiKey"),
    "ExpireApiKey": _op("ExpireApiKey", "POST", "/apikey/expire", {"id": (Id, None), "prefix": (Prefix, None)}),
    "ListApiKeys": _op("ListApiKeys", "GET", "/apikey", response="apiKeys", read=True),
    "DeleteApiKey": _op("DeleteApiKey", "DELETE", "/apikey/{prefix}", {"prefix": (Prefix, ...)}, path_fields=("prefix",)),
    "GetPolicy": _op("GetPolicy", "GET", "/policy", response="policy", read=True),
    "SetPolicy": _op("SetPolicy", "PUT", "/policy", {"policy": (PolicyText, ...)}, response="policy"),
    "CheckPolicy": _op("CheckPolicy", "POST", "/policy/check", {"policy": (PolicyText, ...)}, read=True),
    "Health": _op("Health", "GET", "/health", response="databaseConnectivity", read=True),
})


def native_arguments(name, parameters):
    try:
        op = NATIVE_OPERATIONS[name]
        args = op.model.model_validate(parameters).model_dump(exclude_unset=True, exclude_none=True)
        if name == "ListUsers" and len(args) > 1:
            raise ValueError()
        if name == "CreatePreAuthKey" and not (args.get("user") or args.get("aclTags")):
            raise ValueError()
        if name == "ExpireApiKey" and len(args) != 1:
            raise ValueError()
        if name == "ExpireNode" and args.get("disableExpiry") and "expiry" in args:
            raise ValueError()
        if name == "BackfillNodeIPs" and args.get("confirmed") is not True:
            raise ValueError()
        if name in ("DeleteApiKey", "ExpireApiKey") and "prefix" in args:
            if len(args["prefix"]) == 10 and args["prefix"].endswith("***"):
                args["prefix"] = args["prefix"][:-3]
        return op, args
    except (KeyError, TypeError, ValueError, ValidationError):
        raise HeadscaleError("HEADSCALE_INVALID_ARGUMENTS") from None

