"""管理頁與文件裡的 n8n API URL 範例，必須是預設 strict SSRF 規則下存得進去的網址（第二輪審查 F2）。

存檔時就套 SSRF 規則之後（connection_apply.refuse_blocked_urls），照著 placeholder 或 README 範例填
``http://localhost:5678``／``http://n8n:5678`` 會直接 400 ssrf_blocked；範例一律用對外網址，欄位說明要寫出會被拒的網址。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from mcp_admin_core.util import ssrf

APP_DIR = Path(__file__).resolve().parents[1]
DOCS = [APP_DIR / "README.md", APP_DIR / "README_zh-TW.md", APP_DIR / "docs" / "architecture.md"]


def _strict_save_would_accept(url: str) -> bool:
    """不查 DNS 的近似：語法層通過 strict，而且主機像對外網域（有點、不是叢集內或 .local 名稱）。
    ``http://n8n:5678`` 這種叢集內名稱語法層擋不下（要解析 DNS 才知道是私網），存檔時會在 DNS 那一步被拒。"""
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or ""
    internal = host.endswith((".svc", ".local", ".internal", ".lan")) or ".svc." in host
    return ssrf.check_url_syntax(url, "strict").ok and "." in host and not internal


def _url_field() -> str:
    src = (APP_DIR / "frontend" / "src" / "appConfig.jsx").read_text()
    return next(line for line in src.splitlines() if "name: 'n8n_api_url'" in line)


def _doc_examples() -> list[tuple[str, str]]:
    found = []
    for doc in DOCS:
        text = doc.read_text()
        for url in re.findall(r'"(?:n8n_api_url|N8N_API_URL)":\s*"([^"]+)"', text):
            found.append((doc.name, url))
        for url in re.findall(r"\*\*n8n API URL\*\*[^\n]*?\(e\.g\., `([^`]+)`|n8n API URL\*\* --[^\n]*?（例如 `([^`]+)`", text):
            found.extend((doc.name, u) for u in url if u)
    return found


def test_placeholder_is_a_public_url_and_the_field_explains_what_is_refused():
    line = _url_field()
    placeholder = re.search(r"placeholder: '([^']+)'", line).group(1)
    assert _strict_save_would_accept(placeholder), placeholder
    help_text = re.search(r"help: '([^']+)'", line).group(1)
    for words in ("對外網址", "私網", "本機", "metadata", "帳號密碼", "400"):
        assert words in help_text, words


@pytest.mark.parametrize(("doc", "url"), _doc_examples())
def test_documented_examples_pass_the_strict_save_check(doc, url):
    assert _strict_save_would_accept(url), (doc, url)


@pytest.mark.parametrize("url", ["http://localhost:5678", "http://n8n:5678", "http://n8n.n8n.svc.cluster.local:5678",
                                 "http://10.43.0.5:5678", "https://user:pw@n8n.example.com"])
def test_the_approximation_rejects_the_old_examples(url):
    assert not _strict_save_would_accept(url)


def test_every_doc_has_at_least_one_example():
    docs = {doc for doc, _ in _doc_examples()}
    assert docs == {"README.md", "README_zh-TW.md", "architecture.md"}
