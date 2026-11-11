"""#1530：存檔／測試連線用的 SSRF 檢查（擋／放決定對齊 n8n-mcp 2.96.2 utils/ssrf-protection.js 的三種模式）。"""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from mcp_admin_core.util import ssrf


@pytest.mark.parametrize(
    "address",
    ["10.0.0.5", "172.16.3.4", "172.31.255.255", "192.168.1.1", "127.0.0.1", "169.254.169.254",
     "169.254.170.2", "100.64.0.1", "100.100.100.200", "0.0.0.0", "192.0.0.192", "224.0.0.251",
     "240.0.0.1", "10.43.0.1", "::1", "::ffff:10.0.0.1", "::ffff:8.8.8.8", "fe80::1", "fd00::1",
     "ff02::1", "64:ff9b::a00:1", "2002:a00:1::1"],
)
def test_blocked_addresses(address):
    assert ssrf.address_block_reason(address) is not None


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "172.32.0.1", "100.128.0.1", "2606:4700::1111"])
def test_public_addresses(address):
    assert ssrf.address_block_reason(address) is None


@pytest.mark.parametrize(
    "url",
    ["ftp://example.com", "file:///etc/passwd", "http://user:pw@example.com", "http://localhost:5678",
     "http://metadata.google.internal/", "http://169.254.169.254/latest", "http://10.0.0.5:5678",
     "http://[::1]:5678", "", "not a url"],
)
async def test_check_url_rejects_without_dns(url, monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECURITY_MODE", raising=False)

    async def boom(*a, **kw):  # 語法層就該擋下，不需要 DNS
        raise AssertionError("DNS should not be queried")

    monkeypatch.setattr("asyncio.base_events.BaseEventLoop.getaddrinfo", boom)
    assert (await ssrf.check_url(url)).ok is False


async def test_check_url_resolves_and_blocks_private(monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECURITY_MODE", raising=False)

    async def fake_getaddrinfo(self, host, port, **kw):
        table = {"n8n.internal.example": "10.43.12.7", "n8n.example.com": "203.0.113.10"}
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port))]

    monkeypatch.setattr("asyncio.base_events.BaseEventLoop.getaddrinfo", fake_getaddrinfo)
    blocked = await ssrf.check_url("http://n8n.internal.example:5678")
    assert blocked.ok is False and "blocked address" in blocked.reason
    ok = await ssrf.check_url("https://n8n.example.com")
    assert ok.ok is True and ok.addresses == ["203.0.113.10"]


async def test_check_url_dns_failure(monkeypatch):
    async def fail(self, host, port, **kw):
        raise socket.gaierror("nope")

    monkeypatch.setattr("asyncio.base_events.BaseEventLoop.getaddrinfo", fail)
    res = await ssrf.check_url("https://does-not-exist.example")
    assert res.ok is False and res.reason == "DNS resolution failed"


def test_mode_follows_container_env(monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECURITY_MODE", raising=False)
    assert ssrf.current_mode() == "strict"
    monkeypatch.setenv("WEBHOOK_SECURITY_MODE", "PERMISSIVE")
    assert ssrf.current_mode() == "permissive"
    monkeypatch.setenv("WEBHOOK_SECURITY_MODE", "bogus")
    assert ssrf.current_mode() == "strict"


def test_moderate_and_permissive_modes():
    assert ssrf.address_block_reason("127.0.0.1", "moderate") is None
    assert ssrf.address_block_reason("10.0.0.1", "moderate") is not None
    assert ssrf.address_block_reason("10.0.0.1", "permissive") is None
    # metadata 在任何模式都擋（與上游一致）
    assert ssrf.address_block_reason("169.254.169.254", "permissive") is not None
    assert ssrf.check_url_syntax("http://localhost:5678", "moderate").ok is True
    assert ssrf.check_url_syntax("http://metadata.google.internal", "permissive").ok is False


# ---- 壞 port（第二輪審查 F2：主機名＋壞 port 在 check_url 拋 ValueError，存檔變 HTTP 500；IP＋壞 port 反而放行）----


@pytest.mark.parametrize("url", [
    "https://example.com:99999", "https://example.com:abc", "http://n8n.example.com:5678x/",
    "https://203.0.113.10:99999", "https://203.0.113.10:abc/", "http://[2606:4700::1111]:70000/",
])
async def test_malformed_ports_are_invalid_urls_not_exceptions(url, monkeypatch):
    async def no_dns(*a, **kw):  # 上游 new URL() 在解析 DNS 之前就拒絕
        raise AssertionError("DNS should not be queried")

    monkeypatch.setattr("asyncio.base_events.BaseEventLoop.getaddrinfo", no_dns)
    assert ssrf.check_url_syntax(url).reason == "Invalid URL format"
    for mode in ssrf.MODES:
        result = await ssrf.check_url(url, mode)
        assert (result.ok, result.reason) == (False, "Invalid URL format"), mode


async def test_resolver_value_errors_are_reported_not_raised(monkeypatch):
    async def bad(self, host, port, **kw):
        raise ValueError("embedded null character")

    monkeypatch.setattr("asyncio.base_events.BaseEventLoop.getaddrinfo", bad)
    result = await ssrf.check_url("https://n8n.example.com")
    assert (result.ok, result.reason) == (False, "DNS resolution failed")


# ---- 擋／放決定對齊上游 n8n-mcp 2.96.2（第二輪審查 F2：permissive 放行 IPv6 metadata 與非標準 NAT64）----------

UPSTREAM_FIXTURE = Path(__file__).parent / "fixtures" / "ssrf_upstream_n8n_mcp_2.96.2.json"
UPSTREAM_DECISIONS = json.loads(UPSTREAM_FIXTURE.read_text())["decisions"]


@pytest.fixture
def localhost_only_dns(monkeypatch):
    """判定表只用 IP 字面值與 localhost／metadata 名稱：只有 localhost 會查 DNS（與 /etc/hosts 相同的結果）。"""
    async def fake_getaddrinfo(self, host, port, **kw):
        if host != "localhost":
            raise AssertionError(f"unexpected DNS lookup for {host!r}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0))]

    monkeypatch.setattr("asyncio.base_events.BaseEventLoop.getaddrinfo", fake_getaddrinfo)


@pytest.mark.parametrize("mode", ssrf.MODES)
async def test_decisions_match_the_upstream_table(localhost_only_dns, mode):
    """上游 validateWebhookUrl 對 61 個網址、三種模式的判定（fixtures/ssrf_upstream_n8n_mcp_2.96.2.json，以真的上游
    套件記錄；apps/n8n/tests/test_ssrf_upstream_parity.py 在有套件時直接重跑上游比對）。"""
    wrong = {}
    for url, decisions in UPSTREAM_DECISIONS.items():
        result = await ssrf.check_url(url, mode)
        if result.ok is not decisions[mode]:
            wrong[url] = {"upstream": decisions[mode], "ours": result.ok, "reason": result.reason}
    assert wrong == {}
    assert len(UPSTREAM_DECISIONS) >= 60


@pytest.mark.parametrize("address", ["fd00:ec2::254", "fd20:ce::254", "fe80::a9fe:a9fe", "fd00:a9fe:a9fe::1",
                                     "fd00:0ec2:0000::254", "FE80::A9FE:A9FE"])
def test_ipv6_metadata_is_blocked_in_every_mode(address):
    for mode in ssrf.MODES:
        assert ssrf.address_block_reason(address, mode) == "Cloud metadata endpoint blocked", mode
        assert ssrf.check_url_syntax(f"http://[{address}]/", mode).ok is False, mode


@pytest.mark.parametrize("address", ["::169.254.169.254", "::ffff:0:169.254.169.254", "64:ff9b::a9fe:a9fe",
                                     "2002:a9fe:a9fe::1", "2001:0:4136:e378:8000:63bf:5601:5601"])
def test_metadata_inside_any_ipv6_tunnel_is_blocked_in_every_mode(address):
    for mode in ssrf.MODES:
        assert ssrf.address_block_reason(address, mode) == "Cloud metadata endpoint blocked", mode


@pytest.mark.parametrize("address", ["64:ff9b:1:2::7f00:1", "64:ff9b:1:2::808:808", "64:ff9b:0:0:1::808:808"])
def test_non_canonical_nat64_is_blocked_in_every_mode(address):
    for mode in ssrf.MODES:
        assert ssrf.address_block_reason(address, mode) == "IPv6 private/mapped address not allowed", mode


def test_loopback_follows_upstream_in_moderate_mode():
    # 上游的 localhost＝主機名 localhost／0.0.0.0…、IPv4 127/8、::1；內嵌在 IPv6 裡的 127.x 不算，moderate 也擋
    assert ssrf.address_block_reason("::1", "moderate") is None
    assert ssrf.address_block_reason("127.9.9.9", "moderate") is None
    assert ssrf.address_block_reason("0.0.0.0", "moderate", host="0.0.0.0") is None
    assert ssrf.address_block_reason("0.0.0.0", "moderate", host="n8n.example.com") is not None
    assert ssrf.address_block_reason("::ffff:127.0.0.1", "moderate") == "IPv6 private/mapped address not allowed"
    assert ssrf.address_block_reason("64:ff9b:1::7f00:1", "moderate") == "IPv6 private/mapped address not allowed"
