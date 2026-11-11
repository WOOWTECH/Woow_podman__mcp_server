"""管理頁與文件指引的 MCP 連線網址要是 Streamable HTTP ``/private_<token>/mcp``（DATA-DOC-002）。

Token 頁原本寫 ``MCP proxy URL: /private_{token}/sse``，跟同頁的 ``claude mcp add --transport http
… /mcp`` 指令互相矛盾；Hermes／n8n 文件與 seed 文件也只指向 ``/sse``。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def test_token_manager_shows_streamable_http_url():
    """Tokens 頁直接顯示完整網址（第三輪 G4）：TokenManager 用 mcpUrl.js 組 ``/private_<token>/mcp``，沒有 ``/sse``。"""
    src = (ROOT / "packages/mcp-admin-ui/src/pages/TokenManager.jsx").read_text(encoding="utf-8")
    helper = (ROOT / "packages/mcp-admin-ui/src/mcpUrl.js").read_text(encoding="utf-8")
    assert "/sse" not in src and "/sse" not in helper
    assert "from '../mcpUrl'" in src and "mcpUrl(origin" in src
    assert "/private_${token || TOKEN_PLACEHOLDER}/mcp" in helper


@pytest.mark.parametrize(
    "doc",
    [
        "apps/hermes/README.md",
        "apps/hermes/README.zh-TW.md",
        "apps/hermes/docs/cloud-service-seed.md",
        "apps/n8n/README.md",
        "apps/n8n/README_zh-TW.md",
    ],
)
def test_docs_point_clients_to_mcp_endpoint(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    assert "/private_{token}/mcp" in text or "/private_{your-token}/mcp" in text
    # client 設定範例不可再指向 legacy /sse
    assert "private_your-token-here/sse" not in text
    assert "private_{your-token}/sse" not in text
    assert '"transport": "sse"' not in text
