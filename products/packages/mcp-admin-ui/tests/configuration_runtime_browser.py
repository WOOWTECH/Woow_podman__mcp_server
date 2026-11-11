"""真實 React 畫面 + loopback 靜態站；設定 API 為隔離 fixture，無部署寫入。"""
import functools
import http.server
import json
from pathlib import Path
import threading
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[3]
DIST = ROOT / 'apps/hermes/frontend/dist'


class StaticApp(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path in ('/config', '/settings'):
            self.path = '/index.html'
        super().do_GET()

    def log_message(self, *args):
        pass


class RuntimeConfigurationBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(StaticApp, directory=str(DIST)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f'http://127.0.0.1:{cls.server.server_port}'
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
        self.revision = 'r1'
        self.apply_state = 'failed'
        self.connection = {'gateway_url': 'http://saved.invalid', 'dashboard_username': 'saved-user'}
        self.env = {'EDIT': '1'}
        self.timeout = 86400
        self.requests = []
        self.delay_save = False
        self.pending = None
        self.errors = []
        self.context = self.browser.new_context(viewport={'width': 390, 'height': 844})
        self.context.add_init_script("localStorage.setItem('mcp-admin-token', 'fixture-admin');")
        self.context.route('**/api/**', self.api)
        self.page = self.context.new_page()
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def state(self):
        return {'revision': self.revision, 'saved_revision': 'launch-' + self.revision,
                'applied_revision': 'launch-' + self.revision if self.apply_state == 'applied' else None,
                'apply_state': self.apply_state,
                'last_error_code': 'child_start_failed' if self.apply_state == 'failed' else None}

    def api(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if request.method in ('PUT', 'POST'):
            self.requests.append({'path': path, 'revision': request.headers.get('if-match'), 'body': request.post_data_json if request.post_data else None})
            if self.delay_save and path == '/api/config/connection':
                self.pending = route
                return
            if request.headers.get('if-match') != '"' + self.revision + '"':
                route.fulfill(status=409, json={'detail': {'code': 'config_conflict', 'message': '設定已更新，請重新載入。', 'configuration': self.state()}})
                return
            if path == '/api/settings/mcp/apply':
                self.apply_state = 'applied'
            else:
                self.revision = 'r' + str(int(self.revision[1:]) + 1)
                self.apply_state = 'failed'
                payload = request.post_data_json
                if path == '/api/config/connection':
                    self.connection.update(payload)
                elif path == '/api/settings/mcp_server':
                    self.env.update(payload['env'])
                elif path == '/api/settings/proxy':
                    self.timeout = payload['timeout']
            route.fulfill(json={'status': 'partial' if self.apply_state == 'failed' else 'ok', 'saved': True,
                                'apply_state': self.apply_state, 'message': '設定已儲存。', 'configuration': self.state()})
            return
        if path == '/api/config':
            body = {**self.connection, 'configuration': self.state()}
        elif path == '/api/settings':
            body = {'mcp_server': {'command': 'fixture-child', 'env': self.env}, 'proxy': {'timeout': self.timeout},
                    'editable_env_keys': ['EDIT'], 'configuration': self.state()}
        elif path == '/api/settings/mcp/status':
            body = {'running': self.apply_state == 'applied', 'configuration': self.state()}
        elif path == '/api/capabilities':
            body = {'backend': {'mode': 'manual', 'can_edit': True}, 'capabilities': {}}
        elif path == '/api/health':
            body = {'mcp_server': {'healthy': False}}
        else:
            body = {}
        route.fulfill(json=body)

    def gateway(self):
        return self.page.get_by_placeholder('http://hermes-agent-svc:8642', exact=True)

    def test_failed_state_survives_reload_and_retry_never_submits_draft(self):
        self.page.goto(self.origin + '/config', wait_until='networkidle')
        expect(self.page.get_by_text('已儲存，但子程序啟動或設定驗證失敗。', exact=True)).to_be_visible()
        self.page.reload(wait_until='networkidle')
        expect(self.page.get_by_text('已儲存，但子程序啟動或設定驗證失敗。', exact=True)).to_be_visible()
        self.gateway().fill('http://unsaved.invalid')
        self.page.get_by_role('button', name='重試套用已儲存設定', exact=True).click()
        expect(self.page.get_by_text('已儲存，且此版本已由目前的 MCP 子程序確認套用。', exact=True)).to_be_visible()
        expect(self.gateway()).to_have_value('http://unsaved.invalid')
        self.assertEqual(self.requests, [{'path': '/api/settings/mcp/apply', 'revision': '"r1"', 'body': None}])
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))

    def test_conflict_keeps_original_form_revision_and_draft_until_explicit_reload(self):
        self.page.goto(self.origin + '/config', wait_until='networkidle')
        self.gateway().fill('http://draft.invalid')
        self.revision = 'r2'
        self.connection['gateway_url'] = 'http://other-editor.invalid'
        self.page.get_by_role('button', name='重新讀取狀態', exact=True).click()
        expect(self.gateway()).to_have_value('http://draft.invalid')
        self.page.get_by_role('button', name='Save & Restart MCP', exact=True).click()
        expect(self.page.get_by_role('button', name='重新載入（捨棄草稿）', exact=True)).to_be_visible()
        expect(self.gateway()).to_have_value('http://draft.invalid')
        expect(self.page.get_by_role('button', name='Save & Restart MCP', exact=True)).to_be_disabled()
        self.assertEqual(self.requests[-1]['revision'], '"r1"')
        self.assertEqual(len(self.requests), 1)
        self.page.get_by_role('button', name='重新載入（捨棄草稿）', exact=True).click()
        expect(self.gateway()).to_have_value('http://other-editor.invalid')
        self.gateway().fill('http://reviewed.invalid')
        self.page.get_by_role('button', name='Save & Restart MCP', exact=True).click()
        expect(self.page.get_by_text('設定已儲存。', exact=True)).to_be_visible()
        self.assertEqual(self.requests[-1]['revision'], '"r2"')

    def test_delayed_save_response_does_not_replace_newer_input(self):
        self.delay_save = True
        self.page.goto(self.origin + '/config', wait_until='networkidle')
        self.gateway().fill('http://submitted.invalid')
        self.page.get_by_role('button', name='Save & Restart MCP', exact=True).click()
        expect(self.page.get_by_role('button', name='Save & Restart MCP', exact=True)).to_be_disabled()
        self.gateway().fill('http://newer-draft.invalid')
        self.assertIsNotNone(self.pending)
        self.revision = 'r2'
        self.connection['gateway_url'] = 'http://submitted.invalid'
        self.pending.fulfill(json={'status': 'partial', 'saved': True, 'apply_state': 'failed', 'configuration': self.state()})
        expect(self.page.get_by_role('button', name='Save & Restart MCP', exact=True)).to_be_enabled()
        expect(self.gateway()).to_have_value('http://newer-draft.invalid')
        expect(self.page.get_by_text('有尚未儲存的變更', exact=True)).to_be_visible()
        self.assertEqual(len(self.requests), 1)

    def test_settings_refetch_preserves_other_dirty_section_and_its_revision(self):
        self.page.goto(self.origin + '/settings', wait_until='networkidle')
        self.page.locator('input[type="number"]').fill('120')
        self.page.locator('input[value="1"]').fill('2')
        self.page.get_by_role('button', name='Save MCP Config', exact=True).click()
        expect(self.page.get_by_text('設定已儲存。', exact=True)).to_be_visible()
        expect(self.page.locator('input[type="number"]')).to_have_value('120')
        self.page.get_by_role('button', name='Save Proxy Config', exact=True).click()
        expect(self.page.get_by_role('button', name='重新載入設定（捨棄草稿）', exact=True)).to_be_visible()
        expect(self.page.locator('input[type="number"]')).to_have_value('120')
        self.assertEqual(self.requests[-1]['revision'], '"r1"')
        self.assertEqual(self.timeout, 86400)


if __name__ == '__main__':
    unittest.main()
