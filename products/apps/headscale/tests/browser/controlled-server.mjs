// Stateful HTTP fixture for browser regressions. Only binds to loopback; no upstream access.
// The native catalog schemas mirror the native remediation registry, including minItems.
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';

export async function controlledServer({staticRoot = new URL('../../static/', import.meta.url),debugEnabled=false,requireLogin=false,mcpSSE=false} = {}) {
  const catalog = JSON.parse(await readFile(new URL('./catalog.json', import.meta.url)));
  catalog.tools.find(t=>t.name==='headscale_debug_node').enabled=debugEnabled;
  const digest = s => createHash('sha256').update(s).digest('hex');
  const user = {id:'101', name:'BrowserOwner', email:'browser@example.invalid'};
  const node = {id:'201', givenName:'browser-node', user, online:true, ipAddresses:['100.64.0.1','fd7a:115c:a1e0::1'], tags:['tag:test'], approvedRoutes:['10.20.0.0/16','192.168.20.0/24'], availableRoutes:['10.20.0.0/16','192.168.20.0/24']};
  const state = {users:[user], nodes:[node], preAuthKeys:[], apiKeys:[], policy:'{"acls":[]}', sequence:300, mcpRotations:0, mcpRestarts:0,mcpRestores:0,mcpRevision:1,mcpToken:'fixture-original-mcp-token'};
  const profiles=[{id:'default',label:'預設設定檔',port:19984,is_default:true}];
  const profileStates=new Map([['default',state]]);
  let nextProfile=0;
  const calls=[], faults=[], nativeCalls=[], nativeFaults=[], registrations=new Map();
  const username=catalog.tools.find(t=>t.name==='headscale_users').input_schema.$defs.headscale_users_create.properties.name;
  const validUsername=value=>typeof value==='string'&&value.length>=username.minLength&&value.length<=username.maxLength&&new RegExp(username.pattern).test(value)&&!username.not.enum.includes(value);
  let delay=0,connectionDelay=0,restartFailure=false;
  const reject = (code='HEADSCALE_INVALID_ARGUMENTS',outcome='not_sent') => ({status:422,body:{detail:{code,outcome}}});
  function execute(name,a,state) {
    const op=a.operation;
    if (name==='headscale_routes_replace') {
      const n=state.nodes.find(n=>n.id===a.node_id);
      if(!n||!Array.isArray(a.routes)||a.routes.some(r=>!/^\d+\.\d+\.\d+\.\d+\/(\d|[12]\d|3[0-2])$/.test(r)))return reject();
      n.approvedRoutes=[...a.routes];return {node:n};
    }
    if(name==='headscale_users') {
      if(op==='list')return {users:state.users};
      if(['create','rename'].includes(op)&&!validUsername(a.name??a.new_name))return reject();
      if(op==='create'){const u={id:String(++state.sequence),name:a.name,email:a.email||''};state.users.push(u);return {user:u};}
      const u=state.users.find(u=>u.id===a.user_id);if(!u)return reject();
      if(op==='rename')u.name=a.new_name;
      if(op==='delete')state.users=state.users.filter(x=>x!==u);
      return op==='delete'?{}:{user:u};
    }
    if(name==='headscale_nodes') {
      if(op==='list')return {nodes:state.nodes};
      const n=state.nodes.find(n=>n.id===a.node_id);if(!n)return reject('HEADSCALE_NOT_FOUND');
      if(op==='rename')n.givenName=a.new_name;
      if(op==='set_tags'){if(!a.tags?.length)return reject();n.tags=a.tags;}
      if(op==='expire')n.expiry=a.mode==='disable'?null:a.expiry||new Date().toISOString();
      if(op==='delete'){state.nodes=state.nodes.filter(x=>x!==n);return {};}
      return {node:n};
    }
    if(name==='headscale_debug_node'){
      if(!debugEnabled||a.confirm!==true||registrations.has(a.auth_id)||!validUsername(a.user))return reject();
      const owner=state.users.find(u=>u.name===a.user);if(!owner)return reject();
      const pending={id:'0',givenName:a.name,user:owner,online:false,tags:[],approvedRoutes:[],availableRoutes:a.routes||[]};
      registrations.set(a.auth_id,pending);return {node:pending,synthetic:true};
    }
    if(name==='headscale_register'){
      const pending=registrations.get(a.auth_id);
      if(!pending)return reject('HEADSCALE_NOT_FOUND');
      const owner=state.users.find(u=>u.name===a.user);if(!owner)return reject();
      const n={...pending,user:owner,id:String(++state.sequence),ipAddresses:['100.64.0.2','fd7a:115c:a1e0::2']};
      registrations.delete(a.auth_id);state.nodes.push(n);return {node:n};
    }
    if(name==='headscale_auth'){
      if(!registrations.has(a.auth_id))return reject('HEADSCALE_NOT_FOUND');
      registrations.delete(a.auth_id);return {decision:op,registered:false};
    }
    if(name==='headscale_backfill_ips')return a.confirm===true?{changes:[]}:reject();
    if(name==='headscale_policy') {
      if(op==='check')return {valid:true,persisted:false};
      if(op==='diff')return {diff:'fixture-policy-diff',digest:digest(state.policy)};
      if(op==='apply'){if(a.expected_digest!==digest(state.policy))return reject('HEADSCALE_POLICY_CONFLICT');state.policy=a.policy;}
      return {policy:state.policy,digest:digest(state.policy),exists:true,atomic_cas:false};
    }
    if(name==='headscale_preauth_keys'||name==='headscale_api_keys') {
      const key=name==='headscale_api_keys'?'apiKeys':'preAuthKeys';
      if(op==='list')return {[key]:state[key]};
      if(op==='create') {
        const k={id:String(++state.sequence),prefix:String(state.sequence).padStart(7,'a'),expiration:a.expiration,user:state.users.find(u=>u.id===a.user_id)||user,used:false};state[key].push(k);
        return key==='apiKeys'?{apiKey:'fixture-only-secret',secret_disclosure:'created_once'}:{preAuthKey:{...k,key:'fixture-only-secret'},secret_disclosure:'created_once'};
      }
      const k=state[key].find(k=>k.id===a.key_id||k.prefix===a.prefix);if(!k)return reject();
      if(op==='expire')k.expiration=new Date(Date.now()-1000).toISOString();
      if(op==='delete')state[key]=state[key].filter(x=>x!==k);
      return {};
    }
    if(name==='headscale_health')return {databaseConnectivity:true};
    return reject('HEADSCALE_POLICY_DENIED');
  }
  const server=http.createServer(async(req,res)=>{
    try {
      let body='';for await(const chunk of req)body+=chunk;
      const payload=body?JSON.parse(body):{};
      const path=new URL(req.url,'http://localhost').pathname;
      let responseState=state;
      const send=(value,status=200)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store',ETag:'"fixture-'+responseState.mcpRevision+'"'});res.end(JSON.stringify(value));};
      if(path.startsWith('/private_')){
        const rpcSend=value=>{if(!mcpSSE)return send(value);res.writeHead(200,{'Content-Type':'text/event-stream'});res.end('event: message\r\ndata: '+JSON.stringify(value)+'\r\n\r\n');};
        if(path!=='/private_'+state.mcpToken+'/mcp')return send({},403);
        if(req.method==='DELETE')return send({});
        if(payload.method==='initialize'){
          res.setHeader('Mcp-Session-Id','fixture-session');
          return rpcSend({jsonrpc:'2.0',id:payload.id,result:{protocolVersion:'2025-03-26',capabilities:{},serverInfo:{name:'fixture',version:'1'}}});
        }
        if(payload.method==='notifications/initialized')return send({},202);
        if(payload.method==='tools/call'&&payload.params?.name==='headscale_health'){
          return rpcSend({jsonrpc:'2.0',id:payload.id,result:{structuredContent:{databaseConnectivity:true},content:[],isError:false}});
        }
        return send({},400);
      }
      if(path.startsWith('/api/v1/')){
        nativeCalls.push({method:req.method,path});
        if(req.method!=='GET')return send({},405);
        if(requireLogin&&(req.headers.authorization!=='Bearer fixture-native-key'||req.headers.cookie))return send({},401);
        const i=nativeFaults.findIndex(f=>f.path===path&&--f.after===0);
        if(i>=0)return send(nativeFaults.splice(i,1)[0].response);
        const reads={'/api/v1/user':{users:state.users},'/api/v1/node':{nodes:state.nodes},'/api/v1/preauthkey':{preAuthKeys:state.preAuthKeys},'/api/v1/apikey':{apiKeys:state.apiKeys},'/api/v1/policy':{policy:state.policy},'/api/v1/health':{databaseConnectivity:true}};
        if(reads[path])return send(reads[path]);
        if(/^\/api\/v1\/node\/[1-9][0-9]*$/.test(path)){
          const n=state.nodes.find(n=>n.id===path.split('/').pop());return send(n?{node:n}:{},n?200:404);
        }
        return send({},404);
      }
      if(path==='/api/auth/login'){
        if(requireLogin&&payload.password!=='fixture-admin-password')return send({},401);
        res.setHeader('Set-Cookie','fixture-session=yes; HttpOnly; SameSite=Strict; Path=/');return send({ok:true});
      }
      if(requireLogin&&path.startsWith('/api/')&&!req.headers.cookie?.includes('fixture-session=yes'))return send({},401);
      if(path==='/api/auth/logout')return send({ok:true});
      if(path==='/api/profiles'){
        if(req.method==='GET')return send({profiles});
        if(req.method==='POST'){
          const label=payload.label?.trim();if(!label)return send({detail:'label_required'},422);
          const profile={id:'profile-'+(++nextProfile),label,port:19984+nextProfile,is_default:false};
          profiles.push(profile);
          profileStates.set(profile.id,{users:[],nodes:[],preAuthKeys:[],apiKeys:[],policy:'{"acls":[]}',sequence:300,mcpRotations:0,mcpRestarts:0,mcpRestores:0,mcpRevision:1,mcpToken:'fixture-profile-token-'+nextProfile});
          return send({profile},201);
        }
        return send({},405);
      }
      if(path.startsWith('/api/profiles/')){
        const id=decodeURIComponent(path.slice('/api/profiles/'.length));
        const profile=profiles.find(p=>p.id===id);if(!profile)return send({},404);
        if(req.method==='PATCH'){
          if(id==='default')return send({},422);
          const label=payload.label?.trim();if(!label)return send({detail:'label_required'},422);
          profile.label=label;return send({profile});
        }
        if(req.method==='DELETE'){
          if(id==='default')return send({},403);
          profiles.splice(profiles.indexOf(profile),1);profileStates.delete(id);
          res.writeHead(204,{'Cache-Control':'no-store'});res.end();return;
        }
        return send({},405);
      }
      const profileId=req.headers['x-mcp-profile']||'default';
      responseState=profileStates.get(profileId);
      if(!responseState){responseState=state;return send({},404);}
      const activeState=responseState;
      if(path==='/api/headscale/connection'){
        if(connectionDelay&&profileId==='default')await new Promise(resolve=>setTimeout(resolve,connectionDelay));
        return send({connection:{base_url:profileId==='default'?'https://headscale.example.invalid':'',enrollment_url:profileId==='default'?'https://'+'long-enrollment-hostname-'.repeat(5)+'.example.invalid':'',headplane_url:'',paas_base_url:''},credentials:{api_key:{set:profileId==='default'}},configuration:{apply_state:profileId==='default'?'applied':'unconfigured',server_time_ms:Date.now()}});
      }
      if(path==='/api/headscale/catalog')return send(catalog);
      if(path==='/api/headscale/overview')return send({users:activeState.users,nodes:activeState.nodes,database:true});
      if(path==='/api/settings/mcp/status')return send({running:true,configuration:{server_time_ms:Date.now()}});
      if(path==='/api/tokens'&&req.method==='GET')return send({state:'active',token_set:true,configuration:{server_time_ms:Date.now()}});
      if(path==='/api/tokens/rotate'||path==='/api/settings/mcp/restart'||path==='/api/tokens'&&req.method==='PUT'){
        // Mirrors core operations.py ID_PATTERN, future rejection and 24h TTL.
        // Negative HTTP tests must fail before any fixture mutation occurs.
        const id=req.headers['idempotency-key'],now=Date.now();
        if(id){
          if(!/^[0-9]{13}\.[A-Za-z0-9_-]{16,64}$/.test(id)||Number(id.split('.')[0])>now)return send({detail:{code:'operation_id_invalid'}},400);
          if(Number(id.split('.')[0])<=now-86400000)return send({detail:{code:'operation_id_expired'}},410);
        }
        if(!req.headers['if-match'])return send({detail:{code:'config_revision_required'}},428);
        if(req.headers['if-match']!=='"fixture-'+activeState.mcpRevision+'"')return send({detail:{code:'config_conflict'}},409);
        if(!id)return send({detail:{code:'operation_id_required'}},428);
        if(path==='/api/tokens'){
          if(!/^[A-Za-z0-9_-]{16,256}$/.test(payload.token))return send({},422);
          activeState.mcpToken=payload.token;activeState.mcpRestores++;activeState.mcpRevision++;
          return send({status:'ok',token_state:'applied',configuration:{server_time_ms:now}});
        }
        if(path==='/api/tokens/rotate'){
          activeState.mcpRotations++;activeState.mcpRevision++;activeState.mcpToken='fixture-rotated-'+profileId+'-mcp-token-'+activeState.mcpRotations;
          return send({status:'ok',token_state:'applied',token:activeState.mcpToken,configuration:{server_time_ms:now}});
        }
        if(restartFailure){restartFailure=false;return send({detail:{code:'fixture_restart_failed'}},500);}
        activeState.mcpRestarts++;activeState.mcpRevision++;return send({status:'ok',configuration:{server_time_ms:now}});
      }
      if(path==='/api/tools')return send({tools:catalog.tools,disabled_operations:{},disabled_tools:catalog.tools.filter(t=>!t.enabled).map(t=>t.name)});
      if(path.startsWith('/api/headscale/tools/')) {
        const name=path.split('/').pop(), args=payload.arguments;
        calls.push({name,args:structuredClone(args)});
        const i=faults.findIndex(f=>f.name===name&&(!f.op||f.op===args.operation));
        const fault=i<0?null:faults.splice(i,1)[0];
        if(delay)await new Promise(r=>setTimeout(r,delay));
        let result=(!fault||fault.afterWrite)?execute(name,args,activeState):null;
        if(fault)result=fault.response;
        result=structuredClone(result);
        return send(result?.body||result,result?.status||200);
      }
      const files={'/':'index.html','/assets/app.js':'assets/app.js','/assets/style.css':'assets/style.css'};
      if(!files[path])return send({},404);
      const file=await readFile(new URL(files[path],staticRoot));
      res.writeHead(200,{'Content-Type':path.endsWith('.js')?'application/javascript':path.endsWith('.css')?'text/css':'text/html'});res.end(file);
    }catch {res.writeHead(500,{'Content-Type':'application/json'});res.end('{"detail":"FIXTURE_ERROR"}');}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  return {base:`http://127.0.0.1:${server.address().port}`,catalog,state,calls,nativeCalls,
    delay(ms){delay=ms;},fault(f){faults.push(f);},
    delayConnection(ms){connectionDelay=ms;},
    nativeFault(f){nativeFaults.push(f);},
    pending(id){registrations.set(id,{id:'0',givenName:'fixture-pending',online:false,tags:[],approvedRoutes:[],availableRoutes:[]});},
    failNextRestart(){restartFailure=true;},
    async close(){server.closeAllConnections();await new Promise(r=>server.close(r));}};
}
