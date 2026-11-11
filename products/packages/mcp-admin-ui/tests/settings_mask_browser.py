"""共用設定頁瀏覽器回歸：先 build 任一 app，再以已裝 Playwright 的 Python 執行。

python -B packages/mcp-admin-ui/tests/settings_mask_browser.py apps/n8n/frontend/dist
僅啟動 loopback 靜態網站；API／剪貼簿邊界使用記憶體假資料，不連實際服務。
"""
import functools
import http.server
import json
import pathlib
import secrets
import sys
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

DIST = pathlib.Path(sys.argv.pop(1)).resolve()


class StaticApp(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path in ("/tokens", "/settings"):
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, *args):
        pass


class MaskedSettingsBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(StaticApp, directory=str(DIST))
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.token = secrets.token_hex(32)
        self.bearer = secrets.token_hex(32)
        self.reveals = []
        self.saves = []
        self.fail_reveal = False
        self.revoked = False
        self.delay_reveal = False
        self.pending_reveal = None
        self.context = self.browser.new_context()
        self.context.add_init_script("""
            localStorage.setItem('mcp-admin-token', 'synthetic-session');
            window.copiedValues = [];
            Object.defineProperty(navigator, 'clipboard', { value: {
                writeText: async (value) => { window.copiedValues.push(value); }
            }});
        """)
        self.context.route("**/api/**", self.api)
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()

    def api(self, route):
        path = urlsplit(route.request.url).path
        if path == '/api/profiles':
            body = {'profiles': [{'id': 'default', 'label': 'Default', 'port': 8000, 'is_default': True}]}
        elif path.endswith("/reveal"):
            self.reveals.append(path)
            if self.delay_reveal:
                self.pending_reveal = (route, self.token)
                return
            if self.fail_reveal:
                route.fulfill(status=503, json={"detail": "Reveal unavailable"})
                return
            body = {"value": self.bearer if "/proxy/" in path else self.token}
        elif path == "/api/tokens/revoke":
            self.revoked = True
            body = {"status": "ok"}
        elif path == "/api/tokens/rotate":
            self.revoked = False
            self.token = secrets.token_hex(32)
            body = {"status": "ok", "token": self.token}
        elif path == "/api/tokens":
            body = {"state": "revoked" if self.revoked else "active", "token_set": not self.revoked,
                    "token_masked": "" if self.revoked else self.token[:4] + "****" + self.token[-4:], "history": [],
                    "configuration": {"revision": "fixture-r1", "server_time_ms": int(time.time() * 1000)}}
        elif path == "/api/settings/proxy" and route.request.method == "PUT":
            self.saves.append(route.request.post_data_json)
            body = {"status": "ok"}
        elif path == "/api/settings":
            body = {
                "mcp_auth_token": "" if self.revoked else self.token[:4] + "****" + self.token[-4:],
                "mcp_auth_token_masked": "" if self.revoked else self.token[:4] + "****" + self.token[-4:],
                "mcp_auth_token_set": not self.revoked, "mcp_server": {}, "tools": {},
                "proxy": {"timeout": 86400, "bearer_token": "****", "bearer_token_masked": "****"},
                "editable_env_keys": [],
            }
        else:
            body = {"running": False}
        route.fulfill(json=body)

    def tokens(self):
        self.page.goto(self.origin + "/tokens")
        self.page.get_by_title("Copy token", exact=True).wait_for()

    def proxy(self):
        self.page.goto(self.origin + "/settings")
        field = self.page.get_by_placeholder("Stored token; leave blank to keep")
        field.wait_for()
        return field, field.locator("..").locator("button")

    def test_loading_settings_does_not_reveal(self):
        self.tokens()
        self.assertFalse(self.reveals)
        field, _ = self.proxy()
        self.assertTrue(field.input_value() == "")
        self.assertFalse(self.reveals)

    def test_copy_token_and_command_use_revealed_value(self):
        self.tokens()
        self.page.get_by_title("Copy token", exact=True).click()
        self.page.wait_for_function("window.copiedValues.length === 1")
        self.assertTrue(self.page.evaluate("window.copiedValues[0]") == self.token)
        self.page.get_by_title("Copy command", exact=True).click()
        self.page.wait_for_function("window.copiedValues.length === 2")
        command = self.page.evaluate("window.copiedValues[1]")
        self.assertTrue(f"/private_{self.token}/mcp" in command)
        self.assertFalse("****" in command)

    def test_failed_reveal_never_copies_mask(self):
        self.fail_reveal = True
        self.tokens()
        self.page.get_by_title("Copy token", exact=True).click()
        self.page.get_by_text("Reveal unavailable", exact=True).wait_for()
        self.assertFalse(self.page.evaluate("window.copiedValues"))

    def test_masked_token_can_revoke_without_reveal_and_rotate_restores_copy(self):
        self.tokens()
        self.page.get_by_role("button", name="Revoke Token", exact=True).click()
        self.assertFalse(self.reveals)
        self.page.get_by_role("button", name="Confirm Revoke", exact=True).click()
        self.page.get_by_text("Revoked — this profile's clients blocked", exact=True).wait_for()
        for title in ("Copy token", "Copy command", "Show token"):
            self.assertTrue(self.page.get_by_title(title, exact=True).is_disabled())
        self.page.reload()
        self.page.get_by_text("Revoked — this profile's clients blocked", exact=True).wait_for()
        self.assertTrue(self.page.get_by_title("Copy token", exact=True).is_disabled())
        self.page.get_by_role("button", name="Rotate Token", exact=True).click()
        self.page.get_by_role("button", name="Confirm Rotate", exact=True).click()
        self.page.get_by_text("New token generated for this profile. Copy it and update its MCP clients.", exact=True).wait_for()
        self.assertFalse(self.reveals)
        self.page.get_by_title("Copy token", exact=True).click()
        self.page.wait_for_function("window.copiedValues.length === 1")
        self.assertEqual(self.page.evaluate("window.copiedValues[0]"), self.token)
        self.page.get_by_title("Show token", exact=True).click()
        self.page.get_by_text(self.token, exact=True).wait_for()
        self.page.get_by_role("button", name="Revoke Token", exact=True).click()
        self.page.get_by_role("button", name="Confirm Revoke", exact=True).click()
        self.page.get_by_text("Revoked — this profile's clients blocked", exact=True).wait_for()
        self.assertNotIn(self.token, self.page.locator("body").inner_text())

    def test_typed_proxy_value_survives_show_hide_and_save(self):
        field, eye = self.proxy()
        replacement = secrets.token_hex(32)
        field.fill(replacement)
        eye.click()
        self.assertTrue(field.input_value() == replacement)
        eye.click()
        self.assertTrue(field.input_value() == replacement)
        self.assertFalse(self.reveals)
        self.page.get_by_role("button", name="Save Proxy Config").click()
        self.page.get_by_text("Proxy config saved", exact=True).wait_for()
        self.assertTrue(self.saves[-1]["bearer_token"] == replacement)

    def test_external_rotation_refreshes_copy_and_hides_previous_reveal(self):
        self.page.clock.install()
        self.tokens()
        self.page.get_by_title("Show token", exact=True).click()
        self.page.get_by_text(self.token, exact=True).wait_for()
        previous = self.token
        self.token = secrets.token_hex(32)
        # Explicit copy always retrieves current credentials, even before metadata refetch.
        self.page.get_by_title("Copy token", exact=True).click()
        self.page.wait_for_function("window.copiedValues.length === 1")
        self.assertEqual(self.page.evaluate("window.copiedValues[0]"), self.token)
        self.page.get_by_title("Copy command", exact=True).click()
        self.page.wait_for_function("window.copiedValues.length === 2")
        self.assertIn(self.token, self.page.evaluate("window.copiedValues[1]"))
        self.assertNotIn(previous, self.page.evaluate("window.copiedValues[1]"))
        # Simulate metadata refresh after another tab rotates while this view stays mounted.
        previous = self.token
        self.token = secrets.token_hex(32)
        self.page.clock.fast_forward(11_000)  # exceed the app's 10s query freshness window
        self.context.set_offline(True)
        self.page.wait_for_function("!navigator.onLine")
        with self.page.expect_response(lambda response: urlsplit(response.url).path == "/api/tokens"):
            self.context.set_offline(False)
        self.page.get_by_title("Show token", exact=True).wait_for()
        self.assertNotIn(previous, self.page.locator("body").inner_text())

    def test_stored_proxy_reveal_and_blank_save(self):
        field, eye = self.proxy()
        eye.click()
        self.page.wait_for_function("document.querySelector('input[placeholder=\"Stored token; leave blank to keep\"]').type === 'text'")
        self.assertTrue(field.input_value() == self.bearer)
        eye.click()
        self.assertTrue(field.input_value() == "")
        self.page.get_by_role("button", name="Save Proxy Config").click()
        self.page.get_by_text("Proxy config saved", exact=True).wait_for()
        self.assertTrue(self.saves[-1]["bearer_token"] == "")

    def test_delayed_reveal_cannot_override_newer_observed_token(self):
        self.page.clock.install()
        self.tokens()
        for title in ("Show token", "Copy token", "Copy command"):
            with self.subTest(action=title):
                self.delay_reveal = True
                self.pending_reveal = None
                with self.page.expect_request(lambda request: urlsplit(request.url).path.endswith('/mcp_auth_token/reveal')):
                    self.page.get_by_title(title, exact=True).click()
                self.page.wait_for_timeout(10)  # dispatch the intercepted request handler
                self.assertIsNotNone(self.pending_reveal)
                previous = self.token
                self.token = secrets.token_hex(32)
                self.page.clock.fast_forward(11_000)
                self.context.set_offline(True)
                self.page.wait_for_function("!navigator.onLine")
                with self.page.expect_response(lambda response: urlsplit(response.url).path == "/api/tokens"):
                    self.context.set_offline(False)
                self.page.get_by_text(self.token[:4] + "****" + self.token[-4:], exact=True).wait_for()
                route, delayed_token = self.pending_reveal
                self.assertEqual(delayed_token, previous)
                route.fulfill(json={"value": delayed_token})
                self.page.get_by_text("Token changed while loading. Please try again.", exact=True).wait_for()
                self.assertNotIn(previous, self.page.locator('body').inner_text())
                self.assertFalse(self.page.evaluate('window.copiedValues'))
                self.assertEqual(self.page.get_by_title('Hide token', exact=True).count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
