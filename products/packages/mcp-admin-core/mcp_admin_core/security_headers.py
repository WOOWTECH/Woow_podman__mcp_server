"""管理後台安全標頭 middleware（#1372）。

管理頁存放 MCP 存取 token 與連線帳密，必須禁止被第三方頁面嵌入（clickjacking），
並關閉 MIME sniffing、限制 Referer 外洩。

實作為**純 ASGI middleware**（不用 ``BaseHTTPMiddleware``）：只在
``http.response.start`` 訊息補標頭，body 原樣逐塊放行，不緩衝串流回應。

``/private_*`` 為 MCP reverse proxy（SSE／Streamable HTTP），回應標頭原樣透傳
子行程的結果，刻意**不套用**——避免干擾 MCP client 與串流行為。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, MutableMapping

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

SECURITY_HEADERS: dict[str, str] = {
    # 180 天、不帶 includeSubDomains／preload（那是 Cloudflare zone 層的決定，與平台頁面一致）；
    # 走 HTTP 的內部連線瀏覽器會忽略這個標頭。
    "Strict-Transport-Security": "max-age=15552000",
    "Permissions-Policy": "geolocation=(), payment=(), usb=()",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}

# MCP proxy 路徑前綴（見 proxy.py：/private_{token} 與 /private_{token}/{path}）
_EXCLUDED_PREFIX = "/private_"


class SecurityHeadersMiddleware:
    """對非 MCP proxy 的 HTTP 回應補上安全標頭（已存在的同名標頭不覆寫）。"""

    def __init__(self, app: ASGIApp, headers: dict[str, str] | None = None) -> None:
        self.app = app
        self._headers = [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in (headers or SECURITY_HEADERS).items()
        ]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "").startswith(_EXCLUDED_PREFIX):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                existing = {k.lower() for k, _ in raw}
                for k, v in self._headers:
                    if k not in existing:
                        raw.append((k, v))
                message["headers"] = raw
            await send(message)

        await self.app(scope, receive, send_with_headers)
