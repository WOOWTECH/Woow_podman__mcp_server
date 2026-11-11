"""Odoo/LiteLLM connection forms, local mocked API; real API contract tested separately.

Build both frontends first. No customer services, provider calls or child starts.
"""
import argparse
import functools
import http.server
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect


class Static(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path in ('/config', '/login'):
            self.path = '/index.html'
        super().do_GET()
    def log_message(self, *args): pass


def verify(browser, root, product, output, baseline=False):
    key = 'odoo_password' if product == 'odoo' else 'master_key'
    label = 'API Key' if product == 'odoo' else 'LiteLLM 金鑰'
    public = dict(odoo_url='https://odoo.example.test', odoo_db='testdb', odoo_username='testuser') if product == 'odoo' else dict(base_url='https://llm.example.test')
    secret = 'SYNTHETIC_LOCAL_OLD_KEY'
    state = dict(revision=1, secret=secret, writes=[], effects=0, lose_reply=False, can_edit=True, revision_visible=True, public=public.copy())
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Static, directory=str(root/'apps'/product/'frontend/dist')))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    context = browser.new_context(viewport={'width':360,'height':900}, service_workers='block')
    context.add_init_script("localStorage.setItem('mcp-admin-token','SYNTHETIC_LOCAL_AUTH')")
    def configuration(): return dict(revision='r'+str(state['revision']) if state['revision_visible'] else None,apply_state='applied')
    def routing(route):
        req=route.request; path=urlsplit(req.url).path
        if not req.url.startswith(origin+'/'): route.abort();return
        if not path.startswith('/api/'):route.continue_();return
        if req.method == 'GET':
            if path == '/api/config': body={**state['public'],key+'_masked':'********' if state['secret'] else '(not set)','configuration':configuration()}
            elif path == '/api/settings/mcp/status':body={'running':True,'configuration':configuration()}
            elif path == '/api/capabilities':body={'backend':{'can_edit':state['can_edit'],'can_test':False}}
            else: body={}
            route.fulfill(json=body);return
        assert req.method=='PUT' and path in ('/api/settings/connection','/api/config/connection'), (req.method,path)
        data=req.post_data_json; state['writes'].append({'path':path,'revision':req.headers.get('if-match'),'data':data})
        if req.headers.get('if-match') != '"r'+str(state['revision'])+'"':
            route.fulfill(status=409,json={'detail':{'code':'config_conflict','message':'synthetic conflict'}});return
        if path == '/api/settings/connection':
            assert data == {key:None},data
            state['secret']=None
        else:
            if data.get(key):state['secret']=data[key]
            state['public'].update({k:v for k,v in data.items() if k!=key})
        state['revision']+=1;state['effects']+=1
        if state['lose_reply']:route.abort('connectionreset');return
        route.fulfill(json={'saved':True,'status':'ok','apply_state':'applied','message':'SYNTHETIC_SAVED','configuration':configuration()})
    context.route('**/*',routing);page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(origin+'/config',wait_until='networkidle')
        clear=page.get_by_role('button',name=f'清除已儲存的{label}',exact=True)
        expect(clear).to_be_visible(timeout=2000)  # red on the exact 3c93 UI
        if baseline:return {'product':product,'unexpected_baseline_clear':True}
        password=page.locator('input[type=password]');save=page.get_by_role('button',name='Save',exact=True)
        state['revision_visible']=False;page.reload(wait_until='networkidle');expect(clear).to_be_disabled()
        state['revision_visible']=True;state['can_edit']=None;page.reload(wait_until='networkidle');expect(clear).to_be_disabled()
        state['can_edit']=True;page.reload(wait_until='networkidle');expect(clear).to_be_enabled()
        expect(password).to_have_value('');assert secret not in page.content()
        save.click();expect(page.get_by_text('SYNTHETIC_SAVED',exact=True)).to_be_visible();assert state['secret']==secret
        password.fill('SYNTHETIC_REPLACEMENT');expect(clear).to_be_disabled()
        save.click();expect(password).to_have_value('');assert state['secret']=='SYNTHETIC_REPLACEMENT'
        public_input=page.locator('input[type=url]');public_input.fill('https://draft.example.test');expect(clear).to_be_disabled()
        page.reload(wait_until='networkidle');expect(clear).to_be_enabled()
        clear.click();dialog=page.get_by_role('dialog');expect(dialog).to_be_visible();before=len(state['writes'])
        page.set_viewport_size({'width':320,'height':900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(output/f'{product}-confirmation-320.png'),full_page=True)
        dialog.get_by_role('button',name='取消',exact=True).click();assert len(state['writes'])==before
        clear.click();state['revision']+=1;dialog.get_by_role('button',name='確認清除',exact=True).click()
        expect(page.get_by_role('button',name='重新載入（捨棄草稿）')).to_be_visible()
        assert len(state['writes'])==before+1 and state['secret']=='SYNTHETIC_REPLACEMENT'
        expect(clear).to_be_disabled();expect(save).to_be_disabled()
        page.get_by_role('button',name='重新載入（捨棄草稿）').click();expect(clear).to_be_enabled()
        before=state['effects'];clear.click();dialog.get_by_role('button',name='確認清除',exact=True).click()
        expect(page.get_by_text('已清除儲存的金鑰。',exact=False)).to_be_visible()
        expect(clear).to_be_disabled();page.reload(wait_until='networkidle');expect(clear).to_be_disabled()
        assert state['secret'] is None and state['public']==public and state['effects']==before+1
        assert state['writes'][-1]['data']=={key:None}
        password.fill('SYNTHETIC_RESTORED');save.click();expect(password).to_have_value('');expect(clear).to_be_enabled()
        before=state['effects'];state['lose_reply']=True;clear.click();dialog.get_by_role('button',name='確認清除',exact=True).click()
        expect(page.get_by_role('button',name='重新載入（捨棄草稿）')).to_be_visible()
        expect(clear).to_be_disabled();expect(save).to_be_disabled();assert state['effects']==before+1
        state['lose_reply']=False;page.get_by_role('button',name='重新載入（捨棄草稿）').click();expect(clear).to_be_disabled()
        assert state['effects']==before+1
        for width in (320,360,1440):
            page.set_viewport_size({'width':width,'height':900})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output/f'{product}-{width}.png'),full_page=True)
        state['can_edit']=False;page.reload(wait_until='networkidle')
        expect(clear).to_have_count(0);expect(page.get_by_text('Fixed backend',exact=True)).to_be_visible()
        assert not errors,errors
        assert 'SYNTHETIC_RESTORED' not in page.evaluate('JSON.stringify({...localStorage,...sessionStorage})')
        return {'product':product,'passed':True,'checks':['keep','replace','missing_revision','unknown_edit_capability','dirty_interlock','cancel','stale_cas','null_clear_only','reload','lost_reply_no_retry','fixed_backend','320_360_1440','no_secret_storage','no_page_errors'],'mutation_count':len(state['writes']),'apply_count':state['effects']}
    finally:
        context.close();server.shutdown();thread.join();server.server_close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--chromium',required=True);parser.add_argument('--baseline',action='store_true');args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    results=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=args.chromium,headless=True,args=['--no-sandbox'])
        try:
            for product in ('odoo','litellm'):
                try:results.append(verify(browser,args.root,product,args.output,args.baseline))
                except Exception as exc:results.append({'product':product,'passed':False,'error':str(exc)[:1500]})
        finally:browser.close()
    (args.output/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n');print(json.dumps(results,ensure_ascii=False));raise SystemExit(0 if all(r.get('passed') for r in results) else 1)
if __name__=='__main__': main()
