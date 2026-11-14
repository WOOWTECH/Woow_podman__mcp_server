"""子行程轉址的 ``Location`` 改寫以「用戶端實際送出的路徑」為基準（#145 審查的非阻擋建議；P3-c 的延伸）。

P3-c 把子行程回的 ``Location: http://127.0.0.1:<port>/…`` 改成相對參照，但基準取的是轉給子行程的路徑。裸網址
``/private_<token>``（沒有尾端路徑）轉給子行程的是根路徑 ``/``，算出來的參照（例如 ``mcp``）卻交給一個位於
``/private_<token>``（沒有斜線，最後一段是 token 那一段）的用戶端解析，結果是 ``/mcp``：跳出 ``/private_<token>/``
前綴。目前沒有子程式會對根路徑轉址，所以是防禦性的修正。尾斜線的情況也一樣：``/private_<token>/mcp/`` 轉給子行程的是
``/mcp``，用戶端卻站在 ``/private_<token>/mcp/`` 底下。

現在基準就是用戶端實際送出的路徑（``/private_<token>`` 底下的位置完全對應子行程的根）：目標路徑先在子行程的路徑空間
消掉 ``.``／``..``（爬不出根），再接上 ``/private_<token>``，最後算出從用戶端路徑出發的相對參照。有尾端路徑時
``private_<token>`` 一定是共同的前綴，所以參照裡沒有 token；裸網址沒有尾端路徑，要留在前綴內就一定得寫出那一段，
這是 Location 唯一會帶 token 的情況（只回給送出這個 token 的同一個用戶端，legacy SSE 的 endpoint 事件也是同樣的做法）。
"""
from __future__ import annotations

import httpx
import pytest

TOKEN = "bare-url-location-test-token-0000000"  # 合成值
PREFIX = f"private_{TOKEN}"

pytestmark = pytest.mark.usefixtures("synthetic_child")


def _gateway(make_app, monkeypatch, location: str):
    from mcp_admin_core import proxy

    seen: list[str] = []

    def child(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(307, headers={"location": location})

    monkeypatch.setattr(proxy, "_get_client",
                        lambda timeout=86400: httpx.AsyncClient(transport=httpx.MockTransport(child)))
    client = make_app({"mcp_auth_token": TOKEN, "mcp_server": {"command": "", "args": [], "port": 8123, "env": {}}})
    return client, seen


@pytest.mark.parametrize(("child_location", "expected"), [
    ("http://127.0.0.1:8123/mcp", f"{PREFIX}/mcp"),
    ("http://localhost:8123/mcp?x=1#frag", f"{PREFIX}/mcp?x=1#frag"),
    ("http://[::1]:8123/", f"{PREFIX}/"),
    ("/mcp", f"{PREFIX}/mcp"),
    ("/a/b/../c", f"{PREFIX}/a/c"),
    ("/../../outside", f"{PREFIX}/outside"),  # 子行程的根以上沒有東西：爬不出前綴
    ("sse/", f"{PREFIX}/sse/"),  # 相對於子行程根路徑的參照：同樣要放回前綴底下
    ("../../x", f"{PREFIX}/x"),
    ("https://example.com/elsewhere", "https://example.com/elsewhere"),  # 其他主機原樣
    ("?retry=1", "?retry=1"),  # 只有 query：指向同一個文件，留在原處
])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_bare_token_url_redirect_stays_inside_the_prefix(make_app, monkeypatch, method, child_location, expected):
    client, seen = _gateway(make_app, monkeypatch, child_location)
    response = client.request(method, f"/private_{TOKEN}", follow_redirects=False)
    assert response.status_code == 307
    assert seen == ["/"]  # 裸網址轉給子行程的是根路徑
    location = response.headers["location"]
    assert location == expected
    assert "127.0.0.1" not in location and "localhost" not in location and "::1" not in location
    resolved = httpx.URL(f"https://tenant.example/private_{TOKEN}").join(location)
    if "example.com" in expected:
        assert resolved.host == "example.com"
    else:
        # 用戶端照 RFC 3986 解析：回到同一個 /private_<token>（沒有斜線的裸網址）或它底下，不會跳到 /mcp 之類的根路徑
        assert resolved.host == "tenant.example"
        assert resolved.path == f"/private_{TOKEN}" or resolved.path.startswith(f"/private_{TOKEN}/")


def test_bare_url_with_trailing_slash_keeps_the_token_out_of_the_location(make_app, monkeypatch):
    """``/private_<token>/``（有斜線）：用戶端站在前綴目錄裡，``mcp`` 解析成 ``/private_<token>/mcp``，不需要寫 token。"""
    client, _ = _gateway(make_app, monkeypatch, "http://127.0.0.1:8123/mcp")
    response = client.get(f"/private_{TOKEN}/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "mcp"
    assert TOKEN not in response.headers["location"]
    assert httpx.URL(f"https://tenant.example/private_{TOKEN}/").join("mcp").path == f"/private_{TOKEN}/mcp"


@pytest.mark.parametrize(("path", "child_location", "expected"), [
    # /mcp/ 轉給子行程的是 /mcp，用戶端卻站在 /private_<token>/mcp/ 底下
    ("mcp/", "http://127.0.0.1:8123/mcp/x", "x"),
    ("mcp/", "/mcp", "../mcp"),
    ("mcp//", "http://127.0.0.1:8123/mcp/y", "../y"),  # /mcp// 也當成 /mcp：用戶端的基準是兩層目錄
])
def test_trailing_slash_requests_resolve_against_what_the_client_sent(make_app, monkeypatch, path, child_location,
                                                                      expected):
    client, seen = _gateway(make_app, monkeypatch, child_location)
    response = client.get(f"/private_{TOKEN}/{path}", follow_redirects=False)
    assert response.status_code == 307 and seen == ["/mcp"]
    location = response.headers["location"]
    assert location == expected and TOKEN not in location
    resolved = httpx.URL(f"https://tenant.example/private_{TOKEN}/{path}").join(location)
    assert resolved.path.startswith(f"/private_{TOKEN}/")


@pytest.mark.parametrize(("path", "child_location", "expected"), [
    # 與 P3-c 的案例一致（test_proxy_trailing_slash.py）：有尾端路徑時結果沒有改變
    ("sse/", "http://127.0.0.1:8123/sse", "../sse"),
    ("messages", "http://127.0.0.1:8123/messages/?session_id=abc", "messages/?session_id=abc"),
    ("a/b/c", "http://127.0.0.1:9999/", "../../"),
    ("x", "/../../outside", "outside"),
    ("sse/", "relative/path", "relative/path"),
])
def test_requests_with_a_path_are_rewritten_exactly_as_before(make_app, monkeypatch, path, child_location, expected):
    client, _ = _gateway(make_app, monkeypatch, child_location)
    response = client.get(f"/private_{TOKEN}/{path}", follow_redirects=False)
    assert response.headers["location"] == expected
