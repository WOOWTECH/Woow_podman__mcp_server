const $ = (s, parent = document) => parent.querySelector(s);
const S = {connection: {}, credentials: {}, tools: [], overview: null, revision: null, policyDigest: '', serverTimeMs: null};
const pendingTools = new Set();
const pages = {overview:'連線概況', setup:'連接控制台', pairing:'配對與加入', users:'組織使用者', devices:'裝置', routes:'子網路與出口路由', policy:'存取政策', mcp:'MCP 連線與權限', platform:'PaaS 服務設定', maintenance:'憑證與維護'};
const toolTitles = {headscale_platform:'PaaS 配對與服務設定', headscale_users:'組織使用者', headscale_nodes:'裝置管理', headscale_register:'完成裝置配對', headscale_preauth_keys:'裝置加入金鑰', headscale_auth:'核准登入驗證', headscale_api_keys:'控制台 API 金鑰', headscale_policy:'存取政策', headscale_routes_replace:'設定裝置核准路由', headscale_health:'檢查資料庫', headscale_debug_node:'建立測試註冊', headscale_backfill_ips:'補齊裝置 IP'};
const operationTitles = {tailscale_services:'查看工作區的 VPN 節點', configuration:'讀取服務設定', configure:'更新服務設定', restart:'重新啟動 Headscale', operation:'查看服務作業進度', pairing_options:'查看 VPN 配對選項', pair:'配對 PaaS 上的 Tailscale', disconnect:'中斷 PaaS 節點連線', pairing_operation:'查看配對進度', list:'查看清單', get:'讀取目前設定', create:'建立', rename:'重新命名', delete:'刪除', set_tags:'設定標籤', expire:'設定到期', register:'完成註冊', legacy:'相容模式註冊', approve:'核准', reject:'拒絕', check:'驗證草稿', diff:'預覽差異', apply:'套用完整政策', replace:'替換核准路由', read:'檢查', update:'執行維護'};
const fieldTitles = {tailscale_service_id:'Tailscale 服務 ID', headscale_user_id:'歸屬使用者 ID（選填）', expected_binding_version:'目前綁定版本', idempotency_key:'此次作業識別碼', operation_id:'作業 ID', expected_revision:'已讀取設定的版本', values:'要更新的設定（JSON）', name:'名稱', user:'歸屬使用者', user_id:'歸屬使用者', node_id:'裝置', key_id:'金鑰 ID', prefix:'金鑰前綴', new_name:'新名稱', display_name:'顯示名稱', email:'電子郵件', picture_url:'頭像網址', auth_id:'用戶端顯示的註冊／驗證 ID', tags:'標籤', routes:'完整核准路由清單', reusable:'允許重複使用', ephemeral:'暫時性裝置', expiration:'金鑰到期時間（含時區）', expiry:'裝置到期時間（含時區）', mode:'到期方式', reveal_once:'我會妥善保存這次產生的金鑰', policy:'完整 HuJSON 存取政策', expected_digest:'已讀取政策的版本摘要', confirm:'我了解這是會修改資料的維護操作'};
const messages = {HEADSCALE_SETUP_REQUIRED:'請先設定 Headscale API 位址與金鑰。', HEADSCALE_INVALID_REQUEST:'請檢查欄位格式，密碼欄請使用「替換」或「清除」。', HEADSCALE_INVALID_ARGUMENTS:'請檢查必要欄位、裝置 ID 與輸入格式。', HEADSCALE_POLICY_DENIED:'這項工具或操作已停用，可到「MCP 連線與權限」調整。', HEADSCALE_INVALID_URL:'網址格式不正確。API 位址應是控制台根網址或 /api/v1。', CREDENTIAL_REPLACEMENT_REQUIRED_FOR_NEW_ORIGIN:'控制台位址已變更，請同時替換或清除原有金鑰。', PAAS_BINDING_INCOMPLETE:'PaaS 位址、工作區 ID 與 Headscale 服務 ID 必須一起填寫。', config_conflict:'設定已更新，請重新載入後確認內容再儲存。', HEADSCALE_DESTINATION_DENIED:'目前部署未允許這個 API 目的地，請檢查控制台綁定與網路設定。'};

function el(tag, text, attrs={}) { const n=document.createElement(tag); if(text!==null && text!==undefined)n.textContent=text; for(const [k,v] of Object.entries(attrs)) {if(k==='class')n.className=v; else n.setAttribute(k,v);} return n; }
function show(message, error=false) {const n=$('#notice');n.textContent=message;n.className=error?'error':'';n.hidden=false;}
function failure(error) {show(error.message || '操作未完成，請先重新讀取目前狀態。',true);}
async function api(path, {method='GET',body,revision=false}={}) {
  const headers={};if(body!==undefined)headers['Content-Type']='application/json';
  if(revision) {headers['If-Match']=typeof revision==='string'?revision:S.revision;headers['Idempotency-Key']=(S.serverTimeMs??Date.now())+'.'+crypto.randomUUID().replaceAll('-','');}
  let r;
  try{r=await fetch(path,{method,headers,credentials:'same-origin',body:body===undefined?undefined:JSON.stringify(body)});}
  catch{throw Object.assign(new Error('連線中斷；結果未確認，請先讀回狀態，避免重複送出修改。'),{outcome:'unknown'});}
  let data;try{data=await r.json();}catch{throw Object.assign(new Error('服務回應無法讀取。請重新檢查狀態，避免重複送出修改。'),{outcome:'unknown'});}
  if(r.status===401){displayLogin();throw new Error('請重新登入管理介面。');}
  if(!r.ok){let detail=data.detail||data;const code=typeof detail==='string'?detail:detail.code;const outcome=detail.outcome||(r.status>=500?'unknown':'not_sent');throw Object.assign(new Error((messages[code]||code||'操作失敗')+(outcome==='unknown'?'；結果未確認，請先讀回狀態再決定是否重試。':'')),{outcome});}
  // Core operation IDs reject future timestamps; prefer the editor read's server clock.
  if(Number.isSafeInteger(data.configuration?.server_time_ms))S.serverTimeMs=data.configuration.server_time_ms;
  if(r.headers.get('ETag'))S.revision=r.headers.get('ETag');
  return data;
}
function displayLogin() {$('#login').hidden=false;$('#workspace').hidden=true;S.overview=null;}
function card(title, description) {const n=el('section',null,{class:'card'});if(title)n.append(el('h2',title));if(description)n.append(el('p',description));return n;}
function button(title, action, secondary=false) {const b=el('button',title,{type:'button',class:secondary?'secondary':''});b.onclick=async()=>{if(b.disabled)return;b.disabled=true;try{await action();}catch(e){failure(e);}finally{b.disabled=false;}};return b;}
function label(title, input, help) {const l=el('label',title);l.append(input);if(help)l.append(el('small',help,{class:'help'}));return l;}
function input(value='',type='text') {const n=el('input',null,{type});n.value=value;return n;}
function output(value, container) {
  container.replaceChildren();
  const columns=[['id','ID'],['name','名稱'],['email','電子郵件']];
  if(Array.isArray(value.users))container.append(table(value.users,columns));
  else if(Array.isArray(value.services))container.append(table(value.services,[['id','服務 ID'],['name','服務名稱'],['state','狀態']]));
  else if(Array.isArray(value.nodes)||value.node)container.append(table(value.nodes||[value.node],[['id','ID'],['givenName','裝置名稱'],['ipAddresses','Tailnet IP'],[r=>r.online?'在線':'離線','狀態']]));
  else if(value.user)container.append(table([value.user],columns));
  else if(Array.isArray(value.preAuthKeys))container.append(table(value.preAuthKeys,[['id','ID'],[r=>r.user?.name,'使用者'],['expiration','到期時間'],[r=>r.used?'已使用':'未使用','狀態']]));
  else if(Array.isArray(value.apiKeys))container.append(table(value.apiKeys,[['id','ID'],['prefix','前綴'],['expiration','到期時間']]));
  else if(value.operation){const op=value.operation;container.append(el('p','作業狀態：'+({accepted:'已受理',queued:'等待執行',running:'執行中',succeeded:'已完成',done:'已完成',failed:'失敗',unknown:'結果待確認'}[op.state]||op.state)),el('p','作業 ID：'+op.id));}
  else if(value.valid===true)container.append(el('p','政策驗證通過，尚未儲存。'));
  else if(typeof value.diff==='string')container.append(el('pre',value.diff||'政策內容沒有差異。'));
  else if(typeof value.policy==='string')container.append(el('pre',value.exists===false?'尚未建立存取政策。':value.policy));
  else if(value.databaseConnectivity===true)container.append(el('p','Headscale 資料庫連線正常。'));
  else {container.append(el('pre',JSON.stringify(value,null,2)));return;}
  const details=el('details');details.append(el('summary','詳細結果'),el('pre',JSON.stringify(value,null,2)));container.append(details);
}
function status(ok) {const n=$('#status');n.textContent=ok?'控制台 API 可連線':'尚未確認 API 連線';n.className='badge'+(ok?' good':'');}
async function loadConnection(){const data=await api('/api/headscale/connection');S.connection=data.connection;S.credentials=data.credentials;return data;}
async function loadOverview(){S.overview=await api('/api/headscale/overview');status(S.overview.database===true);return S.overview;}
async function tool(name,args){const data=await api('/api/headscale/tools/'+encodeURIComponent(name),{method:'POST',body:{arguments:args}});return data;}

function table(rows, columns) {
  if(!rows.length)return el('p','目前沒有資料。',{class:'empty'});
  const wrap=el('div',null,{class:'table-wrap',tabindex:'0','aria-label':'可橫向捲動的資料表'}),t=el('table'),head=el('tr');
  for(const [,title] of columns)head.append(el('th',title));t.append(head);
  for(const row of rows){const tr=el('tr');for(const [key] of columns){let value=typeof key==='function'?key(row):row[key];tr.append(el('td',Array.isArray(value)?value.join(', '):String(value??'—')));}t.append(tr);}wrap.append(t);return wrap;
}

async function renderOverview(root) {
  if(!S.connection.base_url||!S.credentials.api_key?.set){const c=card('先連接組織的 Headscale','設定 API 位址與金鑰後，就能在這裡查看使用者與裝置。');c.append(button('開始設定',()=>{location.hash='setup';}));root.append(c);return;}
  const data=await loadOverview(),metrics=el('div',null,{class:'metrics'});
  for(const [title,count,note] of [['組織使用者',data.users.length,'Headscale 使用者'],['裝置',data.nodes.length,'已註冊裝置'],['在線裝置',data.nodes.filter(n=>n.online).length,'控制台回報狀態']]){const c=card();c.classList.add('metric');c.append(el('span',title),el('strong',String(count)),el('span',note));metrics.append(c);}root.append(metrics);
  const c=card('管理配對與連線','先建立或選擇使用者，再完成裝置註冊。控制台回報在線，與裝置之間實際可互通，是兩項不同的檢查。');
  const actions=el('div',null,{class:'actions'});actions.append(button('讓新裝置加入',()=>{location.hash='pairing';}),button('重新整理',()=>route(),true));
  if(S.connection.headplane_url){const a=el('a','開啟 Headplane',{href:S.connection.headplane_url,target:'_blank',rel:'noopener noreferrer'});actions.append(a);}c.append(actions);root.append(c);
  const devices=card('目前裝置');devices.append(table(data.nodes,[['id','ID'],['givenName','名稱'],[r=>r.user?.name,'使用者'],['ipAddresses','Tailnet IP'],[r=>r.online?'在線':'離線','狀態']]));root.append(devices);
}

async function renderSetup(root) {
  const data=await loadConnection(),form=el('form'),fields={};
  const c=card(),step=el('div',null,{class:'step'});step.append(el('b','1'),el('h2','連接控制台 API'));c.append(step,el('p','MCP 使用這個位址管理 Headscale。PaaS 內可使用同工作區的內部服務位址，API 金鑰需由該 Headscale 建立。'));
  fields.base_url=input(S.connection.base_url);fields.base_url.required=true;fields.base_url.placeholder='https://headscale.example.com';
  c.append(label('Headscale API 位址',fields.base_url,'填入控制台根網址；請勿填入 Headplane 的 /admin 管理頁。'));
  const credentialFields={};
  function credential(name,title){const box=el('div'),select=el('select');for(const [value,text]of [['retain','保留目前設定'],['replace','替換金鑰'],['clear','清除金鑰']]){const o=el('option',text,{value});select.append(o);}if(!S.credentials[name]?.set&&name==='api_key')select.value='replace';const value=input('','password');value.autocomplete='new-password';value.setAttribute('aria-label',title+'（新值）');value.hidden=select.value!=='replace';select.onchange=()=>{value.hidden=select.value!=='replace';value.value='';};box.append(label(title,select,S.credentials[name]?.set?'已設定，讀取頁面不會顯示金鑰。':'尚未設定。'),value);credentialFields[name]={select,value};return box;}
  c.append(credential('api_key','Headscale API 金鑰'));form.append(c);
  const publicCard=card(),step2=el('div',null,{class:'step'});step2.append(el('b','2'),el('h2','設定使用者加入的入口'));publicCard.append(step2,el('p','這兩個欄位供使用者開啟頁面與查看加入指引，不會取代上方的管理 API 位址。'));
  fields.enrollment_url=input(S.connection.enrollment_url);fields.enrollment_url.placeholder='https://your-organization.ngrok.app';
  fields.headplane_url=input(S.connection.headplane_url);fields.headplane_url.placeholder='https://headplane.example.com/admin';
  publicCard.append(label('Tailscale 登入伺服器網址（選填）',fields.enrollment_url,'外部裝置需能直接連到這個控制台入口。'),label('Headplane 管理頁網址（選填）',fields.headplane_url));form.append(publicCard);
  const p=card('PaaS 服務綁定（選填）','使用平台配對與服務設定功能時，綁定同一個工作區中的 Headscale。這組憑證與 Headscale API 金鑰分開管理。');
  for(const [key,title]of [['paas_base_url','PaaS 根網址'],['workspace_id','工作區 ID'],['headscale_service_id','Headscale 服務 ID']]){fields[key]=input(S.connection[key]);p.append(label(title,fields[key]));}p.append(credential('paas_api_token','PaaS API Token'));const advanced=el('details');advanced.open=Boolean(S.connection.paas_base_url);advanced.append(el('summary','PaaS 服務整合（選填）'),p);form.append(advanced);
  const actions=el('div',null,{class:'actions'}),save=el('button','儲存並套用',{type:'submit'});actions.append(save,button('檢查已儲存的連線',async()=>{const result=await api('/api/headscale/connection/test',{method:'POST'});status(result.verified);show(result.verified?'Headscale API 認證與資料庫檢查成功。可以開始配對裝置。':'尚未通過 API 檢查。',!result.verified);},true));form.append(actions,el('p','儲存狀態：'+({applied:'已套用',pending:'等待套用',failed:'套用失敗',unknown:'尚未套用',unconfigured:'尚未設定'}[data.configuration.apply_state]||data.configuration.apply_state),{class:'help'}));
  form.onsubmit=async e=>{e.preventDefault();save.disabled=true;try{const body=Object.fromEntries(Object.entries(fields).map(([k,v])=>[k,v.value.trim()]));for(const [k,{select,value}]of Object.entries(credentialFields)){body[k]={action:select.value};if(select.value==='replace')body[k].value=value.value;}const result=await api('/api/headscale/connection',{method:'PUT',body,revision:true});for(const {value}of Object.values(credentialFields))value.value='';await loadConnection();status(false);show(result.applied?'設定已儲存並套用；請按「檢查已儲存的連線」確認控制台可用。':'設定已儲存，MCP 程序尚未確認套用成功。');}catch(error){failure(error);}finally{save.disabled=false;}};
  root.append(form);
}

function resolved(schema, root){if(schema.$ref)return root.$defs[schema.$ref.split('/').pop()];return schema;}
// Readback uses the same permission-checked native tools as explicit reads.
function readbackRequest(name,args,data={}) {
  if(name==='headscale_routes_replace')return ['headscale_nodes',{operation:'get',node_id:args.node_id}];
  if(name==='headscale_users')return [name,{operation:'list'}];
  if(name==='headscale_nodes')return [name,args.operation==='delete'?{operation:'list'}:{operation:'get',node_id:args.node_id}];
  if(name==='headscale_preauth_keys'||name==='headscale_api_keys')return [name,{operation:'list'}];
  if(name==='headscale_policy')return [name,{operation:'get'}];
  if(name==='headscale_register'||name==='headscale_backfill_ips')return ['headscale_nodes',data.node?.id?{operation:'get',node_id:String(data.node.id)}:{operation:'list'}];
  return null; // Auth verdicts and synthetic registrations have no native read endpoint.
}
function sameItems(a,b){return Array.isArray(a)&&Array.isArray(b)&&JSON.stringify([...a].sort())===JSON.stringify([...b].sort());}
function checkReadback(name,args,written,current) {
  let matches=true;
  if(name==='headscale_routes_replace')matches=sameItems(current.node?.approvedRoutes,args.routes.length?(written.node?.approvedRoutes||args.routes):[]);
  if(name==='headscale_policy')matches=current.digest===written.digest;
  if(name==='headscale_users') {
    const id=args.user_id||written.user?.id;
    const row=current.users?.find(u=>String(u.id)===String(id));
    matches=args.operation==='delete'?!row:!!row&&row.name===(args.new_name||args.name);
  }
  if(name==='headscale_nodes') {
    if(args.operation==='delete')matches=Array.isArray(current.nodes)&&!current.nodes.some(n=>String(n.id)===args.node_id);
    else if(args.operation==='rename')matches=current.node?.givenName===args.new_name;
    else if(args.operation==='set_tags')matches=sameItems(current.node?.tags,args.tags);
    else matches=!!current.node;
  }
  if(!matches)throw new Error('讀回結果與寫入內容不一致；可能有其他管理者同時修改。請核對目前狀態，勿直接重送。');
}
function routeSummary(data,container) {
  const node=data.node;
  if(!node||!Array.isArray(node.approvedRoutes))throw new Error('無法確認目前核准路由；請重新讀取裝置狀態。');
  container.replaceChildren(el('p','裝置：'+(node.givenName||node.name||node.id)+' · '+node.id),
    el('p','控制台核准路由：'+(node.approvedRoutes.join('、')||'無（全部已撤銷）')),
    el('p','裝置公告路由：'+((node.availableRoutes||[]).join('、')||'無')+'。公告不等於控制台核准。',{class:'help'}));
}
function renderTask(root,spec){
  const c=card(toolTitles[spec.name]||spec.name);c.dataset.tool=spec.name;
  if(!spec.enabled){c.append(el('p','此工具目前已停用。請到 MCP 連線與權限調整。'));root.append(c);return;}
  const schema=spec.input_schema,variants=(schema.oneOf||schema.anyOf||[schema]).map(s=>resolved(s,schema));
  const picker=el('select',null,{class:'task-picker','aria-label':'選擇操作'});
  variants.forEach((v,i)=>{const op=v.properties?.operation?.const||spec.operations[0];picker.append(el('option',operationTitles[op]||op,{value:String(i)}));});
  if(variants.length>1)c.append(label('想進行的操作',picker));
  const slot=el('div'),result=el('div',null,{class:'task-result','aria-live':'polite'});c.append(slot,result);root.append(c);
  function build(){
    slot.replaceChildren();result.replaceChildren();result.classList.remove('secret-output');
    const v=variants[Number(picker.value)||0],op=v.properties?.operation?.const||spec.operations[0],form=el('form'),getters={},fields={};
    const isRoutes=spec.name==='headscale_routes_replace';
    const mutation=spec.name!=='headscale_platform'&&!['list','get','read','check','diff'].includes(op);
    const denied=spec.denied_operations.includes(op);
    let busy=false,needsReview=false;
    const notes={headscale_routes_replace:'這會替換裝置的全部核准路由。留空清單會撤銷全部；裝置仍須自行公告路由。',headscale_register:'先在 Tailscale 設定此控制台，取得註冊頁的 ID，再選擇歸屬使用者完成註冊。',headscale_auth:'用於已產生的驗證請求，例如 SSH check。裝置首次加入請使用「完成裝置配對」。',headscale_preauth_keys:'一般裝置建議使用短效、單次金鑰。新金鑰只在建立結果中顯示。',headscale_policy:'先讀取目前政策，再驗證草稿／預覽差異。套用會替換完整政策，其他管理者同時修改仍可能產生競爭（無原子版本鎖定）。',headscale_debug_node:'只建立測試用的註冊資料，並不代表真實裝置已連上。'};
    if(notes[spec.name])form.append(el('p',notes[spec.name]));
    if(spec.name==='headscale_users'&&op==='rename')form.append(el('p','此操作只修改 Headscale 使用者名稱，不會變更 OIDC 身分提供者帳號；下次登入的同步行為需另行確認。',{class:'help'}));
    for(const [key,raw]of Object.entries(v.properties||{})){
      const prop=resolved(raw,schema);if(key==='operation'){getters[key]=()=>op;continue;}
      let n,read;const required=(v.required||[]).includes(key);
      if(prop.type==='boolean'){n=input('','checkbox');n.checked=prop.default===true;n.required=key==='confirm'||key==='reveal_once';read=()=>n.checked;}
      else if(key==='user_id'&&S.overview?.users?.length || key==='user'&&S.overview?.users?.length || key==='node_id'&&S.overview?.nodes?.length){n=el('select');n.append(el('option','請選擇',{value:''}));const rows=key==='node_id'?S.overview.nodes:S.overview.users;for(const r of rows)n.append(el('option',(r.givenName||r.name||r.id)+' · '+r.id,{value:String(key==='user'?r.name:r.id)}));read=()=>n.value||undefined;}
      else if(prop.enum){n=el('select');for(const value of prop.enum)n.append(el('option',({now:'現在到期',at:'指定時間',disable:'停用到期'})[value]||value,{value}));n.value=prop.default??prop.enum[0];read=()=>n.value;}
      else if(prop.type==='array'){
        n=el('textarea');n.placeholder=key==='routes'?'10.20.0.0/16\n192.168.20.0/24':'tag:server';
        read=()=>n.value.split(/[\s,]+/).filter(Boolean);
        // JSON required means the property must be sent, not that the array is nonempty.
        const validate=()=>{const values=read();let error='';
          if(values.length<(prop.minItems||0))error=(fieldTitles[key]||key)+'至少需要 '+prop.minItems+' 項；每行一項。';
          else if(prop.maxItems!==undefined&&values.length>prop.maxItems)error='最多可填 '+prop.maxItems+' 項。';
          else if(key==='routes'&&values.some(value=>!value.includes('/')||value==='[]'))error='請每行填寫一個 CIDR（例如 10.20.0.0/16）；撤銷全部請留空，不要輸入 []。';
          n.setCustomValidity(error);};
        n.addEventListener('input',validate);validate();
      }
      else if(prop.type==='object'){n=el('textarea');n.placeholder='{"headscale.logLevel":"info"}';read=()=>JSON.parse(n.value);}
      else if(key==='policy'){n=el('textarea');n.rows=14;read=()=>n.value;}
      else if(prop.type==='integer'){n=input(prop.default??'','number');n.min=String(prop.minimum??0);read=()=>n.value===''?undefined:Number(n.value);}
      else{n=input(prop.default??'');if(key==='idempotency_key')n.value=crypto.randomUUID();if(key==='expiration')n.value=new Date(Date.now()+20*60*1000).toISOString();if(key==='expected_digest')n.value=S.policyDigest;n.placeholder=key==='auth_id'?'hskey-authreq-…':'';read=()=>n.value||undefined;}
      if(!['boolean','array'].includes(prop.type))n.required=required;
      n.name=key;fields[key]=n;getters[key]=read;
      let help=key==='routes'||key==='tags'?'每行一項；也可以逗號分隔。':null;
      if(key==='routes'&&isRoutes)help+='留空表示撤銷全部核准路由，不要輸入 []。';
      if(prop.minItems)help=(help||'')+'至少 '+prop.minItems+' 項，不可留空。';
      form.append(label(fieldTitles[key]||key,n,help));
    }
    const submit=el('button',operationTitles[op]||'執行',{type:'submit'}),actions=el('div',null,{class:'actions'});
    const state=el('div',null,{class:'readback','aria-live':'polite'});
    actions.append(submit);form.append(actions,state);
    if(denied){submit.disabled=true;form.append(el('p','這項操作已被權限設定停用。'));}
    if(isRoutes){
      actions.append(button('讀取目前核准路由',async()=>{
        if(!fields.node_id.reportValidity())return;
        routeSummary(await tool('headscale_nodes',{operation:'get',node_id:getters.node_id()}),state);
      },true));
      const revoke=el('button','撤銷全部核准路由',{type:'button',class:'danger'});revoke.disabled=denied;
      revoke.onclick=()=>{if(busy||needsReview||denied)return;fields.routes.value='';fields.routes.dispatchEvent(new Event('input'));form.requestSubmit();};actions.append(revoke);
      fields.node_id.addEventListener('change',()=>state.replaceChildren());
    }
    const setBusy=value=>{busy=value;picker.disabled=value;form.setAttribute('aria-busy',String(value));for(const n of form.elements)n.disabled=value;submit.disabled=value||denied||needsReview;for(const n of actions.querySelectorAll('.danger'))n.disabled=value||denied||needsReview;};
    form.onsubmit=async e=>{
      e.preventDefault();if(busy||needsReview||denied||pendingTools.has(spec.name)||!form.reportValidity())return;
      let args;try{args=Object.fromEntries(Object.entries(getters).map(([k,f])=>[k,f()]).filter(([,v])=>v!==undefined));}catch{failure(new Error('請檢查輸入格式；JSON 欄位需填寫有效 JSON。'));return;}
      let sent=false,written=false,data;
      pendingTools.add(spec.name);setBusy(true);
      const readCurrent=async()=>{
        const request=readbackRequest(spec.name,args,data);
        if(!request)return false;
        const current=await tool(...request);state.replaceChildren();
        if(isRoutes)routeSummary(current,state);else{state.append(el('h3','重新讀取的目前狀態'));output(current,state.appendChild(el('div')));}
        if(current.digest){S.policyDigest=current.digest;if(fields.expected_digest)fields.expected_digest.value=current.digest;}
        if(written)checkReadback(spec.name,args,data,current);
        return true;
      };
      try{
        let confirmation='確定要'+(operationTitles[op]||'執行')+'？請確認目標與完整設定內容。';
        if(isRoutes){
          const before=await tool('headscale_nodes',{operation:'get',node_id:args.node_id});routeSummary(before,state);
          confirmation=(args.routes.length?'替換核准路由':'撤銷全部核准路由')+'？\n裝置：'+(before.node.givenName||before.node.id)+' · '+args.node_id+'\n目前核准：'+(before.node.approvedRoutes.join('、')||'無')+'\n變更後：'+(args.routes.join('、')||'無；此裝置的全部路由核准都會撤銷。')+'\n裝置公告路由不會因此移除。';
        }
        if(['delete','apply','replace','set_tags'].includes(op)&&!window.confirm(confirmation)){show('已取消，未送出修改。');return;}
        result.replaceChildren();state.replaceChildren();result.classList.remove('secret-output');
        show(mutation?'正在送出修改，請等待讀回確認。':'正在讀取…');sent=true;
        data=await tool(spec.name,args);written=mutation;
        if(data.digest)S.policyDigest=data.digest;if(data.policy_digest)S.policyDigest=data.policy_digest;output(data,result);
        if(op==='create'&&spec.name.includes('keys')){result.classList.add('secret-output');result.prepend(el('p','請現在保存金鑰；離開這個頁面後不會再次顯示。'));}
        if(mutation){
          S.overview=null;
          const read=await readCurrent();
          show(isRoutes&&!args.routes.length?'已讀回確認：全部核准路由已撤銷。':read?'已收到寫入回應並重新讀取目前狀態，請核對下方結果。':'已收到寫入回應；此操作沒有獨立的原生讀回介面，請核對結果。');
        }else show(data.operation?'作業已受理，請查看作業進度確認結果。':'讀取／檢查已完成。');
      }catch(error){
        needsReview=mutation&&sent&&(written||error.outcome==='unknown');
        if(needsReview){
          S.overview=null;
          const message=written?'已收到寫入回應，但讀回尚未確認。請先核對狀態，勿重複送出。':'寫入結果未確認。請先核對目前狀態，勿重複送出。';
          show(message+' '+error.message,true);
          if(readbackRequest(spec.name,args,data))result.append(button('重新讀回目前狀態',async()=>{
            await readCurrent();needsReview=false;setBusy(false);
            show(written?'已重新讀取目前狀態；請核對下方結果。':'已讀取目前狀態；請核對是否已生效，再決定下一步。');
          },true));
        }else failure(error);
      }finally{pendingTools.delete(spec.name);setBusy(false);}
    };slot.append(form);
  }picker.onchange=build;build();
}

async function renderTasks(root,page){
  try{if(!S.overview)await loadOverview();}catch(e){if(!S.connection.base_url)throw e;}
  const names={platform:['headscale_platform'],pairing:['headscale_register','headscale_preauth_keys','headscale_auth'],users:['headscale_users'],devices:['headscale_nodes'],routes:['headscale_routes_replace'],policy:['headscale_policy'],maintenance:['headscale_api_keys','headscale_health','headscale_debug_node','headscale_backfill_ips']}[page];
  if(page==='pairing'){
    const c=card('讓使用者的 Tailscale 加入組織','1. 安裝並開啟 Tailscale。2. 設定這個組織的登入伺服器。3. 將註冊頁顯示的 ID 交給管理者，或使用短效加入金鑰。');
    c.append(el('p',S.connection.enrollment_url?'登入伺服器：'+S.connection.enrollment_url:'尚未設定使用者登入伺服器網址。請先到「連接控制台」填寫。'));
    c.append(el('p','實際設定入口依 Tailscale 作業系統版本不同。此介面不會建立員工帳號密碼登入系統。',{class:'help'}));root.append(c);
  }
  if(page==='platform'&&!S.connection.paas_base_url){root.append(card('先綁定 PaaS 服務','請在「連接控制台」的 PaaS 服務整合填寫工作區、Headscale 服務與 API Token。平台設定涵蓋已宣告的 DNS、到期、記錄層級及 ngrok 非秘密設定；其他設定會明確拒絕。'));return;}
  if(page==='routes'){root.append(card('控制台核准與裝置公告','此處核准子網路／出口路由。Tailscale 用戶端的公告、封包轉送與選擇出口節點，需在該裝置設定。'));}
  for(const name of names){const spec=S.tools.find(t=>t.name===name);if(spec)renderTask(root,spec);}
}

async function renderMcp(root){
  const runtime=await api('/api/settings/mcp/status'),diagnostics=card('MCP 服務狀態',runtime.running?'MCP 程序正在運行。':'MCP 程序尚未運行，請檢查控制台設定或重新啟動。');
  diagnostics.append(button('重新啟動 MCP',async()=>{await api('/api/settings/mcp/status');await api('/api/settings/mcp/restart',{method:'POST',body:{},revision:true});show('已送出重新啟動，請重新整理狀態確認。');},true));
  if(!runtime.running&&runtime.last_output?.length){const details=el('details');details.append(el('summary','查看啟動診斷'),el('pre',runtime.last_output.join('\n')));diagnostics.append(details);}root.append(diagnostics);
  const info=await api('/api/tokens'),c=card('連接 AI 工具','透過 Streamable HTTP 連接此組織的 Headscale MCP。MCP 存取憑證與 Headscale API 金鑰不同。');
  c.append(el('p','MCP 憑證狀態：'+({active:'有效',revoked:'已撤銷',unconfigured:'尚未設定'}[info.state]||info.state)));
  const result=el('div'),actions=el('div',null,{class:'actions'});
  actions.append(button('產生並顯示新連線網址',async()=>{if(info.token_set&&!confirm('產生新憑證會使原有 MCP 連線網址失效，確定繼續？'))return;await api('/api/tokens');const data=await api('/api/tokens/rotate',{method:'POST',body:{length:64},revision:true});const url=location.origin+'/private_'+data.token+'/mcp';output({url,transport:'streamable-http'},result);result.prepend(el('p','請保存此連線網址。它包含存取憑證，只會在這次顯示。'));}),button('撤銷目前連線',async()=>{if(!confirm('確定撤銷所有使用目前憑證的 MCP 連線？'))return;await api('/api/tokens');await api('/api/tokens/revoke',{method:'POST',revision:true});show('MCP 存取憑證已撤銷。');await route();},true));c.append(actions,result);root.append(c);
  const permissions=await api('/api/tools'),p=card('工具權限','停用會同時隱藏工具並拒絕呼叫。這是整個 MCP 實例的設定，適用於所有持有此連線憑證的管理者。'),checks=[];const permissionRevision=S.revision;
  for(const t of permissions.tools){const row=el('div',null,{class:'check-row'}),n=input('','checkbox');n.checked=t.enabled;checks.push([t.name,n]);n.id='permission-'+t.name;const text=label(toolTitles[t.name]||t.name,el('small',t.category));text.htmlFor=n.id;row.append(n,text);p.append(row);}
  const opChecks=[];const operationDetails=el('details');operationDetails.append(el('summary','依操作調整權限'));
  for(const t of permissions.tools){const group=el('div');group.append(el('h3',toolTitles[t.name]||t.name));for(const op of t.operations){const n=input('','checkbox');n.checked=!(permissions.disabled_operations[t.name]||[]).includes(op);opChecks.push([t.name,op,n]);group.append(label(operationTitles[op]||op,n));}operationDetails.append(group);}p.append(operationDetails);
  p.append(button('儲存操作權限',async()=>{const denied={};for(const [name,op,n] of opChecks)if(!n.checked)(denied[name]??=[]).push(op);await api('/api/tools/operations',{method:'PUT',body:{disabled_operations:denied},revision:permissionRevision});S.tools=(await api('/api/headscale/catalog')).tools;await route();show('操作權限已套用。');},true));
  p.append(button('儲存工具權限',async()=>{await api('/api/tools',{method:'PUT',body:{disabled_tools:checks.filter(([,n])=>!n.checked).map(([name])=>name)},revision:permissionRevision});S.tools=(await api('/api/headscale/catalog')).tools;await route();show('工具權限已套用。');}));root.append(p);
}

let routing=0;
async function route(){const run=++routing;const page=location.hash.slice(1)||({logs:'mcp',config:'setup',tokens:'mcp'}[location.pathname.replaceAll('/','')]||'overview');const chosen=pages[page]?page:'overview';$('#page-title').textContent=pages[chosen];for(const a of $('#navigation').children)a.classList.toggle('active',a.hash==='#'+chosen);$('#notice').hidden=true;const root=$('#content');root.replaceChildren();try{await loadConnection();if(run!==routing)return;if(!S.tools.length)S.tools=(await api('/api/headscale/catalog')).tools;if(chosen==='setup')await renderSetup(root);else if(chosen==='overview')await renderOverview(root);else if(chosen==='mcp')await renderMcp(root);else await renderTasks(root,chosen);}catch(e){failure(e);}}
for(const [name,title]of Object.entries(pages))$('#navigation').append(el('a',title,{href:'#'+name}));
$('#login-form').onsubmit=async e=>{e.preventDefault();const b=$('button',e.target);b.disabled=true;try{await api('/api/auth/login',{method:'POST',body:{password:$('#password').value}});$('#password').value='';$('#login').hidden=true;$('#workspace').hidden=false;await route();}catch(error){$('#login-error').textContent=error.message;}finally{b.disabled=false;}};
$('#logout').onclick=async()=>{await api('/api/auth/logout',{method:'POST'});S.tools=[];displayLogin();};
window.addEventListener('hashchange',route);
try{await loadConnection();$('#workspace').hidden=false;await route();}catch{displayLogin();}
