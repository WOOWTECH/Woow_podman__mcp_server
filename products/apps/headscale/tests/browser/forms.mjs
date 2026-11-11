// Default: controlled browser regressions (28 form variants). External writes
// require --isolated, --credentials-file, --oracle-url and a serialized handoff.
// --debug-nodes creates disposable synthetic registrations (not client evidence).
// --pending-file is private JSON with optional register/legacy/approve/reject auth
// IDs from fresh, dedicated clients; registered nodes are mutated and deleted.
// --tag must already exist in policy. --backfill explicitly permits a global
// no-op maintenance check. No MCP token, permission or runtime changes occur.
import {randomBytes,createHash} from 'node:crypto';
import {open} from 'node:fs/promises';
import {resolve} from 'node:path';
import {start,go,task,operation,fill,submit,screenshot,run,assert,options,privateJSON} from './common.mjs';
await run('forms',async()=>{
  const pending=options['pending-file']?await privateJSON(options['pending-file']):{};
  assert(Object.keys(pending).every(k=>['register','legacy','approve','reject'].includes(k)),'Only dedicated pending auth IDs belong in the manifest');
  assert(Object.values(pending).every(v=>typeof v==='string'&&/^hskey-authreq-[A-Za-z0-9_-]{24}$/.test(v)));
  assert.equal(new Set(Object.values(pending)).size,Object.values(pending).length,'Each flow needs a fresh auth ID');
  const env=await start({mutations:true,nativeOracle:true}),{page,fixture,errors,oracle}=env;
  const covered=new Set(),skipped=[],registrations=[];
  const suffix=Date.now().toString(36)+'-'+randomBytes(3).toString('hex');
  const owned={users:[],nodes:[],preAuthKeys:[],apiKeys:[],policy_restore_required:false};
  let phase='preflight',ledger;
  const future=()=>new Date(Date.now()+2*60*60*1000).toISOString();
  const hash=s=>createHash('sha256').update(s).digest('hex');
  const ids=rows=>rows.map(r=>String(r.id)).sort();
  const nodeList=async()=>(await oracle('/api/v1/node')).nodes;
  const nodeRead=async id=>(await oracle('/api/v1/node/'+id)).node;
  const userList=async()=>(await oracle('/api/v1/user')).users;
  const readback=async card=>JSON.parse(await card.locator('.readback pre').last().textContent());
  async function save(){if(ledger)await ledger.writeFile(JSON.stringify({phase,owned})+'\n');}
  async function own(kind,id){assert(/^[1-9][0-9]*$/.test(String(id)));owned[kind].push(String(id));await save();}
  async function removed(kind,id){owned[kind]=owned[kind].filter(v=>v!==String(id));await save();}
  async function act(name,op,values={}){
    phase=name+':'+op;
    const card=await operation(page,name,op);
    for(const [key,value] of Object.entries(values))await fill(card,key,value);
    assert(await card.locator('form').evaluate(f=>f.checkValidity()),'Positive form must be valid before submitting');
    const response=page.waitForResponse(r=>r.url().endsWith('/tools/'+name)&&(!r.request().postDataJSON().arguments.operation||r.request().postDataJSON().arguments.operation===op));
    await submit(page,card);const r=await response;assert(r.ok(),'Native form returned success');
    const data=await r.json();
    // Record IDs before readback assertions, so a failed run leaves recovery data
    // without dumping tokens, auth IDs, policies, or complete native responses.
    if(name==='headscale_users'&&op==='create')await own('users',data.user.id);
    if(name==='headscale_register'&&data.node&&!owned.nodes.includes(String(data.node.id)))await own('nodes',data.node.id);
    assert.equal(await page.locator('#notice').evaluate(n=>n.classList.contains('error')),false,'No false success on readback failure');
    covered.add(phase);return {data,card};
  }
  async function unchecked(name,values){
    const card=await operation(page,name,'create');
    for(const [key,value] of Object.entries(values))await fill(card,key,value);
    await fill(card,'confirm',false);let writes=0;
    const observe=r=>{if(r.method()==='POST'&&r.url().endsWith('/tools/'+name))writes++;};
    page.on('request',observe);
    try{await card.locator('button[type="submit"]').click();assert.equal(await card.locator('[name="confirm"]').evaluate(n=>n.checkValidity()),false);assert.equal(writes,0);}
    finally{page.off('request',observe);}
  }
  try{
    ledger=await open(resolve(options.output,'ownership-'+suffix+'.jsonl'),'wx',0o600);await save();
    // Fail on oracle/auth/config errors before creating any resources.
    const initialUsers=ids(await userList()),initialNodes=ids(await nodeList());await oracle('/api/v1/apikey');
    assert.equal((await oracle('/api/v1/health')).databaseConnectivity,true);
    const name='Browser-'+suffix,renamed='Renamed-'+suffix;
    await go(page,'users');let {data,card}=await act('headscale_users','list');assert.deepEqual(ids(data.users),ids(await userList()));
    ({data,card}=await act('headscale_users','create',{name,display_name:'Browser regression'}));const userId=String(data.user.id);
    assert((await readback(card)).users.some(u=>String(u.id)===userId&&u.name===name));
    assert((await userList()).some(u=>String(u.id)===userId&&u.name===name));
    if(fixture){
      fixture.delay(150);card=await operation(page,'headscale_users','rename');await fill(card,'user_id',userId);await fill(card,'new_name',renamed);
      const before=fixture.calls.length;
      await card.locator('form').evaluate(f=>{f.requestSubmit();f.dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));});
      await page.waitForFunction(()=>document.querySelector('form[aria-busy="true"]'));
      assert.equal(await card.locator('.task-picker').isDisabled(),true);
      await page.waitForFunction(()=>!document.querySelector('form[aria-busy="true"]'));
      assert.equal(fixture.calls.slice(before).filter(c=>c.args.operation==='rename').length,1);fixture.delay(0);
      covered.add('headscale_users:rename');
    }else({card}=await act('headscale_users','rename',{user_id:userId,new_name:renamed}));
    assert((await readback(card)).users.some(u=>String(u.id)===userId&&u.name===renamed));
    assert((await userList()).some(u=>String(u.id)===userId&&u.name===renamed));
    card=await operation(page,'headscale_users','delete');await fill(card,'user_id',userId);
    const callsBefore=fixture?.calls.length;await submit(page,card,{accept:false});
    if(fixture)assert.equal(fixture.calls.length,callsBefore);
    assert((await userList()).some(u=>String(u.id)===userId),'Cancelled delete preserves user');
    for(const [tool,pageName,listKey,path] of [['headscale_preauth_keys','pairing','preAuthKeys','preauthkey'],['headscale_api_keys','maintenance','apiKeys','apikey']]){
      await go(page,pageName);const before=(await oracle('/api/v1/'+path))[listKey];
      assert.deepEqual(ids((await act(tool,'list')).data[listKey]),ids(before));
      const expiration=future(),fields={expiration,reveal_once:true};if(tool==='headscale_preauth_keys')fields.user_id=userId;
      ({card}=await act(tool,'create',fields));assert.equal(await card.locator('.secret-output').count(),1);
      const after=(await oracle('/api/v1/'+path))[listKey],fresh=after.filter(k=>!before.some(old=>String(old.id)===String(k.id)));assert.equal(fresh.length,1);
      const key=fresh[0];await own(listKey,key.id);
      assert((await readback(card))[listKey].some(k=>String(k.id)===String(key.id)));
      assert.equal(Date.parse(key.expiration),Date.parse(expiration));
      if(listKey==='preAuthKeys')assert.equal(String(key.user.id),userId);
      await screenshot(page,tool+'-created-masked');
      if(fixture&&listKey==='preAuthKeys')assert.deepEqual(fixture.calls.findLast(c=>c.name===tool&&c.args.operation==='create').args.tags,[]);
      ({card}=await act(tool,'expire',{key_id:String(key.id)}));
      assert(Date.parse((await readback(card))[listKey].find(k=>String(k.id)===String(key.id)).expiration)<=Date.now());
      assert(Date.parse((await oracle('/api/v1/'+path))[listKey].find(k=>String(k.id)===String(key.id)).expiration)<=Date.now());
      ({card}=await act(tool,'delete',listKey==='apiKeys'?{prefix:key.prefix}:{key_id:String(key.id)}));
      assert(!(await readback(card))[listKey].some(k=>String(k.id)===String(key.id)));
      assert.deepEqual(ids((await oracle('/api/v1/'+path))[listKey]),ids(before));await removed(listKey,key.id);
    }
    await go(page,'policy');const original=(await act('headscale_policy','get')).data;
    const policySuffix='\n// GUI browser regression '+suffix+'\n';
    const candidate=(original.exists===false?'{}':original.policy)+policySuffix;
    if(original.exists!==false)assert.equal((await oracle('/api/v1/policy')).policy,original.policy);
    assert.equal((await act('headscale_policy','check',{policy:candidate})).data.valid,true);
    const diff=(await act('headscale_policy','diff',{policy:candidate})).data;assert(diff.diff.length>0);
    assert.equal((await act('headscale_policy','get')).data.digest,original.digest,'Check/diff must not persist');
    if(original.exists!==false){
      assert.equal((await oracle('/api/v1/policy')).policy,original.policy);
      owned.policy_restore_required=true;owned.original_policy_digest=hash(original.policy);owned.policy_suffix=policySuffix;await save();
      ({card}=await act('headscale_policy','apply',{policy:candidate,expected_digest:original.digest}));
      const current=await readback(card);assert.equal(current.policy,candidate);assert.equal((await oracle('/api/v1/policy')).policy,candidate);
      assert.notEqual(current.digest,original.digest);
      await act('headscale_policy','apply',{policy:original.policy,expected_digest:current.digest});
      assert.equal((await oracle('/api/v1/policy')).policy,original.policy);owned.policy_restore_required=false;await save();
      card=await operation(page,'headscale_policy','apply');await fill(card,'policy',candidate);await fill(card,'expected_digest','0'.repeat(64));
      await submit(page,card);assert.equal(await page.locator('#notice').evaluate(n=>n.classList.contains('error')),true);
      assert.equal((await oracle('/api/v1/policy')).policy,original.policy,'Conflict must not change policy');
    }else skipped.push('policy:apply (absence cannot be restored through native API)');
    // Use freshly issued IDs only. Debug seeds are consumed immediately and never
    // reported as real clients. Existing --node-id is read-only in external mode.
    const enrollmentPlans=['register','legacy'].map(op=>({op,authId:pending[op]}));
    if(options['debug-nodes']&&pending.register&&pending.legacy)enrollmentPlans.push({op:'legacy'});
    for(const plan of enrollmentPlans){
      const op=plan.op;let authId=plan.authId,source='dedicated-client';const before=await nodeList();
      if(!authId&&options['debug-nodes']){
        authId='hskey-authreq-'+randomBytes(18).toString('base64url');source='synthetic';
        await go(page,'maintenance');const values={user:renamed,auth_id:authId,name:'browser-'+op+'-'+suffix,routes:''};
        await unchecked('headscale_debug_node',values);
        const debug=(await act('headscale_debug_node','create',{...values,confirm:true})).data;
        assert.equal(debug.synthetic,true);assert.equal(String(debug.node.id),'0');assert.deepEqual(ids(await nodeList()),ids(before));
      }
      if(!authId&&fixture){authId='hskey-authreq-'+randomBytes(18).toString('base64url');fixture.pending(authId);source='controlled-pending';}
      if(authId){
        await go(page,'pairing');({data,card}=await act('headscale_register',op,{user:renamed,auth_id:authId}));
        const id=String(data.node.id);assert(!ids(before).includes(id),'Registration must create an owned node');
        assert.equal(String((await readback(card)).node.id),id);
        const node=await nodeRead(id);assert.equal(String(node.user.id),userId);assert.deepEqual(ids(await nodeList()),[...ids(before),id].sort());
        if(source==='synthetic')assert.equal(node.online,false);
        registrations.push({operation:op,source});
      }else skipped.push('register:'+op+' (needs fresh pending ID or --debug-nodes)');
    }
    const disposable=[...owned.nodes];
    const nodeIds=[...new Set([...disposable,...(options['node-id']?[options['node-id']]:[])])];
    await go(page,'devices');assert.deepEqual(ids((await act('headscale_nodes','list')).data.nodes),ids(await nodeList()));
    for(const nodeId of nodeIds){
      await go(page,'devices');({data}=await act('headscale_nodes','get',{node_id:nodeId}));assert.equal(String(data.node.id),String((await nodeRead(nodeId)).id));
      if(!disposable.includes(nodeId))continue;
      ({card}=await act('headscale_nodes','rename',{node_id:nodeId,new_name:'browser-'+suffix}));
      assert.equal((await readback(card)).node.givenName,'browser-'+suffix);assert.equal((await nodeRead(nodeId)).givenName,'browser-'+suffix);
      for(const mode of ['now','disable','at']){
        const expiry=future();({card}=await act('headscale_nodes','expire',{node_id:nodeId,mode,...(mode==='at'?{expiry}:{})}));
        for(const node of [(await readback(card)).node,await nodeRead(nodeId)]){
          if(mode==='disable')assert([null,undefined,'','0001-01-01T00:00:00Z'].includes(node.expiry));
          else if(mode==='at')assert.equal(Date.parse(node.expiry),Date.parse(expiry));
          else assert(Date.parse(node.expiry)<=Date.now());
        }
      }
      card=await operation(page,'headscale_nodes','set_tags');await fill(card,'node_id',nodeId);
      let tagWrites=0;const observe=r=>{if(r.method()==='POST'&&r.url().endsWith('/tools/headscale_nodes'))tagWrites++;};page.on('request',observe);
      try{for(const tags of ['',' \n , \t ']){await fill(card,'tags',tags);await card.locator('button[type="submit"]').click();assert.equal(await card.locator('[name="tags"]').evaluate(n=>n.checkValidity()),false);}assert.equal(tagWrites,0);}
      finally{page.off('request',observe);}
      const tag=options.tag||(fixture?'tag:test':null);
      if(tag){
        ({card}=await act('headscale_nodes','set_tags',{node_id:nodeId,tags:tag}));
        assert.deepEqual((await readback(card)).node.tags,[tag]);assert.deepEqual((await nodeRead(nodeId)).tags,[tag]);
      }else skipped.push('nodes:set_tags (requires existing policy --tag)');
      await screenshot(page,'node-lifecycle-masked');
      ({card}=await act('headscale_nodes','delete',{node_id:nodeId}));
      assert(!(await readback(card)).nodes.some(n=>String(n.id)===nodeId));assert(!ids(await nodeList()).includes(nodeId));await removed('nodes',nodeId);
    }
    if(!disposable.length)skipped.push('nodes:rename/expire/set_tags/delete (no disposable registration)');
    for(const op of ['approve','reject']){
      const authId=pending[op]||(fixture?'hskey-authreq-'+randomBytes(18).toString('base64url'):null);
      if(!authId){skipped.push('auth:'+op+' (requires fresh pending ID; SSH check remains root-owned)');continue;}
      if(fixture)fixture.pending(authId);
      const before=ids(await nodeList());await go(page,'pairing');const verdict=(await act('headscale_auth',op,{auth_id:authId})).data;
      assert.equal(verdict.decision,op);assert.equal(verdict.registered,false);assert.deepEqual(ids(await nodeList()),before);
    }
    await go(page,'maintenance');assert.equal((await act('headscale_health','read')).data.databaseConnectivity,true);assert.equal((await oracle('/api/v1/health')).databaseConnectivity,true);
    if(fixture||options.backfill){
      const ips=nodes=>nodes.map(n=>[String(n.id),[...(n.ipAddresses||[])].sort()]).sort(([a],[b])=>a.localeCompare(b));
      const before=ips(await nodeList());await unchecked('headscale_backfill_ips',{});
      assert.deepEqual((await act('headscale_backfill_ips','update',{confirm:true})).data.changes,[],'Isolated backfill requires an already complete IP inventory');
      assert.deepEqual(ips(await nodeList()),before);
    }else skipped.push('backfill:update (global operation requires --backfill)');
    if(fixture&&!options['debug-nodes'])assert.equal(await task(page,'headscale_debug_node').locator('form').count(),0,'Debug stays disabled');
    if(!covered.has('headscale_debug_node:create'))skipped.push('debug:create (requires enabled tool and --debug-nodes)');
    await go(page,'users');({card}=await act('headscale_users','delete',{user_id:userId}));
    assert(!(await readback(card)).users.some(u=>String(u.id)===userId));assert(!(await userList()).some(u=>String(u.id)===userId));await removed('users',userId);
    assert.deepEqual(ids(await userList()),initialUsers);assert.deepEqual(ids(await nodeList()),initialNodes);
    assert.deepEqual(errors,[]);assert(Object.values(owned).filter(Array.isArray).every(a=>a.length===0));assert.equal(owned.policy_restore_required,false);
    if(fixture)assert(fixture.nativeCalls.length>0&&fixture.nativeCalls.every(c=>c.method==='GET'));
    phase='complete';await save();
    console.log(JSON.stringify({mode:fixture?'controlled':'actual',positive_form_variants:covered.size,native_operations:covered.size-(covered.has('headscale_policy:diff')?1:0),native_oracle_reads:env.nativeReads,registrations,cleanup_confirmed:true,coverage:[...covered].sort(),skipped}));
  }catch(error){await save();console.error(JSON.stringify({failed_phase:phase,recovery_ledger:'ownership-'+suffix+'.jsonl'}));throw error;}
  finally{await ledger?.close();await env.close();}
});
