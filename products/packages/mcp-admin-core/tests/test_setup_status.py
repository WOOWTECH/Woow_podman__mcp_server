"""子行程沒在跑時，閘道自己回答的最小 MCP 伺服器（``mcp_admin_core/setup_status.py``）與 ``/healthz`` 的 ``mcp`` 欄位。

背景（2026-10-27）：PaaS 新開的 Nextcloud MCP 在管理頁存好連線之前，子行程缺必要連線欄位就結束（exit 2），閘道對所有 MCP
請求一律回 HTTP 503，平台卻顯示「運作中」——租戶在 Claude.ai／Claude Code 看不出哪裡不對、要去哪裡設定（round3 staging：
initialize／tools/list 都 503）。修正後 token 驗過、子行程沒在跑時，``/mcp`` 由閘道回一個合法的最小 MCP 伺服器，說明原因與
要去哪裡設定；子行程起來之後，還帶著佔位 session 的請求回 404，讓用戶端依規格重新 initialize、拿到真的 session 與工具。

新模組一律在測試裡才 import（修正前的基底沒有這個模組，同一份測試在基底上逐項失敗，而不是整個檔案收集失敗）。
"""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

TOKEN = "setup-status-test-token-0123456789"
URL = f"/private_{TOKEN}/mcp"
HOST = "paas-cs-test0001.woowtech.io"
ADMIN = f"https://{HOST}/"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
FIELDS = (("base_url", "Nextcloud 網址"), ("username", "Nextcloud 帳號"), ("app_password", "App password"))
COMPLETE = {"base_url": "https://cloud.example.com", "username": "assistant", "app_password": "app-pass-123"}
REAL_CHILD = Path(__file__).with_name("_streamable_slow_server.py")
_DEFAULT = object()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _policy(required=FIELDS):
    from mcp_admin_core.launch_policy import LaunchPolicy

    return LaunchPolicy(
        command="nextcloud-mcp-server",
        args=("--port", "{port}"),
        env_prefix="NEXTCLOUD_MCP_",
        connection_keys=frozenset({"base_url", "username", "app_password"}),
        required_connection=required,
    )


class _RunningPM:
    """子行程在跑（只給閘道與 /healthz 看狀態；不 spawn 任何東西）。"""

    is_running = True
    child_pid = 4242


@pytest.fixture
def gateway(tmp_config_path, monkeypatch):
    """``gateway(connection=..., policy=..., port=..., running=...)`` → 指向閘道的 TestClient（不進 lifespan、不 spawn 子行程）。

    預設政策＝Nextcloud 的三個必要欄位；``policy=None`` 是沒有政策（core 自行組裝）。``port`` 預設是沒人在聽的埠。
    """
    import mcp_admin_core.config.store as store_mod
    import mcp_admin_core.process as process_mod
    import mcp_admin_core.proxy as proxy_mod
    from mcp_admin_core import create_app

    def _make(*, connection=None, policy=_DEFAULT, port=None, running=False, tools=None,
              title="Nextcloud MCP Admin", base_url=f"https://{HOST}", app_kwargs=None, **client_kwargs):
        cfg = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
        cfg.update({"mcp_auth_token": TOKEN, "admin_password": "admin-SECRET-pw"})
        cfg["connection"] = dict(connection or {})
        cfg["mcp_server"] = {**cfg["mcp_server"], "port": port or _free_port()}
        if tools is not None:
            cfg["tools"] = tools
        tmp_config_path.write_text(json.dumps(cfg))
        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
        monkeypatch.setattr(store_mod, "_instance", None)
        monkeypatch.setattr(process_mod, "_instance", _RunningPM() if running else None)
        monkeypatch.setattr(proxy_mod, "_client", None)
        store_mod.get_config_store()
        app = create_app(title=title, launch_policy=_policy() if policy is _DEFAULT else policy, **(app_kwargs or {}))
        return TestClient(app, base_url=base_url, **client_kwargs)

    return _make


def _rpc(method, rpc_id=1, params=None):
    message = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _initialize(client, version="2025-06-18", headers=HEADERS):
    return client.post(URL, headers=headers, json=_rpc("initialize", 1, {
        "protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}}))


def _session(client) -> dict:
    response = _initialize(client)
    assert response.status_code == 200, response.text
    return {**HEADERS, "Mcp-Session-Id": response.headers["mcp-session-id"]}


def _instructions(client) -> str:
    return _initialize(client).json()["result"]["instructions"]


# ---------------------------------------------------------------------------
# U1：最小 MCP 伺服器的每一種方法
# ---------------------------------------------------------------------------


def test_initialize_answers_a_minimal_server_that_says_what_to_configure(gateway):
    from mcp_admin_core import setup_status

    c = gateway()
    r = _initialize(c)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 1 and "error" not in body
    result = body["result"]
    assert set(result) == {"protocolVersion", "capabilities", "serverInfo", "instructions"}
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    info = result["serverInfo"]
    assert "Nextcloud" in info["name"] and "尚未設定" in info["name"] and "not configured" in info["name"]
    assert isinstance(info["version"], str) and info["version"]
    text = result["instructions"]
    assert "這個 Nextcloud MCP 還沒設定 Nextcloud 連線" in text
    assert "This Nextcloud MCP server is not configured yet" in text
    assert ADMIN in text and f"{ADMIN}config" in text and "Connection" in text and "Save" in text
    for key, label in FIELDS:
        assert f"{label}（{key}）" in text and f"{label} ({key})" in text
    sid = r.headers["mcp-session-id"]
    assert sid.startswith(setup_status.PLACEHOLDER_SESSION_PREFIX)
    assert len(sid) >= len(setup_status.PLACEHOLDER_SESSION_PREFIX) + 32
    assert all(0x21 <= ord(ch) <= 0x7E for ch in sid)  # MCP：session id 只能是可見 ASCII
    assert _initialize(c).headers["mcp-session-id"] != sid  # 每次 initialize 一個新的


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("2024-11-05", "2024-11-05"),
        ("2025-03-26", "2025-03-26"),
        ("2025-06-18", "2025-06-18"),
        ("2025-11-25", "2025-11-25"),
        ("2026-07-28", "2025-11-25"),  # 2026 世代沒有 initialize 握手：回 2025 世代最新的
        ("1999-01-01", "2025-11-25"),
        (None, "2025-11-25"),
    ],
)
def test_protocol_version_follows_the_client_within_the_supported_range(gateway, requested, expected):
    c = gateway()
    params = {"capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}}
    if requested is not None:
        params["protocolVersion"] = requested
    r = c.post(URL, headers=HEADERS, json=_rpc("initialize", "init-1", params))
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "init-1"
    assert r.json()["result"]["protocolVersion"] == expected


def test_supported_versions_match_the_handshake_versions_of_the_children_sdk():
    """與子行程（MCP SDK 2.x）的 initialize 協商同一組版本；SDK 增加握手版本時這裡要一起看。"""
    sdk = pytest.importorskip("mcp_types.version")
    from mcp_admin_core import setup_status

    assert setup_status.PROTOCOL_VERSIONS == sdk.HANDSHAKE_PROTOCOL_VERSIONS
    assert setup_status.PROTOCOL_VERSIONS[-1] == sdk.LATEST_HANDSHAKE_VERSION


@pytest.mark.parametrize(
    "payload",
    [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        [{"jsonrpc": "2.0", "method": "notifications/initialized"},
         {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}}],
        {"jsonrpc": "2.0", "id": 5, "result": {}},  # 用戶端對伺服器請求的回應
    ],
)
def test_notifications_and_client_responses_get_202_without_body(gateway, payload):
    c = gateway()
    r = c.post(URL, headers=_session(c), json=payload)
    assert r.status_code == 202
    assert r.content == b""


def test_ping_returns_an_empty_result(gateway):
    c = gateway()
    r = c.post(URL, headers=_session(c), json=_rpc("ping", 7))
    assert r.status_code == 200
    assert r.json() == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_tools_list_offers_only_the_setup_status_tool(gateway):
    c = gateway()
    r = c.post(URL, headers=_session(c), json=_rpc("tools/list", 2))
    assert r.status_code == 200
    result = r.json()["result"]
    assert [tool["name"] for tool in result["tools"]] == ["setup_status"]
    assert "nextCursor" not in result
    tool = result["tools"][0]
    assert "呼叫這支工具可取得設定步驟" in tool["description"] and "Call this tool to get the setup steps" in tool["description"]
    assert tool["inputSchema"] == {"type": "object", "properties": {}, "additionalProperties": False}
    assert tool["annotations"]["readOnlyHint"] is True and tool["annotations"]["destructiveHint"] is False


@pytest.mark.parametrize(
    "params",
    [
        {"name": "setup_status", "arguments": {}},
        {"name": "get_file_tree", "arguments": {"path": ""}},  # 真的子行程才有的工具（舊連線或模型猜的名字）
        {"name": "no_such_tool"},
        None,
    ],
)
def test_tools_call_with_any_name_is_an_error_result_with_the_setup_steps(gateway, params):
    c = gateway()
    headers = _session(c)
    instructions = _instructions(c)
    r = c.post(URL, headers=headers, json=_rpc("tools/call", 9, params))
    assert r.status_code == 200
    assert r.json() == {"jsonrpc": "2.0", "id": 9,
                        "result": {"content": [{"type": "text", "text": instructions}], "isError": True}}


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("resources/list", {"resources": []}),
        ("resources/templates/list", {"resourceTemplates": []}),
        ("prompts/list", {"prompts": []}),
    ],
)
def test_other_list_methods_return_empty_lists(gateway, method, expected):
    c = gateway()
    r = c.post(URL, headers=_session(c), json=_rpc(method, 4))
    assert r.status_code == 200
    assert r.json() == {"jsonrpc": "2.0", "id": 4, "result": expected}


@pytest.mark.parametrize(
    "method", ["resources/read", "prompts/get", "completion/complete", "logging/setLevel", "tasks/list", "no/such"],
)
def test_other_methods_are_method_not_found(gateway, method):
    c = gateway()
    r = c.post(URL, headers=_session(c), json=_rpc(method, 4, {}))
    assert r.status_code == 200
    assert r.json() == {"jsonrpc": "2.0", "id": 4, "error": {"code": -32601, "message": "Method not found"}}


def test_server_discover_still_falls_back_to_initialize(gateway):
    """2026 世代的 server/discover 照舊回 -32601（protocol_era），用戶端改走 initialize 握手。"""
    c = gateway()
    r = c.post(URL, headers=HEADERS, json=_rpc("server/discover", 3, {}))
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32601


def test_get_is_405_and_delete_is_accepted(gateway):
    c = gateway()
    headers = _session(c)
    r = c.get(URL, headers={**headers, "Accept": "text/event-stream"})
    assert r.status_code == 405
    assert "POST" in r.headers["allow"] and "DELETE" in r.headers["allow"]
    assert "Connection" in r.json()["detail"]  # 直接在瀏覽器打開 MCP 網址也看得到原因
    assert c.delete(URL, headers=headers).status_code == 200
    assert c.delete(URL, headers=HEADERS).status_code == 200  # 沒帶 session 也接受


def test_batch_answers_every_request_in_order(gateway):
    c = gateway()
    r = c.post(URL, headers=_session(c), json=[
        _rpc("ping", 1), {"jsonrpc": "2.0", "method": "notifications/initialized"},
        _rpc("tools/list", 2), _rpc("no/such", 3)])
    assert r.status_code == 200
    replies = r.json()
    assert [reply["id"] for reply in replies] == [1, 2, 3]
    assert replies[0]["result"] == {}
    assert replies[1]["result"]["tools"][0]["name"] == "setup_status"
    assert replies[2]["error"]["code"] == -32601


def test_oversized_batch_is_refused_instead_of_amplified(gateway):
    """每則 tools/call 都回約 1.5 KB 的說明：不設限的話，16 MiB 的 batch 會變成幾百 MB 的回應。"""
    from mcp_admin_core import setup_status

    c = gateway()
    calls = [_rpc("tools/call", index, {"name": "x"}) for index in range(setup_status.MAX_BATCH_MESSAGES + 1)]
    r = c.post(URL, headers=HEADERS, json=calls)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32600 and r.json()["id"] is None
    assert len(r.content) < 200
    r = c.post(URL, headers=HEADERS, json=calls[:-1])  # 上限以內照常逐一回答
    assert r.status_code == 200 and len(r.json()) == setup_status.MAX_BATCH_MESSAGES


def test_unparseable_body_is_a_parse_error(gateway):
    c = gateway()
    r = c.post(URL, headers=HEADERS, content=b"{not json")
    assert r.status_code == 400
    assert r.json() == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([], {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}),
        ({"jsonrpc": "2.0"}, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}),
        ([1, {"jsonrpc": "2.0", "id": 2, "method": "ping"}],
         [{"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
          {"jsonrpc": "2.0", "id": 2, "result": {}}]),
    ],
)
def test_invalid_messages_are_invalid_request(gateway, payload, expected):
    c = gateway()
    r = c.post(URL, headers=HEADERS, json=payload)
    assert r.json() == expected


# ---------------------------------------------------------------------------
# U1：兩種原因、主機、不洩漏
# ---------------------------------------------------------------------------


def test_missing_only_some_fields_lists_only_those(gateway):
    c = gateway(connection={"base_url": "https://cloud.example.com", "username": "  "})
    text = _instructions(c)
    assert "Nextcloud 帳號（username）、App password（app_password）" in text
    assert "base_url" not in text and "cloud.example.com" not in text


REAL_SESSION = "0123456789abcdef0123456789abcdef"  # 子行程發的（uuid hex），不是佔位 session


@pytest.mark.parametrize(("connection", "reason"), [(COMPLETE, "子行程目前沒有在跑"), ({}, "還沒設定")],
                         ids=["not_running", "not_configured"])
def test_a_real_session_that_loses_its_child_is_told_to_retry_the_tool_it_was_using(gateway, connection, reason):
    """子行程當掉前就連上、拿著真的 session 的用戶端（8 個產品都會遇到），呼叫真的工具拿到的是佔位說明；它的工具清單是真的工具、
    沒有 setup_status，所以說明請它「再呼叫一次原本要用的工具」（子行程重啟過的話那次拿到子行程的 404，Claude Code 重新連線並
    重試），不提 setup_status——真的 Claude Code 照「再呼叫一次 setup_status」去找，只找到「No matching deferred tools found」。"""
    c = gateway(connection=connection)
    real = {**HEADERS, "Mcp-Session-Id": REAL_SESSION}
    r = c.post(URL, headers=real, json=_rpc("tools/call", 5, {"name": "get_file_tree", "arguments": {"path": ""}}))
    assert r.status_code == 200 and "mcp-session-id" not in r.headers
    result = r.json()["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert reason in text
    for phrase in ("已經連上的用戶端不會自己重新連線", "請再呼叫一次原本要用的工具", "Claude Code 會因此自動重新連線並重試",
                   "/mcp → 選這個伺服器 → Reconnect", "does not reconnect by itself",
                   "call the tool you were using once more", "Claude Code reconnects automatically and retries it"):
        assert phrase in text, phrase
    assert "setup_status" not in text and "Unknown tool" not in text
    assert c.get(URL, headers=real).json()["detail"] == text  # GET 405 的內容同一段


def test_initialize_with_a_stale_session_is_answered_as_a_new_client(gateway):
    """initialize 帶著舊的 session（照規格不該帶）：拿到的是佔位 session 與只有 setup_status 的工具清單，說明照新連上的用戶端寫。"""
    c = gateway(connection=COMPLETE)
    r = _initialize(c, headers={**HEADERS, "Mcp-Session-Id": REAL_SESSION})
    assert r.status_code == 200
    assert "請再呼叫一次 setup_status" in r.json()["result"]["instructions"]
    assert "原本要用的工具" not in r.json()["result"]["instructions"]


def test_not_running_for_another_reason_only_points_to_the_logs(gateway):
    import mcp_admin_core.process as process_mod

    c = gateway(connection=COMPLETE)  # 必要欄位都有：子行程是當掉、或自動重啟中
    process_mod.get_process_manager()._last_output.extend(
        ["Traceback (most recent call last):", "RuntimeError: SECRET-CHILD-OUTPUT at https://cloud.example.com"])
    r = _initialize(c)
    result = r.json()["result"]
    assert "not running" in result["serverInfo"]["name"] and "沒有在跑" in result["serverInfo"]["name"]
    text = result["instructions"]
    assert "這個 Nextcloud MCP 的子行程目前沒有在跑" in text and "is not running right now" in text
    assert "Logs" in text and f"{ADMIN}logs" in text
    assert "Connection" not in text and f"{ADMIN}config" not in text
    for key, label in FIELDS:
        assert key not in text and label not in text
    headers = {**HEADERS, "Mcp-Session-Id": r.headers["mcp-session-id"]}
    answers = [r, c.post(URL, headers=headers, json=_rpc("tools/list", 2)),
               c.post(URL, headers=headers, json=_rpc("tools/call", 3, {"name": "setup_status", "arguments": {}})),
               c.get(URL, headers=headers)]
    for answer in answers:
        assert "SECRET-CHILD-OUTPUT" not in answer.text and "Traceback" not in answer.text
        assert "cloud.example.com" not in answer.text


@pytest.mark.parametrize("connection", [{}, COMPLETE], ids=["not_configured", "not_running"])
def test_guidance_says_how_an_already_connected_client_gets_the_full_tools(gateway, connection):
    """已經連上的用戶端不會自己換成完整的工具：佔位 session 要等用戶端送出下一個請求才拿到 404，而真的 Claude Code 2.1.311
    在同一段對話裡，模型不再呼叫這個伺服器的工具就不送任何 MCP 請求（對抗式審查 run1）；模型再呼叫一次 setup_status 才觸發
    404 → 重新連線 → 完整工具（run2）。所以說明（instructions＝工具結果＝GET 405 的內容）與工具說明都要請模型「之後再呼叫一次
    setup_status」，或請使用者在用戶端重新連線；不再保證「下一次請求時自動重新初始化」。"""
    c = gateway(connection=connection)
    headers = _session(c)
    text = _instructions(c)
    assert c.get(URL, headers=headers).json()["detail"] == text
    already_connected = {"not_configured": ("已經連上的用戶端不會自己換成完整的工具", "does not switch to the full tool list"),
                         "not_running": ("已經連上的用戶端不會自己重新連線", "does not reconnect by itself")}
    for phrase in (*already_connected["not_configured" if connection == {} else "not_running"],
                   "請再呼叫一次 setup_status", "Claude Code 會因此自動重新連線",
                   "/mcp → 選這個伺服器 → Reconnect", "重新連接或開新對話", "Unknown tool",
                   "call setup_status once more", "Claude Code then reconnects automatically",
                   "/mcp → select this server → Reconnect", "start a new conversation"):
        assert phrase in text, phrase
    assert "原本要用的工具" not in text  # 佔位 session 的工具清單只有 setup_status
    for overclaim in ("在下一次請求時自動重新初始化", "re-initialize on its next request"):
        assert overclaim not in text
    description = c.post(URL, headers=headers, json=_rpc("tools/list", 2)).json()["result"]["tools"][0]["description"]
    for phrase in ("再呼叫一次", "Claude Code 會因此自動重新連線", "call it once more", "Claude Code reconnects automatically"):
        assert phrase in description, phrase


@pytest.mark.parametrize("policy_kind", ["no_policy", "nothing_required"])
def test_products_without_required_fields_say_not_running(gateway, policy_kind):
    c = gateway(policy=None if policy_kind == "no_policy" else _policy(()), title="n8n MCP Admin")
    text = _instructions(c)
    assert "這個 n8n MCP 的子行程目前沒有在跑" in text and "Logs" in text
    assert "Connection" not in text


def test_no_token_password_backend_url_or_child_output_in_any_answer(gateway):
    import mcp_admin_core.process as process_mod

    c = gateway(connection={"base_url": "https://nextcloud-backend.internal:8443/nc", "username": "",
                            "app_password": "pw-SECRET-123"})
    process_mod.get_process_manager()._last_output.append("SECRET-CHILD-OUTPUT https://nextcloud-backend.internal")
    first = _initialize(c)
    headers = {**HEADERS, "Mcp-Session-Id": first.headers["mcp-session-id"]}
    answers = [first]
    for payload in (_rpc("tools/list", 2), _rpc("tools/call", 3, {"name": "setup_status", "arguments": {}}),
                    _rpc("ping", 4), _rpc("no/such", 5), {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    [_rpc("ping", 6), _rpc("prompts/list", 7)]):
        answers.append(c.post(URL, headers=headers, json=payload))
    answers += [c.post(URL, headers=headers, content=b"{oops"), c.get(URL, headers=headers),
                c.delete(URL, headers=headers)]
    secrets = (TOKEN, "pw-SECRET-123", "nextcloud-backend", "admin-SECRET-pw", "SECRET-CHILD-OUTPUT", "/private_")
    for answer in answers:
        blob = answer.text + json.dumps(dict(answer.headers))
        for secret in secrets:
            assert secret not in blob, (secret, answer.request.method, answer.status_code)


@pytest.mark.parametrize(
    ("base_url", "extra", "expected"),
    [
        ("https://paas-cs-abc.woowtech.io", {}, "https://paas-cs-abc.woowtech.io/"),
        ("http://paas-cs-abc.woowtech.io", {}, "https://paas-cs-abc.woowtech.io/"),  # TLS 在 Cloudflare 終止
        ("http://paas-cs-abc.woowtech.io", {"X-Forwarded-Proto": "http"}, "https://paas-cs-abc.woowtech.io/"),
        ("http://localhost:8080", {}, "http://localhost:8080/"),  # 直接連容器（docker compose、port-forward）
        ("http://10.0.0.5:8080", {}, "http://10.0.0.5:8080/"),
        ("http://localhost:8080", {"X-Forwarded-Proto": "https"}, "https://localhost:8080/"),
        ("http://Paas-CS-ABC.WoowTech.io", {}, "https://paas-cs-abc.woowtech.io/"),
    ],
)
def test_admin_url_comes_from_the_request(gateway, base_url, extra, expected):
    c = gateway(base_url=base_url)
    text = _initialize(c, headers={**HEADERS, **extra}).json()["result"]["instructions"]
    assert f"{expected}config" in text


def test_admin_url_keeps_the_root_path(gateway):
    c = gateway(root_path="/admin-x")
    r = _initialize(c)
    assert f"{ADMIN}admin-x/config" in r.json()["result"]["instructions"]


@pytest.mark.parametrize("host", ["evil.example.com/../x", "a b.example.com", "example.com:99999",
                                  "x" * 300 + ".com", "evil.example.com\tx", "user@evil.example.com"])
def test_unusable_host_header_falls_back_to_a_generic_hint(gateway, host):
    c = gateway()
    r = _initialize(c, headers={**HEADERS, "Host": host})
    text = r.json()["result"]["instructions"]
    assert "http://" not in text and "https://" not in text and host not in text
    assert "管理頁" in text and "Connection" in text and "base_url" in text


def test_wrong_token_is_refused_before_any_placeholder_answer(gateway):
    c = gateway()
    r = c.post("/private_wrong-token/mcp", headers=HEADERS, json=_rpc("initialize", 1, {}))
    assert r.status_code == 403
    assert "mcp-session-id" not in r.headers


_REFUSED = {"detail": "Invalid or revoked MCP auth token"}
_PLACEHOLDER_REQUESTS = [
    ("POST", _rpc("initialize", 1, {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "pytest", "version": "0"}})),
    ("POST", _rpc("tools/list", 2)),
    ("POST", _rpc("tools/call", 3, {"name": "setup_status", "arguments": {}})),
    ("GET", None),
    ("DELETE", None),
]


@pytest.mark.parametrize(("method", "payload"), _PLACEHOLDER_REQUESTS)
def test_wrong_token_with_a_placeholder_session_is_refused(gateway, method, payload):
    """佔位 session 不是通行證：閘道先驗 token，錯的 token 帶著真的發過的佔位 session 也一律 403，不回任何佔位內容。"""
    c = gateway()
    placeholder = _session(c)
    r = c.request(method, "/private_wrong-token/mcp", headers=placeholder, json=payload)
    assert r.status_code == 403, r.text
    assert r.json() == _REFUSED
    assert "mcp-session-id" not in r.headers


@pytest.mark.parametrize(("method", "payload"), _PLACEHOLDER_REQUESTS)
def test_revoked_token_with_a_placeholder_session_is_refused(gateway, method, payload):
    """拿到佔位 session 之後 token 被撤銷：原本的 token 帶著佔位 session 也是 403（佔位 session 不會讓撤銷失效）。"""
    from mcp_admin_core.config import get_config_store

    c = gateway()
    placeholder = _session(c)
    asyncio.run(get_config_store().change_mcp_token(None, action="revoke"))
    r = c.request(method, URL, headers=placeholder, json=payload)
    assert r.status_code == 403, r.text
    assert r.json() == _REFUSED
    assert "mcp-session-id" not in r.headers


@pytest.mark.parametrize("change", ["rotate", "revoke"])
@pytest.mark.parametrize("with_session", [True, False], ids=["placeholder_session", "no_session"])
def test_token_changed_while_the_placeholder_answer_is_prepared_is_refused(gateway, monkeypatch, change, with_session):
    """閘道自己回答之前再確認一次 lease：算佔位答案的期間 token 被輪替或撤銷，舊 token 的這次請求 403、不帶佔位內容。

    兩條路都要守：帶佔位 session 的請求（不轉發）與沒帶 session、轉發時連不上子行程的請求。注入點是佔位答案讀連線設定的
    那一刻（之後才組回應），模擬管理員剛好在這時輪替或撤銷 token。
    """
    from mcp_admin_core.config import get_config_store

    c = gateway()
    headers = _session(c) if with_session else HEADERS
    store = get_config_store()
    real_get = store.get
    changed = []

    async def get_then_change_token(key, default=None):
        value = await real_get(key, default)
        if key == "connection" and not changed:
            changed.append(change)
            await store.change_mcp_token(f"rotated-{TOKEN}" if change == "rotate" else None, action=change)
        return value

    monkeypatch.setattr(store, "get", get_then_change_token)
    r = c.post(URL, headers=headers, json=_rpc("tools/list", 2))
    assert changed == [change]  # 真的走到了佔位答案
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "MCP access changed; reconnect using the current shared token"}
    assert "mcp-session-id" not in r.headers and "setup_status" not in r.text


@pytest.mark.parametrize(("method", "path"), [("GET", "/sse"), ("POST", "/messages/?session_id=abc"),
                                              ("GET", ""), ("POST", "/other")])
def test_legacy_sse_and_other_paths_keep_the_503(gateway, method, path):
    c = gateway()
    r = c.request(method, f"/private_{TOKEN}{path}", headers=HEADERS,
                  json=_rpc("tools/list") if method == "POST" else None)
    assert r.status_code == 503
    assert "not running" in r.json()["detail"]


def test_running_child_that_is_not_listening_yet_is_still_502(gateway):
    c = gateway(connection=COMPLETE, running=True)  # 剛 spawn、還沒開始聽 port（啟動窗口）
    assert _initialize(c).status_code == 502


def test_restart_attempt_without_required_fields_still_answers_the_setup_steps(gateway):
    """看門狗的自動重啟嘗試期間子行程「在跑」，但缺必要欄位一定起不來：照樣回設定步驟，不回 502。"""
    c = gateway(running=True)
    r = _initialize(c)
    assert r.status_code == 200
    assert "Connection" in r.json()["result"]["instructions"]


def test_tool_manager_does_not_hide_or_block_the_setup_tool(gateway):
    c = gateway(tools={"disabled": ["setup_status", "get_file_tree"], "disabled_operations": {}})
    # 沒帶 session 的 tools/list：轉發 → 連不上 → 佔位，回應不經停用清單過濾
    r = c.post(URL, headers=HEADERS, json=_rpc("tools/list", 1))
    assert [tool["name"] for tool in r.json()["result"]["tools"]] == ["setup_status"]
    headers = _session(c)  # 佔位 session 是閘道自己的：停用清單不適用
    r = c.post(URL, headers=headers, json=_rpc("tools/list", 2))
    assert [tool["name"] for tool in r.json()["result"]["tools"]] == ["setup_status"]
    r = c.post(URL, headers=headers, json=_rpc("tools/call", 3, {"name": "setup_status", "arguments": {}}))
    result = r.json()["result"]
    assert result["isError"] is True
    assert "disabled by the administrator" not in result["content"][0]["text"]
    assert "Connection" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# U1：銜接——子行程起來之後，佔位 session 回 404、重新 initialize 拿到真的子行程
# ---------------------------------------------------------------------------


@pytest.fixture
def real_child():
    """真的 Streamable HTTP MCP 伺服器（_streamable_slow_server.py：MCP SDK MCPServer＋uvicorn），呼叫 start() 才啟動。"""
    pytest.importorskip("mcp.server.mcpserver")
    port = _free_port()
    procs: list[subprocess.Popen] = []

    def start() -> None:
        proc = subprocess.Popen([sys.executable, str(REAL_CHILD), str(port)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(proc)
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                return
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("test MCP server did not start")
                time.sleep(0.1)

    yield port, start
    for proc in procs:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _sse_or_json(response) -> list[dict]:
    if response.headers.get("content-type", "").startswith("application/json"):
        body = response.json()
        return body if isinstance(body, list) else [body]
    out = []
    for event in response.text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
        if data:
            out.append(json.loads(data))
    return out


def test_after_the_child_comes_up_the_placeholder_session_gets_404_and_reinitialize_reaches_the_child(
        gateway, real_child, monkeypatch):
    import mcp_admin_core.process as process_mod
    import mcp_admin_core.proxy as proxy_mod

    port, start = real_child
    c = gateway(connection=COMPLETE, port=port)
    placeholder = _session(c)  # 子行程沒在跑：閘道的佔位 session
    tools = c.post(URL, headers=placeholder, json=_rpc("tools/list", 2)).json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["setup_status"]

    start()  # 連線存好、子行程起來
    monkeypatch.setattr(process_mod, "_instance", _RunningPM())
    for method in ("POST", "GET"):
        proxy_mod._client = None
        r = c.request(method, URL, headers=placeholder, json=_rpc("tools/list", 3) if method == "POST" else None)
        assert r.status_code == 404, (method, r.text)
        # 與 MCP SDK 2.x 子行程對不存在的 session 回的同形狀
        assert r.json() == {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Session not found"}}
    # 終止佔位 session 一律 200（冪等、沒有狀態）：回 404 的話 TS SDK terminateSession() 會丟錯、留著舊的 session id
    r = c.delete(URL, headers=placeholder)
    assert r.status_code == 200 and r.content == b"" and "mcp-session-id" not in r.headers

    proxy_mod._client = None  # 不以 context manager 用的 TestClient 每個請求一個事件迴圈
    r = _initialize(c)  # 用戶端依規格重新 initialize（不帶 session）→ 轉給真的子行程
    assert r.status_code == 200, r.text
    assert _sse_or_json(r)[0]["result"]["serverInfo"]["name"] == "deadline-test"
    real = {**HEADERS, "Mcp-Session-Id": r.headers["mcp-session-id"]}
    assert not real["Mcp-Session-Id"].startswith("woow-")
    proxy_mod._client = None
    assert c.post(URL, headers=real, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    proxy_mod._client = None
    names = {tool["name"] for tool in _sse_or_json(c.post(URL, headers=real, json=_rpc("tools/list", 4)))[0]["result"]["tools"]}
    assert names == {"slow", "slow_blocking"}


def test_placeholder_session_keeps_the_setup_answers_until_the_child_accepts_connections(gateway, monkeypatch):
    """存好連線後子行程剛 spawn、還沒開始聽 port：佔位 session 先照樣回設定狀態，不讓用戶端 404 → 重新 initialize → 502。"""
    import mcp_admin_core.process as process_mod

    c = gateway(connection=COMPLETE)  # 沒人在聽的 port
    placeholder = _session(c)
    monkeypatch.setattr(process_mod, "_instance", _RunningPM())
    r = c.post(URL, headers=placeholder, json=_rpc("tools/list", 2))
    assert r.status_code == 200
    assert [tool["name"] for tool in r.json()["result"]["tools"]] == ["setup_status"]


def test_placeholder_session_with_required_fields_missing_never_gets_404(gateway):
    """缺必要欄位時子行程不可能真的起來（在跑的只是重啟嘗試）：佔位 session 一律照樣回答。"""
    import mcp_admin_core.process as process_mod
    from mcp_admin_core import setup_status

    with socket.socket() as listener:  # 有東西在聽 port 也一樣
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        c = gateway(port=listener.getsockname()[1], running=True)
        # 佔位伺服器沒有狀態：任何這個前綴的 session 都是它的（不先 initialize，免得 initialize 轉給只聽不答的 port）
        placeholder = {**HEADERS, "Mcp-Session-Id": setup_status.PLACEHOLDER_SESSION_PREFIX + "0" * 32}
        assert isinstance(process_mod.get_process_manager(), _RunningPM)
        r = c.post(URL, headers=placeholder, json=_rpc("ping", 2))
        assert r.status_code == 200 and r.json() == {"jsonrpc": "2.0", "id": 2, "result": {}}


# ---------------------------------------------------------------------------
# n8n 與其他會正常起來的產品：行為不變
# ---------------------------------------------------------------------------


def test_n8n_documentation_mode_is_forwarded_unchanged(gateway, monkeypatch):
    import mcp_admin_core.proxy as proxy_mod
    from mcp_admin_core.request_limits import McpRequestLimits
    from mcp_admin_core.session_reclaim import SessionReclaimer
    from n8n_mcp_admin import child_env, tool_registry
    from n8n_mcp_admin.main import LAUNCH_POLICY

    doc_tools = [{"name": f"doc_tool_{index}", "inputSchema": {"type": "object"}} for index in range(7)]
    seen = []

    def handler(request):
        message = json.loads(request.content)
        seen.append(message["method"])
        if message["method"] == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "real-n8n-session"}, json={
                "jsonrpc": "2.0", "id": message["id"],
                "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "n8n-documentation-mcp", "version": "2.96.2"}}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": {"tools": doc_tools}})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    c = gateway(policy=LAUNCH_POLICY, title="n8n MCP Admin", running=True, app_kwargs=dict(
        tool_registry=tool_registry, mcp_request_limits=McpRequestLimits(4 * 1024 * 1024, 10_000),
        session_reclaimer=SessionReclaimer(is_session_limit=child_env.is_session_limit_reply,
                                           session_lifetime=child_env.upstream_session_lifetime_seconds,
                                           label="n8n")))
    monkeypatch.setattr(proxy_mod, "_get_client", lambda *args: upstream)
    r = _initialize(c)  # 沒有 n8n 連線：子行程照樣以文件模式在跑
    assert r.status_code == 200 and r.headers["mcp-session-id"] == "real-n8n-session"
    assert r.json()["result"]["serverInfo"]["name"] == "n8n-documentation-mcp"
    r = c.post(URL, headers={**HEADERS, "Mcp-Session-Id": "real-n8n-session"}, json=_rpc("tools/list", 2))
    assert r.json()["result"]["tools"] == doc_tools
    assert seen == ["initialize", "tools/list"]
    # mcp 照舊是 running（子行程在跑）；第五輪起 n8n 宣告後端連線欄位，文件模式（沒有 API 網址與 key）另報 backend: missing。
    assert c.get("/healthz").json() == {"status": "ok", "mcp": "running", "backend": "missing"}
    asyncio.run(upstream.aclose())


# ---------------------------------------------------------------------------
# U2：/healthz 的 mcp 欄位
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("policy_kind", "connection", "running", "expected"),
    [
        ("required", {}, False, "not_configured"),
        ("required", {"base_url": "https://cloud.example.com", "username": "u"}, False, "not_configured"),
        ("required", {}, True, "not_configured"),  # 看門狗重啟嘗試中：在跑，但缺欄位一定起不來
        ("required", COMPLETE, True, "running"),
        ("required", COMPLETE, False, "not_running"),
        ("nothing_required", {}, True, "running"),
        ("nothing_required", {}, False, "not_running"),
        ("no_policy", {}, True, "running"),
        ("no_policy", {}, False, "not_running"),
    ],
)
def test_healthz_reports_a_coarse_mcp_state(gateway, policy_kind, connection, running, expected):
    policy = {"required": lambda: _DEFAULT, "nothing_required": lambda: _policy(()), "no_policy": lambda: None}[policy_kind]
    c = gateway(connection=connection, running=running, policy=policy())
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "mcp": expected}  # 沒有任何細節（欄位名稱、原因、輸出）


def test_healthz_stays_200_when_the_config_cannot_be_read(gateway, monkeypatch):
    from mcp_admin_core.config import get_config_store
    from mcp_admin_core.config.store import ConfigUnavailable

    c = gateway()

    async def unreadable(*args, **kwargs):
        raise ConfigUnavailable("Configuration state unavailable")

    monkeypatch.setattr(get_config_store(), "get", unreadable)
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "mcp": "not_running"}


def test_healthz_does_not_wait_for_a_slow_config_write(gateway, monkeypatch):
    """寫檔卡住時（ConfigStore 寫檔與 fsync 整段都拿著鎖，例如 Longhorn／iSCSI 卡住）/healthz 照樣立刻回 200。

    k8s readiness 逾時 3 秒、liveness 5 秒：/healthz 跟著等鎖的話 pod 會被判 NotReady、甚至被重啟（對抗式審查：寫檔慢 7 秒時
    /healthz 5 秒逾時，修正前 0.00 秒）。寫入完成前回的是寫入前、最後一次完成的版本，寫完才換。
    """
    from mcp_admin_core.config import get_config_store

    c = gateway()  # 三個必要欄位都沒填
    store = get_config_store()
    release = threading.Event()
    real_write = store._write_sync

    def slow_write(data):
        if not release.wait(10):
            raise TimeoutError("test did not release the config write")
        real_write(data)

    monkeypatch.setattr(store, "_write_sync", slow_write)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url=f"https://{HOST}") as client:
            before = await client.get("/healthz")  # 第一次讀會載入設定（lifespan 也一定先載入過）
            writer = asyncio.create_task(store.patch("connection", COMPLETE))  # 管理員存連線，寫檔卡住
            try:
                while not store._lock.locked():
                    await asyncio.sleep(0.01)
                started = time.monotonic()
                during = await asyncio.wait_for(client.get("/healthz"), 2)
                elapsed = time.monotonic() - started
            finally:
                release.set()
                await writer
            after = await client.get("/healthz")
        return before, during, elapsed, after

    before, during, elapsed, after = asyncio.run(scenario())
    assert before.json() == {"status": "ok", "mcp": "not_configured"}
    assert during.status_code == 200 and elapsed < 1, elapsed
    assert during.json() == {"status": "ok", "mcp": "not_configured"}  # 寫入前的版本
    assert after.json() == {"status": "ok", "mcp": "not_running"}  # 寫完才換：欄位齊全、子行程沒在跑


def test_healthz_without_a_snapshot_waits_only_briefly_for_the_config(gateway):
    """還沒有快照（還沒載入過設定）又剛好碰上別人拿著鎖：最多等 PROBE_CONFIG_WAIT_SECONDS，逾時就只看子行程、照樣 200。"""
    from mcp_admin_core import setup_status
    from mcp_admin_core.config import get_config_store

    c = gateway()  # 不進 lifespan：還沒有任何請求載入過設定
    store = get_config_store()

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url=f"https://{HOST}") as client:
            async with store._lock:  # 例如正在寫檔
                started = time.monotonic()
                response = await asyncio.wait_for(client.get("/healthz"), 3)
                return response, time.monotonic() - started

    response, elapsed = asyncio.run(scenario())
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mcp": "not_running"}  # 不知道缺什麼：只看子行程
    assert setup_status.PROBE_CONFIG_WAIT_SECONDS <= elapsed < setup_status.PROBE_CONFIG_WAIT_SECONDS + 1, elapsed
