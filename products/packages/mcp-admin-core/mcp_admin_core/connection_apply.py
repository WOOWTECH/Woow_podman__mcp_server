"""Connection saves acknowledge persistence separately from process application."""
from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

from fastapi import HTTPException, status

from .util import ssrf

#: 存檔時被 SSRF 規則擋下的錯誤代碼（HTTP 400 的 ``detail.code``；前端顯示 ``detail.message``）。
SSRF_BLOCKED = "ssrf_blocked"
logger = logging.getLogger(__name__)


async def save_bound_connection(store, pm, updates: dict[str, Any], *, identity_keys: frozenset[str], make_token):
    """Stop an old account before publishing its replacement token and creds."""
    stopped = False

    async def stop_old_child():
        nonlocal stopped
        if pm.is_running:
            stopped = True
            await pm.stop()

    try:
        return await store.patch_connection_bound(
            updates, identity_keys=identity_keys, make_token=make_token,
            before_rotate=stop_old_child)
    except BaseException:
        # A failed durable write leaves the old token/account intact. Restore
        # the old child if it was stopped before that failed write.
        if stopped:
            try:
                await pm.start()
            except Exception:
                logger.warning("Could not restore MCP child after failed connection save")
        raise


def connection_outcome(requested: bool, applied: bool, *, started: bool = False) -> dict:
    failed = requested and not applied
    if failed:
        message = 'Connection saved, but MCP failed to start or restart. 設定已存檔，但 MCP 啟動失敗或重啟失敗；請查看 Settings。'
    elif applied:
        message = ('Connection saved; MCP server started (原本未執行，已啟動).' if started
                   else 'Connection saved; MCP server restarted (已重啟).')
    else:
        message = 'Connection saved; process application was not requested.'
    return {'status': 'partial' if failed else 'ok', 'saved': True,
            'apply_state': 'failed' if failed else 'applied' if applied else 'not_requested',
            'restarted': applied, 'started': started, 'message': message}


def ssrf_blocked_message(field: str, reason: str) -> str:
    return (f"{field} blocked by SSRF protection: {reason}. The MCP server refuses private, loopback and cloud "
            "metadata addresses and URLs with a username or password; use the backend's public URL. "
            f"({field} 被 SSRF 防護擋下，未存檔：MCP 子行程不連私有網段、本機與雲端 metadata，也不接受帶帳號密碼的網址；"
            "請改用後端的對外網址。)")


async def refuse_blocked_urls(values: Mapping[str, Any], fields: Iterable[str]) -> None:
    """存檔前檢查後端網址欄位：用對齊子行程（上游 n8n-mcp）規則的 ``util/ssrf.check_url``，被擋就拋 400、不存檔。

    空字串＝有意清空，不檢查；沒有出現在 *values* 的欄位（沿用存檔值）也不檢查。``detail`` 是
    ``{"code": "ssrf_blocked", "field", "reason", "message"}``，前端（``apiFetch``）顯示 ``message``。
    """
    for field in sorted(set(fields)):
        url = values.get(field)
        if not isinstance(url, str) or not url.strip():
            continue
        try:
            result = await ssrf.check_url(url)
        except Exception:  # noqa: BLE001  檢查本身出錯（怪到解析器處理不了的網址）也當成擋下：回 400、不回 500
            result = ssrf.SsrfCheck(False, "Invalid URL format")
        if not result.ok:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail={
                "code": SSRF_BLOCKED, "field": field, "reason": result.reason,
                "message": ssrf_blocked_message(field, result.reason),
            })
