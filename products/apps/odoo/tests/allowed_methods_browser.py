"""Built Odoo UI, loopback static server, in-memory APIs; no live service calls.

python apps/odoo/tests/allowed_methods_browser.py apps/odoo/frontend/dist /tmp/odoo-methods-evidence
"""
import functools
import http.server
import sys
import threading
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

DIST = Path(sys.argv.pop(1)).resolve()
EVIDENCE = Path(sys.argv.pop(1)).resolve()
EVIDENCE.mkdir(parents=True, exist_ok=True)


class StaticApp(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path == '/settings':
            self.path = '/index.html'
        super().do_GET()

    def log_message(self, *args): pass


class MethodsBrowser(unittest.TestCase):
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
        self.methods = []
        self.source = 'legacy_empty'
        self.revision = 'one'
        self.saves = []
        self.fail_apply = False
        self.errors = []
        self.context = self.browser.new_context()
        self.context.add_init_script("localStorage.setItem('mcp-admin-token', 'synthetic-admin');")
        self.context.route('**/api/**', self.api)
        self.page = self.context.new_page()
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.context.close()

    def api(self, route):
        path = urlsplit(route.request.url).path
        configuration = {'revision': self.revision, 'apply_state': 'unknown'}
        if path == '/api/odoo/allowed-methods':
            if route.request.method == 'PUT':
                if route.request.headers.get('if-match') != '"' + self.revision + '"':
                    route.fulfill(status=409, json={'detail': {'code': 'config_conflict', 'message': '設定已更新'}})
                    return
                self.methods = route.request.post_data_json['odoo_allowed_methods']
                self.saves.append(self.methods)
                self.source = 'saved'
                self.revision += 'x'
                body = {'saved': True, 'message': '方法清單已儲存，但子程序未能啟動。' if self.fail_apply else '方法清單已儲存並套用。',
                        'status': 'partial' if self.fail_apply else 'ok', 'configuration': configuration}
            else:
                body = {'odoo_allowed_methods': self.methods, 'odoo_allowed_methods_source': self.source,
                        'configuration': configuration}
        elif path == '/api/settings':
            body = {'mcp_server': {'env': {}}, 'editable_env_keys': [], 'proxy': {}, 'configuration': configuration}
        else:
            body = {'running': False, 'configuration': configuration}
        route.fulfill(json=body)

    def open(self):
        self.page.goto(self.origin + '/settings')
        self.field = self.page.get_by_label('Allowed model.method entries')
        self.save = self.page.get_by_role('button', name='Save reviewed methods', exact=True)
        expect(self.field).to_be_enabled()

    def test_save_reload_clear_and_failed_apply(self):
        self.open()
        expect(self.field).to_have_value('')
        expect(self.save).to_be_disabled()
        self.field.fill('sale.order.action_confirm\nres.partner.get_portal_url')
        self.save.click()
        expect(self.page.get_by_role('status').filter(has_text='方法清單已儲存並套用。')).to_be_visible()
        expect(self.save).to_be_disabled()
        self.page.reload()
        expect(self.field).to_have_value('sale.order.action_confirm\nres.partner.get_portal_url')
        self.fail_apply = True
        self.field.fill('')
        self.save.click()
        expect(self.page.get_by_role('status').filter(has_text='子程序未能啟動')).to_be_visible()
        expect(self.save).to_be_disabled()
        self.assertEqual(self.saves, [['sale.order.action_confirm', 'res.partner.get_portal_url'], []])

    def test_validation_and_conflict_preserve_draft(self):
        self.open()
        for value in ('sale.order.*', 'sale.order._private', ' sale.order.action_confirm',
                      'sale.order.action_confirm\nsale.order.action_confirm',
                      '\n'.join(f'x.method{i}' for i in range(129))):
            self.field.fill(value)
            expect(self.save).to_be_disabled()
            expect(self.field).to_have_attribute('aria-invalid', 'true')
        self.field.fill('sale.order.action_confirm')
        self.revision = 'other-editor'
        self.methods = ['res.partner.copy']
        self.save.click()
        expect(self.page.get_by_role('alert').filter(has_text='草稿仍保留')).to_be_visible()
        expect(self.field).to_have_value('sale.order.action_confirm')
        expect(self.save).to_be_disabled()
        self.page.get_by_role('button', name='重新載入（捨棄草稿）', exact=True).click()
        expect(self.field).to_have_value('res.partner.copy')
        self.assertEqual(self.saves, [])

    def test_inherited_source_and_responsive_editor(self):
        self.source = 'container_environment'
        self.methods = ['sale.order.action_confirm']
        self.open()
        expect(self.page.get_by_text('目前顯示容器環境提供的清單。', exact=False)).to_be_visible()
        for width in (1280, 360, 320):
            self.page.set_viewport_size({'width': width, 'height': 900})
            self.page.wait_for_timeout(300)  # Existing responsive sidebar finishes its transform.
            self.field.scroll_into_view_if_needed()
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
            box = self.field.bounding_box()
            self.assertGreater(box['width'], 100)
            self.assertGreaterEqual(self.save.bounding_box()['height'], 44)
            self.page.screenshot(path=str(EVIDENCE / f'odoo-methods-{width}.png'), full_page=True)
            self.page.locator('section[aria-labelledby="odoo-methods-heading"]').screenshot(
                path=str(EVIDENCE / f'odoo-methods-editor-{width}.png'))


if __name__ == '__main__':
    unittest.main()
