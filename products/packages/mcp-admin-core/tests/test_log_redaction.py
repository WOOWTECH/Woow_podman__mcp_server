"""woow_paas_platform#1614：URL 裡的憑證不進 admin log。

- MCP path token：``/private_<token>/…`` → ``/private_***/…``
- 管理頁 JWT：Logs 頁 EventSource 的 ``?token=<JWT>`` → ``?token=***``

單元測試驗 ``mcp_admin_core.log_redaction``；端到端測試以正式映像同樣的方式（``uvicorn <app>_mcp_admin.main:app``）
把九個 app 各起一個真的 uvicorn，打秘密路徑與帶 ``?token=`` 的管理 API，然後檢查容器 stdout／stderr
（＝``kubectl logs``）與管理頁 Logs 的 ring buffer 都沒有 token。修正前除了 litellm、nextcloud 以外的 app
會在 access log 印出完整 token，九個 app 都會印出 JWT。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from mcp_admin_core import log_redaction

TOKEN = "Zq9" + "x" * 40 + "Tk7"


# ---------------------------------------------------------------------------
# redact()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"/private_{TOKEN}/mcp", "/private_***/mcp"),
        (f"/private_{TOKEN}", "/private_***"),
        (f"/private_{TOKEN}/messages/?session_id=abc", "/private_***/messages/?session_id=abc"),
        (f'"GET /private_{TOKEN}?x=1 HTTP/1.1"', '"GET /private_***?x=1 HTTP/1.1"'),
        (f"/api/logs/stream?token={TOKEN}", "/api/logs/stream?token=***"),
        (f"/api/logs/search?q=x&token={TOKEN}&limit=5", "/api/logs/search?q=x&token=***&limit=5"),
        (f"/x?access_token={TOKEN}", "/x?access_token=***"),
        (f"/x?TOKEN={TOKEN}", "/x?TOKEN=***"),
        (f"/x?ToKeN={TOKEN}", "/x?ToKeN=***"),
    ],
)
def test_redact_masks_url_credentials(raw, expected):
    assert log_redaction.redact(raw) == expected
    assert TOKEN not in log_redaction.redact(raw)


@pytest.mark.parametrize(
    "text",
    ["", "/api/settings", "/private", "GET /healthz", "/x?session_token=abc", "token rotated", "/x?q=token"],
)
def test_redact_leaves_other_text_untouched(text):
    assert log_redaction.redact(text) == text


# ---------------------------------------------------------------------------
# record factory／filter
# ---------------------------------------------------------------------------


@pytest.fixture
def installed():
    log_redaction.install()
    yield


def test_any_logger_is_redacted(installed, caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("some.app.logger").warning("proxy %s failed", f"/private_{TOKEN}/sse")
    logging.getLogger("mcp_admin_core.process").info(f"[mcp-server] GET /private_{TOKEN}/mcp")
    assert TOKEN not in caplog.text
    assert caplog.text.count("/private_***") == 2


def test_non_string_args_are_redacted(installed, caplog):
    caplog.set_level(logging.INFO)
    url = httpx.URL(f"http://127.0.0.1:8080/private_{TOKEN}/mcp?token={TOKEN}")
    logging.getLogger("httpx").info("HTTP Request: %s %s %d", "POST", url, 200)
    assert TOKEN not in caplog.text
    assert "/private_***/mcp?token=***" in caplog.text and " 200" in caplog.text


def test_exception_text_is_redacted(installed, caplog):
    caplog.set_level(logging.INFO)
    try:
        raise RuntimeError(f"upstream failed for /private_{TOKEN}/mcp")
    except RuntimeError:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
    assert TOKEN not in caplog.text
    assert "/private_***/mcp" in caplog.text


def test_uvicorn_access_formatter_still_works(installed):
    """args 維持 5-tuple：uvicorn AccessFormatter 拆得開，而且輸出不含 token。"""
    from uvicorn.logging import AccessFormatter

    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:5555", "POST", f"/private_{TOKEN}/mcp", "1.1", 200), None,
    )
    out = AccessFormatter(fmt='%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False).format(record)
    assert out == '10.0.0.1:5555 - "POST /private_***/mcp HTTP/1.1" 200 OK'


def test_filter_redacts_even_without_record_factory():
    """第二道防線：record factory 被別人換掉時，uvicorn.access 的 filter 仍會遮。"""
    log_redaction.install()
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s %s",
                               ("GET", f"/private_{TOKEN}/sse?token={TOKEN}"), None)
    assert any(f.filter(record) for f in logging.getLogger("uvicorn.access").filters
               if isinstance(f, log_redaction.PathTokenRedactionFilter))
    assert record.getMessage() == "GET /private_***/sse?token=***"


def test_install_is_idempotent():
    log_redaction.install()
    factory = logging.getLogRecordFactory()
    filters = list(logging.getLogger("uvicorn.access").filters)
    log_redaction.install()
    log_redaction.install(["uvicorn.access"])
    assert logging.getLogRecordFactory() is factory
    assert logging.getLogger("uvicorn.access").filters == filters


def test_create_app_installs_redaction(make_app):
    make_app()
    assert any(isinstance(f, log_redaction.PathTokenRedactionFilter)
               for f in logging.getLogger("uvicorn.access").filters)
    # record factory 可能被其他 app（nextcloud）再包一層，所以以實際行為確認
    rec = logging.getLogger("x").makeRecord("x", logging.INFO, __file__, 1, "%s", (f"/private_{TOKEN}",), None)
    assert rec.getMessage() == "/private_***"


# ---------------------------------------------------------------------------
# 端到端：九個 app 各起真的 uvicorn（與映像 CMD 相同），檢查 stdout／stderr 與 Logs ring buffer
# ---------------------------------------------------------------------------

# apps/odoo-manage 已封存且移出 uv workspace（#78：它的 mcp-server-odoo 0.7.1 要求 mcp<2），不再測試。
APPS = ("hermes", "n8n", "odoo", "k8s", "vk", "opendesign", "nextcloud", "litellm")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("app", APPS)
def test_uvicorn_access_log_has_no_url_credentials(app, tmp_path: Path):
    path_token = secrets.token_urlsafe(32)
    jwt_secret = secrets.token_urlsafe(32)
    admin_password = secrets.token_urlsafe(12)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "mcp_auth_token": path_token,
        "admin_password": admin_password,
        "mcp_server": {"command": "", "args": [], "port": _free_port(), "env": {}},
    }))
    port = _free_port()
    # Never pass provider credentials into these local subprocesses.
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "LANG", "LC_ALL") if k in os.environ}
    env.update({"MCP_ADMIN_CONFIG": str(config), "JWT_SECRET": jwt_secret, "PYTHONUNBUFFERED": "1"})
    out_path = tmp_path / "container.log"
    with out_path.open("wb") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", f"{app}_mcp_admin.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=tmp_path, env=env, stdout=out, stderr=subprocess.STDOUT,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.time() + 60
            while True:
                try:
                    if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if proc.poll() is not None or time.time() > deadline:
                    pytest.fail(f"{app} admin did not start:\n{out_path.read_text(errors='replace')[-2000:]}")
                time.sleep(0.2)

            with httpx.Client(base_url=base, timeout=10) as c:
                login = c.post("/api/auth/login", json={"password": admin_password})
                assert login.status_code == 200
                admin_jwt = login.json()["token"]
                c.cookies.clear()  # The logs URL query token is the authentication oracle.
                # MCP client 的三種打法（子行程沒跑 → 503；錯 token → 403），都會進 access log
                c.post(f"/private_{path_token}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                c.get(f"/private_{path_token}")
                c.get(f"/private_{path_token}/messages/", params={"session_id": "abc"})
                c.get(f"/private_wrong{path_token[:8]}/sse")
                # 管理頁 Logs（EventSource 只能用 ?token= 帶 JWT）
                r = c.get("/api/logs/search", params={"q": "private_", "limit": 1000, "token": admin_jwt})
                assert r.status_code == 200, r.text
                time.sleep(0.3)
                # 管理頁 Logs 的 ring buffer：上面所有請求的 access log 都要已遮
                r = c.get("/api/logs/search", params={"q": "/", "limit": 1000, "token": admin_jwt})
                assert r.status_code == 200, r.text
                buffer_text = "\n".join(e["message"] for e in r.json()["entries"])
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    container_log = out_path.read_text(errors="replace")
    assert path_token not in container_log
    assert admin_jwt not in container_log
    assert "/private_***/mcp" in container_log  # access log 確實有印出（只是遮了）
    assert "token=***" in container_log
    assert path_token not in buffer_text and admin_jwt not in buffer_text
    assert "/private_***" in buffer_text
