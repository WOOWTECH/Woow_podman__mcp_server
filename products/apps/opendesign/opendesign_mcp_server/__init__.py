"""vendored Open Design MCP server 套件。

#1354：Open Design daemon 前置 basic auth（PaaS 的 open-design 服務）。admin 把 connection 的
``od_basic_user``／``od_basic_password`` 注入子行程 env ``OD_BASIC_USER``／``OD_BASIC_PASSWORD``。

vendored ``od_mcp_server.py`` 要維持 byte-faithful 不改檔，它只認 ``OD_API_BASE``（模組頂層
讀取）並以 ``httpx.Client(base_url=OD_API_BASE, ...)`` 建 client（``_get_client`` 與每次
``_api_post_sse`` 各建一個）。因此在**本套件 import 時**：

1. 先決定帳密：``OD_BASIC_USER`` 非空就用它；否則相容舊做法——``OD_API_BASE`` 內的
   ``user:password@`` userinfo。
2. 把 ``OD_API_BASE`` 的 userinfo 去掉再讓 vendored 模組讀取——帳密若留在 URL，httpx 的
   錯誤訊息（``raise_for_status``）會把 ``user:password@`` 原文帶進工具錯誤回給 MCP client。
3. import vendored 模組後，把它的 ``httpx`` 換成只多一件事的 shim：``Client(...)`` 預設帶
   ``auth=httpx.BasicAuth(user, password)``；其餘屬性原樣委派給真正的 httpx。

沒有帳密時完全不動（``OD_API_BASE`` 原樣、``httpx`` 不換）。

MCP SDK 2.x（#78）：vendored 模組寫的是 ``from mcp.server.fastmcp import FastMCP``；mcp 2.x 把 FastMCP 改名為
``mcp.server.mcpserver.MCPServer``，``mcp.server.fastmcp`` 一 import 就丟 ``ModuleNotFoundError``。為了不改
vendored 檔（re-sync 上游時整檔覆蓋），import 它的那一刻暫時讓 ``mcp.server.fastmcp`` 指到只有
``FastMCP = MCPServer`` 的別名模組，import 完立刻拿掉（見 :func:`_import_vendored`），其他程式碼看不到這個別名。
"""
from __future__ import annotations

import importlib
import os
import sys
import types
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx


def _split_userinfo(url: str) -> tuple[str, str | None, str | None]:
    """(去掉 userinfo 的 URL, user, password)；無 userinfo 時 user/password 為 None。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url, None, None
    if "@" not in parts.netloc:
        return url, None, None
    userinfo, _, hostport = parts.netloc.rpartition("@")
    user, sep, password = userinfo.partition(":")
    return urlunsplit(parts._replace(netloc=hostport)), unquote(user), (unquote(password) if sep else None)


def resolve_basic_auth_env(environ: dict | Any | None = None) -> tuple[str, str] | None:
    """依 env 決定 basic auth 帳密，並把 ``OD_API_BASE`` 改寫成不含 userinfo；回 (user, password) 或 None。"""
    env = os.environ if environ is None else environ
    base = env.get("OD_API_BASE", "")
    creds: tuple[str, str] | None = None
    if base:
        clean, url_user, url_password = _split_userinfo(base)
        if url_user is not None:
            env["OD_API_BASE"] = clean
            if url_user:
                creds = (url_user, url_password or "")
    user = env.get("OD_BASIC_USER", "")
    if user:
        creds = (user, env.get("OD_BASIC_PASSWORD", ""))
    return creds


class _HttpxWithBasicAuth(types.ModuleType):
    """vendored 模組專用的 httpx shim：``Client`` 預設帶 BasicAuth，其餘委派真正的 httpx。"""

    def __init__(self, user: str, password: str) -> None:
        super().__init__("httpx")
        self._auth = httpx.BasicAuth(user, password)

    def Client(self, *args: Any, **kwargs: Any) -> httpx.Client:  # noqa: N802 — 對齊 httpx API
        kwargs.setdefault("auth", self._auth)
        return httpx.Client(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(httpx, name)


def install_basic_auth(module: types.ModuleType, creds: tuple[str, str] | None) -> bool:
    """有帳密時把 *module*（vendored od_mcp_server）的 ``httpx`` 換成帶 BasicAuth 的 shim。"""
    if not creds:
        return False
    module.httpx = _HttpxWithBasicAuth(*creds)
    client = getattr(module, "_client", None)
    if client is not None:  # 已建立的共用 client 作廢，下次 _get_client 會用 shim 重建
        client.close()
        module._client = None
    return True


_FASTMCP_MODULE = "mcp.server.fastmcp"


def _import_vendored() -> types.ModuleType:
    """import vendored ``od_mcp_server``；只在 import 期間把 ``mcp.server.fastmcp`` 別名成 mcp 2.x 的 MCPServer。"""
    from mcp.server.mcpserver import MCPServer

    alias = types.ModuleType(_FASTMCP_MODULE)
    alias.FastMCP = MCPServer
    previous = sys.modules.get(_FASTMCP_MODULE)
    sys.modules[_FASTMCP_MODULE] = alias
    try:
        return importlib.import_module(f"{__name__}.od_mcp_server")
    finally:
        if previous is None:
            sys.modules.pop(_FASTMCP_MODULE, None)
        else:
            sys.modules[_FASTMCP_MODULE] = previous


_creds = resolve_basic_auth_env()

_od_mcp_server = _import_vendored()  # 必須在 OD_API_BASE 改寫之後 import

install_basic_auth(_od_mcp_server, _creds)

# Install before either the production HTTP launcher or the stdio wrapper
# starts the vendored server. The vendored source itself remains byte-faithful.
from .input_contract import install_input_contract
from .input_policy import install_input_guards
from .output_policy import install_output_guards

install_input_guards(_od_mcp_server.mcp)
install_output_guards(_od_mcp_server.mcp)
install_input_contract(_od_mcp_server.mcp)
