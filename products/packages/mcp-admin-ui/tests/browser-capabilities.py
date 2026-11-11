"""已建置九產品的瀏覽器驗證；API 使用隔離 fixture，不接觸部署設定。

Run: uv run --no-project --with playwright python packages/mcp-admin-ui/tests/browser-capabilities.py
"""
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[3]
OUT = Path('/tmp/paas-parity-browser')
OUT.mkdir(exist_ok=True)
APPS = ['hermes', 'k8s', 'litellm', 'n8n', 'nextcloud', 'odoo', 'odoo-manage', 'opendesign', 'vk']
state = {'product': APPS[0], 'unknown': False, 'fixed': False, 'disabled': ['disabled', 'absent_saved_deny']}

class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        root = ROOT / 'apps' / state['product'] / 'frontend' / 'dist'
        file = root / path.split('?', 1)[0].lstrip('/')
        return str(file if file.is_file() else root / 'index.html')
    def log_message(self, *args):
        pass

server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{server.server_port}'
name = 'runtime_only_long_tool_name_for_mobile_layout_validation'
schema = {'type': 'object', 'properties': {'resource_selector': {'type': 'string', 'description': 'x' * 180}}}

def api(route):
    path = route.request.url.split('/api', 1)[1].split('?', 1)[0]
    if path == '/capabilities':
        items = [{'name': n, 'category': 'Runtime tools' if n == name else 'Catalog', 'description': 'Tool description stays readable on small screens.',
                  'policy_enabled': n not in state['disabled'], 'effective': None if state['unknown'] else n not in state['disabled'],
                  'runtime_only': n == name, 'inputSchema': schema, **({'reason': 'policy_disabled'} if n in state['disabled'] else {})}
                 for n in ['listed', 'disabled', name]]
        items[0]['operations'] = [{'name': 'get', 'write': False, 'policy_enabled': True}, {'name': 'delete', 'write': True, 'policy_enabled': False}]
        body = {'schema_version': 1, 'product': {'id': state['product'], 'version': 'fixture'}, 'deployment': {'kind': 'paas'},
                'capabilities': {'operation_policy': state['product'] == 'n8n', 'logs': True},
                'backend': {'mode': 'fixed' if state['fixed'] else 'manual', 'can_edit': not state['fixed'], 'can_test': True},
                'health': {'child': 'running', 'backend': 'unknown', 'public_endpoint': 'unverified'},
                'tools': {'catalog_count': 3, 'status': 'unknown' if state['unknown'] else 'observed',
                          'effective_count': None if state['unknown'] else sum(i['effective'] for i in items), 'items': items}}
    elif path == '/tools':
        if route.request.method == 'PUT':
            state['disabled'] = route.request.post_data_json['disabled_tools']
            body = {'status': 'ok', 'message': 'Tool policy saved.'}
        else:
            body = {'disabled_tools': state['disabled'], 'tools': [{'name': n, 'category': 'Catalog', 'enabled': n not in state['disabled']} for n in ['listed', 'disabled']]}
    elif path == '/health':
        body = {'mcp_server': {'healthy': True, 'process': 'fixture'}, 'target_app': {'healthy': False}, 'proxy': {'healthy': True}}
    elif path == '/config':
        body = {'permissions': {}} if state['product'] == 'n8n' else {}
    elif path == '/settings':
        body = {'mcp_server': {'env': {'OPTIONAL': '1'}}, 'editable_env_keys': None}
    elif path == '/settings/mcp/status':
        body = {'running': True}
    else:
        body = {}
    route.fulfill(status=200, content_type='application/json', body=json.dumps(body))

results = []
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        context.add_init_script("localStorage.setItem('mcp-admin-token', 'fixture')")
        context.route('**/api/**', api)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        for app in APPS:
            state['product'] = app
            for width in [1280, 360, 320]:
                page.set_viewport_size({'width': width, 'height': 900})
                page.goto(base + '/tools', wait_until='networkidle')
                expect(page.get_by_text('2 effective', exact=False)).to_be_visible()
                expect(page.get_by_text('Operations: get, delete (disabled)', exact=False)).to_be_visible()
                assert page.locator('nav a[href="/permissions"]').count() == (1 if app == 'n8n' else 0)
                assert page.locator('nav a[href="/logs"]').count() == 1
                assert page.locator('nav a[href="/settings"]').count() == 1
                page.get_by_role('textbox', name='Search tools and input schemas').fill('resource_selector')
                expect(page.get_by_text(name, exact=True)).to_be_visible()
                page.locator('details').last.locator('summary').click()
                expect(page.get_by_label(name + ' input schema')).to_be_visible()
                overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth')
                assert not overflow, (app, width, 'overflow')
                assert page.evaluate("getComputedStyle(document.body).fontFamily.includes('Outfit')")
                assert page.locator('link[href*="@mdi/font@7.4.47"]').count() == 1
                assert page.evaluate("[...document.styleSheets].some(s => s.href && s.href.includes('materialdesignicons'))")
                results.append({'app': app, 'width': width, 'overflow': overflow})
                if app == 'n8n' and width in (1280, 320):
                    page.screenshot(path=str(OUT / f'tools-{width}.png'), full_page=True)
            page.goto(base + '/', wait_until='networkidle')
            expect(page.get_by_role('region', name='MCP availability')).to_be_visible()
            if app == 'vk':
                page.screenshot(path=str(OUT / 'dashboard-320.png'), full_page=True)
        # Runtime-only control retains denies for tools absent from the visible catalog.
        page.goto(base + '/tools', wait_until='networkidle')
        page.get_by_role('button', name='Disable ' + name, exact=True).click()
        expect(page.get_by_role('button', name='Enable ' + name, exact=True)).to_be_visible()
        assert set(state['disabled']) == {'disabled', 'absent_saved_deny', name}
        state['unknown'] = True
        page.get_by_role('button', name='Refresh availability', exact=True).click()
        expect(page.get_by_text('Unknown effective', exact=False)).to_be_visible()
        state['fixed'] = True
        page.goto(base + '/config', wait_until='networkidle')
        expect(page.get_by_role('heading', name='Fixed backend')).to_be_visible()
        assert page.get_by_role('button', name='Save', exact=True).count() == 0
        page.goto(base + '/permissions', wait_until='networkidle')
        expect(page.get_by_role('heading', name='Operation permissions')).to_be_visible()
        assert page.locator('textarea').count() == 0
        assert not errors, errors
        (OUT / 'results.json').write_text(json.dumps({'viewports': results, 'errors': errors, 'interactive_checks': 'passed'}, indent=2))
        print(f'PASS: {len(results)} tool viewports, nine dashboards, runtime toggle, unknown count, fixed backend, unsupported permissions. Evidence: {OUT}')
        browser.close()
finally:
    server.shutdown()
