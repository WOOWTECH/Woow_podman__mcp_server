"""Hermes MCP 的輸入守衛：拼進網址的 id、設定寫入的 deny-list、MCP server 網址。

MCP 工具（server.py）與管理後台的 dashboard 直通（hermes_mcp_admin/routers/dashboard_proxy.py）共用同一套規則。
商用驗收 QA 服務 249 實測（2026-10-16）的四個缺口：

- AI-HMCP-001-1：session_id／job_id／name 用 f-string 拼進 dashboard 網址。httpx 送出前會把 ``..`` 正規化掉，
  ``session_id="../config/raw"`` 實際打到 ``GET /api/config/raw``，讀出含 provider api_key 明文的 yaml。
  → ``path_segment()``：白名單＋拒絕 ``..``，再 ``quote(safe="")``。
- AI-HMCP-001-3：deny-list 只做字串全等，``terminal``（父鍵整棵換成純量）、大小寫、空白、全形、零寬字元都繞得過。
  → ``config_key_denial()``：先正規化（NFKC、去除格式字元、strip、casefold、拆段），再比對前綴與祖先。
- AI-HMCP-002-2：``hermes_mcp add`` 只擋小寫 ``stdio:``／``command:`` 前綴；後台直通可新增 stdio command。
  → ``check_mcp_server_url()``／``check_mcp_server_body()``：只收 http／https，並套用 SSRF 檢查。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import quote, urlsplit

from mcp_admin_core.util import ssrf


class GuardError(ValueError):
    """輸入被安全規則拒絕。ValueError：MCP 工具回 isError=true（文字由 server.py 的 keep_error_text 保留），後台轉成 HTTP 400。"""


# ---------------------------------------------------------------------------
# 拼進網址的 id／name（AI-HMCP-001-1）
# ---------------------------------------------------------------------------

# Hermes 的 id 實際長相：session 20261016_193839_d56dfbc7、UUID、qa1011-hermes-g56-import-01；cron job 12 位 hex；
# webhook／toolset／MCP server 名稱。只收字母數字（含 Unicode）與 _ . - : @ + ~，中間可以有單一空白。
# 沒有 / \ ? # %，所以一個值永遠只佔一段路徑；再拒絕含 ".." 與單一 "."（httpx 會把這兩種段落正規化掉）。
_SEGMENT = re.compile(r"[\w.:@+~-]+(?: [\w.:@+~-]+)*")
MAX_SEGMENT = 200


def path_segment(value: Any, field: str) -> str:
    """回傳可以安全拼進 dashboard 網址的單一路徑段（已百分比編碼）；不合格丟 GuardError。"""
    if not isinstance(value, str) or not value:
        raise GuardError(f"{field} is required")
    if len(value) > MAX_SEGMENT or value == "." or ".." in value or not _SEGMENT.fullmatch(value):
        raise GuardError(
            f"invalid {field}: only letters, digits, spaces and _ . - : @ + ~ are allowed, "
            f"without '..' or '/'"
        )
    return quote(value, safe="")


# ---------------------------------------------------------------------------
# 設定寫入的 deny-list（AI-HMCP-001-3）
# ---------------------------------------------------------------------------

# 根鍵：本身與底下任何鍵都不能經 MCP 寫入（舊清單的 terminal.backend、api_server.host、dashboard.basic_auth、
# secrets 都含在內）。model 與 MCP server 有專用工具（hermes_model、hermes_mcp），不開放用 hermes_config 繞過驗證；
# security 是指令安全掃描設定；providers／custom_providers 決定模型流量送去哪裡、帶什麼金鑰。
DENIED_CONFIG_KEYS = frozenset({
    "api_server",
    "custom_providers",
    "dashboard",
    "mcp_servers",
    "model",
    "providers",
    "secrets",
    "security",
    "terminal",
})

# 正規化後每一段只能是這些字元；空段（a..b、.a、a.）、括號索引（a[0]）、空白都不收。
_KEY_SEGMENT = re.compile(r"[a-z0-9_-]+")


def normalize_config_key(key: str) -> str:
    """比對用：NFKC（全形→半形）、去掉格式字元（零寬空白、soft hyphen）、strip、casefold。"""
    return _normalized_key_spelling(key).casefold()


def _normalized_key_spelling(key: str) -> str:
    # Keep camel-case boundaries for the credential-name detector.
    text = unicodedata.normalize("NFKC", key)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf").strip()


def config_key_denial(key: Any, secret_key: Any = None) -> str | None:
    """回傳拒絕原因；可以寫入時回 None。``secret_key(name)`` 為 True 的段落也拒絕（不經 MCP 寫憑證）。"""
    if not isinstance(key, str) or not key.strip():
        return "key is required"
    parts = normalize_config_key(key).split(".")
    if not all(_KEY_SEGMENT.fullmatch(part) for part in parts):
        return "key must be dot-separated segments of letters, digits, '_' or '-'"
    if parts[0].startswith("_"):
        return "internal keys (leading '_') cannot be written"
    for denied in DENIED_CONFIG_KEYS:
        d_parts = denied.split(".")
        # 本身或子鍵（terminal.backend）被擋；祖先（寫父鍵會把整棵子樹換掉）也被擋。
        if parts[: len(d_parts)] == d_parts or d_parts[: len(parts)] == parts:
            return f"'{denied}' and the keys under it cannot be changed through MCP"
    if secret_key is not None and any(secret_key(part) for part in _normalized_key_spelling(key).split(".")):
        return "credential keys cannot be changed through MCP"
    return None


def config_value_denial(key: str, value: Any, secret_key: Any = None) -> str | None:
    """Apply the same path policy to every descendant, including objects in lists."""
    denial = config_key_denial(key, secret_key)
    if denial:
        return denial
    if isinstance(value, dict):
        for child, item in value.items():
            denial = config_value_denial(f"{key}.{child}", item, secret_key)
            if denial:
                return denial
    elif isinstance(value, list):
        for item in value:
            denial = config_value_denial(key, item, secret_key)
            if denial:
                return denial
    return None


def config_patch_applied(requested: Any, actual: Any) -> bool:
    """Hermes deep-merges objects; untouched siblings are not failed writes."""
    if isinstance(requested, dict):
        return isinstance(actual, dict) and all(
            key in actual and config_patch_applied(value, actual[key])
            for key, value in requested.items()
        )
    # Arrays and scalar values are replaced, rather than merged.
    return _exact_config_value(requested, actual)


def _exact_config_value(requested: Any, actual: Any) -> bool:
    if type(actual) is not type(requested):
        return False
    if isinstance(requested, dict):
        return requested.keys() == actual.keys() and all(
            _exact_config_value(value, actual[key]) for key, value in requested.items()
        )
    if isinstance(requested, list):
        return len(requested) == len(actual) and all(
            _exact_config_value(value, observed) for value, observed in zip(requested, actual)
        )
    return actual == requested


def clean_config_key(key: str) -> str:
    """實際送給 Hermes 的鍵：NFKC＋strip（大小寫保留）。只在 config_key_denial() 回 None 之後使用。"""
    return unicodedata.normalize("NFKC", key).strip()


# ---------------------------------------------------------------------------
# MCP server 網址（AI-HMCP-002-2）
# ---------------------------------------------------------------------------

# 只有 stdio transport 會用到的欄位：帶了就代表要 Hermes spawn 本機程式。
STDIO_FIELDS = ("command", "args", "env", "cwd")


# One wording for a loopback MCP server URL on PaaS and the HA add-on (the HA add-on checks the name and
# every resolved address itself; here the SSRF module's loopback verdict is mapped to the same sentence).
LOOPBACK_MCP_URL_MESSAGE = "loopback MCP servers (localhost, 127.0.0.0/8, ::1) cannot be added"
_SSRF_LOCALHOST_REASON = "Localhost access is blocked"


async def check_mcp_server_url(url: Any) -> str:
    """回傳 strip 過的網址；不是 http／https、帶帳密，或指向內網／metadata（SSRF strict）就丟 GuardError。"""
    if not isinstance(url, str) or not url.strip():
        raise GuardError("url is required")
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise GuardError("invalid url") from exc
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise GuardError("only http:// or https:// MCP servers can be added (stdio/command transports are blocked)")
    result = await ssrf.check_url(url)
    if not result.ok:
        if result.reason == _SSRF_LOCALHOST_REASON:
            raise GuardError(LOOPBACK_MCP_URL_MESSAGE)
        raise GuardError(f"url blocked by SSRF protection: {result.reason}")
    return url


async def check_mcp_server_body(body: Any) -> dict[str, Any]:
    """後台直通 POST /api/mcp/servers 的 payload：拒絕 stdio、command 類欄位與不合格網址。"""
    if not isinstance(body, dict):
        raise GuardError("body must be a JSON object")
    for field in ("transport", "type"):
        value = body.get(field)
        if value is not None and (not isinstance(value, str) or value.strip().casefold() == "stdio"):
            raise GuardError("stdio transport is blocked for security")
    present = [field for field in STDIO_FIELDS if body.get(field) not in (None, "", [], {})]
    if present:
        raise GuardError(f"stdio transport is blocked for security ({', '.join(present)} not allowed)")
    path_segment(body.get("name"), "name")
    return {**body, "url": await check_mcp_server_url(body.get("url"))}
