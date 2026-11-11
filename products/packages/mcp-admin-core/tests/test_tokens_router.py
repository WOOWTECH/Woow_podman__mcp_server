"""tokens canon 契約測試 + token_history 三形狀容忍（防禦性 .get）。"""
from __future__ import annotations

from management_client import revision_headers


def _login(c):
    return c.post("/api/auth/login", json={"password": "admin"}).json()["token"]


def test_get_tokens_empty(make_app):
    c = make_app()
    t = _login(c)
    r = c.get("/api/tokens", headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"token_masked", "token_set", "last_rotated", "history", "state", "shared", "configuration"}
    assert body["token_set"] is False


def test_generate_length_bounds(make_app):
    c = make_app()
    t = _login(c)
    h = {"Authorization": f"Bearer {t}"}
    ok = c.post("/api/tokens/generate", params={"length": 64}, headers=h)
    assert ok.status_code == 200
    assert ok.json()["length"] == 64
    bad = c.post("/api/tokens/generate", params={"length": 16}, headers=h)
    assert bad.status_code == 400


def test_rotate_then_get_masked(make_app):
    c = make_app()
    t = _login(c)
    h = {"Authorization": f"Bearer {t}"}
    rot = c.post("/api/tokens/rotate", json={"length": 64}, headers=revision_headers(c, h, read_path='/api/settings'))
    assert rot.status_code == 200
    assert rot.json()["status"] == "ok"
    new_token = rot.json()["token"]
    assert len(new_token) == 64
    got = c.get("/api/tokens", headers=h).json()
    assert got["token_set"] is True
    assert got["token_masked"].startswith(new_token[:4])
    assert got["token_masked"].endswith(new_token[-4:])


def test_history_mixed_shapes_does_not_500(make_app):
    # 既有 token_history 同時含 canon / odoo / settings 三種形狀 → GET 不得 500
    overrides = {
        "mcp_auth_token": "abcd" + "e" * 56 + "wxyz",
        "token_history": [
            {"rotated_at": "2026-01-01T00:00:00+00:00", "previous_token_masked": "ab**yz"},  # canon
            {"token_prefix": "abcdefgh", "created_at": "2026-01-02T00:00:00+00:00", "rotated_by": "admin"},  # odoo
            {"token_masked": "ab**yz", "rotated_at": "2026-01-03T00:00:00+00:00"},  # settings
        ],
    }
    c = make_app(overrides)
    t = _login(c)
    r = c.get("/api/tokens", headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 200
    assert r.json()["token_set"] is True


def test_history_odoo_shape_first_uses_get_fallback(make_app):
    # history[0] 為 odoo 形狀（無 rotated_at）→ 若用 history[0]["rotated_at"] 會 KeyError→500。
    # 防禦性 .get 必須讓 GET 回 200 且 last_rotated 為 None（此測試沒有 .get 就會 fail）。
    overrides = {
        "mcp_auth_token": "abcd" + "e" * 56 + "wxyz",
        "token_history": [
            {"token_prefix": "abcdefgh", "created_at": "2026-01-02T00:00:00+00:00", "rotated_by": "admin"},  # odoo（無 rotated_at）
            {"rotated_at": "2026-01-01T00:00:00+00:00", "previous_token_masked": "ab**yz"},  # canon
        ],
    }
    c = make_app(overrides)
    t = _login(c)
    r = c.get("/api/tokens", headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 200
    assert r.json()["last_rotated"] is None


def test_set_token_via_put(make_app):
    c = make_app()
    t = _login(c)
    h = {"Authorization": f"Bearer {t}"}
    r = c.put("/api/tokens", json={"token": "x" * 32}, headers=revision_headers(c, h, read_path='/api/settings'))
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_set_token_rejects_path_controls_and_keeps_previous(make_app, tmp_config_path):
    import json
    c = make_app({'mcp_auth_token': 'old-shared-credential'})
    headers = {'Authorization': 'Bearer ' + _login(c)}
    for value in ['x' * 16 + '/path', 'x' * 16 + '?q=1', 'x' * 16 + '\\n']:
        response = c.put('/api/tokens', headers=revision_headers(c, headers, read_path='/api/settings'), json={'token': value})
        assert response.status_code == 422
        assert json.loads(tmp_config_path.read_text())['mcp_auth_token'] == 'old-shared-credential'
