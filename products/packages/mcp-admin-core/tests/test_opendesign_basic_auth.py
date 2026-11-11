"""opendesign 連線 basic auth（#1354）。

- admin：od_basic_user／od_basic_password（secret、遮罩、留空沿用）存取；URL userinfo 舊寫法
  存檔時拆進兩欄、URL 不再存明文；GET 遮掉舊 URL 內密碼；health probe 帶 BasicAuth 且
  顯示的 URL 不含帳密；core /api/settings 也遮罩 od_basic_password。
- 子行程（opendesign_mcp_server 套件）：依 env 決定帳密、OD_API_BASE 去掉 userinfo、
  vendored 模組的 httpx.Client 預設帶 BasicAuth。
"""
from __future__ import annotations

from management_client import revision_headers

import base64
import json
import types

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client():
    from opendesign_mcp_admin.routers import config as od_config

    app = FastAPI()
    app.include_router(od_config.router)
    return TestClient(app)


def _put(c, **body):
    body.setdefault("restart", False)
    r = c.put("/api/config/connection", json=body, headers=revision_headers(c, None, read_path='/api/config'))
    assert r.status_code == 200, r.text
    return r


def _saved(store_path) -> dict:
    return json.loads(store_path.read_text())["connection"]


# ---------------------------------------------------------------------------
# admin：存取與遮罩
# ---------------------------------------------------------------------------


def test_put_and_get_basic_auth_fields(store_env, tmp_config_path):
    c = _client()
    _put(c, od_api_base="https://od.example.com", od_basic_user="admin", od_basic_password="Sup3rSecret!")
    conn = _saved(tmp_config_path)
    assert conn["od_basic_user"] == "admin"
    assert conn["od_basic_password"] == "Sup3rSecret!"

    body = c.get("/api/config").json()
    assert body["od_basic_user"] == "admin"
    assert "Sup3rSecret!" not in json.dumps(body)
    assert "*" in body["od_basic_password_masked"]
    assert "od_basic_password" not in body


def test_blank_password_keeps_existing(store_env, tmp_config_path):
    c = _client()
    _put(c, od_api_base="https://od.example.com", od_basic_user="admin", od_basic_password="pw1")
    _put(c, od_api_base="https://od2.example.com", od_basic_user="admin", od_basic_password="")
    conn = _saved(tmp_config_path)
    assert conn["od_basic_password"] == "pw1"
    assert conn["od_api_base"] == "https://od2.example.com"


def test_clearing_user_disables_auth(store_env, tmp_config_path):
    from opendesign_mcp_admin.basic_auth import resolve_target

    c = _client()
    _put(c, od_api_base="https://od.example.com", od_basic_user="admin", od_basic_password="pw1")
    _put(c, od_api_base="https://od.example.com", od_basic_user="")
    conn = _saved(tmp_config_path)
    assert conn["od_basic_user"] == ""
    assert resolve_target(conn) == ("https://od.example.com", None)


def test_url_userinfo_is_moved_into_fields(store_env, tmp_config_path):
    c = _client()
    _put(c, od_api_base="https://admin:p%40ss@od.example.com/", od_basic_user="", od_basic_password="")
    conn = _saved(tmp_config_path)
    assert conn["od_api_base"] == "https://od.example.com/"  # URL 欄不再存帳密
    assert conn["od_basic_user"] == "admin"
    assert conn["od_basic_password"] == "p@ss"  # percent-decode


def test_explicit_fields_win_over_url_userinfo(store_env, tmp_config_path):
    c = _client()
    _put(c, od_api_base="https://old:oldpw@od.example.com", od_basic_user="admin", od_basic_password="newpw")
    conn = _saved(tmp_config_path)
    assert conn["od_api_base"] == "https://od.example.com"
    assert (conn["od_basic_user"], conn["od_basic_password"]) == ("admin", "newpw")


async def test_legacy_url_is_redacted_on_get_and_migrated_on_resave(store_env, tmp_config_path):
    # 舊設定：帳密直接存在 URL（尚未重存）
    await store_env.put("connection", {"od_api_base": "https://admin:legacyPW@od.example.com"})
    c = _client()
    body = c.get("/api/config").json()
    assert "legacyPW" not in json.dumps(body)
    assert body["od_api_base"] == "https://admin:****@od.example.com"

    # 表單原樣送回（URL 帶遮罩密碼、密碼欄留空）→ 帳密移進欄位、URL 乾淨、不會存成 ****
    _put(c, od_api_base=body["od_api_base"], od_basic_user=body["od_basic_user"], od_basic_password="")
    conn = _saved(tmp_config_path)
    assert conn["od_api_base"] == "https://od.example.com"
    assert conn["od_basic_user"] == "admin"
    assert conn["od_basic_password"] == "legacyPW"


def test_core_settings_masks_basic_password(make_app):
    c = make_app({"connection": {"od_api_base": "https://od", "od_basic_user": "admin",
                                 "od_basic_password": "VerySecretPassword"}})
    token = c.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    r = c.get("/api/settings/connection", headers={"Authorization": f"Bearer {token}"})
    assert "VerySecretPassword" not in r.text
    assert r.json()["od_basic_user"] == "admin"


# ---------------------------------------------------------------------------
# admin：health probe
# ---------------------------------------------------------------------------


@pytest.fixture()
def capture_probe(monkeypatch):
    from opendesign_mcp_admin import health_probes as hp

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        status = 200 if seen["auth"] else 401
        return httpx.Response(status, json={"version": "1.2.3"})

    real = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(hp.httpx, "AsyncClient", _factory)
    return seen


def _basic(user: str, pw: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


async def test_probe_sends_basic_auth_from_fields(capture_probe):
    from opendesign_mcp_admin.health_probes import fetch_info, probe_target

    conn = {"od_api_base": "https://od.example.com", "od_basic_user": "admin", "od_basic_password": "pw"}
    out = await probe_target(conn)
    assert out["healthy"] is True
    assert capture_probe["auth"] == _basic("admin", "pw")
    assert (await fetch_info(conn))["version"] == "1.2.3"


async def test_probe_legacy_url_userinfo_not_shown(capture_probe):
    from opendesign_mcp_admin.health_probes import probe_target

    out = await probe_target({"od_api_base": "https://admin:pw@od.example.com"})
    assert out["healthy"] is True
    assert capture_probe["auth"] == _basic("admin", "pw")
    assert out["url"] == "https://od.example.com"
    assert "pw@" not in capture_probe["url"]


async def test_probe_401_has_readable_error(capture_probe):
    from opendesign_mcp_admin.health_probes import probe_target

    out = await probe_target({"od_api_base": "https://od.example.com"})
    assert out["healthy"] is False and out["status_code"] == 401
    assert "basic auth" in out["error"]


# ---------------------------------------------------------------------------
# 子行程：env → OD_API_BASE 改寫＋httpx shim
# ---------------------------------------------------------------------------


def test_resolve_env_from_fields_strips_url_userinfo():
    from opendesign_mcp_server import resolve_basic_auth_env

    env = {"OD_API_BASE": "https://old:oldpw@od.example.com", "OD_BASIC_USER": "admin",
           "OD_BASIC_PASSWORD": "p@ss:w/rd"}
    assert resolve_basic_auth_env(env) == ("admin", "p@ss:w/rd")
    assert env["OD_API_BASE"] == "https://od.example.com"


def test_resolve_env_legacy_url_userinfo():
    from opendesign_mcp_server import resolve_basic_auth_env

    env = {"OD_API_BASE": "https://admin:s%3Acret@od.example.com"}
    assert resolve_basic_auth_env(env) == ("admin", "s:cret")
    assert env["OD_API_BASE"] == "https://od.example.com"


def test_resolve_env_no_auth_is_noop():
    from opendesign_mcp_server import resolve_basic_auth_env

    env = {"OD_API_BASE": "http://open-design-svc:7457"}
    assert resolve_basic_auth_env(env) is None
    assert env == {"OD_API_BASE": "http://open-design-svc:7457"}
    assert resolve_basic_auth_env({}) is None


def test_install_basic_auth_shim_injects_auth_and_hides_password_in_errors():
    from opendesign_mcp_server import install_basic_auth

    fake = types.ModuleType("fake_vendored")
    fake.httpx = httpx
    fake._client = None
    assert install_basic_auth(fake, None) is False and fake.httpx is httpx
    assert install_basic_auth(fake, ("admin", "pw")) is True

    seen: dict = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(404)

    # vendored 用法：httpx.Client(base_url=..., timeout=...)、httpx.Timeout、httpx.HTTPStatusError
    client = fake.httpx.Client(base_url="https://od.example.com", timeout=fake.httpx.Timeout(5.0),
                               transport=httpx.MockTransport(handler))
    resp = client.get("/api/projects/x")
    assert seen["auth"] == _basic("admin", "pw")
    with pytest.raises(fake.httpx.HTTPStatusError) as exc:
        resp.raise_for_status()
    assert "pw" not in str(exc.value)
