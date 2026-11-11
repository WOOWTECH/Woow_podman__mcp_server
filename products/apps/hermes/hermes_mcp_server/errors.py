"""Hermes 後端（Gateway／Dashboard）失敗時給人看的訊息（商用驗收 AI-HMCP-003-3）。

httpx 原文（``Server error '502 Bad Gateway' for url …``＋developer.mozilla.org 連結）沒說是哪個後端、
也沒有下一步；後台「測試連線」更曾把 Cloudflare 502 整頁 HTML 塞進訊息。這裡統一成：

    Hermes Dashboard (http://…:9119) 不可達 (unreachable): HTTP 502 Bad Gateway. <建議動作>

MCP 工具（hermes_mcp_server.server）與後台測試連線（hermes_mcp_admin.routers.config）共用同一套說法。
回應本文只取 JSON 的 detail／error／message 欄位，HTML 一律不轉述。
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

# 後端 → (顯示名稱, 管理頁區塊)
BACKENDS = {
    "gateway": ("Hermes Gateway", "Connection > Gateway API Server"),
    "dashboard": ("Hermes Dashboard", "Connection > Dashboard REST API"),
}

_DETAIL_LIMIT = 200
_URL_PASSWORD = re.compile(r"(://[^/\s:@]*:)[^/\s@]+@")
# 前面的反向代理（Cloudflare、ingress）找不到後端時的狀態碼
_UNREACHABLE_STATUS = frozenset({502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530})


class HermesBackendError(RuntimeError):
    """Hermes 後端連不上、逾時或回了看不懂的內容；訊息已整理成給人看的句子。"""


class HermesInputError(HermesBackendError, ValueError):
    """An input guard refused the caller's arguments (``INVALID_INPUT: text``); still a ValueError for callers."""


class HermesBackendHTTPError(httpx.HTTPStatusError):
    """Hermes 後端回 4xx／5xx；保留 HTTPStatusError 型別（request／response 仍可取），只換訊息。"""


class DashboardResponseDecodeError(ValueError):
    """Dashboard JSON 解析失敗；公開文字固定且不包含上游本文。"""


class DashboardResponseShapeError(ValueError):
    """Dashboard JSON 形狀不符；公開文字固定且不包含上游本文。"""


def dashboard_shape(valid: bool) -> None:
    """Reject a decoded Dashboard field without reflecting its value or type."""
    if not valid:
        raise DashboardResponseShapeError(
            "BACKEND_UNEXPECTED_RESPONSE: Hermes Dashboard returned an unexpected JSON shape; "
            "verify Connection > Dashboard REST API in the MCP admin page."
        ) from None


def dashboard_json(response: httpx.Response, *, require_object: bool = False) -> Any:
    """Parse a Dashboard response and validate an object where the caller needs one.

    Keep this boundary around response.json(), not around the whole tool: local
    programming errors and cancellation must still propagate unchanged.
    """
    try:
        value = response.json()
    except (ValueError, RecursionError):
        raise DashboardResponseDecodeError(
            "BACKEND_UNEXPECTED_RESPONSE: Hermes Dashboard returned non-JSON; "
            "verify Connection > Dashboard REST API in the MCP admin page."
        ) from None
    if require_object and not isinstance(value, dict):
        raise DashboardResponseShapeError(
            "BACKEND_UNEXPECTED_RESPONSE: Hermes Dashboard returned a non-object JSON response; "
            "verify Connection > Dashboard REST API in the MCP admin page."
        ) from None
    return value


# ---------------------------------------------------------------------------
# Stable error codes: every MCP-visible Hermes error reads ``CODE: text``.
# Codes of the HA add-on (apps/runtime/backend_policy.py) are reused unchanged; the
# local ones below are the only additions. The same table is carried by the HA
# add-on's vendored copy of this module.
#
#   BACKEND_HTTP_ERROR status=N      backend answered with another HTTP status
#   BACKEND_AUTH_REJECTED status=N   backend answered 401/403
#   BACKEND_REDIRECT_REJECTED        backend answered a redirect
#   BACKEND_UNAVAILABLE              backend unreachable, 5xx proxy statuses, or not configured
#   BACKEND_TIMEOUT                  backend did not answer in time
#   BACKEND_UNEXPECTED_RESPONSE      backend answered with a wrong shape / non-JSON / ended a stream early
#   INVALID_INPUT                    the caller's arguments were refused by an input guard
#   NOT_FOUND                        the named session / cron job / webhook / MCP server does not exist
#   CONFIRMATION_REQUIRED            Hermes needs an explicit confirmation (expensive model); nothing changed
#   CONFIG_NOT_APPLIED               a config write was accepted but reads back differently
#   CHAT_TURN_FAILED                 the Gateway finished the chat turn as failed / incomplete / cancelled
#   SESSION_DELETE_OUTCOME_UNKNOWN   (existing) a session delete may or may not have been committed
#   CRON_CREATE_OUTCOME_UNKNOWN      (existing) a cron create may or may not have been committed
# ---------------------------------------------------------------------------

INVALID_INPUT = "INVALID_INPUT"
NOT_FOUND = "NOT_FOUND"
CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
CONFIG_NOT_APPLIED = "CONFIG_NOT_APPLIED"
CHAT_TURN_FAILED = "CHAT_TURN_FAILED"
BACKEND_UNEXPECTED_RESPONSE = "BACKEND_UNEXPECTED_RESPONSE"


def coded(code: str, text: str) -> str:
    """``CODE: text`` (idempotent when *text* already starts with the code)."""
    return text if text.startswith(code + ":") else f"{code}: {text}"


def coded_error(code: str, text: str) -> HermesBackendError:
    return HermesBackendError(coded(code, text))


_PUBLIC_HINT_SUFFIX = " in the MCP admin page"
_CONFLICT_CODES = frozenset({"revision_conflict", "admission_conflict", "client_upgrade_required"})
_CONNECT_ERROR = (
    "BACKEND_UNAVAILABLE: {label} 不可達 (unreachable): Check that the Hermes service is running "
    "and that {hint} has the right address."
)


def public_backend_error(exc: BaseException, backend: str) -> str:
    """MCP-visible Hermes backend errors contain only a category, HTTP status and repair guidance.

    The configured URL, httpx exception text, response body and redirect Location can all contain
    credentials or internal detail. Keep those out of both tool errors and inspect's structured result;
    the admin GUI's separate connection diagnostics (:func:`describe`) are unchanged.
    The three fenced-session 409 codes are an exact allowlist because callers need to distinguish them.
    """
    label, section = BACKENDS[backend]
    hint = section + _PUBLIC_HINT_SUFFIX
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return (
                f"BACKEND_AUTH_REJECTED status={status}: {label} 憑證被拒 (credentials rejected); "
                f"update {hint} and press Save & Restart."
            )
        if 300 <= status < 400:
            return (
                f"BACKEND_REDIRECT_REJECTED status={status}: {label} returned a redirect; "
                f"verify {hint} and press Save & Restart."
            )
        if status in _UNREACHABLE_STATUS:
            return (
                f"BACKEND_UNAVAILABLE status={status}: {label} 不可達 (unreachable); "
                f"check that the Hermes service is running and verify {hint}."
            )
        # Fenced session mutations need these stable conflict names so a client can distinguish stale
        # revisions from reuse of an admission key. Never reflect arbitrary upstream detail.
        if status == 409:
            try:
                detail = dashboard_json(exc.response, require_object=True).get("detail")
            except (DashboardResponseDecodeError, DashboardResponseShapeError):
                detail = None
            if detail in _CONFLICT_CODES:
                return (
                    f"BACKEND_HTTP_ERROR status=409 code={detail}: {label} rejected the operation; "
                    f"check the session state and {hint}."
                )
        return (
            f"BACKEND_HTTP_ERROR status={status}: {label} request failed; "
            f"verify {hint} and the operation settings."
        )
    if isinstance(exc, httpx.TimeoutException):
        return f"BACKEND_TIMEOUT: {label} timed out; check the Hermes service and {hint}."
    if isinstance(exc, httpx.TransportError):
        return _CONNECT_ERROR.format(label=label, hint=hint)
    if isinstance(exc, json.JSONDecodeError):
        return f"BACKEND_UNEXPECTED_RESPONSE: {label} returned non-JSON; verify {hint}."
    if isinstance(exc, httpx.HTTPError):
        return f"BACKEND_UNEXPECTED_RESPONSE: {label} returned an unreadable response; verify {hint}."
    raise TypeError(f"Unsupported Hermes backend error type: {type(exc).__name__}")


def _safe_url(url: str) -> str:
    return _URL_PASSWORD.sub(r"\1***@", (url or "").strip().rstrip("/"))


def response_detail(response: httpx.Response) -> str:
    """回應本文的重點：JSON 的 detail／error／message（巢狀 error.message 也認），否則空字串。

    不回傳 HTML 或其他純文字本文——代理錯誤頁對使用者沒有資訊、還會把訊息撐爆。
    """
    try:
        body: Any = response.json()
    except (ValueError, RecursionError):
        return ""
    for _ in range(3):
        if isinstance(body, dict):
            for key in ("detail", "error", "message"):
                if key in body and body[key] not in (None, "", {}, []):
                    body = body[key]
                    break
            else:
                break
        else:
            break
    if isinstance(body, list) and body and all(isinstance(e, dict) and "loc" in e and "msg" in e for e in body):
        # FastAPI 422（AI-HMCP-001-2）：[{"loc": ["body", "scope"], "msg": "Field required"}…] → "scope: Field required"
        text = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'] if p != 'body') or 'body'}: {e['msg']}" for e in body
        )
    elif isinstance(body, (dict, list)):
        text = json.dumps(body, ensure_ascii=False)
    elif isinstance(body, (str, int, float)):
        text = str(body)
    else:
        return ""
    text = " ".join(text.split())
    if "<" in text:  # 夾著 HTML 的 detail 也不轉述
        return ""
    return text if len(text) <= _DETAIL_LIMIT else text[: _DETAIL_LIMIT - 1] + "…"


def _status_text(response: httpx.Response) -> str:
    phrase = response.reason_phrase or ""
    return f"HTTP {response.status_code}" + (f" {phrase}" if phrase else "")


def describe(exc: BaseException, backend: str, url: str) -> str:
    """把 httpx／JSON 例外翻成「哪個後端、怎麼了、下一步做什麼」。"""
    label, hint = BACKENDS[backend]
    where = f"{label} ({_safe_url(url)})"

    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        status = response.status_code
        detail = response_detail(response)
        suffix = f": {detail}" if detail else ""
        if status in (401, 403):
            return (
                f"{where} 憑證被拒 (credentials rejected): {_status_text(response)}{suffix}. "
                f"The Hermes password / API key may have changed; update it in the MCP admin page under "
                f"{hint} and press Save & Restart."
            )
        if status in _UNREACHABLE_STATUS:
            return (
                f"{where} 不可達 (unreachable): {_status_text(response)}{suffix}. "
                f"Check that the Hermes service is running (restart it from its service manager if needed) and retry; "
                f"if its address changed, update {hint} in the MCP admin page."
            )
        if status == 404:
            return (
                f"{where} 找不到這個 API (not found): {_status_text(response)} for {exc.request.url.path}{suffix}. "
                f"Check that {hint} points at the Hermes {label.split()[-1].lower()} and that this Hermes version "
                f"supports the feature."
            )
        return f"{where} 回應錯誤 (request failed): {_status_text(response)}{suffix}."

    if isinstance(exc, httpx.TimeoutException):
        return (
            f"{where} 逾時 (timed out): no response in time. Hermes may be busy or restarting; retry shortly, "
            f"and check the Hermes service status if it keeps happening."
        )

    if isinstance(exc, httpx.TransportError):
        reason = str(exc).strip() or type(exc).__name__
        return (
            f"{where} 不可達 (unreachable): {reason}. Check that the Hermes service is running and that {hint} "
            f"in the MCP admin page has the right address."
        )

    if isinstance(exc, json.JSONDecodeError):
        return (
            f"{where} 回了非 JSON 的內容 (unexpected non-JSON response). Check that {hint} points at the "
            f"Hermes {label.split()[-1].lower()}, not another page or proxy."
        )

    return f"{where}: {exc}"


def as_backend_error(exc: BaseException, backend: str, url: str) -> Exception:
    """回傳要往上拋的例外：HTTP 錯誤保留 HTTPStatusError 型別，其餘用 HermesBackendError。"""
    message = describe(exc, backend, url)
    if isinstance(exc, httpx.HTTPStatusError):
        return HermesBackendHTTPError(message, request=exc.request, response=exc.response)
    return HermesBackendError(message)


# 交給 describe() 翻譯的例外種類；其他（程式錯誤、ConnectionNotConfiguredError）原樣往上。
TRANSLATABLE = (httpx.HTTPError, json.JSONDecodeError)
