// External targets require an explicit URL and a private JSON credential file.
// Credentials may be direct {admin_password:...}, or a location file {path:...}.
// No HAR, trace, response dumps, cookies or raw exception messages are written.
import {createRequire} from 'node:module';
import {open,mkdir} from 'node:fs/promises';
import {constants} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {parseArgs} from 'node:util';
import assert from 'node:assert/strict';
import {controlledServer} from './controlled-server.mjs';

const {values:options}=parseArgs({options:{
  'base-url':{type:'string'},'credentials-file':{type:'string'},'node-id':{type:'string'},
  'oracle-url':{type:'string'},'pending-file':{type:'string'},'debug-nodes':{type:'boolean',default:false},
  'tag':{type:'string'},'backfill':{type:'boolean',default:false},
  'isolated':{type:'boolean',default:false},'output':{type:'string',default:'/tmp/headscale-browser'},
  'static-root':{type:'string'},'chromium':{type:'string'},
}});
export {options,assert};
export const pages=['overview','setup','pairing','users','devices','routes','policy','mcp','platform','maintenance'];
export async function privateJSON(path){
  const file=await open(path,constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);
  try{
    const info=await file.stat();
    assert(info.isFile()&&(info.mode&0o077)===0&&info.uid===process.getuid(),'Credential file must be owned, regular and private');
    return JSON.parse(await file.readFile('utf8'));
  }finally{await file.close();}
}
export async function start({mutations=false,nativeOracle=false}={}) {
  let fixture,base=options['base-url'],password,secrets;
  if(base){
    const url=new URL(base);
    assert(['http:','https:'].includes(url.protocol)&&!url.username&&!url.password&&!url.search&&!url.hash&&url.pathname==='/', 'Use an origin without credentials');
    base=url.origin;
    assert(!mutations||options.isolated,'External writes require --isolated and dedicated test data');
    assert(options['credentials-file'],'External target requires --credentials-file');
    secrets=await privateJSON(options['credentials-file']);
    if(!secrets.admin_password){assert(secrets.path,'Location file must have a path');secrets=await privateJSON(secrets.path);}
    password=secrets.admin_password;assert(typeof password==='string'&&password.length>0);
    if(nativeOracle)assert(options['oracle-url']&&secrets.headscale_api_key,'Native workflow checks require --oracle-url and the private Headscale API key');
  }else{
    fixture=await controlledServer({debugEnabled:options['debug-nodes'],...(options['static-root']?{staticRoot:pathToFileURL(resolve(options['static-root'])+'/')}:{})});base=fixture.base;
  }
  let browser;
  try{
    let nativeReads=0,oracle;
    if(nativeOracle){
      const url=new URL(fixture?base:options['oracle-url']);
      assert(['http:','https:'].includes(url.protocol)&&!url.username&&!url.password&&!url.search&&!url.hash&&url.pathname==='/','Oracle must be an origin without credentials');
      // An independent GET-only native client: never forwards GUI cookies, follows
      // redirects, or exposes mutation endpoints. Responses stay in memory.
      oracle=async path=>{
        assert(/^\/api\/v1\/(user|node(?:\/[1-9][0-9]*)?|preauthkey|apikey|policy|health)$/.test(path),'Only native read endpoints are allowed');
        const response=await fetch(url.origin+path,{method:'GET',redirect:'error',signal:AbortSignal.timeout(30000),headers:secrets?{Authorization:'Bearer '+secrets.headscale_api_key}:{}});
        nativeReads++;assert(response.ok,'Native oracle read failed');return response.json();
      };
    }
    const require=createRequire(import.meta.url);
    const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
    browser=await chromium.launch({headless:true,executablePath:options.chromium||process.env.CHROMIUM_PATH||undefined,args:['--no-sandbox']});
    const context=await browser.newContext({viewport:{width:1440,height:1000}});
    const page=await context.newPage(),errors=[];
    page.on('pageerror',()=>errors.push('pageerror'));
    await page.goto(base);
    if(password){await page.locator('#login').waitFor({state:'visible'});await page.locator('#password').fill(password);await page.locator('#login-form button').click();}
    await page.locator('#workspace').waitFor({state:'visible'});
    await page.locator('#content .card').first().waitFor();
    await mkdir(options.output,{recursive:true,mode:0o700});
    return {page,context,fixture,base,errors,oracle,get nativeReads(){return nativeReads;},async close(){await browser.close();await fixture?.close();}};
  }catch(error){await browser?.close();await fixture?.close();throw error;}
}
export function task(page,name){return page.locator(`[data-tool="${name}"]`);}
export async function go(page,hash){
  // Hash navigation does not reset Playwright's network-idle lifecycle. Observe
  // the new route's connection response before waiting for its remaining reads.
  const connection=page.waitForResponse(r=>r.url().endsWith('/api/headscale/connection'));
  if(await page.evaluate(hash=>location.hash===`#${hash}`,hash))await page.reload();
  else await page.locator(`#navigation a[href="#${hash}"]`).click();
  await connection;
  // Some pages append an early card before their next API read; networkidle may
  // still describe the previous hash route. Wait for the final rendered card.
  const finalHeading={overview:'目前裝置',setup:'設定使用者加入的入口',pairing:'核准登入驗證',users:'組織使用者',devices:'裝置管理',routes:'設定裝置核准路由',policy:'存取政策',mcp:'工具權限',maintenance:'補齊裝置 IP'}[hash];
  if(finalHeading)await page.locator('#content').getByRole('heading',{name:finalHeading,exact:true}).waitFor({state:'attached'});
  else await page.locator('#content .card').first().waitFor();
}
export async function operation(page,name,op){
  const card=task(page,name);
  const picker=card.locator('.task-picker');
  if(await picker.count()){
    const labels={list:'查看清單',get:'讀取目前設定',create:'建立',rename:'重新命名',delete:'刪除',set_tags:'設定標籤',expire:'設定到期',apply:'套用完整政策',check:'驗證草稿',diff:'預覽差異',register:'完成註冊',legacy:'相容模式註冊',approve:'核准',reject:'拒絕'};
    await picker.selectOption({label:labels[op]});
  }
  return card;
}
export async function fill(card,name,value){const field=card.locator(`[name="${name}"]`);if(await field.evaluate(n=>n.tagName==='SELECT'))await field.selectOption(String(value));else if(typeof value==='boolean')await field.setChecked(value);else await field.fill(String(value));}
export async function submit(page,card,{accept=true,confirmation}={}){
  const dialog=async d=>{if(confirmation)confirmation(d.message());if(accept)await d.accept();else await d.dismiss();};
  page.once('dialog',dialog);
  await card.locator('button[type="submit"]').click();
  await page.waitForFunction(()=>document.querySelector('#notice')?.textContent&&!document.querySelector('form[aria-busy="true"]'));
  page.removeListener('dialog',dialog);
}
export async function screenshot(page,name){
  await page.screenshot({path:resolve(options.output,name+'.png'),fullPage:true,mask:[page.locator('input, textarea, select, pre, .secret-output, .task-result, .readback, .table-wrap, #notice, a[href*="private_"]')]});
}
export async function run(name,fn){
  try{await fn();console.log(JSON.stringify({suite:name,status:'PASS'}));}
  catch(error){console.error(JSON.stringify({suite:name,status:'FAIL',error_type:error.name,location:error.stack?.split('\n').find(line=>/at .*\/tests\/browser\/[^:]+:\d+/.test(line))?.trim()}));process.exitCode=1;}
}
