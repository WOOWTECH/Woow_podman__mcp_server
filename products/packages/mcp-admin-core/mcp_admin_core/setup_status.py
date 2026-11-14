"""子行程沒在跑時，閘道自己回答的最小 MCP 伺服器（佔位），與 ``/healthz`` 的粗略狀態。

為什麼（2026-10-27 負責人）：PaaS 新開的 Nextcloud MCP 在管理頁存好連線之前，子行程缺必要連線欄位就結束（exit 2），
閘道對所有 MCP 請求回 HTTP 503，平台卻顯示「運作中」——租戶在 Claude.ai／Claude Code 只看到連不上，看不出哪裡不對、
要去哪裡設定。

現在 token 驗過、子行程沒在跑時，Streamable HTTP（``/mcp``）由閘道回答一個合法的最小 MCP 伺服器（MCP 2025-03-26／
2025-06-18 的 Streamable HTTP；回應一律 ``application/json``，規格要求用戶端 JSON 與 SSE 兩種都收）：

- ``initialize`` → 200＋InitializeResult：``protocolVersion`` 照用戶端要的版本（在 :data:`PROTOCOL_VERSIONS` 內；否則回其中
  最新的，與 MCP SDK 2.x 子行程的協商相同）、``capabilities`` 只有 ``tools``、``serverInfo`` 標明尚未設定／沒有在跑、
  ``instructions`` 以中英文說明原因與要去哪裡設定；回應帶 :data:`PLACEHOLDER_SESSION_PREFIX` 開頭的 ``Mcp-Session-Id``。
- 只有通知或用戶端回應 → 202、沒有內容；``ping`` → ``{}``；``tools/list`` → 只有一支 :data:`SETUP_TOOL`（說明寫明呼叫它
  可取得設定步驟）；``tools/call``（不論名稱）→ ``isError: true`` 的工具結果，文字同 ``instructions``；``resources/list``、
  ``resources/templates/list``、``prompts/list`` → 空清單；其他方法 → -32601；batch 逐一回答（最多
  :data:`MAX_BATCH_MESSAGES` 則）；解析不了 → 400＋-32700，沒有任何合法訊息或 batch 太大 → 400＋-32600。
- ``GET`` → 405（不提供 SSE 串流；內容是同一段說明，直接用瀏覽器打開 MCP 網址也看得到原因）；``DELETE`` → 200（帶著
  佔位 session 時子行程起來之後也是 200，見下面「銜接」）。
- 不檢查 ``MCP-Protocol-Version`` 標頭、也不要求帶 session：這個伺服器沒有任何狀態，寬鬆比較不會讓用戶端卡住。

兩種原因：``not_configured``＝``LaunchPolicy.required_connection`` 有欄位沒填（文字指向 Connection 頁並列出缺的欄位）；
``not_running``＝其他原因的沒在跑（當掉、正在啟動或自動重啟），文字只請管理員看 Logs 頁。回應不含 token、密碼、後端網址
或子行程輸出——只用到欄位名稱與 Connection 頁上的標籤；管理頁網址取自這次請求的 Host（只接受合法的主機名稱或 IP，
否則改成不帶網址的說法）。

銜接：佔位 session 是閘道自己發的（不轉發、不經 Tool Manager 的停用清單）。子行程起來、真的在聽 port 之後，帶著佔位
session 的請求（DELETE 以外）一律回 404（與 MCP SDK 2.x 子行程對不存在的 session 回的同形狀），用戶端依規格重新
initialize，拿到真的 session 與完整工具。DELETE（終止 session）一律 200：佔位伺服器沒有狀態要清、終止是冪等的；回 404 的話
TS SDK 的 ``terminateSession()`` 會丟錯並留著舊的 session id、Python SDK 會記 warning。子行程還在啟動（在跑、還沒聽
port）時照樣回佔位答案，免得用戶端 404 → 重新 initialize → 502。
但 404 要等用戶端送出下一個請求才拿得到：真的 Claude Code 2.1.311 在同一段對話裡，模型不再呼叫這個伺服器的工具就不會送
任何 MCP 請求（工具清單停在 setup_status）；MCP Python SDK（1.30、2.3）與 TS SDK（1.32.2）把 404 當錯誤丟給呼叫端、要應用
自己重連。所以說明（:attr:`_Guide.text`）與工具說明（:meth:`_Guide.tool`）都請模型「存好之後再呼叫一次 setup_status」——那次
呼叫拿到 404，Claude Code 會重新連線、載入完整工具——或請使用者在用戶端重新連線。

不涵蓋：legacy SSE（``/sse``、``/messages``）與其他路徑照舊 503；子行程在跑但連不上（啟動窗口）、必要欄位也都有時照舊 502。

``/healthz`` 另有 ``backend`` 欄位（:func:`health_fields`，2026-10-27 第五輪）：子行程沒有連線也會啟動、只是沒接上後端的
產品（odoo、litellm、hermes、n8n 文件模式、vk、opendesign）宣告 ``LaunchPolicy.backend_connection``，欄位沒填完是
``missing``、都填了是 ``configured``，給平台顯示較輕的「尚未設定後端連線」提示。後端沒設定本身不改變閘道的行為：子行程在跑
就照常轉發（子行程當掉時照舊是上面的佔位答案），也不改子行程的 ``initialize`` 回應——不在 ``instructions`` 補說明：
instructions 只在連線時讀一次、會進到模型的系統提示，存好連線之後沒有重新連線的對話（opendesign、vk 的子行程是 stateless，
重啟後不會回 404）會一直留著「還沒設定」，反而誤導。hermes、odoo、litellm 子行程自己的工具錯誤說得出原因；opendesign 的錯誤
（Name or service not known）與 n8n 文件模式（沒有錯誤、只是沒有管理工具）靠平台提示補上。
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import re
import secrets
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Sequence

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .config import get_config_store
from .config.store import ConfigUnavailable
from .launch_policy import get_launch_policy
from .process import get_process_manager

#: 佔位 session id 的前綴（後面接 32 個十六進位字元）；真的子行程的 session id（uuid hex）不會是這個開頭。
PLACEHOLDER_SESSION_PREFIX = "woow-mcp-setup-"
#: 佔位伺服器唯一的工具：呼叫它取得設定（或處理）步驟。
SETUP_TOOL = "setup_status"
#: 支援的 initialize 協商版本＝MCP SDK 2.x 子行程的握手版本（mcp_types.HANDSHAKE_PROTOCOL_VERSIONS）；最後一個是最新的。
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")

#: ``/healthz`` 的 ``mcp`` 欄位；後兩個也是佔位答案的兩種原因（``_Guide.reason``）。
RUNNING = "running"
NOT_CONFIGURED = "not_configured"
NOT_RUNNING = "not_running"
#: ``/healthz`` 的 ``backend`` 欄位（只有宣告 ``LaunchPolicy.backend_connection`` 的產品、而且讀得到設定時才有）：
#: 後端連線欄位都填了／有欄位沒填。與 ``mcp`` 互不影響（見 :func:`health_fields`）。
BACKEND_CONFIGURED = "configured"
BACKEND_MISSING = "missing"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
#: batch 最多回答幾則：每則 tools/call 都回約 2 KB 的說明，16 MiB 的 batch 不設限會放大成幾百 MB 的回應。
MAX_BATCH_MESSAGES = 64
#: ``/healthz`` 沒有設定快照可讀（還沒載入過）時，最多等設定檔的鎖幾秒；k8s readiness 的逾時是 3 秒。
PROBE_CONFIG_WAIT_SECONDS = 0.5
#: :func:`probe_connection` 讀不到設定（設定壞掉、讀檔失敗、等鎖逾時）。
UNREADABLE = object()

_NO_STORE = {"Cache-Control": "no-store"}
_EMPTY_LISTS = {
    "resources/list": "resources",
    "resources/templates/list": "resourceTemplates",
    "prompts/list": "prompts",
}
_HOSTNAME = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
_PORT = re.compile(r"[0-9]{1,5}")
_ROOT_PATH = re.compile(r"(?:/[A-Za-z0-9._~-]+)*")


# ---------------------------------------------------------------------------
# 狀態：缺哪些必要欄位、/healthz（mcp 與 backend）
# ---------------------------------------------------------------------------


async def missing_fields(store=None) -> list[tuple[str, str]]:
    """存檔的連線設定缺哪些 ``LaunchPolicy.required_connection`` 欄位（``[(鍵, 標籤)]``，依宣告順序）。

    沒有政策或沒宣告必要欄位就不讀設定；讀不到設定時回 ``[]``（不知道缺什麼，呼叫端只看子行程）。MCP 回答用：與閘道的
    其他讀取一樣等設定檔的鎖、讀最新的；``/healthz`` 用 :func:`probe_missing_fields`。
    """
    policy = get_launch_policy()
    if policy is None or not policy.required_connection:
        return []
    try:
        connection = await (store or get_config_store()).get("connection", {})
    except (ConfigUnavailable, OSError):
        return []
    return policy.missing_connection(connection)


async def probe_missing_fields(store=None) -> list[tuple[str, str]]:
    """``/healthz`` 用的 :func:`missing_fields`：不等設定檔的鎖。

    ConfigStore 寫檔（含檔案與目錄的 fsync）的整段時間都拿著鎖，Longhorn／iSCSI 卡住時可能卡很久；k8s readiness 的逾時是
    3 秒、liveness 5 秒，``/healthz`` 跟著等鎖的話 pod 會被判 NotReady、甚至被重啟（修正前 ``/healthz`` 完全不碰設定）。
    所以先讀 :meth:`ConfigStore.peek` 的快照（最後一次載入或寫入完成的版本；寫檔進行中時是寫入前的）；還沒有快照（還沒
    載入過）才讀檔，最多等 :data:`PROBE_CONFIG_WAIT_SECONDS`。逾時或讀不到就回 ``[]``（不知道缺什麼，呼叫端只看子行程）。
    """
    policy = get_launch_policy()
    if policy is None or not policy.required_connection:
        return []
    connection = await probe_connection(store)
    return [] if connection is UNREADABLE else policy.missing_connection(connection)


async def probe_connection(store=None) -> Any:
    """``/healthz`` 讀存檔的 ``connection``，不等設定檔的鎖（理由見 :func:`probe_missing_fields`）；讀不到回 :data:`UNREADABLE`。

    先讀 :meth:`ConfigStore.peek` 的快照；還沒有快照才讀檔，最多等 :data:`PROBE_CONFIG_WAIT_SECONDS`。
    """
    try:
        store = store or get_config_store()
        try:
            return store.peek("connection", {})
        except LookupError:
            return await asyncio.wait_for(store.get("connection", {}), PROBE_CONFIG_WAIT_SECONDS)
    except (ConfigUnavailable, OSError, asyncio.TimeoutError):
        return UNREADABLE


async def mcp_state(store=None) -> str:
    """``/healthz`` 的 ``mcp``：``running``｜``not_configured``｜``not_running``，不含任何細節。

    缺必要欄位時一律 ``not_configured``——管理程序這時不 spawn 子行程（2026-11-13 起，``process._start_locked``）；還在跑的
    只可能是以 apply=false 清空欄位前就在跑的舊子行程，它停止後在欄位存好之前不會再啟動。不等設定檔的鎖
    （:func:`probe_missing_fields`）。
    """
    return _mcp_value(bool(await probe_missing_fields(store)))


async def health_fields(store=None) -> dict[str, str]:
    """``/healthz`` 除了 ``status`` 以外的欄位：``mcp``（一定有，見 :func:`mcp_state`）與 ``backend``。

    ``backend``：只有 app 宣告了 ``LaunchPolicy.backend_connection``、而且讀得到設定時才有——後端連線欄位都填了是
    ``configured``，有欄位沒填是 ``missing``；讀不到設定就不帶這個欄位（不知道），``/healthz`` 照樣 200。不含欄位名稱、
    標籤或任何值。與 ``mcp`` 互不影響：``mcp`` 的三個值與判斷和沒有 ``backend`` 時逐字相同（只看 ``required_connection``
    與子行程），``backend`` 也不看子行程在不在跑——「子行程沒在跑」與「後端沒設定」可能同時成立，由平台決定怎麼顯示。
    設定只讀一次（同一份快照），也一樣不等設定檔的鎖（:func:`probe_connection`）。
    """
    policy = get_launch_policy()
    wanted = policy is not None and bool(policy.required_connection or policy.backend_connection)
    connection = await probe_connection(store) if wanted else UNREADABLE
    readable = connection is not UNREADABLE
    missing_required = readable and bool(policy.required_connection) and bool(policy.missing_connection(connection))
    fields = {"mcp": _mcp_value(missing_required)}
    if readable and policy.backend_connection:
        fields["backend"] = BACKEND_MISSING if policy.missing_backend_connection(connection) else BACKEND_CONFIGURED
    return fields


def _mcp_value(missing_required: bool) -> str:
    if missing_required:
        return NOT_CONFIGURED
    return RUNNING if get_process_manager().is_running else NOT_RUNNING


# ---------------------------------------------------------------------------
# 閘道的進入點（proxy.py）
# ---------------------------------------------------------------------------


def owns_session(session_id: str | None) -> bool:
    """*session_id* 是不是閘道發的佔位 session。"""
    return isinstance(session_id, str) and session_id.startswith(PLACEHOLDER_SESSION_PREFIX)


async def placeholder_session_answer(request: Request, rpc: Any, *, store, port: int) -> Response:
    """帶著佔位 session 的 ``/mcp`` 請求：子行程真的起來了就 404，否則照樣回佔位答案；DELETE 一律 200（見模組說明「銜接」）。"""
    if request.method == "DELETE":
        return terminated()
    missing = await missing_fields(store)
    pm = get_process_manager()
    if not missing and pm.is_running and await _child_is_up(pm, port):
        return session_gone()
    return answer(request, rpc, missing)


async def _child_is_up(pm, port: int) -> bool:
    """子行程真的起來了：受管子行程看它的世代是否通過身分檢查（只是有東西在聽 port 不算，blocker 3）。"""
    routable = getattr(pm, "forward_generation", None)
    if routable is not None:
        return routable(port) is not None
    return await child_accepts_connections(port)


async def unreachable_answer(request: Request, rpc: Any, *, store) -> Response | None:
    """轉發 ``/mcp`` 時連不上子行程：沒在跑（或缺必要欄位）就回佔位答案；在跑、欄位也齊全（啟動窗口）回 None＝維持 502。

    帶著 session 的請求（佔位 session 不會走到這裡，所以是子行程之前發的）＝子行程當掉前就連上的用戶端，工具清單是真的工具、
    沒有 setup_status：說明改請它「再呼叫一次原本要用的工具」（:attr:`_Guide.held_session`）。
    """
    missing = await missing_fields(store)
    if not missing and get_process_manager().is_running:
        return None
    return answer(request, rpc, missing, held_session=bool(request.headers.get("mcp-session-id")))


def terminated() -> Response:
    """DELETE（終止 session）：200、沒有內容。佔位伺服器沒有狀態，終止是冪等的。"""
    return Response(status_code=200, headers=_NO_STORE)


def session_gone() -> JSONResponse:
    """佔位 session 在子行程起來之後不存在了：404，內容與 MCP SDK 2.x 對不存在的 session 回的相同。"""
    return JSONResponse(_error(None, INVALID_REQUEST, "Session not found"), status_code=404, headers=_NO_STORE)


async def child_accepts_connections(port: int, timeout: float = 0.5) -> bool:
    """子行程的 loopback port 是否已經接受連線（剛 spawn、還在啟動時是 False）。"""
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


# ---------------------------------------------------------------------------
# 佔位 MCP 伺服器
# ---------------------------------------------------------------------------


def answer(request: Request, rpc: Any, missing: Sequence[tuple[str, str]], *, held_session: bool = False) -> Response:
    """以佔位 MCP 伺服器回答 *request*（*rpc* 是閘道已解析的 JSON-RPC 內容，解析不了是 None）。

    *held_session*＝請求帶著子行程發的 session（見 :func:`unreachable_answer`）；含 initialize 的請求一律當成新連上的用戶端
    （它拿到的是佔位 session 與只有 setup_status 的工具清單）。
    """
    initializing = any(isinstance(m, dict) and m.get("method") == "initialize" and "id" in m
                       for m in (rpc if isinstance(rpc, list) else [rpc]))
    guide = _Guide(product=product_label(getattr(request.app, "title", "")), missing=tuple(missing),
                   admin=admin_root(request), held_session=held_session and not initializing)
    if request.method == "DELETE":
        return terminated()
    if request.method != "POST":
        return JSONResponse({"detail": guide.text}, status_code=405, headers={"Allow": "POST, DELETE", **_NO_STORE})
    if rpc is None:
        return JSONResponse(_error(None, PARSE_ERROR, "Parse error"), status_code=400, headers=_NO_STORE)
    messages = rpc if isinstance(rpc, list) else [rpc]
    if not messages:
        return JSONResponse(_error(None, INVALID_REQUEST, "Invalid Request"), status_code=400, headers=_NO_STORE)
    if len(messages) > MAX_BATCH_MESSAGES:
        return JSONResponse(_error(None, INVALID_REQUEST, f"Invalid Request: batch larger than {MAX_BATCH_MESSAGES}"),
                            status_code=400, headers=_NO_STORE)
    replies: list[dict] = []
    valid = 0
    for message in messages:
        reply, ok = _reply(message, guide)
        valid += ok
        if reply is not None:
            replies.append(reply)
    headers = dict(_NO_STORE)
    if any(isinstance(m, dict) and m.get("method") == "initialize" and "id" in m for m in messages):
        headers["Mcp-Session-Id"] = PLACEHOLDER_SESSION_PREFIX + secrets.token_hex(16)
    if not replies:
        return Response(status_code=202, headers=headers)  # 只有通知或用戶端回應
    return JSONResponse(replies if isinstance(rpc, list) else replies[0], status_code=200 if valid else 400,
                        headers=headers)


def _reply(message: Any, guide: _Guide) -> tuple[dict | None, bool]:
    """單一訊息的回應（通知與用戶端回應是 None）與它是不是合法的 JSON-RPC 訊息。"""
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "Invalid Request"), False
    method = message.get("method")
    if not isinstance(method, str):
        if "id" in message and ("result" in message or "error" in message):
            return None, True  # 用戶端對伺服器請求的回應（這裡從不送請求，照收）
        return _error(message.get("id"), INVALID_REQUEST, "Invalid Request"), False
    if "id" not in message:
        return None, True  # 通知
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        result = {
            "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": guide.server_name, "title": guide.server_name, "version": _version()},
            "instructions": guide.text,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [guide.tool()]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": guide.text}], "isError": True}
    elif method in _EMPTY_LISTS:
        result = {_EMPTY_LISTS[method]: []}
    else:
        return _error(message["id"], METHOD_NOT_FOUND, "Method not found"), True
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}, True


def _error(message_id: Any, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}


@functools.lru_cache(maxsize=1)
def _version() -> str:
    try:
        return version("mcp-admin-core")
    except PackageNotFoundError:
        return "unknown"


@dataclass(frozen=True)
class _Guide:
    """這次要說的話：原因、管理頁網址、缺的欄位（只有名稱與標籤，沒有任何值）。"""

    product: str
    missing: tuple[tuple[str, str], ...]
    admin: str | None
    #: 請求帶著子行程之前發的 session：用戶端的工具清單是真的工具（沒有 setup_status），要它再呼叫原本要用的工具。
    held_session: bool = False

    @property
    def reason(self) -> str:
        return NOT_CONFIGURED if self.missing else NOT_RUNNING

    @property
    def server_name(self) -> str:
        if self.reason == NOT_CONFIGURED:
            return f"{self.product} MCP（尚未設定 not configured）"
        return f"{self.product} MCP（子行程沒有在跑 not running）"

    @property
    def text(self) -> str:
        """``instructions``、``tools/call`` 結果、GET 405 共用的說明（中文在前、英文在後）。

        最後一段說怎麼接回真的子行程：已經連上的用戶端不會自己接回（見模組說明「銜接」）。拿著佔位 session 的用戶端（工具
        清單只有 setup_status）請它再呼叫一次 setup_status——那次呼叫拿到 404，Claude Code 重新連線後會用真的子行程重試這次
        呼叫（真的子行程沒有 setup_status，回找不到工具），接著載入完整的工具；子行程當掉前就連上、拿著真的 session 的用戶端
        （:attr:`held_session`，工具清單是真的工具、沒有 setup_status）請它再呼叫一次原本要用的工具——子行程重啟過的話那次
        拿到子行程的 404，Claude Code 重新連線並重試。兩種都另外寫手動重新連線的方法。
        """
        p, admin = self.product, self.admin
        reconnect_zh = "或讓用戶端重新連線（Claude Code：/mcp → 選這個伺服器 → Reconnect；其他用戶端：重新連接或開新對話）。"
        reconnect_en = ("or reconnect the client (Claude Code: /mcp → select this server → Reconnect; other clients: "
                        "reconnect or start a new conversation).")
        if self.held_session:
            then_zh = ("請再呼叫一次原本要用的工具（子行程重啟過的話這次會拿到 404，Claude Code 會因此自動重新連線並重試），"
                       + reconnect_zh)
            then_en = ("call the tool you were using once more (if the process was restarted, that call gets a 404 and "
                       "Claude Code reconnects automatically and retries it), " + reconnect_en)
        else:
            then_zh = ("請再呼叫一次 setup_status（Claude Code 會因此自動重新連線、載入完整的工具；重新連線後這次呼叫若回 "
                       "Unknown tool 之類「找不到 setup_status」的錯誤，代表已經換好，直接改用其他工具即可），" + reconnect_zh)
            then_en = ("call setup_status once more (Claude Code then reconnects automatically and loads the full tool "
                       "list; if that call then reports that setup_status does not exist, for example \"Unknown tool\", "
                       "the switch is done and the other tools are ready), " + reconnect_en)
        if self.held_session:
            stuck_zh, stuck_en = "已經連上的用戶端不會自己重新連線", "a client that is already connected does not reconnect by itself"
        elif self.reason == NOT_CONFIGURED:
            stuck_zh = "已經連上的用戶端不會自己換成完整的工具"
            stuck_en = "a client that is already connected does not switch to the full tool list by itself"
        else:
            stuck_zh, stuck_en = "已經連上的用戶端不會自己重新連線", "a client that is already connected does not reconnect by itself"
        if self.reason == NOT_CONFIGURED:
            fields_zh = "、".join(f"{label}（{key}）" for key, label in self.missing)
            fields_en = ", ".join(f"{label} ({key})" for key, label in self.missing)
            where_zh = (f"請管理員登入 {admin}（MCP 管理頁），到 Connection 頁（{admin}config）"
                        if admin else "請管理員登入這個 MCP 服務的管理頁（與 MCP 網址同一個主機的根路徑），到 Connection 頁")
            where_en = (f"An administrator needs to sign in to {admin} (MCP admin page), open the Connection page "
                        f"({admin}config)" if admin else
                        "An administrator needs to sign in to this MCP service's admin page (the root path of the "
                        "MCP URL's host), open the Connection page")
            zh = (f"這個 {p} MCP 還沒設定 {p} 連線，所以 MCP 子行程沒有在跑，目前沒有任何 {p} 工具可用。"
                  f"{where_zh}填入：{fields_zh}，再按 Save。儲存後 MCP 子行程會自動啟動，但{stuck_zh}：存好之後{then_zh}")
            en = (f"This {p} MCP server is not configured yet: the {p} connection has not been saved, so the MCP "
                  f"server process is not running and no {p} tools are available. {where_en}, fill in {fields_en}, "
                  f"and click Save. The MCP server process starts automatically after saving, but {stuck_en}: after "
                  f"saving, {then_en}")
        else:
            where_zh = (f"請管理員登入 {admin}（MCP 管理頁）查看 Logs 頁（{admin}logs）" if admin
                        else "請管理員登入這個 MCP 服務的管理頁（與 MCP 網址同一個主機的根路徑）查看 Logs 頁")
            where_en = (f"An administrator should sign in to {admin} (MCP admin page) and check the Logs page "
                        f"({admin}logs)" if admin else
                        "An administrator should sign in to this MCP service's admin page (the root path of the "
                        "MCP URL's host) and check the Logs page")
            zh = (f"這個 {p} MCP 的子行程目前沒有在跑（可能剛結束，或正在啟動、自動重啟中），暫時沒有任何 {p} 工具可用。"
                  f"{where_zh}。子行程恢復後，{stuck_zh}：{then_zh}")
            en = (f"The MCP server process of this {p} MCP is not running right now (it may have exited, or be "
                  f"starting or restarting), so no {p} tools are available. {where_en}. Once the process is running "
                  f"again, {stuck_en}: {then_en}")
        return f"{zh}\n\n{en}"

    def tool(self) -> dict:
        """唯一的工具。說明寫明「之後再呼叫一次」：Claude Code 只有在模型呼叫這個伺服器的工具時才會拿到 404、重新連線。"""
        p = self.product
        if self.reason == NOT_CONFIGURED:
            description = (f"{p} MCP 尚未設定：還沒設定 {p} 連線，目前沒有其他工具可用。呼叫這支工具可取得設定步驟"
                           f"（不需要參數）；管理員存好連線後再呼叫一次，Claude Code 會因此自動重新連線、載入完整的工具。"
                           f"This {p} MCP server is not configured yet (no {p} connection), so no other tools are "
                           f"available. Call this tool to get the setup steps (no arguments); after an administrator has "
                           f"saved the connection, call it once more and Claude Code reconnects automatically and loads "
                           f"the full tool list.")
        else:
            description = (f"{p} MCP 的子行程目前沒有在跑，暫時沒有其他工具可用。呼叫這支工具可取得處理步驟"
                           f"（不需要參數）；子行程恢復後再呼叫一次，Claude Code 會因此自動重新連線、載入完整的工具。"
                           f"The {p} MCP server process is not running right now, so no other tools are available. "
                           f"Call this tool to get the next steps (no arguments); once the process is running again, "
                           f"call it once more and Claude Code reconnects automatically and loads the full tool list.")
        title = f"{p} MCP setup status"
        return {
            "name": SETUP_TOOL,
            "title": title,
            "description": description,
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"title": title, "readOnlyHint": True, "destructiveHint": False,
                            "idempotentHint": True, "openWorldHint": False},
        }


def product_label(title: str) -> str:
    """``create_app`` 的 title（「Nextcloud MCP Admin」）→ 說明裡的產品名稱（「Nextcloud」）。"""
    label = (title or "").strip()
    if label.endswith(" MCP Admin"):
        label = label[: -len(" MCP Admin")].strip()
    return label or "MCP"


def admin_root(request: Request) -> str | None:
    """管理頁的根網址（結尾帶 ``/``），取自這次請求的 Host；Host 不是合法的主機名稱或 IP［:port］就回 None。

    scheme：請求本身或 ``X-Forwarded-Proto`` 是 https 就是 https；直接連容器（帶 port、IP、localhost）用請求的 scheme；
    其餘（不帶 port 的網域名稱）一律 https——PaaS 的 TLS 在 Cloudflare 終止，進到容器的是 http。
    """
    split = _split_host((request.headers.get("host") or "").strip().lower())
    if split is None:
        return None
    name, port = split
    scheme = str(request.scope.get("scheme") or "http").lower()
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if forwarded == "https" or scheme == "https":
        scheme = "https"
    elif not (port or name == "localhost" or name.endswith(".localhost") or _is_ip(name)):
        scheme = "https"
    root = request.scope.get("root_path") or ""
    if not isinstance(root, str) or not _ROOT_PATH.fullmatch(root):
        root = ""
    return f"{scheme}://{name}{':' + port if port else ''}{root}/"


def _split_host(host: str) -> tuple[str, str] | None:
    if host.startswith("["):
        end = host.find("]")
        name, rest = host[: end + 1], host[end + 1:]
        if end < 0 or not _is_ip(name):
            return None
    else:
        name, sep, port = host.partition(":")
        rest = sep + port
        if not _HOSTNAME.fullmatch(name):
            return None
    if not rest:
        return name, ""
    port = rest[1:]
    if not rest.startswith(":") or not _PORT.fullmatch(port) or not 1 <= int(port) <= 65535:
        return None
    return name, port


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name[1:-1] if name.startswith("[") and name.endswith("]") else name)
    except ValueError:
        return False
    return True
