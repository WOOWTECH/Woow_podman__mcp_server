"""MCP 表面：vendored 子行程（in-process FastMCP client）對 mock Nextcloud 真的提供 9 支工具。

政策 → env 走 launcher 的同一段程式（``launcher.policy_env``），所以這裡同時證明「新安裝政策（2026-10-25 起全開）→
子行程 9 支」「停用 4 支寫入／刪除（唯讀政策，舊服務的出廠政策）→ 只有 5 支讀取工具」，以及工具呼叫確實打到
Nextcloud（WebDAV／CalDAV）。
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastmcp import Client

from nextcloud_mcp_admin import launcher, tool_registry
from nextcloud_mcp_server.server import create_server
from nextcloud_mcp_server.settings import load_settings

from .fake_nextcloud import BASE_URL, LOGIN, PASSWORD, vtodo

ALL = tool_registry.get_all_tool_names()
READ = tool_registry.read_only_tool_names()


def _server_for_policy(monkeypatch, tmp_path, disabled, fake):
    """模擬 McpProcessManager＋launcher：connection env（core 注入）＋政策 env（launcher 算）→ vendored server。"""
    cfg = tmp_path / "policy.json"
    cfg.write_text(json.dumps({"tools": {"disabled": disabled}}))
    monkeypatch.setenv("NEXTCLOUD_MCP_BASE_URL", BASE_URL)
    monkeypatch.setenv("NEXTCLOUD_MCP_USERNAME", LOGIN)
    monkeypatch.setenv("NEXTCLOUD_MCP_APP_PASSWORD", PASSWORD)
    for key, value in launcher.policy_env(cfg).items():
        monkeypatch.setenv(key, value)
    return create_server(load_settings(), transport=fake.transport)


async def _names(server) -> list[str]:
    async with Client(server) as client:
        return [t.name for t in await client.list_tools()]


async def test_all_nine_tools_when_policy_allows(monkeypatch, tmp_path, fake):
    server = _server_for_policy(monkeypatch, tmp_path, [], fake)
    assert await _names(server) == ALL
    assert fake.requests == []  # 列工具不碰 Nextcloud（子行程可以先於後端啟動）


async def test_fresh_install_policy_has_all_nine_tools(monkeypatch, tmp_path, fake):
    """新安裝的停用清單（default_disabled_tools，負責人 2026-10-25「預設全開」）：9 支全部註冊。"""
    assert tool_registry.default_disabled_tools() == []
    server = _server_for_policy(monkeypatch, tmp_path, tool_registry.default_disabled_tools(), fake)
    assert await _names(server) == ALL


async def test_read_only_policy_only_read_tools(monkeypatch, tmp_path, fake):
    server = _server_for_policy(monkeypatch, tmp_path, tool_registry.write_and_dangerous_tool_names(), fake)
    assert await _names(server) == READ


async def test_tool_annotations(monkeypatch, tmp_path, fake):
    async with Client(_server_for_policy(monkeypatch, tmp_path, [], fake)) as client:
        tools = {t.name: t for t in await client.list_tools()}
    for name in READ:
        assert tools[name].annotations.read_only_hint is True
    assert tools["delete_file_checked"].annotations.destructive_hint is True
    for name in tools:
        assert tools[name].input_schema["additionalProperties"] is False


async def test_read_tools_hit_nextcloud(monkeypatch, tmp_path, fake):
    fake.calendars["personal"].objects["t1.ics"] = vtodo("t1", "Write report", "STATUS:NEEDS-ACTION")
    server = _server_for_policy(monkeypatch, tmp_path, tool_registry.write_and_dangerous_tool_names(), fake)
    async with Client(server) as client:
        tree = (await client.call_tool("get_file_tree", {"path": "", "depth": 1})).structured_content
        assert [e["name"] for e in tree["entries"]] == ["Docs", "Empty", "photo.png"]
        text = await client.call_tool("read_text_file", {"path": "Docs/a #1.md"})
        assert text.structured_content["content"] == "hash name"
        plain = await client.call_tool("get_file_content", {"path": "Docs/readme.md"})
        assert plain.content[0].text == "# Hello\n"
        cals = (await client.call_tool("list_calendars", {})).structured_content["calendars"]
        assert [c["id"] for c in cals] == ["personal"]
        tasks = (await client.call_tool("list_tasks", {})).structured_content["tasks"]
        assert [t["summary"] for t in tasks] == ["Write report"]
    # 每個請求都帶 HTTP Basic（app password 只在 header，不在 URL）
    for request in fake.requests:
        assert request.headers["Authorization"].startswith("Basic ")
        assert PASSWORD not in str(request.url)


async def test_gated_write_tool_is_absent_and_refused(monkeypatch, tmp_path, fake):
    server = _server_for_policy(monkeypatch, tmp_path, tool_registry.write_and_dangerous_tool_names(), fake)
    async with Client(server) as client:
        result = await client.call_tool_mcp("create_text_file", {"path": "new.txt", "content": "x"})
    assert result.is_error is True
    assert "new.txt" not in fake.files


async def test_write_and_delete_flow_when_enabled(monkeypatch, tmp_path, fake):
    server = _server_for_policy(monkeypatch, tmp_path, [], fake)
    async with Client(server) as client:
        created = (await client.call_tool("create_text_file", {"path": "Docs/new.md", "content": "v1"})).structured_content
        assert created["status"] == "created"
        updated = (await client.call_tool("update_text_file", {
            "path": "Docs/new.md", "content": "v2", "expected_etag": created["etag"]})).structured_content
        assert updated["status"] == "updated" and fake.files["Docs/new.md"].data == b"v2"
        stale = await client.call_tool_mcp("update_text_file", {
            "path": "Docs/new.md", "content": "v3", "expected_etag": created["etag"]})
        assert stale.is_error is True and fake.files["Docs/new.md"].data == b"v2"
        uploaded = (await client.call_tool("upload_file", {
            "path": "Docs/blob.bin", "content_base64": base64.b64encode(b"\x00\x01").decode()})).structured_content
        assert uploaded["status"] == "created"
        deleted = (await client.call_tool("delete_file_checked", {
            "path": "Docs/new.md", "expected_etag": updated["etag"]})).structured_content
        assert deleted["status"] == "deleted" and "Docs/new.md" not in fake.files


def test_a_content_coding_suffix_is_not_part_of_an_etag():
    """Apache mod_deflate 對壓縮過的 GET 回 "<etag>-gzip"（mod_brotli 是 "-br"）：檔案自己的 etag 是前面那段。"""
    from nextcloud_mcp_server.paths import caller_etag, normalize_etag

    for raw, plain in (
        ('"abc123-gzip"', "abc123"), ('W/"abc123-gzip"', "abc123"), ('"abc123-br"', "abc123"), ("abc123-gzip", "abc123"),
        ('"abc123-deflate"', "abc123"), ('"abc123-zstd"', "abc123"),
        ('"abc123"', "abc123"), ('W/"abc123"', "abc123"), ('"gzip"', "gzip"), ('"x-gzipped"', "x-gzipped"),
        ('"-gzip"', "-gzip"), ('"6b520502fdd60229b4a10a23dd1e910a-gzip"', "6b520502fdd60229b4a10a23dd1e910a"),
    ):
        assert normalize_etag(raw) == plain, raw
        assert caller_etag(raw) == plain, raw
    assert normalize_etag(None) is None and normalize_etag('""') is None and normalize_etag("  ") is None


async def test_the_etag_a_compressed_read_returns_works_for_the_conditional_writes(monkeypatch, tmp_path, fake):
    """較大的檔案被 GET 時 ETag 帶 "-gzip"（run E 在 300 KB 與 200 KB 的檔案上量到）：read_text_file 交出去的要是檔案自己的 etag，
    拿它做 update／delete 才不會永遠 ETAG_MISMATCH；呼叫端手上舊的、帶後綴的 etag 也照收。"""
    fake.gzip_etag_over = 1000
    big, big2, big3 = (fake.add_file(f"Docs/{name}.txt", b"x" * 5000) for name in ("big", "big2", "big3"))
    small = fake.files["Docs/readme.md"]
    server = _server_for_policy(monkeypatch, tmp_path, [], fake)
    async with Client(server) as client:
        read = (await client.call_tool("read_text_file", {"path": "Docs/big.txt"})).structured_content
        assert read["etag"] == big.etag and read["content"] == "x" * 5000
        assert (await client.call_tool("read_text_file", {"path": "Docs/readme.md"})).structured_content["etag"] == small.etag
        updated = (await client.call_tool("update_text_file", {
            "path": "Docs/big.txt", "content": "y" * 3000, "expected_etag": read["etag"]})).structured_content
        assert updated["status"] == "updated" and updated["previous_etag"] == big.etag and fake.files["Docs/big.txt"].data == b"y" * 3000
        read2 = (await client.call_tool("read_text_file", {"path": "Docs/big2.txt"})).structured_content
        deleted = (await client.call_tool("delete_file_checked", {
            "path": "Docs/big2.txt", "expected_etag": read2["etag"]})).structured_content
        assert deleted["status"] == "deleted" and "Docs/big2.txt" not in fake.files
        deleted = (await client.call_tool("delete_file_checked", {
            "path": "Docs/big3.txt", "expected_etag": f'"{big3.etag}-gzip"'})).structured_content
        assert deleted["status"] == "deleted" and "Docs/big3.txt" not in fake.files


@pytest.mark.parametrize(
    ("disabled", "absent"),
    [(["delete_file_checked"], ["delete_file_checked"]),
     (["upload_file", "list_tasks"], ["upload_file", "list_tasks"])],
)
async def test_partial_policies(monkeypatch, tmp_path, fake, disabled, absent):
    names = await _names(_server_for_policy(monkeypatch, tmp_path, disabled, fake))
    assert names == [n for n in ALL if n not in absent]


async def test_tool_errors_never_contain_the_password(monkeypatch, tmp_path, fake):
    fake.override = lambda request: httpx.Response(401)
    async with Client(_server_for_policy(monkeypatch, tmp_path, [], fake)) as client:
        result = await client.call_tool_mcp("get_file_tree", {"path": ""})
    assert result.is_error is True
    assert PASSWORD not in json.dumps(result.model_dump(mode="json"))
