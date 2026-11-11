"""真實 n8n React 建置搭配隔離 API fixture；不連線部署或啟動子行程。

先 npm run build -w apps/n8n/frontend，再以具 Playwright 的 Python 執行。
PYTHONPATH 需包含本 worktree 的 packages/mcp-admin-core 與 apps/n8n。
"""
import functools
import http.server
import json
import os
from pathlib import Path
import threading
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
from mcp_admin_core.capabilities import tool_snapshot
from mcp_admin_core.operation_policy import disabled_operation_calls
from n8n_mcp_admin import tool_registry

ROOT = Path(__file__).resolve().parents[3]
UPDATER = 'n8n_update_partial_workflow'
VERSIONS = 'n8n_workflow_versions'
EXPOSURES = [VERSIONS, 'n8n_test_workflow']


class StaticApp(http.server.SimpleHTTPRequestHandler):
    """只提供目前 worktree 的前端靜態建置。"""
    def do_GET(self):
        """支援 SPA 的兩個待測入口。"""
        if urlsplit(self.path).path in ('/tools', '/permissions'):
            self.path = '/index.html'
        super().do_GET()

    def log_message(self, *args):
        """省略本地 HTTP 存取日誌。"""


class ConditionalWritesBrowser(unittest.TestCase):
    """驗證顯示、儲存回讀與既有 guard 的政策一致。"""
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
            functools.partial(StaticApp, directory=str(ROOT / 'apps/n8n/frontend/dist')))
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
        self.policy = {'disabled': [UPDATER], 'disabled_operations': {'retired_tool': ['old'], VERSIONS: ['rollback']}}
        self.writes = []
        self.fail_save = False
        self.revision = 1
        self.errors = []
        self.context = self.browser.new_context()
        self.context.add_init_script("localStorage.setItem('mcp-admin-token', 'fixture-admin')")
        self.context.route('**/api/**', self.api)
        self.page = self.context.new_page()
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def api(self, route):
        """描述採正式 registry；儲存只改本測試的記憶體政策。"""
        path = urlsplit(route.request.url).path
        if route.request.method == 'PUT':
            payload = route.request.post_data_json
            self.writes.append({'path': path, 'payload': payload, 'revision': route.request.headers.get('if-match')})
            if self.fail_save:
                route.fulfill(status=503, json={'detail': '測試儲存失敗'})
                return
            assert route.request.headers['if-match'] == f'"r{self.revision}"'
            if path == '/api/tools':
                self.policy['disabled'] = payload['disabled_tools']
            elif path == '/api/tools/operations':
                self.policy['disabled_operations'] = payload['disabled_operations']
            else:
                raise AssertionError(f'未預期的寫入：{path}')
            self.revision += 1
            route.fulfill(json={'saved': True, 'apply_state': 'proxy_policy_applied', 'message': '測試政策已儲存並立即套用'})
            return
        snapshot = tool_snapshot(tool_registry, {'tools': self.policy}, None, 'child_stopped')
        if path == '/api/tools':
            items = [{**item, 'enabled': item['name'] not in self.policy['disabled'],
                      'operations': [op['name'] for op in item['operations']]} for item in snapshot['items']]
            body = {'tools': items, 'disabled_tools': self.policy['disabled'],
                    'disabled_operations': self.policy['disabled_operations'], 'configuration': {'revision': f'r{self.revision}'}}
        elif path == '/api/capabilities':
            body = {'schema_version': 1, 'product': {'id': 'n8n'}, 'capabilities': {'operation_policy': True}, 'tools': snapshot}
        else:
            body = {}
        route.fulfill(json=body)

    def condition(self, name=VERSIONS):
        """取得工具對應的可見條件說明。"""
        return self.page.get_by_label(f'{name} 條件寫入', exact=True)

    def refused(self, name, flag=True):
        """使用正式 guard 確認 fixture 儲存後的政策效果。"""
        call = {'method': 'tools/call', 'params': {'name': name, 'arguments': {
            'mode': 'list', 'method': 'prepare', 'exposeToMcp': flag}}}
        return bool(disabled_operation_calls(call, self.policy, tool_registry))

    def test_工具啟閉立即回讀且保留操作限制(self):
        self.page.goto(self.origin + '/tools')
        for name in EXPOSURES:
            expect(self.condition(name)).to_contain_text('相依權限：已儲存政策禁止')
            self.assertTrue(self.refused(name))
            self.assertFalse(self.refused(name, False))
        before = json.loads(json.dumps(self.policy['disabled_operations']))
        self.page.get_by_role('button', name=f'Enable {UPDATER}', exact=True).click()
        expect(self.condition()).to_contain_text('相依權限：已儲存政策允許')
        for name in EXPOSURES:
            self.assertFalse(self.refused(name, ' true '))
        self.assertEqual(self.policy['disabled_operations'], before)
        self.page.get_by_role('button', name=f'Disable {UPDATER}', exact=True).click()
        expect(self.condition()).to_contain_text('相依權限：已儲存政策禁止')
        self.assertTrue(self.refused(VERSIONS))
        self.assertEqual(len(self.writes), 2)

    def test_expose草稿儲存與失敗不改相依授權(self):
        self.policy['disabled'] = []
        self.page.goto(self.origin + '/permissions')
        checkbox = self.page.get_by_role('checkbox', name=f'Allow {VERSIONS}: expose', exact=True)
        expect(self.condition()).to_contain_text('相依權限：已儲存政策允許')
        checkbox.uncheck()
        self.assertFalse(self.refused(VERSIONS))
        self.assertEqual(self.writes, [])
        self.page.get_by_role('button', name='Save permissions', exact=True).click()
        expect(self.page.get_by_text('測試政策已儲存並立即套用', exact=True)).to_be_visible()
        self.assertTrue(self.refused(VERSIONS))
        self.assertFalse(self.refused(VERSIONS, False))
        self.assertFalse(self.refused('n8n_test_workflow'))
        self.assertEqual(self.policy['disabled_operations'], {VERSIONS: ['expose', 'rollback'], 'retired_tool': ['old']})
        checkbox.check()
        self.fail_save = True
        self.page.get_by_role('button', name='Save permissions', exact=True).click()
        expect(self.page.get_by_role('alert')).to_have_text('測試儲存失敗')
        expect(checkbox).to_be_checked()
        expect(self.page.get_by_text('Unsaved changes', exact=True)).to_be_visible()
        self.assertTrue(self.refused(VERSIONS))
        self.fail_save = False
        self.page.get_by_role('button', name='Save permissions', exact=True).click()
        expect(self.page.get_by_text('測試政策已儲存並立即套用', exact=True)).to_be_visible()
        self.assertFalse(self.refused(VERSIONS))
        self.assertEqual(self.policy['disabled_operations'], {VERSIONS: ['rollback'], 'retired_tool': ['old']})

    def test_兩個入口與窄螢幕顯示條件說明(self):
        output = Path(os.environ.get('CONDITIONAL_EVIDENCE', '/tmp/paas-conditional-ui'))
        output.mkdir(parents=True, exist_ok=True)
        for width in (320, 360, 1440):
            self.page.set_viewport_size({'width': width, 'height': 960})
            for path in ('tools', 'permissions'):
                self.page.goto(self.origin + '/' + path)
                search = 'Search tools and input schemas' if path == 'tools' else 'Search operation controls'
                self.page.get_by_label(search, exact=True).fill(VERSIONS)
                expect(self.condition()).to_be_visible()
                expect(self.condition()).to_contain_text('將工作流程開放給 MCP 存取')
                expect(self.condition()).to_contain_text('false 或省略')
                expect(self.condition()).to_contain_text('原工具與操作權限仍須允許')
                self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), width)
                self.page.screenshot(path=str(output / f'paas-{path}-{width}.png'), full_page=True)
        self.assertEqual(self.writes, [])


if __name__ == '__main__':
    unittest.main()
