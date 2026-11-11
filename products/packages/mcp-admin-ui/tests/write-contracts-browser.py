"""Build the n8n UI first. Actual Chromium forms, controlled loopback admin.

API durability/auth are tested separately through the real ASGI routes. This
fixture deliberately simulates a lost reply and server/client clock skew.
"""
import argparse
import functools
import hashlib
import http.server
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect


class Static(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path in ('/settings', '/tokens'):
            self.path = '/index.html'
        super().do_GET()

    def log_message(self, *args):
        pass


def verify(browser, dist, restart_status='ok'):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Static, directory=str(dist)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = 'http://127.0.0.1:' + str(server.server_port)
    state = {'revision': 1, 'timeout': 86400, 'writes': [], 'restart_effects': 0, 'receipts': {}, 'queries': 0}
    context = browser.new_context()
    context.add_init_script("localStorage.setItem('mcp-admin-token','synthetic-only'); Date.now = () => 1999999999999;")
    def configuration():
        return {'revision': 'r' + str(state['revision']), 'apply_state': 'applied', 'server_time_ms': int(time.time() * 1000)}
    def api(route):
        req = route.request
        path = urlsplit(req.url).path
        if path.startswith('/api/operations/'):
            state['queries'] += 1
            key = path.rsplit('/', 1)[1]
            route.fulfill(json=state['receipts'][key]); return
        if req.method in ('PUT', 'POST'):
            revision = req.headers.get('if-match')
            key = req.headers.get('idempotency-key')
            state['writes'].append({'path': path, 'revision': revision, 'key': key})
            if key and key in state['receipts']:
                receipt = state['receipts'][key]
                route.fulfill(json={**receipt['result'], 'operation': {**receipt['operation'], 'replayed': True}, 'configuration': configuration()}); return
            if revision != '"r' + str(state['revision']) + '"':
                route.fulfill(status=409, json={'detail': {'code': 'config_conflict', 'message': 'synthetic conflict'}}); return
            if path == '/api/settings/proxy':
                state['timeout'] = req.post_data_json['timeout']; state['revision'] += 1
            elif path == '/api/settings/admin_password':
                state['revision'] += 1  # fake fixture does not persist the body
            elif path in ('/api/settings/mcp/restart', '/api/tokens/rotate'):
                assert key and int(key.split('.')[0]) <= int(time.time() * 1000)
                assert int(key.split('.')[0]) > int(time.time() * 1000) - 60000
                receipt = {'operation': {'id': key, 'kind': 'mcp.restart' if path.endswith('restart') else 'token.rotate', 'state': 'completed', 'replayed': False}, 'result': {'status': restart_status if path.endswith('restart') else 'ok'}}
                state['receipts'][key] = receipt
                if path.endswith('restart'):
                    state['restart_effects'] += 1
                    route.abort('connectionreset'); return  # attempt completed; its success/error reply was lost
                state['revision'] += 1
                route.fulfill(json={**receipt['result'], 'operation': receipt['operation'], 'configuration': configuration()}); return
            else:
                raise AssertionError('Unexpected mutation')
            route.fulfill(json={'status': 'ok', 'message': 'Saved fixture', 'configuration': configuration()}); return
        if path == '/api/settings':
            body = {'configuration': configuration(), 'mcp_server': {'command': 'synthetic-child', 'env': {}},
                    'editable_env_keys': [], 'proxy': {'timeout': state['timeout']}, 'admin_password_masked': '***',
                    'mcp_auth_token_masked': '****', 'mcp_auth_token_set': True}
        elif path == '/api/settings/mcp/status':
            body = {'running': True, 'configuration': configuration()}
        elif path == '/api/tokens':
            body = {'configuration': configuration(), 'state': 'active', 'token_set': True, 'token_masked': '****', 'history': []}
        else:
            body = {}
        route.fulfill(json=body)
    def routing(route):
        if route.request.url.startswith(origin + '/api/'):
            api(route)
        elif route.request.url.startswith(origin + '/'):
            route.continue_()
        else:
            route.abort()
    context.route('**/*', routing)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(origin + '/settings', wait_until='networkidle')
        password = page.get_by_placeholder('Minimum 4 characters')
        password.fill('synthetic-browser-password')
        page.locator('input[type=number]').last.fill('5000')
        page.get_by_role('button', name='Save Proxy Config', exact=True).click()
        expect(page.get_by_text('Saved fixture', exact=True)).to_be_visible()
        page.get_by_role('button', name='Update Password', exact=True).click()
        expect(page.get_by_role('button', name='重新載入設定（捨棄草稿）')).to_be_visible()
        assert state['writes'][-1]['revision'] == '"r1"' and state['revision'] == 2
        expect(password).to_have_value('synthetic-browser-password')
        page.get_by_role('button', name='重新載入設定（捨棄草稿）').click()
        expect(password).to_have_value('')
        password.fill('another-synthetic-password')
        page.get_by_role('button', name='Update Password', exact=True).click()
        expect(password).to_have_value('')
        assert state['writes'][-1]['revision'] == '"r2"'

        page.reload(wait_until='networkidle')
        page.get_by_role('button', name='Restart', exact=True).click()
        expect(page.get_by_role('button', name='查詢操作狀態')).to_be_visible()
        assert state['restart_effects'] == 1
        original_key = state['writes'][-1]['key']
        page.get_by_role('button', name='查詢操作狀態').click()
        expect(page.get_by_text('操作狀態：completed', exact=False)).to_be_visible()
        assert state['queries'] == 1
        page.reload(wait_until='networkidle')
        page.get_by_role('button', name='Restart', exact=True).click()
        expect(page.get_by_role('button', name='查詢操作狀態')).to_have_count(0)
        assert state['writes'][-1]['key'] == original_key and state['restart_effects'] == 1
        expected_message = 'MCP server restarted' if restart_status == 'ok' else 'Failed to restart MCP server — check server status and logs'
        expect(page.get_by_text(expected_message, exact=True)).to_be_visible()
        if restart_status != 'ok':
            expect(page.get_by_text('MCP server restarted', exact=True)).to_have_count(0)
            assert state['receipts'][original_key]['result'] == {'status': 'error'}

        page.goto(origin + '/tokens', wait_until='networkidle')
        page.get_by_role('button', name='Rotate Token', exact=True).click()
        state['revision'] += 1
        page.get_by_role('button', name='Confirm Rotate', exact=True).click()
        expect(page.get_by_role('button', name='重新載入設定', exact=True)).to_be_visible()
        page.get_by_role('button', name='重新載入設定', exact=True).click()
        page.get_by_role('button', name='Rotate Token', exact=True).click()
        page.get_by_role('button', name='Confirm Rotate', exact=True).click()
        expect(page.get_by_role('button', name='Confirm Rotate', exact=True)).to_have_count(0)
        assert not errors
        assert page.evaluate("Object.values(sessionStorage).join('')").find('synthetic-browser-password') == -1
        return {'passed': True, 'password_revision_frozen_through_other_save': True,
                'conflict_retains_draft_then_explicit_reload_clears': True,
                'restart_lost_reply_query_and_same_id_after_page_reload': True,
                'restart_receipt_status': restart_status, 'restart_replay_message': expected_message,
                'restart_effects': state['restart_effects'], 'token_confirmation_conflict_reload': True,
                'rotate_without_token_result_works': True, 'client_clock_ahead_ignored': True,
                'scope': 'local source-built UI + actual Chromium + controlled fake admin; no published image claim',
                'asset_sha256': {str(p.relative_to(dist)): hashlib.sha256(p.read_bytes()).hexdigest() for p in dist.rglob('*') if p.is_file()}}
    finally:
        context.close(); server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dist', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            proof = verify(browser, args.dist.resolve())
            proof['failed_restart_replay'] = verify(browser, args.dist.resolve(), restart_status='error')
            args.output.write_text(json.dumps(proof, indent=2) + '\n')
        finally:
            browser.close()
