"""後台「測試連線」／健康探測用的 SSRF 檢查（woow_paas_platform#1530）。

規則對齊 n8n-mcp 2.96.2 ``dist/utils/ssrf-protection.js``（``SSRFProtection.validateWebhookUrl``／
``validateResolvedAddress``，``WEBHOOK_SECURITY_MODE`` 預設 strict）：子行程連後端前就是用這套規則擋，管理頁的
存檔與測試連線若不比照，會出現「存檔／測試連線通過、工具全部被 SSRF 擋下」的矛盾結果，也讓管理 API 本身變成
探測叢集內網的跳板。這是 Python 的重寫、不是同一份程式：擋／放的決定與上游相同（``tests/test_ssrf.py`` 逐案比對），
訊息文字不一定逐字相同；另外比上游多擋兩種（帶帳密的網址、``*.localhost``），見下。

- 只允許 http／https；URL 不得帶 userinfo（帳號密碼）。上游 instance-context 路徑的 ``validateUrlSync`` 同樣拒絕；
  env 路徑（``N8N_API_URL``）的 n8n-api-client 則是把帳密拿掉後照連（帳密從不送出、只檢查主機）。管理端在存檔
  （``connection_apply.refuse_blocked_urls``）與測試連線都拒絕（:func:`has_userinfo`），新的設定存不進來；舊設定裡
  還留著的，n8n launcher 拿掉帳密再交給子行程並印一行提醒（不讓既有服務的管理工具消失；也不能原樣交給上游——上游的
  health check 與錯誤訊息會帶出原始網址）。
- 主機名（含 DNS 解析出的每個位址）不得是雲端 metadata、loopback、私有／保留網段（IPv4：
  10/8、172.16/12、192.168/16、169.254/16、127/8、0/8、100.64/10（CGNAT）、192.0.0/24、
  224/4、240/4；IPv6：``::`` 開頭、IPv4-mapped、fe80::/10、fec0::/10、fc00::/7、ff00::/8，
  以及 IPv4-compatible／mapped／translated、NAT64（64:ff9b::/96、64:ff9b:1::/48）、6to4、Teredo 內嵌的 IPv4
  再判一次）。
- **所有模式都擋**（permissive 也一樣，同上游 ``isCloudMetadata``＋``tunneledIPv6BlockReason``）：雲端 metadata
  （IPv4 與 IPv6：AWS ``fd00:ec2::254``、``fd20:ce::254``、``fe80::a9fe:a9fe``、``fd00:a9fe:a9fe::1``，以標準寫法
  比對）、隧道內嵌的 IPv4 metadata，以及中間不為 0 的非標準 NAT64（``64:ff9b:…``）。
- port 不是數字或超出 0–65535 的網址一律「Invalid URL format」（上游 ``new URL()`` 直接拒絕；過去 Python 這裡會拋
  ``ValueError``，存檔變成 HTTP 500）。
- 模式跟子行程一樣讀容器環境變數 ``WEBHOOK_SECURITY_MODE``（預設 strict；moderate 放行 localhost、
  permissive 只擋 metadata）。這個變數只能由部署端（chart extraEnv）設定，租戶從管理頁改不了
  （launch_policy 黑名單），所以兩邊永遠同一個模式。

DNS rebinding（檢查後改變解析結果）不在這裡處理：PaaS 的 mcp-server egress NetworkPolicy
已排除叢集與私有網段（woow-paas-charts mcp-server ≥ 0.1.6）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

CLOUD_METADATA = frozenset(
    {
        "169.254.169.254",
        "169.254.170.2",
        "metadata.google.internal",
        "metadata",
        "100.100.100.200",
        "192.0.0.192",
        # 上游 2.96.2 起：IPv6 metadata（以 ipaddress 的標準寫法比對）
        "fd00:ec2::254",
        "fd20:ce::254",
        "fe80::a9fe:a9fe",
        "fd00:a9fe:a9fe::1",
    }
)
LOCALHOST_NAMES = frozenset({"localhost", "localhost.localdomain", "127.0.0.1", "::1", "0.0.0.0"})

_PRIVATE_V4 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "127.0.0.0/8",
        "0.0.0.0/8",
        "100.64.0.0/10",
        "192.0.0.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
_PRIVATE_V6 = tuple(
    ipaddress.ip_network(n)
    for n in ("fe80::/10", "fec0::/10", "fc00::/7", "ff00::/8")
)


@dataclass
class SsrfCheck:
    ok: bool
    reason: str = ""
    addresses: list[str] = field(default_factory=list)


#: 上游 ``tryExtractTunneledIPv4`` 的 ``'non_canonical'``：中間不為 0 的 NAT64 前綴，所有模式都擋。
_NON_CANONICAL = "non_canonical"
_LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
_LOOPBACK_V6 = ipaddress.ip_address("::1")


def _v4(hi: int, lo: int) -> ipaddress.IPv4Address:
    return ipaddress.IPv4Address((hi << 16) | lo)


def _tunneled_ipv4(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | str | None:
    """IPv6 位址裡內嵌的 IPv4（逐項照上游 ``SSRFProtection.tryExtractTunneledIPv4``）。

    ``::a.b.c.d``（compatible）、``::ffff:a.b.c.d``（mapped）、``::ffff:0:a.b.c.d``（translated）、NAT64
    ``64:ff9b::/96``（RFC 6052）與 ``64:ff9b:1::/48`` 中間全 0 的寫法（RFC 8215）、6to4 ``2002::/16``、Teredo
    ``2001:0::/32``（client 位址取反）。``64:ff9b:`` 開頭但中間不為 0 回 :data:`_NON_CANONICAL`。
    """
    p = [int.from_bytes(addr.packed[i:i + 2], "big") for i in range(0, 16, 2)]
    if p[0] == p[1] == p[2] == p[3] == 0 and ((p[4] == 0 and p[5] in (0, 0xFFFF)) or (p[4] == 0xFFFF and p[5] == 0)):
        return _v4(p[6], p[7])
    if p[0] == 0x64 and p[1] == 0xFF9B:
        if p[3] == p[4] == p[5] == 0 and p[2] in (0, 1):
            return _v4(p[6], p[7])
        return _NON_CANONICAL
    if p[0] == 0x2002:
        return _v4(p[1], p[2])
    if p[0] == 0x2001 and p[1] == 0:
        return _v4(p[6] ^ 0xFFFF, p[7] ^ 0xFFFF)
    return None


MODES = ("strict", "moderate", "permissive")


def current_mode() -> str:
    """與 n8n-mcp 相同：``WEBHOOK_SECURITY_MODE``（容器環境變數），未設或不認得時為 strict。"""
    mode = os.environ.get("WEBHOOK_SECURITY_MODE", "strict").strip().lower()
    return mode if mode in MODES else "strict"


def address_block_reason(address: str, mode: str = "strict", *, host: str | None = None) -> str | None:
    """判定單一 IP 位址；回傳封鎖原因，可放行時回 None（判斷順序與上游 ``validateResolvedAddress`` 相同）。

    *host*：網址裡的主機名（小寫、沒有中括號）。上游的 localhost 判斷同時看主機名（``localhost``、``0.0.0.0``…）
    與位址（IPv4 127/8、``::1``）；內嵌在 IPv6 裡的 127.x 不算 localhost，當成 IPv6 私網位址擋（moderate 也擋）。
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return "Invalid IP address"
    tunneled = _tunneled_ipv4(ip) if isinstance(ip, ipaddress.IPv6Address) else None
    # 所有模式（含 permissive）：metadata（IPv4／IPv6、隧道內嵌）與非標準 NAT64
    if str(ip) in CLOUD_METADATA or (isinstance(tunneled, ipaddress.IPv4Address) and str(tunneled) in CLOUD_METADATA):
        return "Cloud metadata endpoint blocked"
    if tunneled == _NON_CANONICAL:
        return "IPv6 private/mapped address not allowed"
    if mode == "permissive":
        return None
    is_localhost = (host in LOCALHOST_NAMES if host else False) or ip == _LOOPBACK_V6 or (
        isinstance(ip, ipaddress.IPv4Address) and ip in _LOOPBACK_V4)
    if is_localhost:
        return None if mode == "moderate" else "Localhost access is blocked"
    if isinstance(ip, ipaddress.IPv4Address):
        return "Private IP addresses not allowed" if any(ip in net for net in _PRIVATE_V4) else None
    if isinstance(tunneled, ipaddress.IPv4Address) and any(tunneled in net for net in _PRIVATE_V4):
        return "IPv6 private/mapped address not allowed"
    if str(ip).startswith("::") or any(ip in net for net in _PRIVATE_V6):
        return "IPv6 private address not allowed"
    return None


def has_userinfo(url: str) -> bool:
    """URL 是否帶帳號或密碼（``scheme://user:pw@host``）；解析不了的字串回 False（交給其他檢查）。"""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return bool(parts.username or parts.password)


def check_url_syntax(url: str, mode: str | None = None) -> SsrfCheck:
    """不解析 DNS 的檢查（scheme、userinfo、metadata／localhost 名稱、IP 字面值）。"""
    mode = mode or current_mode()
    if not isinstance(url, str) or not url.strip():
        return SsrfCheck(False, "URL is empty")
    try:
        parts = urlsplit(url.strip())
        parts.port  # 非數字或超出 0–65535 的 port 會拋 ValueError；上游 new URL() 也拒絕
    except ValueError:
        return SsrfCheck(False, "Invalid URL format")
    if parts.scheme not in ("http", "https"):
        return SsrfCheck(False, "Invalid protocol. Only HTTP/HTTPS allowed.")
    if has_userinfo(url):
        return SsrfCheck(False, "Userinfo in URL is not allowed")
    host = (parts.hostname or "").lower()
    if not host:
        return SsrfCheck(False, "Invalid URL format")
    if host in CLOUD_METADATA:
        return SsrfCheck(False, "Cloud metadata endpoint blocked")
    if mode == "strict" and (host in LOCALHOST_NAMES or host.endswith(".localhost")):
        return SsrfCheck(False, "Localhost access is blocked")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return SsrfCheck(True)
    reason = address_block_reason(host, mode, host=host)
    return SsrfCheck(reason is None, reason or "", [host] if reason is None else [])


async def check_url(url: str, mode: str | None = None) -> SsrfCheck:
    """完整檢查：語法＋DNS 解析後的每一個位址都必須是公開位址（依模式放寬）。"""
    mode = mode or current_mode()
    first = check_url_syntax(url, mode)
    if not first.ok or first.addresses:
        return first
    parts = urlsplit(url.strip())
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError):
        return SsrfCheck(False, "DNS resolution failed")
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        return SsrfCheck(False, "DNS resolution failed")
    for address in addresses:
        reason = address_block_reason(address, mode, host=host.lower())
        if reason is not None:
            if "Localhost" in reason:
                return SsrfCheck(False, "Localhost access is blocked")
            return SsrfCheck(False, f"Hostname resolves to a blocked address ({reason})")
    return SsrfCheck(True, "", addresses)
