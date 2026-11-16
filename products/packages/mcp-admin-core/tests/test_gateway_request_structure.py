"""閘道的結構守門（第二輪審查 F1）：位元組預算只算 body 的位元組，擋不住結構化 JSON 解析後的記憶體放大。

16 MiB 的 ``[[],[],…]``、``[{},{}…]`` 解析成 Python 物件後多出約 390～400 MiB（24～25 倍），純字串只多 1 倍（審查 amp.py）。
非 n8n／Headscale 的產品過去沒有任何深度或元素上限。現在 ``create_app`` 給每個產品一個預設的 ``McpRequestLimits``
（``app.state.mcp_structure_limits``），proxy 在 ``json.loads`` 之前先掃描結構字元（``request_limits.structure_violation``），
超過就拒收（不轉發、不含內容）。

這個檔案：

- 單元：審查四種病態 body（pad 以外）都被擋、合法的大呼叫（Nextcloud 13.3 MiB 上傳、Odoo 批次 create、n8n 大 workflow）放行，
  字串內的括號與逗號不計，邊界值，掃描成本有上限；
- HTTP：拒收時的回應（知道 id 就 200＋該 id 的 -32602、否則 413）、不轉發、合法請求照常轉發、不是 JSON 的內容維持原本行為。
"""
from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

from mcp_admin_core.request_limits import (
    DEFAULT_MAX_DEPTH, DEFAULT_MAX_TOTAL_ELEMENTS, McpRequestLimits, structure_violation,
)

MIB = 1024 * 1024
N = 16 * MIB


def _violation(body: bytes):
    return structure_violation(body, max_depth=DEFAULT_MAX_DEPTH, max_elements=DEFAULT_MAX_TOTAL_ELEMENTS)


def _envelope(arguments_json: bytes, rpc_id: int = 7) -> bytes:
    return (b'{"jsonrpc":"2.0","id":%d,"method":"tools/call","params":{"name":"t","arguments":' % rpc_id
            + arguments_json + b"}}")


def worst_bodies() -> dict[str, bytes]:
    """審查 amp.py 的四種 16 MiB body（pad 是對照：一個大字串）。"""
    head = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"p":'
    return {
        "pad": head + b'"' + b"x" * (N - 1024) + b'"}}',
        "lists": head + b"[" + b"[]," * ((N - 1024) // 3) + b"[]]}}",
        "dicts": head + b"[" + b"{}," * ((N - 1024) // 3) + b"{}]}}",
        "kv": head + b"[" + b'{"a":1},' * ((N - 1024) // 8) + b"{}]}}",
        "floats": head + b"[" + b"1.5," * ((N - 1024) // 4) + b"1]}}",
        "strings": head + b"[" + b'"ab",' * ((N - 1024) // 5) + b'"x"]}}',
    }


@pytest.mark.parametrize("kind", ["lists", "dicts", "kv", "floats", "strings"])
def test_pathological_16_mib_bodies_are_refused(kind):
    reason = _violation(worst_bodies()[kind])
    assert reason is not None and "elements" in reason


def test_one_big_string_has_no_structure_to_refuse():
    assert _violation(worst_bodies()["pad"]) is None


def test_legitimate_nextcloud_upload_of_13_3_mib_passes():
    """10 MiB 檔案 base64 後約 13.3 MiB 的單一字串（Nextcloud ``upload_file`` 的產品上限）。"""
    content = base64.b64encode(b"\x00\xffbinary" * (10 * MIB // 8)).decode()
    assert len(content) > 13 * MIB
    body = _envelope(json.dumps({"path": "/a/b.bin", "content": content}).encode())
    assert _violation(body) is None


def test_legitimate_odoo_batch_create_of_thousands_of_records_passes():
    """Odoo ``execute_method`` 批次 create：3,000 筆、每筆 30 個欄位（含巢狀的 many2many 命令）。"""
    record = {f"x_field_{i}": f"value {i}" for i in range(28)}
    record["tag_ids"] = [[6, 0, [1, 2, 3]]]
    record["line_ids"] = [[0, 0, {"name": "line", "qty": 2.5}]]
    body = _envelope(json.dumps({"model": "res.partner", "method": "create", "args": [[record] * 3000]}).encode())
    assert len(body) > 1 * MIB
    assert _violation(body) is None


def test_legitimate_large_n8n_workflow_passes():
    node = {"id": "n1", "name": "HTTP", "type": "n8n-nodes-base.httpRequest", "position": [0, 0],
            "parameters": {"url": "https://example.test", "options": {"headers": [{"name": "a", "value": "b"}] * 5}}}
    workflow = {"name": "big", "nodes": [node] * 1500, "connections": {f"n{i}": {"main": [[{"node": "x", "index": 0}]]}
                                                                         for i in range(1500)}}
    assert _violation(_envelope(json.dumps({"workflow": workflow}).encode())) is None


def test_batch_of_thousands_of_small_requests_passes():
    batch = [{"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": "t", "arguments": {"a": [1, 2, 3]}}}
             for i in range(5000)]
    assert _violation(json.dumps(batch).encode()) is None


def test_brackets_and_commas_inside_strings_do_not_count():
    noisy = b'{"text":"' + b'{[,],} ' * (N // 7) + b'","q":"\\"[[[["}'
    assert _violation(noisy) is None


def test_depth_boundary():
    def nested(levels: int) -> bytes:
        return b"[" * levels + b"]" * levels

    assert _violation(nested(DEFAULT_MAX_DEPTH)) is None
    assert "nesting" in _violation(nested(DEFAULT_MAX_DEPTH + 1))
    # 字串裡的括號不算層數，跳脫的引號不會提早結束字串
    assert _violation(b'{"a":"' + b"[" * 500 + b'\\"' + b"[" * 500 + b'"}') is None


def test_element_boundary_counts_commas_and_containers():
    limit = 1000
    # [0,0,…] 共 limit 個元素 ＝ (limit-1) 個逗號 ＋ 1 個容器
    at_limit = b"[" + b",".join([b"0"] * limit) + b"]"
    over = b"[" + b",".join([b"0"] * (limit + 1)) + b"]"
    assert structure_violation(at_limit, max_depth=64, max_elements=limit) is None
    assert "elements" in structure_violation(over, max_depth=64, max_elements=limit)


def test_scanning_cost_is_bounded_by_the_limit_not_the_body_size():
    """病態 body 掃到上限就停；不合法 JSON（字串沒結束）不會造成二次方時間。"""
    unterminated = b'"' + b"[,]" * 100_000 + b'"' + b'"' + b"x" * N
    for body in (worst_bodies()["lists"], worst_bodies()["kv"], worst_bodies()["strings"], unterminated):
        started = time.perf_counter()
        _violation(body)
        assert time.perf_counter() - started < 2.0


def test_mcp_request_limits_check_structure_error_has_no_content():
    limits = McpRequestLimits(max_bytes=N, max_argument_elements=10)
    from mcp_admin_core.request_limits import RequestLimitError

    with pytest.raises(RequestLimitError) as caught:
        limits.check_structure(b'{"secret-value":' + b"[" * 100 + b"]" * 100 + b"}")
    assert "secret-value" not in caught.value.payload["error"]["message"]
    assert caught.value.status == 413 and caught.value.payload["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# HTTP：沒有宣告 McpRequestLimits 的產品（create_app 的預設結構守門）
# ---------------------------------------------------------------------------

pytestmark_http = pytest.mark.usefixtures("synthetic_child")


@pytest.fixture
def gateway(make_app, monkeypatch, synthetic_child):
    from mcp_admin_core import proxy

    forwarded = []

    def upstream(request):
        forwarded.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 7, "result": {"content": []}})

    monkeypatch.setattr(proxy, "_client", None)
    monkeypatch.setattr(proxy, "_get_client",
                        lambda timeout=86400: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    client = make_app({"mcp_auth_token": "secret"})
    assert client.app.state.mcp_request_limits is None  # 這條路徑就是「產品沒宣告自己的上限」
    return client, forwarded


def _post(client, body: bytes, path="mcp"):
    return client.post("/private_secret/" + path, content=body,
                       headers={"content-type": "application/json", "accept": "application/json, text/event-stream"})


def test_default_structure_limits_are_installed_by_create_app(gateway):
    client, _ = gateway
    limits = client.app.state.mcp_structure_limits
    assert isinstance(limits, McpRequestLimits)
    assert (limits.max_depth, limits.max_total_elements) == (DEFAULT_MAX_DEPTH, DEFAULT_MAX_TOTAL_ELEMENTS)
    assert limits.max_bytes == 16 * MIB


@pytest.mark.parametrize("path", ["mcp", "messages/"])
@pytest.mark.parametrize("kind", ["lists", "kv"])
def test_pathological_body_is_refused_before_any_forward(gateway, path, kind):
    client, forwarded = gateway
    response = _post(client, worst_bodies()[kind], path)
    payload = response.json()
    assert payload["error"]["code"] == -32602 and "structure" in payload["error"]["message"]
    if path == "mcp":  # Streamable HTTP 且認得 id：200＋該 id 的錯誤，用戶端只有這次呼叫失敗
        assert response.status_code == 200 and payload["id"] == 1
    else:  # legacy SSE 的 /messages/ POST 只看狀態碼
        assert response.status_code == 413
    assert not forwarded


def test_too_deep_body_is_refused_and_the_message_has_no_content(gateway):
    client, forwarded = gateway
    body = _envelope(b'{"secret-marker":' + b"[" * (DEFAULT_MAX_DEPTH + 1) + b"]" * (DEFAULT_MAX_DEPTH + 1) + b"}")
    response = _post(client, body)
    assert response.status_code == 200 and response.json()["id"] == 7
    assert "levels of nesting" in response.json()["error"]["message"]
    assert "secret-marker" not in response.text
    assert not forwarded


def test_batch_without_a_recoverable_id_is_refused_with_413(gateway):
    client, forwarded = gateway
    body = b"[" + b",".join([b'{"jsonrpc":"2.0","method":"notifications/x"}'] * (DEFAULT_MAX_TOTAL_ELEMENTS + 1)) + b"]"
    response = _post(client, body)
    assert response.status_code == 413 and response.json()["id"] is None
    assert not forwarded


def test_legitimate_large_calls_still_forward(gateway):
    client, forwarded = gateway
    upload = base64.b64encode(b"\x01" * (10 * MIB)).decode()
    record = {f"f{i}": i for i in range(30)}
    for arguments in ({"path": "/x", "content": upload},
                      {"model": "res.partner", "method": "create", "args": [[record] * 3000]}):
        response = _post(client, _envelope(json.dumps(arguments).encode()))
        assert response.status_code == 200 and "error" not in response.json(), response.text[:200]
    assert len(forwarded) == 2


def test_non_json_and_non_post_behave_as_before(gateway):
    client, forwarded = gateway
    assert _post(client, b"this is not json {[[[").status_code == 200  # 不是 JSON：照舊轉給子行程
    assert len(forwarded) == 1
    assert client.get("/private_secret/mcp").status_code in (200, 405, 400, 404)  # GET 沒有 body，不經結構守門
