"""子行程的 ``Location`` 含反斜線或 ``%2e`` 時，改寫後的參照不可以讓用戶端離開 ``/private_<token>/``（獨立審查 N7）。

``proxy._external_location`` 把子行程的轉址改寫成「相對於用戶端目前網址」的參照。RFC 3986 的解析器只把字面的 ``.``／``..`` 當點段、
不把 ``\\`` 當路徑分隔；但 WHATWG URL（瀏覽器、fetch／undici）把 ``\\`` 當成 ``/``、把 ``%2e``（不分大小寫）當成 ``.``。兩邊看法
不同時，改寫出來的參照在 WHATWG 用戶端會跑到別的地方：

- ``Location: /\\\\evil.example/x`` 改寫成 ``\\\\evil.example/x``，瀏覽器解析成 ``//evil.example/x``（換主機）；
- ``Location: /%2e%2e/%2e%2e/admin`` 改寫成 ``../%2e%2e/%2e%2e/admin``，瀏覽器解析成 ``/admin``（爬出前綴）；
- ``Location: //127.255.255.254/mcp/\\\\/a\\b/b/b``（用戶端在 ``/private_<token>/mcp/``）改寫成 ``\\\\/a\\b/b/b``，瀏覽器解析成
  ``https://a/b/b/b``。

前提是子行程自己送出含這類字元的 Location（現有的產品不會；外部用戶端沒有辦法誘導），所以只是防禦性強化：輸出前把 ``\\`` 編成
``%5C``，並把 ``%2e`` 的路徑段當成與字面 ``.``／``..`` 相同的點段。寫實的輸入（``.``、``..``、空段、含空白、``%2F``、``k=v``…）
的輸出與修正前逐位元相同——下面的 ``REALISTIC`` 是用修正前（341ece7）的實作產生的。

這個檔案不依賴 Node：``_browser_resolves`` 是自寫的、只含這個測試用得到的 WHATWG 規則的解析器（修正者另用 Node 的 ``new URL`` 對
數萬個隨機案例核對過它的主機與路徑，不進 CI）。測試資料一律合成。
"""
from __future__ import annotations

import re

import httpx
import pytest

TOKEN = "location-hardening-test-token-00000"  # 合成值
PREFIX = f"/private_{TOKEN}"

pytestmark = pytest.mark.usefixtures("synthetic_child")

_C0_AND_SPACE = "".join(chr(code) for code in range(0x21))


def _browser_resolves(base_path: str, reference: str) -> tuple[str, str]:
    """瀏覽器（WHATWG URL）把 *reference* 當成相對於 ``https://閘道<base_path>`` 的參照解析。

    回傳 ``("gateway", 路徑)``；離開閘道（有 scheme、或 ``//主機``）回傳 ``("elsewhere", 參照)``。只實作這個測試用得到的規則：
    去掉開頭結尾的控制字元與空白、去掉 tab 與換行、``\\`` 當成 ``/``、``.``／``%2e`` 與 ``..``／``.%2e``／``%2e.``／``%2e%2e``
    （不分大小寫）是點段。"""
    text = reference.strip(_C0_AND_SPACE).translate({9: None, 10: None, 13: None})
    if re.match(r"[A-Za-z][A-Za-z0-9+.\-]*:", text):
        return "elsewhere", reference
    text = text.replace("\\", "/")
    if text.startswith("//"):
        return "elsewhere", reference
    path = re.split(r"[?#]", text, maxsplit=1)[0]
    if not path:
        merged = base_path
    elif path.startswith("/"):
        merged = path
    else:
        merged = base_path[: base_path.rfind("/") + 1] + path
    output: list[str] = []
    segments = merged.split("/")[1:]
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        lowered = segment.lower()
        if lowered in (".", "%2e"):
            if last:
                output.append("")
        elif lowered in ("..", ".%2e", "%2e.", "%2e%2e"):
            if output:
                output.pop()
            if last:
                output.append("")
        else:
            output.append(segment)
    return "gateway", "/" + "/".join(output)


def _inside_the_prefix(base_path: str, reference: str) -> bool:
    where, path = _browser_resolves(base_path, reference)
    return where == "gateway" and (path == PREFIX or path.startswith(PREFIX + "/"))


def _rewrite(location: str, client_path: str, child_path: str) -> str:
    from mcp_admin_core import proxy

    return proxy._external_location(location, client_path, child_path, PREFIX)


# (Location, 用戶端送出的路徑, 轉給子行程的路徑, 改寫後的參照)
HOSTILE = {
    "two backslashes where a host would start": (r"/\\evil.example/x", f"{PREFIX}/mcp", "/mcp",
                                                  "%5C%5Cevil.example/x"),
    "one backslash where a host would start": (r"/\evil.example/x", f"{PREFIX}/mcp", "/mcp", "%5Cevil.example/x"),
    "encoded dots climbing out": ("/%2e%2e/%2e%2e/admin", f"{PREFIX}/mcp/", "/mcp", "../admin"),
    "a backslash segment hiding a host change": (r"//127.255.255.254/mcp/\\/a\b/b/b", f"{PREFIX}/mcp/", "/mcp",
                                                  "%5C%5C/a%5Cb/b/b"),
    "loopback url with encoded dots": ("http://127.0.0.1:8123/%2e%2e/x", f"{PREFIX}/a/b", "/a/b", "../x"),
    "encoded dots in the middle": ("/a/%2e%2e/b", f"{PREFIX}/a/b", "/a/b", "../b"),
    "mixed encoded and literal dots": ("/a/.%2E/%2e./b", f"{PREFIX}/a/b", "/a/b", "../b"),
    "an encoded single dot is dropped": ("/a/%2E/b", f"{PREFIX}/a/b", "/a/b", "b"),
    "an encoded dot at the end": ("/a/%2e", f"{PREFIX}/a/b", "/a/b", "./"),
    "backslashes standing in for slashes": (r"/a\..\..\x", f"{PREFIX}/mcp", "/mcp", "a%5C..%5C..%5Cx"),
    "backslashes with a bare url as the base": (r"/\\evil.example/x", PREFIX, "/", f"{PREFIX[1:]}/%5C%5Cevil.example/x"),
    "a relative reference with backslashes": (r"..\..\x", PREFIX, "/", f"{PREFIX[1:]}/..%5C..%5Cx"),
    "a query and a fragment are left alone": (r"/\\evil.example/x?a=1&b=\c#f\g", f"{PREFIX}/mcp", "/mcp",
                                               r"%5C%5Cevil.example/x?a=1&b=\c#f\g"),
}


@pytest.mark.parametrize(("location", "client_path", "child_path", "expected"),
                         [pytest.param(*case, id=name) for name, case in HOSTILE.items()])
def test_a_rewritten_location_never_lets_the_client_leave_the_prefix(location, client_path, child_path, expected):
    reference = _rewrite(location, client_path, child_path)
    assert reference == expected
    assert _inside_the_prefix(client_path, reference), _browser_resolves(client_path, reference)


def test_the_oracle_sees_how_the_unprotected_references_left_the_prefix():
    """對照：沒有保護時的輸出（341ece7 的輸出）在 WHATWG 用戶端確實會離開前綴，所以上面的檢查不是空的。"""
    assert _browser_resolves(f"{PREFIX}/mcp", r"\\evil.example/x")[0] == "elsewhere"
    assert _browser_resolves(f"{PREFIX}/mcp/", "../%2e%2e/%2e%2e/admin") == ("gateway", "/admin")
    assert _browser_resolves(f"{PREFIX}/mcp/", r"\\/a\b/b/b")[0] == "elsewhere"
    assert _browser_resolves(f"{PREFIX}/mcp", r"a\..\..\x") == ("gateway", "/x")
    # 與 RFC 3986 的看法一致的寫法：沒有差別
    assert _browser_resolves(f"{PREFIX}/mcp/", "../admin") == ("gateway", f"{PREFIX}/admin")
    assert _inside_the_prefix(f"{PREFIX}/mcp/", "x/y")
    assert not _inside_the_prefix(f"{PREFIX}/mcp/", "../../admin")


@pytest.mark.parametrize(("path", "expected"), [
    ("/a/%2e%2e/b", "/b"),
    ("/a/%2E%2E/b", "/b"),
    ("/a/.%2e/b", "/b"),
    ("/a/%2e./b", "/b"),
    ("/%2e%2e/%2e%2e/admin", "/admin"),  # 爬不出根
    ("/a/%2e/b", "/a/b"),
    ("/a/%2E/b", "/a/b"),
    ("/a/b/%2e", "/a/b/"),
    ("/a/b/%2e%2e", "/a/"),
    # 看起來像、但不是點段的：原樣保留（與 WHATWG 相同）
    ("/a/%2e%2e%2e/b", "/a/%2e%2e%2e/b"),
    ("/a/.../b", "/a/.../b"),
    ("/a/x%2e/b", "/a/x%2e/b"),
    ("/a/%2ex/b", "/a/%2ex/b"),
    ("/a/%252e%252e/b", "/a/%252e%252e/b"),
    ("/a/%2f/b", "/a/%2f/b"),
    # 字面的點段照舊
    ("/a/./b/../c", "/a/c"),
    ("/a/b/.", "/a/b/"),
    ("/a/b/..", "/a/"),
    ("/../../x", "/x"),
])
def test_encoded_dot_segments_are_removed_like_literal_ones(path, expected):
    from mcp_admin_core import proxy

    assert proxy._remove_dot_segments(path) == expected


# 寫實的輸入：輸出與修正前（341ece7）逐位元相同（這個表是用 341ece7 的實作產生的）
REALISTIC = [
    ("/a/./b/../c", "/private_Tok/a/b", "/a/b", "c"),
    ("/a//b", "/private_Tok/mcp", "/mcp", "a//b"),
    ("/a b/c", "/private_Tok/mcp", "/mcp", "a b/c"),
    ("/c%2Fd/e", "/private_Tok/mcp", "/mcp", "c%2Fd/e"),
    ("/k=v/x.y?z=1#frag", "/private_Tok/mcp", "/mcp", "k=v/x.y?z=1#frag"),
    ("/file%2etxt", "/private_Tok/mcp", "/mcp", "file%2etxt"),
    ("/a/b/.", "/private_Tok/a/b", "/a/b", "b/"),
    ("/a/b/..", "/private_Tok/a/b", "/a/b", "./"),
    ("/a/../../x", "/private_Tok/a/b", "/a/b", "../x"),
    ("/mcp/", "/private_Tok/mcp/", "/mcp", "./"),
    ("http://127.0.0.1:8123/a b/./c", "/private_Tok", "/", "private_Tok/a b/c"),
    ("http://localhost:8123//x//y", "/private_Tok/", "/", ".//x//y"),
    ("http://[::1]:8123/x/y/../z?q=1", "/private_Tok/messages", "/messages", "x/z?q=1"),
    ("sub/./dir/../file", "/private_Tok", "/", "private_Tok/sub/file"),
    ("../up", "/private_Tok/mcp/", "/mcp", "../up"),
    ("x.y/z", "/private_Tok/mcp", "/mcp", "x.y/z"),
    ("/a%20b/c%2Fd?next=//evil.example", "/private_Tok/sse", "/sse", "a%20b/c%2Fd?next=//evil.example"),
]


@pytest.mark.parametrize(("location", "client_path", "child_path", "expected"), REALISTIC)
def test_realistic_locations_are_rewritten_exactly_as_before(location, client_path, child_path, expected):
    from mcp_admin_core import proxy

    assert proxy._external_location(location, client_path, child_path, "/private_Tok") == expected


# --- 端到端：真的經過閘道（子行程是 MockTransport，回 307＋Location）
@pytest.mark.parametrize(("path", "child_location", "expected"), [
    ("mcp", r"/\\evil.example/x", "%5C%5Cevil.example/x"),
    ("mcp/", "/%2e%2e/%2e%2e/admin", "../admin"),
    ("mcp/", r"//127.255.255.254/mcp/\\/a\b/b/b", "%5C%5C/a%5Cb/b/b"),
])
def test_the_gateway_sends_a_location_that_stays_inside_the_prefix(make_app, monkeypatch, path, child_location, expected):
    from mcp_admin_core import proxy

    def child(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": child_location})

    monkeypatch.setattr(proxy, "_get_client",
                        lambda timeout=86400: httpx.AsyncClient(transport=httpx.MockTransport(child)))
    client = make_app({"mcp_auth_token": TOKEN, "mcp_server": {"command": "", "args": [], "port": 8123, "env": {}}})
    response = client.get(f"/private_{TOKEN}/{path}", follow_redirects=False)
    assert response.status_code == 307
    location = response.headers["location"]
    assert location == expected and TOKEN not in location
    assert _inside_the_prefix(f"/private_{TOKEN}/{path}", location)
