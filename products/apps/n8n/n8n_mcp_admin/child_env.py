"""n8n 子行程的推導環境變數（woow_paas_platform#1531）。

管理頁的設定要換算成上游 n8n-mcp 真正會讀的變數：

- Permissions（``tools.disabled_operations``）→ ``DISABLED_TOOL_OPERATIONS``，格式
  ``tool:op1,op2;tool2:op``（上游 ``dist/mcp/tool-policy.js``，操作名比對時轉小寫）。過去只存進
  config.json，子行程從來沒拿到，停用 delete 後 MCP 仍可刪除 workflow／execution。
- Session Timeout（``connection.mcp_session_timeout``，秒）→ ``SESSION_TIMEOUT_MINUTES``（分鐘，
  無條件進位）；Max Sessions（``connection.mcp_max_sessions``）→ ``N8N_MCP_MAX_SESSIONS``。
  過去送的是 ``MCP_SESSION_TIMEOUT``／``MCP_MAX_SESSIONS``，上游不讀；而且舊的固定 HTTP 模式
  根本沒有 session。兩者都只在 SingleSessionHTTPServer（``dist/mcp/index.js``）生效。

Session Timeout 的實際粒度（商用驗收 DATA-NMCP-003-3）：上游 ``dist/http-server-single-session.js``
寫死 ``SESSION_CLEANUP_INTERVAL = 5 * 60 * 1000``，過期 session 只在每 5 分鐘一次的清掃時移除（收到請求時
不檢查是否過期），``SESSION_TIMEOUT_MINUTES`` 又只吃整數分鐘。所以 session 實際在「設定值進位成分鐘」之後
0～5 分鐘內才結束並釋放名額，最小有效粒度約 5 分鐘；上游沒有可調短清掃週期的環境變數。管理頁欄位說明照此寫，
映像建置時以 ``verify_upstream_session_sweep()`` 確認上游週期沒變（變了就在 CI 失敗，提醒同步改說明）。

名額已滿時的行為（本版：自動回收維持關閉，名額滿回 429）：上游名額滿時新的 ``initialize`` 回 429＋-32000
``Session limit reached (N)…``；閘道原樣把這個 429 回給用戶端，**不會**主動結束任何 session（proxy 的
``_AUTOMATIC_RECLAIM_EXPERIMENTAL`` 固定為 False，環境與請求都無法開啟）。Claude.ai 等用戶端重連時不送 DELETE，
舊 session 會一直占名額，直到它逾時：Session Timeout（進位成分鐘）之後，再加上上游最多 5 分鐘的清掃週期
（預設 900 秒 → 約 15～20 分鐘）。這段時間新連線一律 429。維運可以：調高 Max Sessions、縮短 Session Timeout
（最小有效粒度約 5 分鐘），或在 Settings 重啟 MCP Server 子行程立即清空所有 session（連線中的用戶端會斷線，
需重新 initialize）。

閘道回收（``mcp_admin_core.session_reclaim``，main.py 仍宣告但 proxy 不會呼叫）是 0e432c3 之前的行為，對最久沒活動、
閒置超過 5 分鐘的 session 送 ``DELETE /mcp`` 再重試；DELETE 沒有身分檢查，開啟前需另外審查
（見 docs/security/mcp-child-identity.md）。這裡仍提供判斷「名額已滿」的 ``is_session_limit_reply`` 與「閒置多久上游
一定已清掉」的 ``upstream_session_lifetime_seconds``，映像建置時 ``verify_upstream_session_limit_reply()`` 確認上游
的 429 文字與 DELETE 行為沒變（供未來重新啟用回收時使用）。

預設值同時調整：Session Timeout 3600 → 900 秒（被丟下的 session 最晚約 15～20 分鐘清掉）；
Max Sessions 10 → 20。上限依實測（n8n-mcp 2.96.2、Node 26.12.1、與映像相同的三個建置期修補）：每個用過工具的 session
約 1.6～3.1 MiB，子行程 1 個 session 約 141 MiB、80 個約 265 MiB；pod limits 1Gi（requests 256Mi），多 10 個名額約多
30 MiB。已存過 Connection 頁的服務沿用存檔的值，只有沒存過的才吃新預設。
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from typing import Any, Mapping

SESSION_TIMEOUT_DEFAULT_SECONDS = 900
SESSION_TIMEOUT_MIN_SECONDS = 60
SESSION_TIMEOUT_MAX_SECONDS = 86400
MAX_SESSIONS_DEFAULT = 20
MAX_SESSIONS_MIN = 1
MAX_SESSIONS_MAX = 100

# 上游 n8n-mcp 清掃過期 session 的週期（秒）；管理頁 Session Timeout 說明的「約每 5 分鐘」即此值。
UPSTREAM_SESSION_CLEANUP_SECONDS = 300
_UPSTREAM_SESSION_FILE = os.path.join("dist", "http-server-single-session.js")
_UPSTREAM_SWEEP_RE = re.compile(r"^const SESSION_CLEANUP_INTERVAL = (\d+) \* (\d+) \* 1000;$", re.MULTILINE)

# 上游名額已滿的回應（dist/http-server-single-session.js handleRequest）與 DELETE /mcp 立刻釋放名額；閘道回收（目前關閉）靠這兩件事。
SESSION_LIMIT_PREFIX = "Session limit reached ("
_UPSTREAM_LIMIT_REPLY_RE = re.compile(
    r"res\.status\(429\)\.json\(\{\s*jsonrpc: '2\.0',\s*error: \{\s*code: -32000,\s*"
    r"message: `Session limit reached \(\$\{MAX_SESSIONS\}\)\. Please wait for existing sessions to expire\.`")
_UPSTREAM_DELETE_RELEASES_RE = re.compile(
    r"app\.delete\('/mcp'.*?await this\.removeSession\(mcpSessionId, 'manual_termination'\);\s*"
    r"res\.status\(204\)\.send\(\);", re.DOTALL)

_TOOL_RE = re.compile(r"^[a-z0-9_]+$")
_OP_RE = re.compile(r"^[a-z0-9_]+$")


def _clamped_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return min(max(number, lo), hi)


def session_timeout_seconds(connection: Mapping[str, Any]) -> int:
    return _clamped_int(
        connection.get("mcp_session_timeout", SESSION_TIMEOUT_DEFAULT_SECONDS),
        SESSION_TIMEOUT_DEFAULT_SECONDS,
        SESSION_TIMEOUT_MIN_SECONDS,
        SESSION_TIMEOUT_MAX_SECONDS,
    )


def max_sessions(connection: Mapping[str, Any]) -> int:
    return _clamped_int(
        connection.get("mcp_max_sessions", MAX_SESSIONS_DEFAULT),
        MAX_SESSIONS_DEFAULT,
        MAX_SESSIONS_MIN,
        MAX_SESSIONS_MAX,
    )


def disabled_tool_operations(disabled_operations: Any) -> str:
    """``{"n8n_executions": ["delete"]}`` → ``"n8n_executions:delete"``（排序、去重、略過不合法名稱）。"""
    if not isinstance(disabled_operations, Mapping):
        return ""
    entries: list[str] = []
    for tool in sorted(disabled_operations):
        if not isinstance(tool, str) or not _TOOL_RE.match(tool):
            continue
        raw_ops = disabled_operations[tool]
        if not isinstance(raw_ops, (list, tuple)):
            continue
        ops = sorted({str(op).strip().lower() for op in raw_ops if str(op).strip()})
        ops = [op for op in ops if _OP_RE.match(op)]
        if ops:
            entries.append(f"{tool}:{','.join(ops)}")
    return ";".join(entries)


def derived_env(cfg: Mapping[str, Any]) -> dict[str, str]:
    """LaunchPolicy.derived_env：依整份 config.json 推導子行程環境變數。"""
    connection = cfg.get("connection") or {}
    tools = cfg.get("tools") or {}
    env = {
        "SESSION_TIMEOUT_MINUTES": str(max(1, math.ceil(session_timeout_seconds(connection) / 60))),
        "N8N_MCP_MAX_SESSIONS": str(max_sessions(connection)),
    }
    ops = disabled_tool_operations(tools.get("disabled_operations") if isinstance(tools, Mapping) else None)
    if ops:
        env["DISABLED_TOOL_OPERATIONS"] = ops
    return env


def is_session_limit_reply(status: int, body: bytes) -> bool:
    """上游這個回應是不是「session 名額已滿」（429＋JSON-RPC -32000 ``Session limit reached (N)…``）。

    上游另一種 429（認證失敗限流 ``Too many authentication attempts``）不算，閘道不會因此回收任何 session。
    """
    if status != 429:
        return False
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    error = data.get("error") if isinstance(data, dict) else None
    return (isinstance(error, dict) and error.get("code") == -32000
            and isinstance(error.get("message"), str) and error["message"].startswith(SESSION_LIMIT_PREFIX))


def upstream_session_lifetime_seconds(connection: Mapping[str, Any]) -> float:
    """閒置多久之後上游一定已經清掉這個 session：逾時（進位成整數分鐘）＋一次清掃週期。"""
    minutes = max(1, math.ceil(session_timeout_seconds(connection) / 60))
    return float(minutes * 60 + UPSTREAM_SESSION_CLEANUP_SECONDS)


def upstream_session_cleanup_seconds(package_dir: str) -> int | None:
    """讀上游 ``SESSION_CLEANUP_INTERVAL``（秒）；檔案不在或寫法變了（例如改成可由 env 設定）回 None。"""
    try:
        with open(os.path.join(package_dir, _UPSTREAM_SESSION_FILE), encoding="utf-8") as fh:
            source = fh.read()
    except OSError:
        return None
    match = _UPSTREAM_SWEEP_RE.search(source)
    if not match:
        return None
    return int(match.group(1)) * int(match.group(2))


def verify_upstream_session_sweep(package_dir: str | None = None) -> int:
    """映像建置時呼叫：上游清掃週期必須等於 ``UPSTREAM_SESSION_CLEANUP_SECONDS``（0＝通過、1＝失敗）。"""
    if package_dir is None:
        from . import upstream

        package_dir = upstream.package_dir()
    seconds = upstream_session_cleanup_seconds(package_dir)
    if seconds != UPSTREAM_SESSION_CLEANUP_SECONDS:
        print(
            f"[n8n-mcp] SESSION_CLEANUP_INTERVAL = {seconds!r} 秒，與管理頁說明的 "
            f"{UPSTREAM_SESSION_CLEANUP_SECONDS} 秒不符：請更新 child_env.UPSTREAM_SESSION_CLEANUP_SECONDS "
            "與 frontend Session Timeout 欄位說明（DATA-NMCP-003-3）",
            file=sys.stderr,
        )
        return 1
    print(f"[n8n-mcp] session cleanup interval {seconds}s")
    return 0


def upstream_session_limit_contract(package_dir: str) -> list[str]:
    """閘道回收（目前關閉）依賴的上游行為，缺了哪幾項（空清單＝都在）。"""
    try:
        with open(os.path.join(package_dir, _UPSTREAM_SESSION_FILE), encoding="utf-8") as fh:
            source = fh.read()
    except OSError:
        return ["dist/http-server-single-session.js"]
    missing = []
    if not _UPSTREAM_LIMIT_REPLY_RE.search(source):
        missing.append("429 -32000 'Session limit reached (${MAX_SESSIONS}). …'")
    if not _UPSTREAM_DELETE_RELEASES_RE.search(source):
        missing.append("DELETE /mcp → removeSession(…, 'manual_termination') → 204")
    return missing


def verify_upstream_session_limit_reply(package_dir: str | None = None) -> int:
    """映像建置時呼叫：上游「名額已滿」的回應與 DELETE 釋放名額的行為要和閘道回收的假設一致（0＝通過、1＝失敗）。"""
    if package_dir is None:
        from . import upstream

        package_dir = upstream.package_dir()
    missing = upstream_session_limit_contract(package_dir)
    if missing:
        print(
            "[n8n-mcp] 上游 session 名額行為改了，閘道回收（mcp_admin_core.session_reclaim）認不出或無法釋放名額："
            + "；".join(missing) + "。請更新 child_env.is_session_limit_reply／SESSION_LIMIT_PREFIX 與測試",
            file=sys.stderr,
        )
        return 1
    print("[n8n-mcp] session limit reply and DELETE release verified")
    return 0
