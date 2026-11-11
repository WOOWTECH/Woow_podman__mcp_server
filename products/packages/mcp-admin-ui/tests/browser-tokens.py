"""Shared token UI against isolated API fixtures; no deployed credentials used."""
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[3]
state = {'token': 'fixture-old-shared-token', 'revoked': False, 'history': [], 'fail': False}
class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        root = ROOT / 'apps/hermes/frontend/dist'
        target = root / path.split('?', 1)[0].lstrip('/')
        return str(target if target.is_file() else root/'index.html')
    def log_message(self, *args): pass

def api(route):
    path = route.request.url.split('/api', 1)[1].split('?', 1)[0]
    if path == '/tokens/revoke':
        if state['fail']:
            route.fulfill(status=503, json={'detail':'Fixture write unavailable'}); return
        state.update(token='', revoked=True)
        state['history'].insert(0, {'action':'revoke','rotated_at':'fixture-time','previous_token_masked':'fi***en'})
        body = {'status':'ok', 'token_state':'revoked'}
    elif path == '/tokens/rotate':
        state.update(token='fixture-new-shared-token', revoked=False)
        state['history'].insert(0, {'action':'rotate','rotated_at':'fixture-time','previous_token_masked':''})
        body = {'status':'ok','token_state':'applied','token':state['token']}
    elif path == '/tokens':
        body = {'state':'revoked' if state['revoked'] else 'active','shared':True,'token_set':bool(state['token']), 'history':state['history']}
    elif path == '/settings/mcp_auth_token/reveal': body = {'value': state['token']}
    elif path == '/settings':
        body = {'mcp_auth_token_masked': 'fi***en' if state['token'] else '',
                'mcp_auth_token_set': bool(state['token'])}
    elif path == '/capabilities': body = {'capabilities': {'operation_policy':True, 'revoke_token':True}}
    else: body = {}
    route.fulfill(status=200, json=body)

server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
checks=[]
try:
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
        context=browser.new_context();context.add_init_script("localStorage.setItem('mcp-admin-token','fixture')")
        context.route('**/api/**',api);page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        url=f'http://127.0.0.1:{server.server_port}/tokens'
        for width in [360,1280]:
            page.set_viewport_size({'width':width,'height':950});page.goto(url,wait_until='networkidle')
            expect(page.get_by_text('One shared token is used',exact=False)).to_be_visible()
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        checks.append('shared-token text and responsive layout')
        state['fail']=True
        page.get_by_role('button',name='Revoke Token',exact=True).click()
        page.get_by_role('button',name='Confirm Revoke',exact=True).click()
        expect(page.get_by_text('Fixture write unavailable')).to_be_visible()
        assert state['token']=='fixture-old-shared-token'
        checks.append('failed revoke keeps active state')
        state['fail']=False
        page.get_by_role('button',name='Confirm Revoke',exact=True).click()
        expect(page.get_by_text('Revoked — all clients blocked')).to_be_visible()
        expect(page.get_by_role('button',name='Copy command',exact=True)).to_be_disabled()
        expect(page.get_by_role('button',name='Copy token',exact=True)).to_be_disabled()
        page.reload(wait_until='networkidle')
        expect(page.get_by_text('Revoked — all clients blocked')).to_be_visible()
        checks.append('revoke clears copy controls and survives reload')
        page.get_by_role('button',name='Rotate Token',exact=True).click()
        page.get_by_role('button',name='Confirm Rotate',exact=True).click()
        expect(page.get_by_role('button',name='Copy token',exact=True)).to_be_enabled()
        page.clock.install()
        page.get_by_role('button',name='Show token',exact=True).click()
        expect(page.get_by_text('fixture-new-shared-token',exact=True)).to_be_visible()
        assert 'fixture-old-shared-token' not in page.locator('body').inner_text()
        checks.append('explicit reissue replaces shared token and enables connection command')
        page.evaluate("window.dispatchEvent(new Event('blur'))")
        expect(page.get_by_text('fixture-new-shared-token',exact=True)).not_to_be_visible()
        assert 'private_fixture-new-shared-token' not in page.locator('body').inner_text()
        checks.append('window blur hides revealed token and connection command')
        page.get_by_role('button',name='Show token',exact=True).click()
        expect(page.get_by_text('fixture-new-shared-token',exact=True)).to_be_visible()
        page.clock.fast_forward(60001)
        expect(page.get_by_text('fixture-new-shared-token',exact=True)).not_to_be_visible()
        assert 'private_fixture-new-shared-token' not in page.locator('body').inner_text()
        checks.append('reveal expires after 60 seconds')
        # 第三輪 G4：Tokens 頁直接看得到完整 MCP 網址，與 token 同一個揭露狀態（60 秒／離開視窗就隱藏）
        origin=f'http://127.0.0.1:{server.server_port}'
        mcp_url=page.get_by_test_id('mcp-url')
        expect(mcp_url).to_have_text(f'{origin}/private_<TOKEN>/mcp')
        page.get_by_role('button',name='Show MCP URL',exact=True).click()
        expect(mcp_url).to_have_text(f'{origin}/private_fixture-new-shared-token/mcp')
        expect(page.get_by_text('fixture-new-shared-token',exact=True)).to_be_visible()  # 同一個揭露：token 也顯示
        checks.append('full MCP URL is shown on reveal')
        page.evaluate("window.dispatchEvent(new Event('blur'))")
        expect(mcp_url).to_have_text(f'{origin}/private_<TOKEN>/mcp')
        assert 'private_fixture-new-shared-token' not in page.locator('body').inner_text()
        checks.append('window blur hides the MCP URL')
        page.get_by_role('button',name='Show MCP URL',exact=True).click()
        expect(mcp_url).to_have_text(f'{origin}/private_fixture-new-shared-token/mcp')
        page.clock.fast_forward(60001)
        expect(mcp_url).to_have_text(f'{origin}/private_<TOKEN>/mcp')
        assert 'private_fixture-new-shared-token' not in page.locator('body').inner_text()
        checks.append('MCP URL reveal expires after 60 seconds')
        page.get_by_role('button',name='Copy MCP URL',exact=True).click()
        expect(mcp_url).to_have_text(f'{origin}/private_<TOKEN>/mcp')  # 複製不會順便把網址留在畫面上
        checks.append('copying the MCP URL does not reveal it')
        assert not errors,errors
        print(json.dumps({'checks':checks,'page_errors':errors}));browser.close()
finally:
    server.shutdown()
