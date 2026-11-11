"""Open Design daemon 前置 basic auth 的共用 helper（#1354）。

PaaS 的 open-design 服務前面有 basic auth。連線改用兩個獨立欄位：
- ``od_basic_user``（非 secret）
- ``od_basic_password``（secret：鍵名含 ``password``，core 遮罩／drop_blank_secrets 皆適用）

舊做法把帳密塞進 URL（``https://user:pass@host``）仍相容：存檔時會把 userinfo 從
``od_api_base`` 拆出、移進上面兩個欄位；尚未重存的舊 URL 在 GET／health 顯示時遮掉密碼。
"""
from __future__ import annotations

from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

# GET 時舊 URL 內密碼的遮罩字樣；PUT 收到此字樣視同「沒填密碼」（沿用既有值）。
URL_PASSWORD_MASK = "****"


def split_userinfo(url: str) -> tuple[str, str | None, str | None]:
    """拆出 URL 的 userinfo → (去掉 userinfo 的 URL, user, password)；無 userinfo 時 user/password 為 None。"""
    if not url or "@" not in url:
        return url, None, None
    try:
        parts = urlsplit(url)
    except ValueError:
        return url, None, None
    if "@" not in parts.netloc:
        return url, None, None
    userinfo, _, hostport = parts.netloc.rpartition("@")
    user, sep, password = userinfo.partition(":")
    clean = urlunsplit(parts._replace(netloc=hostport))
    return clean, unquote(user), (unquote(password) if sep else None)


def redact_url(url: str) -> str:
    """把 URL userinfo 的密碼換成遮罩字樣（供 GET／health 顯示）；無密碼原樣回傳。"""
    clean, user, password = split_userinfo(url)
    if user is None or password is None:
        return url
    parts = urlsplit(clean)
    return urlunsplit(parts._replace(netloc=f"{user}:{URL_PASSWORD_MASK}@{parts.netloc}"))


def resolve_target(conn: dict) -> tuple[str, httpx.BasicAuth | None]:
    """回 (去掉 userinfo 的 daemon URL, BasicAuth 或 None)。

    優先用 od_basic_user／od_basic_password（user 空＝不帶認證）；未設時退回舊 URL userinfo。
    URL 一律去掉 userinfo 再交給 httpx——否則 httpx 錯誤訊息（如 raise_for_status）會把
    ``user:password@`` 原文印出來。
    """
    url = str(conn.get("od_api_base", "") or "")
    clean, url_user, url_password = split_userinfo(url)
    user = str(conn.get("od_basic_user", "") or "")
    if user:
        return clean, httpx.BasicAuth(user, str(conn.get("od_basic_password", "") or ""))
    if url_user:
        return clean, httpx.BasicAuth(url_user, url_password or "")
    return clean, None
