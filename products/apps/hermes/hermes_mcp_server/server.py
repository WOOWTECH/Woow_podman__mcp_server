"""Hermes MCP Server — MCP SDK ``MCPServer`` with 11 tools bridging Gateway + Dashboard APIs.

Gateway API  (:8642) — OpenAI-compatible API with Bearer auth  [A]
Dashboard API (:9119) — Management REST API with cookie auth   [B]

Environment variables for connection config:
  HERMES_GATEWAY_URL, HERMES_GATEWAY_API_KEY
  HERMES_DASHBOARD_URL, HERMES_DASHBOARD_USERNAME, HERMES_DASHBOARD_PASSWORD
"""

from __future__ import annotations

import asyncio
import email.utils
import functools
import hashlib
import json
import math
import os
import re
import time
from typing import Any, Awaitable, Callable

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from mcp_admin_core.tool_error_text import keep_error_text
from pydantic import StrictInt

from . import errors, session_mutations

# ---------------------------------------------------------------------------
# MCP server instance（MCP SDK 2.x 的 MCPServer，即 1.x 的 FastMCP；#78）
# ---------------------------------------------------------------------------

mcp = MCPServer("hermes-mcp-server")

# ---------------------------------------------------------------------------
# 輸入守衛：拼進網址的 id、設定寫入的 deny-list、MCP server 網址（AI-HMCP-001-1／-001-3／-002-2）
# ---------------------------------------------------------------------------

# 規則放在 guards.py，管理後台的 dashboard 直通也用同一套。舊清單（terminal.backend、api_server.host…）是
# 字串全等比對，大小寫、空白、父鍵（terminal）都繞得過；現在 DENIED_CONFIG_KEYS 是根鍵，比對前先正規化。
from . import guards  # noqa: E402
from .guards import DENIED_CONFIG_KEYS  # noqa: E402,F401  （相容：仍可讀 server.DENIED_CONFIG_KEYS）

# ---------------------------------------------------------------------------
# 設定讀取的憑證遮罩（woow_paas_platform：hermes_config get 曾原文回傳 api_key）
# ---------------------------------------------------------------------------

# Hermes 把自己用的憑證存在設定裡（例如自訂 OpenAI 相容端點的 custom_providers[].api_key），GET /api/config 會原文回傳。
# 讀設定時保留每個欄位名稱，看得出秘密有沒有設，但不回傳值。鍵名拆成單字（非英數與 camelCase 為界）後，任一字在
# SECRET_WORDS，或最後一字在 SECRET_LAST_WORDS（api_key、OPENAI_API_KEY、access_token、basic_auth；不含 max_tokens、
# auth_mode）就是秘密鍵；其下所有非空值（含巢狀物件與 list）換成 [REDACTED]，空字串、null、布林保留，所以「沒設定」
# 仍看得到。所有字串裡網址 userinfo 的密碼也遮掉。寫入（action=set）不變。
REDACTED = "[REDACTED]"
SECRET_WORDS = frozenset({"password", "passwd", "passphrase", "secret", "secrets", "authorization", "credential",
                          "credentials", "apikey", "cookie", "cookies"})
SECRET_LAST_WORDS = frozenset({"key", "token", "auth", "pat", "bearer"})
_KEY_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_URL_PASSWORD = re.compile(r"(://[^/\s:@]*:)[^/\s@]+@")


def _secret_key(name: Any) -> bool:
    words = [word.lower() for word in _KEY_WORD.findall(str(name))]
    return bool(words) and (any(word in SECRET_WORDS for word in words) or words[-1] in SECRET_LAST_WORDS)


def _redact_all(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _redact_all(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_all(v) for v in value]
    if value is None or isinstance(value, bool) or value == "":
        return value
    return REDACTED


def redact_config(value: Any) -> Any:
    """設定值中每個秘密欄位的值換成 [REDACTED]（欄位名稱保留）。"""
    if isinstance(value, dict):
        return {k: _redact_all(v) if _secret_key(k) else redact_config(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_config(v) for v in value]
    if isinstance(value, str):
        return _URL_PASSWORD.sub(r"\1" + REDACTED + "@", value)
    return value


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


class ConnectionNotConfiguredError(RuntimeError):
    """連線設定缺漏（或形狀不對）時丟出，訊息直接指向 admin GUI 的修正動作。

    存在的理由：把 ``base_url=""`` 交給 httpx 只會得到
    ``Request URL is missing an 'http://' or 'https://' protocol.``——那句會把人帶去查
    「哪個 URL 少了 http:// 前綴」，而真因是租戶根本沒填。admin REST 面
    （routers/config.py、routers/health.py、routers/dashboard_proxy.py）本來就會回
    「not configured」，MCP 工具面必須講同一句話。
    """


# 每個連線欄位對應 admin GUI 的哪個區塊（訊息要能直接指向動作）
_ADMIN_HINTS = {
    "gateway_url": "Connection > Gateway API Server",
    "gateway_api_key": "Connection > Gateway API Server",
    "dashboard_url": "Connection > Dashboard REST API",
    "dashboard_username": "Connection > Dashboard REST API",
    "dashboard_password": "Connection > Dashboard REST API",
}


def _get_connection() -> dict[str, str]:
    """Read connection config from environment variables."""
    return {
        "gateway_url": os.environ.get("HERMES_GATEWAY_URL", ""),
        "gateway_api_key": os.environ.get("HERMES_GATEWAY_API_KEY", ""),
        "dashboard_url": os.environ.get("HERMES_DASHBOARD_URL", ""),
        "dashboard_username": os.environ.get("HERMES_DASHBOARD_USERNAME", ""),
        "dashboard_password": os.environ.get("HERMES_DASHBOARD_PASSWORD", ""),
    }


def _require_url(value: str, field: str) -> str:
    """回傳正規化後的 base URL；Dashboard 格式錯誤也回固定設定指引。

    兩種形狀都會讓 httpx 丟出同一句誤導訊息，所以兩種都在這裡攔下來：
    空字串（沒設定）與 ``host:port``（有值但沒有 scheme）。
    """
    url = (value or "").strip()
    where = _ADMIN_HINTS[field]
    if not url:
        raise ConnectionNotConfiguredError(
            f"{field} is not configured - set it in the admin GUI under {where}"
        )
    if not url.startswith(("http://", "https://")):
        raise ConnectionNotConfiguredError(
            f"{field} must start with http:// or https:// - "
            f"fix it in the admin GUI under {where}"
        )
    if field == "dashboard_url":
        try:
            parsed = httpx.URL(url)
            if not parsed.host:
                raise httpx.InvalidURL("missing host")
        except httpx.InvalidURL:
            raise ConnectionNotConfiguredError(
                f"{field} must be a valid http:// or https:// URL - "
                f"fix it in the admin GUI under {where}"
            ) from None
    return url.rstrip("/")


def _check_gateway_response(resp: httpx.Response, api_key: str) -> None:
    """Gateway 回 401/403 而 api key 根本沒設定時，講「沒設定」而不是「認證失敗」。

    只在 key 為空時改寫：key 有填卻被拒＝真的認證失敗，必須原樣回報。
    """
    if resp.status_code in (401, 403) and not api_key:
        raise ConnectionNotConfiguredError(
            f"gateway_api_key is not configured - set it in the admin GUI under "
            f"{_ADMIN_HINTS['gateway_api_key']} (Hermes Gateway returned HTTP {resp.status_code})"
        )


# AI-HMCP-001-6：一輪 agent 對話（POST /v1/responses）常超過舊的固定 30 秒（QA 249：terminal sleep 50 在 30.2 秒
# 回空白錯誤，Hermes 那一輪還繼續跑）。hermes_chat 一輪最多等 CHAT_TIMEOUT 秒，可用 HERMES_MCP_CHAT_TIMEOUT 調
# （1–3600）。PaaS 預設 90：MCP 經 Cloudflare 時單一回應約 100 秒就會被切、閘道 tools/call 期限 110 秒，要在那之前
# 自己逾時並回可讀訊息；遠端是否中止仍需確認。HA add-on 的閘道允許 330 秒，vendor 那份預設 300。其他呼叫維持 30 秒。
CHAT_TIMEOUT = 90.0


def _chat_timeout() -> float:
    try:
        value = float(os.environ.get("HERMES_MCP_CHAT_TIMEOUT", CHAT_TIMEOUT))
    except ValueError:
        return CHAT_TIMEOUT
    return min(max(value, 1.0), 3600.0) if math.isfinite(value) else CHAT_TIMEOUT


def _gateway_client(url: str, api_key: str, timeout: float | httpx.Timeout = 30.0) -> httpx.AsyncClient:
    """Create an httpx client for the Hermes Gateway API with Bearer auth."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return httpx.AsyncClient(
        base_url=_require_url(url, "gateway_url"),
        headers=headers,
        timeout=timeout,
    )


# Dashboard cookie cache: {url#憑證指紋: (cookie, expiry)}
# AI-HMCP-003-1：鍵含帳密指紋（換了帳密就不沿用舊 cookie），遇 401 清除並重登一次（見 _get_dashboard_client）。
_cookie_cache: dict[str, tuple[str, float]] = {}

# 登入的 single-flight 鎖：{快取鍵: asyncio.Lock}（AI-HER-PERF-1；鍵同 _cookie_cache）。
# Hermes dashboard 的 /auth/password-login 每個來源 IP 每 60 秒只放 10 次（成功也算），且 429 不帶 Retry-After。
# 同一個子行程服務所有 MCP session，冷啟動或 cookie 到期時若各自登入，10 個以上並發 session 就會撞限流。
# 同一個 dashboard_url 同時只讓一個呼叫去登入，其他人等它把 cookie 放進快取後直接共用。
_login_locks: dict[str, asyncio.Lock] = {}

# 登入回 429 時的重試：最多再試 LOGIN_MAX_RETRIES 次；有 Retry-After 就照它等（上限 LOGIN_RETRY_AFTER_CAP 秒），
# 沒有（Hermes 0.21.6 就沒帶）就指數退避 1、2 秒。上限壓在 10 秒，免得工具呼叫卡太久。
LOGIN_MAX_RETRIES = 2
LOGIN_BACKOFF_BASE = 1.0
LOGIN_RETRY_AFTER_CAP = 10.0
_sleep = asyncio.sleep  # 測試替換用


def _login_retry_delay(resp: httpx.Response, attempt: int) -> float:
    """第 attempt 次（從 0 起算）被 429 後要等幾秒：Retry-After（秒數或 HTTP 日期）優先，否則指數退避。"""
    value = (resp.headers.get("retry-after") or "").strip()
    delay: float | None = None
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(value)
                delay = when.timestamp() - time.time()
            except (TypeError, ValueError, IndexError, OverflowError):
                delay = None
    if delay is None or delay != delay:  # 解析不了（或 NaN）就退回指數退避
        delay = LOGIN_BACKOFF_BASE * (2 ** attempt)
    return min(max(delay, 0.0), LOGIN_RETRY_AFTER_CAP)


def _cookie_key(url: str, username: str, password: str) -> str:
    """快取鍵：dashboard URL＋帳密的 sha256 指紋（不含明文）。"""
    digest = hashlib.sha256(f"{username}\0{password}".encode()).hexdigest()[:16]
    return f"{url}#{digest}"


def _cached_cookie(key: str) -> str | None:
    cached = _cookie_cache.get(key)
    if cached and cached[1] > time.time():
        return cached[0]
    return None


async def _dashboard_login(url: str, username: str, password: str) -> str:
    """Login to Hermes Dashboard and return session cookie (cached 10 min).

    同一個 dashboard_url 的並發登入合併成一次（single-flight），登入回 429 時退避重試（AI-HER-PERF-1）。
    """
    url = _require_url(url, "dashboard_url")
    key = _cookie_key(url, username, password)
    cached = _cached_cookie(key)
    if cached is not None:
        return cached
    lock = _login_locks.setdefault(key, asyncio.Lock())
    async with lock:
        # 等鎖的期間，前一個持鎖者可能已經登入成功
        cached = _cached_cookie(key)
        if cached is not None:
            return cached
        return await _password_login(url, username, password)


async def _password_login(url: str, username: str, password: str) -> str:
    """實際打 /auth/password-login（呼叫端必須持有該 url 的登入鎖）。"""
    async with httpx.AsyncClient(base_url=url, timeout=10.0) as client:
        for attempt in range(LOGIN_MAX_RETRIES + 1):
            resp = await client.post(
                "/auth/password-login",
                json={"provider": "basic", "username": username, "password": password},
            )
            if resp.status_code != 429 or attempt == LOGIN_MAX_RETRIES:
                break
            await _sleep(_login_retry_delay(resp, attempt))
        # 帳密沒設定又被拒 ⇒ 講「沒設定」；有填卻被拒才是真的認證失敗（維持原樣拋出）。
        if resp.status_code in (401, 403) and not (username and password):
            missing = " / ".join(
                name for name, value in (
                    ("dashboard_username", username), ("dashboard_password", password),
                ) if not value
            )
            raise ConnectionNotConfiguredError(
                f"{missing} is not configured - set it in the admin GUI under "
                f"{_ADMIN_HINTS['dashboard_password']} "
                f"(Hermes Dashboard login returned HTTP {resp.status_code})"
            )
        resp.raise_for_status()
        for h in resp.headers.get_list("set-cookie"):
            if "hermes_session_at=" in h:
                token = h.split("hermes_session_at=")[1].split(";")[0].strip('"')
                _cookie_cache[_cookie_key(url, username, password)] = (token, time.time() + 600)
                return token
        return ""


def _cookie_header(cookie: str) -> str:
    return f'hermes_session_at="{cookie}"'


def _dashboard_client(url: str, cookie: str) -> httpx.AsyncClient:
    """Create an httpx client for the Hermes Dashboard API with cookie auth."""
    return httpx.AsyncClient(
        base_url=_require_url(url, "dashboard_url"),
        headers={"Content-Type": "application/json", "Cookie": _cookie_header(cookie)},
        timeout=30.0,
    )


def _relogin_on_401(client: httpx.AsyncClient, relogin: Callable[[], Awaitable[str]]) -> httpx.AsyncClient:
    """dashboard 回 401（Hermes 重啟或輪替後舊 cookie 失效）時：清快取、重登一次、重送同一個請求。

    只重試一次：重登後仍 401（或登入本身 401＝密碼已變）就把錯誤交給呼叫端，公開訊息走固定安全分類
    「憑證被拒」。帶 hermes_no_auth_retry 的 fenced session DELETE 原樣回傳 401，
    由呼叫端明確決定下一步；不在認證層隱式重送。
    包在 client 實例的 send 上（不換 transport），httpx 的 get／post／request 都經過它。
    """
    send = client.send
    pending = [relogin]

    async def send_with_relogin(request: httpx.Request, **kwargs: Any) -> httpx.Response:
        response = await send(request, **kwargs)
        if response.status_code != 401 or not pending or request.extensions.get("hermes_no_auth_retry") is True:
            return response
        retry = pending.pop()
        await response.aclose()
        header = _cookie_header(await retry())
        client.headers["Cookie"] = header
        request.headers["Cookie"] = header
        return await send(request, **kwargs)

    client.send = send_with_relogin  # type: ignore[method-assign]
    return client


async def _get_dashboard_client(conn: dict) -> httpx.AsyncClient:
    """Get authenticated dashboard client.

    守衛前置：先驗 dashboard_url（缺值、缺 scheme、無效 port 等），再 login。
    server.py 的 dashboard 系工具全部走這條，所以這裡是唯一的把關點。
    """
    url = _require_url(conn.get("dashboard_url", ""), "dashboard_url")
    username, password = conn.get("dashboard_username", ""), conn.get("dashboard_password", "")
    cookie = await _dashboard_login(url, username, password)

    async def relogin() -> str:
        # 只清掉「我剛用過、被拒的那個」cookie：多個 session 同時 401 時，晚到的不會把別人剛重登拿到的新 cookie
        # 也清掉，重登仍經 _dashboard_login 的 single-flight 合併成一次（不撞 247 的登入限流）。
        key = _cookie_key(url, username, password)
        if _cookie_cache.get(key, ("",))[0] == cookie:
            _cookie_cache.pop(key, None)
        return await _dashboard_login(url, username, password)

    return _relogin_on_401(_dashboard_client(url, cookie), relogin)


# ---------------------------------------------------------------------------
# 不存在的目標（AI-HMCP-001-8）
# ---------------------------------------------------------------------------


class HermesNotFoundError(LookupError):
    """要操作的 session／webhook／cron job／MCP server 不存在（不是 API 路徑錯）。"""


class HermesConfirmRequired(RuntimeError):
    """Hermes 要求確認（例如昂貴模型）才執行；什麼都沒改。"""


class HermesConfigNotApplied(RuntimeError):
    """設定寫入後讀回的值與要寫的不同（Hermes 不認得這個鍵，或把值正規化了）。"""


class HermesChatTimeout(TimeoutError):
    """The client stopped waiting; remote cancellation has not been confirmed."""


# list_providers 每個 provider 最多列幾個模型（OpenRouter 這類聚合商有上百個）
MODEL_LIST_LIMIT = 30


async def _resource_request(
    client: httpx.AsyncClient, method: str, path: str, kind: str, ident: str, **kwargs: Any
) -> httpx.Response:
    """對單一具名資源發請求；Hermes 回「這個資源不存在」的 404 時丟 HermesNotFoundError。

    Hermes 對不存在的資源回 404＋說明（「No subscription named 'x'」「Job not found」）；路由本身不存在時是
    FastAPI 預設的 {"detail": "Not Found"}，那種仍交給 errors.describe（指向版本／網址設定）。
    """
    response = await client.request(method, path, **kwargs)
    if response.status_code == 404 and errors.response_detail(response) not in ("", "Not Found"):
        raise HermesNotFoundError(errors.coded(errors.NOT_FOUND, f"Hermes {kind} {ident!r} not found (不存在)"))
    response.raise_for_status()
    return response


# ---------------------------------------------------------------------------
# 後端錯誤 → 給人看的訊息（AI-HMCP-003-3）
# ---------------------------------------------------------------------------

def _dashboard_public_error(exc: Exception) -> str:
    """Fixed MCP-visible Dashboard category, status and repair guidance (see errors.public_backend_error)."""
    return errors.public_backend_error(exc, "dashboard")


def _backend_errors(backend: str) -> Callable[[Callable[..., Awaitable[str]]], Callable[..., Awaitable[str]]]:
    """工具裡的例外翻成 ``CODE: text`` 公開訊息，再往上拋。

    Dashboard 與 Gateway 都使用固定公開分類，不轉述網址、例外原文或上游本文（errors.public_backend_error）。
    輸入守衛（GuardError）改標 INVALID_INPUT；未設定的連線標 BACKEND_UNAVAILABLE。
    例外照樣拋出，所以 MCP 照舊回 isError=true（文字由檔尾的 keep_error_text 保留）。
    其他程式錯誤原樣通過。functools.wraps 保留簽名，tools/list 的 schema 不變。
    """

    def decorate(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> str:
            try:
                return await fn(*args, **kwargs)
            except ConnectionNotConfiguredError as exc:
                raise ConnectionNotConfiguredError(f"BACKEND_UNAVAILABLE: {exc}") from None
            except guards.GuardError as exc:
                raise errors.HermesInputError(errors.coded(errors.INVALID_INPUT, str(exc))) from None
            except errors.TRANSLATABLE as exc:
                message = errors.public_backend_error(exc, backend)
                if isinstance(exc, httpx.HTTPStatusError):
                    raise errors.HermesBackendHTTPError(
                        message, request=exc.request, response=exc.response
                    ) from None
                raise errors.HermesBackendError(message) from None

        return wrapper

    return decorate


def _section_error(exc: Exception, backend: str, conn: dict[str, str]) -> str:
    """hermes_inspect 單一區段的錯誤訊息（沒設定的講沒設定，其餘走固定公開分類）。"""
    if isinstance(exc, ConnectionNotConfiguredError):
        return f"BACKEND_UNAVAILABLE: {exc}"
    if isinstance(exc, errors.TRANSLATABLE):
        return errors.public_backend_error(exc, backend)
    return str(exc)


# ---------------------------------------------------------------------------
# Tool 1: hermes_inspect
# ---------------------------------------------------------------------------


@mcp.tool()
async def hermes_inspect(target: str = "all") -> str:
    """Inspect Hermes Agent. target: all, capabilities, config, model, status

    Any section that fails is reported in its ``*_error`` field and the result is flagged isError=true.
    """
    conn = _get_connection()
    result: dict[str, Any] = {}

    if target in ("all", "capabilities"):
        try:
            async with _gateway_client(conn["gateway_url"], conn["gateway_api_key"]) as client:
                resp = await client.get("/v1/capabilities")
                _check_gateway_response(resp, conn["gateway_api_key"])
                resp.raise_for_status()
                result["capabilities"] = resp.json()
        except Exception as exc:
            result["capabilities_error"] = _section_error(exc, "gateway", conn)

    if target in ("all", "config"):
        try:
            async with await _get_dashboard_client(conn) as client:
                resp = await client.get("/api/config")
                resp.raise_for_status()
                cfg = errors.dashboard_json(resp, require_object=True)
                result["config_summary"] = redact_config({
                    "model": cfg.get("model", ""),
                    "toolsets": cfg.get("toolsets", []),
                    "max_live_sessions": cfg.get("max_live_sessions"),
                })
        except Exception as exc:
            result["config_error"] = _section_error(exc, "dashboard", conn)

    if target in ("all", "model"):
        try:
            async with await _get_dashboard_client(conn) as client:
                resp = await client.get("/api/model/info")
                resp.raise_for_status()
                result["model_info"] = errors.dashboard_json(resp, require_object=True)
        except Exception as exc:
            result["model_info_error"] = _section_error(exc, "dashboard", conn)

    if target in ("all", "status"):
        try:
            async with await _get_dashboard_client(conn) as client:
                resp = await client.get("/api/status")
                resp.raise_for_status()
                st = errors.dashboard_json(resp, require_object=True)
                result["status"] = {
                    "version": st.get("version"),
                    "gateway_running": st.get("gateway_running"),
                    "active_sessions": st.get("active_sessions"),
                }
        except Exception as exc:
            result["status_error"] = _section_error(exc, "dashboard", conn)

    text = json.dumps(result, indent=2, default=str)
    if any(key.endswith("_error") for key in result):
        # AI-HMCP-003-3：任何一段失敗都要讓 client 看到 isError=true（其他 Hermes 工具本來就是）。
        # 內容仍是同一份 JSON（成功的區段照樣回），structuredContent 對齊 ``-> str`` 的 outputSchema。
        return CallToolResult(  # type: ignore[return-value]
            content=[TextContent(type="text", text=text)],
            structuredContent={"result": text},
            isError=True,
        )
    return text


# ---------------------------------------------------------------------------
# Tool 2: hermes_skill
# ---------------------------------------------------------------------------


@mcp.tool()
@_backend_errors("dashboard")
async def hermes_skill(action: str, name: str | None = None) -> str:
    """Manage Hermes skills. Actions: list, enable, disable"""
    conn = _get_connection()

    async with await _get_dashboard_client(conn) as client:
        if action == "list":
            resp = await client.get("/api/skills")
            resp.raise_for_status()
            skills = errors.dashboard_json(resp)
            errors.dashboard_shape(isinstance(skills, list))
            summary = []
            for skill in skills:
                errors.dashboard_shape(isinstance(skill, dict))
                errors.dashboard_shape(skill.get("name") is None or isinstance(skill["name"], str))
                errors.dashboard_shape(skill.get("enabled") is None or type(skill["enabled"]) is bool)
                errors.dashboard_shape(skill.get("category") is None or isinstance(skill["category"], str))
                summary.append({
                    "name": skill.get("name"), "enabled": skill.get("enabled"),
                    "category": skill.get("category"),
                })
            return json.dumps({"total": len(summary), "skills": summary}, indent=2)

        if action in ("enable", "disable"):
            if not name:
                return json.dumps({"error": f"Skill name required for '{action}'"})
            enabled = action == "enable"
            resp = await client.put("/api/skills/toggle", json={"name": name, "enabled": enabled})
            resp.raise_for_status()
            return json.dumps({"ok": True, "name": name, "enabled": enabled})

        return json.dumps({"error": f"Unknown action: {action}. Valid: list, enable, disable"})


# ---------------------------------------------------------------------------
# Tool 3: hermes_mcp (MCP server management)
# ---------------------------------------------------------------------------


@mcp.tool()
@_backend_errors("dashboard")
async def hermes_mcp(action: str, name: str | None = None, url: str | None = None) -> str:
    """Manage Hermes MCP server connections. Actions: list, add, remove"""
    conn = _get_connection()

    async with await _get_dashboard_client(conn) as client:
        if action == "list":
            resp = await client.get("/api/mcp/servers")
            resp.raise_for_status()
            return json.dumps(errors.dashboard_json(resp), indent=2)

        if action == "add":
            if not name or not url:
                return json.dumps({"error": "Both 'name' and 'url' required"})
            # 只收 http／https＋SSRF 檢查（舊版只擋小寫 "stdio:"／"command:" 前綴，"STDIO:"、" stdio:"、file:// 都放行）
            guards.path_segment(name, "name")
            url = await guards.check_mcp_server_url(url)
            resp = await client.post("/api/mcp/servers", json={
                "name": name, "transport": "streamable-http", "url": url,
            })
            resp.raise_for_status()
            return json.dumps({"ok": True, "name": name, "added": True})

        if action == "remove":
            if not name:
                return json.dumps({"error": "Name required for 'remove'"})
            await _resource_request(client, "DELETE", f"/api/mcp/servers/{guards.path_segment(name, 'name')}", "MCP server", name)
            return json.dumps({"ok": True, "name": name, "removed": True})

        return json.dumps({"error": f"Unknown action: {action}. Valid: list, add, remove"})


# ---------------------------------------------------------------------------
# Tool 4: hermes_model
# ---------------------------------------------------------------------------


@mcp.tool()
@_backend_errors("dashboard")
async def hermes_model(
    action: str = "info",
    model: str | None = None,
    provider: str | None = None,
    confirm_expensive: bool = False,
) -> str:
    """Manage Hermes model. Actions: info, set, list_providers

    - info: the configured main model and provider
    - list_providers: providers Hermes can use (from Hermes itself), each with its models
    - set: switch the main model for new sessions (requires model; provider defaults to the current one).
      If Hermes asks to confirm an expensive model, call again with confirm_expensive=true.
    """
    conn = _get_connection()

    async with await _get_dashboard_client(conn) as client:
        if action == "info":
            resp = await client.get("/api/model/info")
            resp.raise_for_status()
            return json.dumps(errors.dashboard_json(resp, require_object=True), indent=2)

        if action == "list_providers":
            # AI-HMCP-001-2：讀 Hermes 自己的 /api/model/options（寫死的清單沒有 custom 等實際 provider）。
            resp = await client.get("/api/model/options")
            resp.raise_for_status()
            data = errors.dashboard_json(resp, require_object=True)
            rows = data.get("providers", [])
            errors.dashboard_shape(isinstance(rows, list))
            errors.dashboard_shape(data.get("model") is None or isinstance(data["model"], str))
            errors.dashboard_shape(data.get("provider") is None or isinstance(data["provider"], str))
            providers = []
            for row in rows:
                errors.dashboard_shape(isinstance(row, dict))
                models = row.get("models", [])
                errors.dashboard_shape(isinstance(models, list) and all(isinstance(m, str) for m in models))
                errors.dashboard_shape(row.get("slug") is None or isinstance(row["slug"], str))
                errors.dashboard_shape(row.get("name") is None or isinstance(row["name"], str))
                errors.dashboard_shape(type(row.get("is_current", False)) is bool)
                providers.append({
                    "slug": row.get("slug"), "name": row.get("name"), "is_current": bool(row.get("is_current")),
                    "total_models": len(models), "models": models[:MODEL_LIST_LIMIT],
                })
            current = {"model": data.get("model"), "provider": data.get("provider")}
            return json.dumps({"current": current, "providers": providers}, indent=2)

        if action == "set":
            # AI-HMCP-001-2：0.21.6 的 POST /api/model/set 要 {scope, provider, model}，只送 model 永遠 422。
            if not model:
                return json.dumps({"error": "Specify 'model' (provider is optional and defaults to the current one)"})
            if not provider:
                info = await client.get("/api/model/info")
                info.raise_for_status()
                provider = errors.dashboard_json(info, require_object=True).get("provider") or ""
                if not provider:
                    return json.dumps({"error": "Hermes has no current provider; specify 'provider' "
                                                "(see hermes_model action=list_providers)"})
            payload: dict[str, Any] = {"scope": "main", "provider": provider, "model": model}
            if confirm_expensive:
                payload["confirm_expensive_model"] = True
            resp = await client.post("/api/model/set", json=payload)
            resp.raise_for_status()
            data = errors.dashboard_json(resp)
            if isinstance(data, dict) and data.get("confirm_required"):
                raise HermesConfirmRequired(
                    f"{errors.CONFIRMATION_REQUIRED}: Hermes did not switch the model: "
                    f"{data.get('confirm_message') or 'confirmation required'} "
                    f"Call hermes_model again with confirm_expensive=true to switch anyway."
                )
            return json.dumps(data, indent=2)

        return json.dumps({"error": f"Unknown action: {action}. Valid: info, set, list_providers"})


# ---------------------------------------------------------------------------
# Tool 5: hermes_config
# ---------------------------------------------------------------------------


@mcp.tool()
@_backend_errors("dashboard")
async def hermes_config(action: str = "get", key: str | None = None, value: str | dict[str, Any] | list[Any] | None = None, dry_run: bool = False) -> str:
    """Read or update Hermes config. Actions: get, set. Value accepts JSON text, objects or arrays."""
    conn = _get_connection()

    async with await _get_dashboard_client(conn) as client:
        if action == "get":
            resp = await client.get("/api/config")
            resp.raise_for_status()
            data = errors.dashboard_json(resp, require_object=True)
            if key and isinstance(data, dict):
                parts = key.split(".")
                current = data
                for part in parts:
                    if isinstance(current, dict) and part in current:
                        current = current[part]
                    else:
                        return json.dumps({"error": f"Key '{key}' not found"})
                # 路徑上任一段是秘密鍵（model.api_key、secrets.x）就整個值遮掉。
                current = _redact_all(current) if any(_secret_key(part) for part in parts) else redact_config(current)
                return json.dumps({"key": key, "value": current}, indent=2, default=str)
            return json.dumps(redact_config(data), indent=2, default=str)

        if action == "set":
            if not key or value is None:
                return json.dumps({"error": "Both 'key' and 'value' required"})
            denial = guards.config_key_denial(key, _secret_key)
            if denial:
                return json.dumps({"error": f"Key '{key}' is denied: {denial}", "denied_keys": sorted(DENIED_CONFIG_KEYS)})
            key = guards.clean_config_key(key)
            # Parse value as JSON if possible
            try:
                parsed_value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                parsed_value = value
            denial = guards.config_value_denial(key, parsed_value, _secret_key)
            if denial:
                return json.dumps({"error": f"Key '{key}' contains a denied value: {denial}"})
            if dry_run:
                return json.dumps({"dry_run": True, "key": key, "value": value, "message": "No changes made"}, indent=2)
            # AI-HMCP-001-4：Hermes 的 PUT /api/config 把 body 深合併進設定，點號 key 原樣送出會變成頂層字面鍵
            # （"display.compact: true"）而 display.compact 沒變。依點號拆段組成巢狀物件，寫完讀回驗證。
            parts = key.split(".")
            if any(not part.strip() for part in parts):
                return json.dumps({"error": f"Invalid key '{key}': empty path segment"})
            nested: Any = parsed_value
            for part in reversed(parts):
                nested = {part: nested}
            resp = await client.put("/api/config", json={"config": nested})
            resp.raise_for_status()

            check = await client.get("/api/config")
            check.raise_for_status()
            found, actual = True, errors.dashboard_json(check)
            for part in parts:
                if isinstance(actual, dict) and part in actual:
                    actual = actual[part]
                else:
                    found = False
                    break
            if not found or not guards.config_patch_applied(parsed_value, actual):
                shown = (_redact_all(actual) if any(_secret_key(part) for part in parts) else redact_config(actual))
                raise HermesConfigNotApplied(
                    f"{errors.CONFIG_NOT_APPLIED}: Hermes accepted the write but '{key}' reads back as "
                    f"{json.dumps(shown, default=str) if found else '(missing)'}, not "
                    f"{json.dumps(parsed_value, default=str)}. The key may not exist in this Hermes version "
                    f"(check with hermes_config action=get) or Hermes normalized the value."
                )
            return json.dumps({"ok": True, "key": key, "value": parsed_value}, indent=2, default=str)

        return json.dumps({"error": f"Unknown action: {action}. Valid: get, set"})


# ---------------------------------------------------------------------------
# Tool 6: hermes_tools
# ---------------------------------------------------------------------------


@mcp.tool()
@_backend_errors("dashboard")
async def hermes_tools(action: str = "list", toolset: str | None = None) -> str:
    """Manage Hermes toolsets. Actions: list, enable, disable"""
    conn = _get_connection()

    async with await _get_dashboard_client(conn) as client:
        if action == "list":
            resp = await client.get("/api/tools/toolsets")
            resp.raise_for_status()
            return json.dumps(errors.dashboard_json(resp), indent=2)

        if action in ("enable", "disable"):
            if not toolset:
                return json.dumps({"error": f"Toolset name required for '{action}'"})
            enabled = action == "enable"
            resp = await client.put(f"/api/tools/toolsets/{guards.path_segment(toolset, 'toolset')}", json={"enabled": enabled})
            resp.raise_for_status()
            return json.dumps({"ok": True, "toolset": toolset, "enabled": enabled})

        return json.dumps({"error": f"Unknown action: {action}. Valid: list, enable, disable"})


# ---------------------------------------------------------------------------
# Tool 7: hermes_gateway
# ---------------------------------------------------------------------------


@mcp.tool()
@_backend_errors("dashboard")
async def hermes_gateway(action: str = "status") -> str:
    """Manage Hermes Gateway. Actions: status, restart"""
    conn = _get_connection()

    async with await _get_dashboard_client(conn) as client:
        if action == "status":
            resp = await client.get("/api/status")
            resp.raise_for_status()
            data = errors.dashboard_json(resp, require_object=True)
            return json.dumps({
                "running": data.get("gateway_running", False),
                "state": data.get("gateway_state", "unknown"),
                "version": data.get("version"),
                "active_sessions": data.get("active_sessions", 0),
            }, indent=2)

        if action == "restart":
            resp = await client.post("/api/gateway/restart")
            resp.raise_for_status()
            return json.dumps({"ok": True, "message": "Gateway restart initiated"})

        return json.dumps({"error": f"Unknown action: {action}. Valid: status, restart"})


# ---------------------------------------------------------------------------
# Tool 8: hermes_chat
# ---------------------------------------------------------------------------


@mcp.tool()
@_backend_errors("gateway")
async def hermes_chat(message: str, session_id: str | None = None) -> str:
    """Send a chat message to Hermes and receive a response.

    session_id: optional conversation name. Reuse the same value to continue a multi-turn conversation
    (Hermes keeps the history under that conversation name; an unknown name starts a new one). A value
    starting with "resp_" continues from that Hermes response id. Omit it for a one-off turn.
    A turn may take up to HERMES_MCP_CHAT_TIMEOUT seconds. On timeout the connection closes;
    remote cancellation is unconfirmed and completed tool effects are not undone.
    """
    conn = _get_connection()
    payload: dict[str, Any] = {"model": "default", "input": message, "stream": True}
    # AI-HMCP-001-5：session_id 以前沒送出去，每輪都是新 session。對到 /v1/responses 的 conversation（具名對話，
    # Hermes 存最新 response id 並接續歷史）；resp_ 開頭的是 response id，用 previous_response_id 接續。
    sid = (session_id or "").strip()
    if sid.startswith("resp_"):
        payload["previous_response_id"] = sid
    elif sid:
        payload["conversation"] = sid

    limit = _chat_timeout()
    # 讀取時限放寬到整輪時限（串流中 Hermes 只在空檔送 keepalive），整輪由下面的 wait_for 把關。
    timeout = httpx.Timeout(limit, connect=30.0)
    async with _gateway_client(conn["gateway_url"], conn["gateway_api_key"], timeout) as client:
        try:
            data = await asyncio.wait_for(_chat_turn(client, payload, conn["gateway_api_key"]), timeout=limit)
        except asyncio.TimeoutError:
            # Closing the stream requests cancellation; it is not an acknowledgement from the backend.
            raise HermesChatTimeout(
                f"BACKEND_TIMEOUT: Hermes Gateway did not finish within {limit:g} seconds. "
                f"Stopped waiting and closed the connection; backend cancellation is unconfirmed. "
                f"Tools already run are not undone. Check the conversation before retrying a write operation."
            ) from None
    if not isinstance(data, dict):
        raise errors.coded_error(errors.BACKEND_UNEXPECTED_RESPONSE, "Hermes returned an invalid response: expected a JSON object")
    if data.get("status") in ("failed", "incomplete", "cancelled") or data.get("error"):
        error = data.get("error") or data.get("incomplete_details")
        detail = (error.get("message") or error.get("reason")) if isinstance(error, dict) else error
        raise errors.coded_error(errors.CHAT_TURN_FAILED, f"Hermes could not complete the turn: {detail or data.get('status')}")
    # Extract text from response
    output = data.get("output", [])
    texts = []
    for item in output if isinstance(output, list) else []:
        if isinstance(item, dict) and item.get("type") == "message":
            for c in item.get("content", []):
                if isinstance(c, dict) and c.get("type") == "output_text":
                    texts.append(c.get("text", ""))
    if texts:
        return "\n".join(texts)
    return json.dumps(data, indent=2, default=str)


async def _chat_turn(client: httpx.AsyncClient, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    """送一輪 /v1/responses（stream=true），回傳最終的 response 物件（response.completed／failed 的 envelope）。

    串流讓逾時時可關閉連線；是否成功中止遠端工作仍取決於後端。
    後端不支援串流、直接回 JSON 時照舊解析。
    """
    async with client.stream("POST", "/v1/responses", json=payload) as resp:
        if resp.status_code >= 400:
            await resp.aread()
            _check_gateway_response(resp, api_key)
            resp.raise_for_status()
        if "text/event-stream" not in resp.headers.get("content-type", ""):
            await resp.aread()
            return resp.json()
        event, data_lines = "", []
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif not line and data_lines:
                if event in ("response.completed", "response.failed", "response.incomplete"):
                    body = json.loads("\n".join(data_lines))
                    envelope = body.get("response") if isinstance(body, dict) else None
                    if not isinstance(envelope, dict):
                        raise errors.coded_error(errors.BACKEND_UNEXPECTED_RESPONSE, "Hermes returned a final stream event without a response")
                    return {**envelope, "status": envelope.get("status") or event.removeprefix("response.")}
                event, data_lines = "", []
    raise errors.coded_error(errors.BACKEND_UNEXPECTED_RESPONSE, "Hermes Gateway ended the reply without a result (stream closed early); retry.")


# ---------------------------------------------------------------------------
# Tool 9: hermes_session
# ---------------------------------------------------------------------------


@mcp.tool()
@_backend_errors("dashboard")
async def hermes_session(
    action: str = "list", session_id: str | None = None, request_id: str | None = None,
    expected_revision: StrictInt | None = None, expected_generation: StrictInt | None = None,
) -> str:
    """Manage Hermes sessions. Actions: list, get, delete.

    Delete reads fresh Agent fences and sends one request. It never retries automatically.
    After an unknown outcome, inspect the session; an explicit identity replay requires
    request_id, expected_revision and expected_generation together, unchanged from the error.
    """
    identity = session_mutations.mutation_identity(request_id, expected_revision, expected_generation)
    if identity is not None and action != "delete":
        raise guards.GuardError("session mutation identity is only valid for delete")
    conn = _get_connection()

    # Sessions go via Dashboard API (has the session data)
    async with await _get_dashboard_client(conn) as client:
        if action == "list":
            resp = await client.get("/api/sessions")
            resp.raise_for_status()
            data = errors.dashboard_json(resp, require_object=True)
            sessions = data.get("sessions", [])
            errors.dashboard_shape(isinstance(sessions, list) and all(isinstance(row, dict) for row in sessions))
            total = data.get("total", len(sessions))
            errors.dashboard_shape(type(total) is int and total >= 0)
            return json.dumps({"total": total, "sessions": sessions[:20]}, indent=2, default=str)

        if action == "get":
            if not session_id:
                return json.dumps({"error": "session_id required"})
            resp = await _resource_request(client, "GET", f"/api/sessions/{guards.path_segment(session_id, 'session_id')}", "session", session_id)
            return json.dumps(errors.dashboard_json(resp, require_object=True), indent=2, default=str)

        if action == "delete":
            if not session_id:
                return json.dumps({"error": "session_id required"})
            await session_mutations.delete_session(client, session_id, identity)
            return json.dumps({"ok": True, "deleted": session_id})

        return json.dumps({"error": f"Unknown action: {action}. Valid: list, get, delete"})


# ---------------------------------------------------------------------------
# Tool 10: hermes_cron
# ---------------------------------------------------------------------------


def _cron_create_unknown() -> errors.HermesBackendError:
    return errors.HermesBackendError(
        "CRON_CREATE_OUTCOME_UNKNOWN: Hermes cron create outcome is unknown. "
        "No automatic retry was made. List scheduled jobs in Hermes before deciding to retry."
    )


@mcp.tool()
@_backend_errors("dashboard")
async def hermes_cron(
    action: str = "list",
    name: str | None = None,
    schedule: str | None = None,
    prompt: str | None = None,
    enabled: bool = True,
    job_id: str | None = None,
) -> str:
    """Manage Hermes cron/scheduled jobs.

    Actions: list, create, update, delete, trigger, pause, resume
    - list: show all cron jobs
    - create: new job (requires name, schedule, prompt)
    - update: modify job (requires job_id, plus fields to change)
    - delete: remove job (requires job_id)
    - trigger: run job now (requires job_id)
    - pause / resume: toggle job state (requires job_id)

    schedule uses cron syntax: '0 9 * * 1-5' = weekdays at 9am
    """
    conn = _get_connection()

    async with await _get_dashboard_client(conn) as client:
        if action == "list":
            resp = await client.get("/api/cron/jobs")
            resp.raise_for_status()
            jobs = errors.dashboard_json(resp)
            errors.dashboard_shape(isinstance(jobs, list))
            summary = []
            for job in jobs:
                errors.dashboard_shape(isinstance(job, dict))
                schedule_value = job.get("schedule")
                errors.dashboard_shape(schedule_value is None or isinstance(schedule_value, (str, dict)))
                if isinstance(schedule_value, dict):
                    schedule_value = schedule_value.get("expr")
                    errors.dashboard_shape(schedule_value is None or isinstance(schedule_value, str))
                prompt_value = job.get("prompt")
                errors.dashboard_shape(prompt_value is None or isinstance(prompt_value, str))
                for field in ("id", "name", "state", "next_run_at"):
                    errors.dashboard_shape(job.get(field) is None or type(job[field]) in (str, int))
                errors.dashboard_shape(job.get("enabled") is None or type(job["enabled"]) is bool)
                summary.append({
                    "id": job.get("id"), "name": job.get("name"),
                    "schedule": schedule_value, "prompt": (prompt_value or "")[:80],
                    "enabled": job.get("enabled"), "state": job.get("state"),
                    "next_run": job.get("next_run_at"),
                })
            return json.dumps({"total": len(summary), "jobs": summary}, indent=2, default=str)

        if action == "create":
            if not name or not schedule or not prompt:
                return json.dumps({"error": "name, schedule, and prompt are all required"})
            try:
                # Current Hermes uses paused; retain enabled for older APIs.
                resp = await client.post("/api/cron/jobs", json={
                    "name": name, "schedule": schedule, "prompt": prompt,
                    "paused": not enabled, "enabled": enabled,
                }, extensions={"hermes_no_auth_retry": True})
            except httpx.HTTPError:
                raise _cron_create_unknown() from None
            if resp.status_code >= 500:
                raise _cron_create_unknown()
            resp.raise_for_status()
            try:
                data = errors.dashboard_json(resp, require_object=True)
                ident = data.get("id")
                errors.dashboard_shape(
                    (isinstance(ident, str) and bool(ident))
                    or (type(ident) is int and ident >= 0)
                )
                errors.dashboard_shape(data.get("ok", True) is True)
                errors.dashboard_shape(data.get("name") is None or isinstance(data["name"], str))
                returned_schedule = data.get("schedule")
                errors.dashboard_shape(
                    returned_schedule is None or isinstance(returned_schedule, (str, dict))
                )
                if isinstance(returned_schedule, dict):
                    errors.dashboard_shape(
                        returned_schedule.get("expr") is None
                        or isinstance(returned_schedule["expr"], str)
                    )
                errors.dashboard_shape(
                    data.get("next_run_at") is None
                    or type(data["next_run_at"]) in (str, int)
                )
            except (errors.DashboardResponseDecodeError, errors.DashboardResponseShapeError):
                raise _cron_create_unknown() from None
            return json.dumps({
                "ok": True, "id": data.get("id"), "name": data.get("name"),
                "schedule": data.get("schedule", {}).get("expr") if isinstance(data.get("schedule"), dict) else schedule,
                "next_run": data.get("next_run_at"),
            }, indent=2, default=str)

        if action == "update":
            if not job_id:
                return json.dumps({"error": "job_id required for update"})
            updates: dict[str, Any] = {}
            if prompt is not None:
                updates["prompt"] = prompt
            if schedule is not None:
                updates["schedule"] = schedule
            if name is not None:
                updates["name"] = name
            if not updates:
                return json.dumps({"error": "provide at least one field to update (prompt, schedule, name)"})
            await _resource_request(client, "PUT", f"/api/cron/jobs/{guards.path_segment(job_id, 'job_id')}", "cron job", job_id,
                                    json={"updates": updates})
            return json.dumps({"ok": True, "updated": job_id}, indent=2)

        if action == "delete":
            if not job_id:
                return json.dumps({"error": "job_id required"})
            await _resource_request(client, "DELETE", f"/api/cron/jobs/{guards.path_segment(job_id, 'job_id')}", "cron job", job_id)
            return json.dumps({"ok": True, "deleted": job_id})

        if action == "trigger":
            if not job_id:
                return json.dumps({"error": "job_id required"})
            await _resource_request(client, "POST", f"/api/cron/jobs/{guards.path_segment(job_id, 'job_id')}/trigger", "cron job", job_id)
            return json.dumps({"ok": True, "triggered": job_id})

        if action == "pause":
            if not job_id:
                return json.dumps({"error": "job_id required"})
            await _resource_request(client, "POST", f"/api/cron/jobs/{guards.path_segment(job_id, 'job_id')}/pause", "cron job", job_id)
            return json.dumps({"ok": True, "paused": job_id})

        if action == "resume":
            if not job_id:
                return json.dumps({"error": "job_id required"})
            await _resource_request(client, "POST", f"/api/cron/jobs/{guards.path_segment(job_id, 'job_id')}/resume", "cron job", job_id)
            return json.dumps({"ok": True, "resumed": job_id})

        return json.dumps({"error": f"Unknown action: {action}. Valid: list, create, update, delete, trigger, pause, resume"})


# ---------------------------------------------------------------------------
# Tool 11: hermes_webhook
# ---------------------------------------------------------------------------

_LEGACY_PAYLOAD = re.compile(r"\{\{\s*payload\s*\}\}")


@mcp.tool()
@_backend_errors("dashboard")
async def hermes_webhook(
    action: str = "list",
    name: str | None = None,
    prompt: str | None = None,
    enabled: bool = True,
) -> str:
    """Manage Hermes webhooks.

    Actions: list, enable_platform, create, delete, toggle
    - list: show webhook platform status and subscriptions
    - enable_platform: enable the webhook listener (triggers gateway restart)
    - create: new webhook (requires name, prompt). In the prompt, {__raw__} is the whole incoming JSON
      payload and {field.path} is one field of it (e.g. {order.id}); {event_type} is the event name
    - delete: remove webhook (requires name)
    - toggle: enable/disable webhook (requires name, set enabled=true/false)

    Example prompt: 'New order {order.id} received: {__raw__}. Summarize the order details.'
    """
    conn = _get_connection()

    async with await _get_dashboard_client(conn) as client:
        if action == "list":
            resp = await client.get("/api/webhooks")
            resp.raise_for_status()
            data = errors.dashboard_json(resp, require_object=True)
            subs = data.get("subscriptions", [])
            errors.dashboard_shape(isinstance(subs, list))
            errors.dashboard_shape(type(data.get("enabled", False)) is bool)
            errors.dashboard_shape(data.get("base_url") is None or isinstance(data["base_url"], str))
            webhooks = []
            for webhook in subs:
                errors.dashboard_shape(isinstance(webhook, dict))
                errors.dashboard_shape(webhook.get("name") is None or isinstance(webhook["name"], str))
                errors.dashboard_shape(webhook.get("enabled") is None or type(webhook["enabled"]) is bool)
                prompt_value = webhook.get("prompt")
                errors.dashboard_shape(prompt_value is None or isinstance(prompt_value, str))
                webhooks.append({
                    "name": webhook.get("name"), "enabled": webhook.get("enabled"),
                    "prompt": (prompt_value or "")[:80],
                })
            return json.dumps({
                "platform_enabled": data.get("enabled", False),
                "base_url": data.get("base_url", ""),
                "total": len(subs), "webhooks": webhooks,
            }, indent=2)

        if action == "enable_platform":
            resp = await client.post("/api/webhooks/enable", json={})
            resp.raise_for_status()
            return json.dumps({"ok": True, "message": "Webhook platform enabled. Gateway will restart."})

        if action == "create":
            if not name or not prompt:
                return json.dumps({"error": "name and prompt are required. Use {__raw__} for the incoming payload."})
            # AI-HMCP-001-7：舊說明教的 {{payload}} Hermes 不認得（只認 {欄位.路徑}／{__raw__}），payload 根本沒進
            # agent。照舊說明寫的 prompt 轉成 {__raw__}，結果裡講明。
            rendered = _LEGACY_PAYLOAD.sub("{__raw__}", prompt)
            resp = await client.post("/api/webhooks", json={
                "name": name, "prompt": rendered, "enabled": enabled,
            })
            resp.raise_for_status()
            result: dict[str, Any] = {"ok": True, "name": name, "created": True}
            if rendered != prompt:
                result["prompt"] = rendered
                result["note"] = "{{payload}} is not a Hermes placeholder; it was replaced with {__raw__}"
            return json.dumps(result)

        if action == "delete":
            if not name:
                return json.dumps({"error": "name required"})
            await _resource_request(client, "DELETE", f"/api/webhooks/{guards.path_segment(name, 'name')}", "webhook", name)
            return json.dumps({"ok": True, "deleted": name})

        if action == "toggle":
            if not name:
                return json.dumps({"error": "name required"})
            await _resource_request(client, "PUT", f"/api/webhooks/{guards.path_segment(name, 'name')}/enabled", "webhook", name,
                                    json={"enabled": enabled})
            return json.dumps({"ok": True, "name": name, "enabled": enabled})

        return json.dumps({"error": f"Unknown action: {action}. Valid: list, enable_platform, create, delete, toggle"})


# ---------------------------------------------------------------------------
# 工具例外的文字照舊給 client（#78）
# ---------------------------------------------------------------------------

# 這個檔與 errors.py／guards.py 的錯誤（後端不通、逾時、輸入被安全規則拒絕…）都是「丟例外、讓 client 看到文字」。
# mcp 2.x 只保留 ToolError 的文字、其他例外只回 "Error executing tool <name>"；在工具邊界換回 1.x 的逐字行為。
keep_error_text(mcp)

# ---------------------------------------------------------------------------
# Low-level server（供雙 transport wrapper 掛 SSE + StreamableHTTP）
# ---------------------------------------------------------------------------

# core 的 DualTransportApp 需要底層 low-level ``Server``（用 StreamableHTTPSessionManager
# 與 ``server.run`` 的 API），MCPServer 內部即持有一個（mcp 2.x 的 ``_lowlevel_server``，1.x 叫 ``_mcp_server``）。
# 以 module-level 名稱 ``server`` 暴露，供 launcher 掛雙 transport。
server = mcp._lowlevel_server
