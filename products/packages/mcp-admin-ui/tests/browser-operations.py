"""操作編輯器的互動回歸；Vite 單一 app、API fixtures，不建置九產品、不連部署。

uv run --no-project --with playwright python packages/mcp-admin-ui/tests/browser-operations.py
"""
import json
import os
import socket
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(os.environ.get('OPERATIONS_EVIDENCE', '/tmp/paas-operation-browser'))
OUT.mkdir(parents=True, exist_ok=True)
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
log = (OUT / 'vite.log').open('w')
server = subprocess.Popen(['node', str(ROOT / 'node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', str(port), '--strictPort'], cwd=ROOT / 'apps/hermes/frontend', stdout=log, stderr=log)
base = f'http://127.0.0.1:{port}'
#: 產品覆核過的工具說明（/api/tools 的 operation_note；例如 Odoo 的審批工具擋不住 execute_method），只顯示在有的工具旁邊。
NOTE = 'Fixture note: these restrictions do not limit execute_method while Allow Direct Writes is on.'
state = {'unsupported': False, 'failure': False, 'pending': False, 'denied': {'hermes_session': ['delete'], 'retired_tool': ['remove'], 'hermes_model': ['list']}, 'writes': []}


def api(route):
    path = route.request.url.split('/api', 1)[1].split('?', 1)[0]
    tools = [
        {'name': 'hermes_session', 'description': 'Manage sessions', 'category': 'Read', 'enabled': True,
         'operations': ['list', 'get', 'delete'], 'operation_policy_supported': True,
         'operation_parameter': 'action', 'operation_default': 'list', 'operation_selector': 'value',
         'operation_note': NOTE},
        {'name': 'hermes_inspect', 'description': 'Inspect selected sections', 'category': 'Read', 'enabled': True,
         'operations': ['all', 'config', 'status'], 'operation_policy_supported': True,
         'operation_parameter': 'target', 'operation_default': 'all', 'operation_selector': 'value',
         'operation_dependencies': {'all': ['config', 'status']}},
        {'name': 'hermes_model', 'description': 'Model configuration', 'category': 'Write', 'enabled': True,
         'operations': ['info', 'set', 'list_providers'], 'operation_policy_supported': True,
         'operation_parameter': 'action', 'operation_default': 'info', 'operation_selector': 'value',
         'operation_legacy_denies': {'list': ['list_providers']}},
        {'name': 'enum_only', 'description': 'Unreviewed enum must not become policy', 'category': 'Runtime', 'enabled': True,
         'operations': ['red', 'blue'], 'inputSchema': {'properties': {'color': {'enum': ['red', 'blue']}}}},
    ]
    if state['unsupported']:
        tools = [tools[-1]]
    if state.get('canonical'):
        tools = [{'name': 'n8n_executions', 'description': 'Execution actions', 'category': 'Read',
                  'enabled': True, 'operations': ['list', 'get', 'delete'],
                  'operation_policy_supported': True, 'operation_parameter': 'action',
                  'operation_default': 'list', 'operation_selector': 'value'}]
    if path == '/tools/operations':
        assert route.request.method == 'PUT'
        payload = route.request.post_data_json
        state['writes'].append(payload)
        if state['failure']:
            route.fulfill(status=503, content_type='application/json', body=json.dumps({'detail': 'Configuration state unavailable'}))
            return
        state['denied'] = payload['disabled_operations']
        body = {'status': 'partial' if state['pending'] else 'ok', 'saved': True,
                'apply_state': 'proxy_policy_applied', 'child_apply_state': 'pending' if state['pending'] else 'applied',
                'message': 'Restrictions enforced; child synchronization pending.' if state['pending'] else 'Operation policy saved and enforced.',
                'disabled_operations': state['denied']}
    elif path == '/tools':
        body = {'total': len(tools), 'enabled': len(tools), 'disabled': 0, 'disabled_tools': [],
                'disabled_operations': state['denied'], 'tools': tools, 'operation_policy_supported': not state['unsupported']}
        if state.get('canonical'):
            body['editable_disabled_operations'] = {
                name: [value.strip().lower() for value in values] if name == 'n8n_executions' else values
                for name, values in state['denied'].items()}
    elif path == '/capabilities':
        body = {'schema_version': 1, 'product': {'id': 'hermes'}, 'capabilities': {'operation_policy': not state['unsupported']},
                'tools': {'items': [], 'disabled_operations': state['denied']}, 'health': {}}
    else:
        body = {}
    route.fulfill(status=200, content_type='application/json', body=json.dumps(body))


try:
    deadline = time.monotonic() + 20
    while True:
        try:
            socket.create_connection(('127.0.0.1', port), timeout=.2).close()
            break
        except OSError:
            if server.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError((OUT / 'vite.log').read_text())
            time.sleep(.05)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        context.add_init_script("localStorage.setItem('mcp-admin-token', 'fixture')")
        context.route('**/api/**', api)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        for width in [1280, 360, 320]:
            page.set_viewport_size({'width': width, 'height': 950})
            page.goto(base + '/permissions', wait_until='networkidle')
            expect(page.get_by_role('heading', name='Operation permissions', exact=True)).to_be_visible()
            expect(page.get_by_role('checkbox', name='Allow hermes_session: delete', exact=True)).not_to_be_checked()
            expect(page.get_by_role('checkbox', name='Allow hermes_session: list', exact=True)).to_be_checked()
            expect(page.get_by_role('checkbox', name='Allow hermes_model: list_providers', exact=True)).not_to_be_checked()
            assert page.get_by_role('checkbox', name='Allow enum_only: red', exact=True).count() == 0
            expect(page.get_by_label('Retained saved restrictions')).to_contain_text('retired_tool')
            expect(page.get_by_role('note', name='hermes_session 說明', exact=True)).to_have_text(NOTE)
            assert page.get_by_role('note', name='hermes_model 說明', exact=True).count() == 0
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), width
            assert page.evaluate("getComputedStyle(document.body).fontFamily.includes('Outfit')")
            assert page.locator('link[href*="@mdi/font@7.4.47"]').count() == 1
            page.screenshot(path=str(OUT / f'permissions-{width}.png'), full_page=True)
        # A read rule can be disabled without re-enabling saved delete/unknown denies.
        page.get_by_role('checkbox', name='Allow hermes_session: get', exact=True).uncheck()
        page.get_by_role('button', name='Save permissions', exact=True).click()
        expect(page.get_by_text('Operation policy saved and enforced.', exact=True)).to_be_visible()
        assert state['writes'][-1] == {'disabled_operations': {'hermes_session': ['delete', 'get'], 'retired_tool': ['remove'], 'hermes_model': ['list_providers']}}
        page.reload(wait_until='networkidle')
        expect(page.get_by_role('checkbox', name='Allow hermes_session: get', exact=True)).not_to_be_checked()
        # Re-enable only the selected operation; reset doesn't send writes.
        page.get_by_role('checkbox', name='Allow hermes_session: delete', exact=True).check()
        page.get_by_role('button', name='Reset', exact=True).click()
        expect(page.get_by_role('checkbox', name='Allow hermes_session: delete', exact=True)).not_to_be_checked()
        assert len(state['writes']) == 1
        page.get_by_role('checkbox', name='Allow hermes_session: delete', exact=True).check()
        state['pending'] = True
        page.get_by_role('button', name='Save permissions', exact=True).click()
        expect(page.get_by_text('Restrictions enforced; child synchronization pending.', exact=True)).to_be_visible()
        assert state['denied']['hermes_session'] == ['get']
        # Rejecting a save retains the draft and never reports success.
        state['failure'] = True
        page.get_by_role('checkbox', name='Allow hermes_session: get', exact=True).check()
        page.get_by_role('button', name='Save permissions', exact=True).click()
        expect(page.get_by_role('alert')).to_have_text('Configuration state unavailable')
        expect(page.get_by_text('Unsaved changes', exact=True)).to_be_visible()
        assert state['denied']['hermes_session'] == ['get']
        page.get_by_role('button', name='Reset', exact=True).click()
        state['failure'] = False
        page.get_by_role('checkbox', name='Allow hermes_inspect: config', exact=True).uncheck()
        expect(page.get_by_text('all · Blocked by: config', exact=True)).to_be_visible()
        # Tool Manager exposes the supported operation editor.
        page.goto(base + '/tools', wait_until='networkidle')
        page.get_by_role('link', name='Edit operation permissions', exact=True).click()
        expect(page.get_by_role('heading', name='Operation permissions', exact=True)).to_be_visible()
        # Unsupported metadata still shows saved restrictions without editing them.
        state['unsupported'] = True
        page.reload(wait_until='networkidle')
        expect(page.get_by_text('This product has no reviewed operation controls.', exact=False)).to_be_visible()
        assert page.get_by_role('checkbox').count() == 0
        assert page.get_by_role('button', name='Save permissions', exact=True).count() == 0
        expect(page.get_by_label('Retained saved restrictions')).to_contain_text('retired_tool')
        # A legacy padded rule is displayed and removed using server-normalized
        # editable labels, while unknown saved restrictions remain untouched.
        state.update(unsupported=False, canonical=True, pending=False,
                     denied={'n8n_executions': [' DELETE '], 'retired_tool': [' RAW ']})
        page.reload(wait_until='networkidle')
        checkbox = page.get_by_role('checkbox', name='Allow n8n_executions: delete', exact=True)
        expect(checkbox).not_to_be_checked()
        checkbox.check()
        page.get_by_role('button', name='Save permissions', exact=True).click()
        expect(page.get_by_text('Operation policy saved and enforced.', exact=True)).to_be_visible()
        assert state['denied'] == {'retired_tool': [' RAW ']}
        page.reload(wait_until='networkidle')
        expect(page.get_by_role('checkbox', name='Allow n8n_executions: delete', exact=True)).to_be_checked()
        assert not errors, errors
        results = {'viewports': [1280, 360, 320], 'errors': errors,
                   'checks': ['operation note shown only where reviewed', 'saved denies', 'legacy aliases mapped', 'reviewed metadata only', 'save/readback', 'targeted re-enable', 'reset', 'retained unknown denies', 'partial child sync', 'failed save draft', 'compound inspect target', 'Tool Manager navigation', 'unsupported read-only', 'canonical padded legacy deny can be re-enabled']}
        (OUT / 'results.json').write_text(json.dumps(results, indent=2))
        print(f'PASS: 3 viewports, {len(results["checks"])} interaction checks, no browser errors. Evidence: {OUT}')
        browser.close()
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=5)
    log.close()
