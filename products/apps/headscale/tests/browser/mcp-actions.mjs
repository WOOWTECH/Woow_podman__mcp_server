// Core timestamp.random browser regression. External runs ROTATE THE MCP TOKEN
// and RESTART MCP: require a serialized handoff and --isolated. The original
// private-state token must work before any mutation; finally restores it through
// a fresh GET + revision-protected PUT, then verifies original works/new revoked.
// No tokens, auth headers, session IDs or MCP response bodies are persisted.
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import {start,go,screenshot,run,assert,options,privateJSON} from './common.mjs';
await run('mcp-actions',async()=>{
  const env=await start({mutations:true}),{page,context,fixture,base,errors}=env;
  let originalToken,restoreRequired=false,rawRejections=0;
  const rotatedTokens=new Set();
  const operationId=now=>{assert(Number.isSafeInteger(now));return now+'.'+randomUUID().replaceAll('-','');};
  async function rpc(token,payload,extra={}){
    const response=await fetch(base+'/private_'+token+'/mcp',{method:'POST',redirect:'error',signal:AbortSignal.timeout(15000),headers:{'Content-Type':'application/json',Accept:'application/json, text/event-stream',Origin:base,...extra},body:JSON.stringify(payload)});
    const result={status:response.status,headers:response.headers};
    if(response.status!==200||!payload.id){await response.body?.cancel();return result;}
    if(!response.headers.get('content-type')?.includes('text/event-stream'))return {...result,data:await response.json()};
    const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';
    try{
      for(;;){
        const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});
        let boundary;
        while((boundary=/\r?\n\r?\n/.exec(buffer))){
          const event=buffer.slice(0,boundary.index);buffer=buffer.slice(boundary.index+boundary[0].length);
          const raw=event.split(/\r?\n/).filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n');
          if(raw){const data=JSON.parse(raw);if(data.id===payload.id)return {...result,data};}
        }
      }
      assert.fail('MCP response ended without the requested result');
    }finally{await reader.cancel();}
  }
  async function mcpWorks(token){
    let session,protocol;
    try{
      const init=await rpc(token,{jsonrpc:'2.0',id:1,method:'initialize',params:{protocolVersion:'2025-03-26',capabilities:{},clientInfo:{name:'gui-token-regression',version:'1'}}});
      session=init.headers.get('mcp-session-id');protocol=init.data?.result?.protocolVersion;
      assert.equal(init.status,200);assert(session&&protocol&&!init.data.error);
      const headers={'Mcp-Session-Id':session,'MCP-Protocol-Version':protocol};
      const ready=await rpc(token,{jsonrpc:'2.0',method:'notifications/initialized'},headers);assert([200,202,204].includes(ready.status));
      const health=await rpc(token,{jsonrpc:'2.0',id:2,method:'tools/call',params:{name:'headscale_health',arguments:{}}},headers);
      assert.equal(health.status,200);assert(health.data.result&&!health.data.result.isError);
      const data=health.data.result.structuredContent||JSON.parse(health.data.result.content.find(c=>c.type==='text').text);
      assert.equal(data.databaseConnectivity,true);
    }finally{
      if(session){
        const closed=await fetch(base+'/private_'+token+'/mcp',{method:'DELETE',redirect:'error',signal:AbortSignal.timeout(15000),headers:{Accept:'application/json, text/event-stream',Origin:base,'Mcp-Session-Id':session,'MCP-Protocol-Version':protocol||'2025-03-26'}});
        await closed.body?.cancel();assert([200,204].includes(closed.status),'MCP probe session must be deleted');
      }
    }
  }
  async function denied(token){
    const response=await rpc(token,{jsonrpc:'2.0',id:1,method:'initialize',params:{protocolVersion:'2025-03-26',capabilities:{},clientInfo:{name:'gui-revoked-probe',version:'1'}}});
    // Clean up an unexpectedly accepted probe before failing the assertion.
    const session=response.headers.get('mcp-session-id');
    if(session){const closed=await fetch(base+'/private_'+token+'/mcp',{method:'DELETE',redirect:'error',signal:AbortSignal.timeout(15000),headers:{Origin:base,Accept:'application/json, text/event-stream','Mcp-Session-Id':session}});await closed.body?.cancel();}
    assert.equal(response.status,403,'Previous MCP token must be rejected');
  }
  try{
    if(fixture)originalToken=fixture.state.mcpToken;
    else{
      let secrets=await privateJSON(options['credentials-file']);if(!secrets.admin_password)secrets=await privateJSON(secrets.path);
      originalToken=secrets.mcp_auth_token;
    }
    assert(typeof originalToken==='string'&&/^[A-Za-z0-9_-]{16,256}$/.test(originalToken),'A restorable original fixture token is required');
    const info=await context.request.get(base+'/api/tokens'),snapshot=await info.json();assert(info.ok());
    assert.equal(snapshot.state,'active');assert.equal(snapshot.token_set,true);await mcpWorks(originalToken);
    const revision=info.headers().etag,now=snapshot.configuration.server_time_ms;
    assert(Number.isSafeInteger(now));assert(revision);
    const invalid=[
      [null,428,'operation_id_required'],
      [randomUUID(),400,'operation_id_invalid'],
      [now+'.short',400,'operation_id_invalid'],
      [(now+3600000)+'.'+'a'.repeat(32),400,'operation_id_invalid'],
      [(now-86400001)+'.'+'a'.repeat(32),410,'operation_id_expired'],
    ];
    // Even a broken rejection could rotate: restore after every attempted write.
    restoreRequired=true;
    for(const [id,status,code] of invalid){
      const headers={Origin:base,'If-Match':revision};if(id)headers['Idempotency-Key']=id;
      const response=await context.request.post(base+'/api/tokens/rotate',{headers,data:{length:64}}),body=await response.json();
      if(typeof body.token==='string')rotatedTokens.add(body.token);
      assert.equal(response.status(),status);assert.equal(body.detail.code,code);rawRejections++;
    }
    await mcpWorks(originalToken);
    if(fixture){assert.equal(fixture.state.mcpRotations,0);assert.equal(fixture.state.mcpRestarts,0);}
    await page.evaluate(()=>{const now=Date.now;Date.now=()=>now()+3600000;});
    await go(page,'mcp');
    const rotate=page.getByRole('button',{name:'產生並顯示新連線網址',exact:true});
    let sent=0;const ids=[];
    page.on('request',r=>{if(r.url().endsWith('/api/tokens/rotate')||r.url().endsWith('/api/settings/mcp/restart')){sent++;ids.push(r.headers()['idempotency-key']);}});
    page.once('dialog',d=>d.dismiss());await rotate.click();assert.equal(sent,0,'Cancel must not rotate');
    const response=page.waitForResponse(r=>r.url().endsWith('/api/tokens/rotate'));
    page.once('dialog',d=>d.accept());await rotate.click();const rotated=await response,body=await rotated.json();
    if(typeof body.token==='string')rotatedTokens.add(body.token);
    assert.equal(rotated.status(),200,'GUI rotation must satisfy shared core operation ID validation');
    const token=body.token;assert(typeof token==='string'&&token.length>0);assert.notEqual(token,originalToken);
    await page.waitForFunction(()=>[...document.querySelectorAll('pre')].some(n=>n.textContent.includes('/private_')));
    assert((await page.locator('#content pre').first().textContent()).includes('/private_'+token+'/mcp'));
    if(fixture)assert.equal(fixture.state.mcpRotations,1);
    await mcpWorks(token);await denied(originalToken);await screenshot(page,'mcp-rotated-masked');
    const restartResponse=page.waitForResponse(r=>r.url().endsWith('/api/settings/mcp/restart'));
    await page.getByRole('button',{name:'重新啟動 MCP',exact:true}).click();assert.equal((await restartResponse).status(),200);
    if(fixture)assert.equal(fixture.state.mcpRestarts,1);
    assert.equal(sent,2);assert.equal(new Set(ids).size,2);
    for(const id of ids)assert(/^[0-9]{13}\.[a-f0-9]{32}$/.test(id),'timestamp + UUID hex');
    assert.deepEqual(errors,[]);
  }finally{
    try{
      if(restoreRequired){
        // The rotation/restart changed the revision; always obtain a fresh read.
        const current=await context.request.get(base+'/api/tokens');assert(current.ok());
        const snapshot=await current.json(),revision=current.headers().etag;assert(revision);
        const restored=await context.request.put(base+'/api/tokens',{headers:{Origin:base,'If-Match':revision,'Idempotency-Key':operationId(snapshot.configuration.server_time_ms)},data:{token:originalToken}});
        assert.equal(restored.status(),200,'Original fixture token must be restored');
        const read=await context.request.get(base+'/api/tokens');assert(read.ok());assert.equal((await read.json()).state,'active');
        // Read-only retries allow the GUI restart to finish starting its child.
        for(let attempt=0;;attempt++){
          try{await mcpWorks(originalToken);break;}
          catch(error){if(attempt===5)throw error;await delay(500);}
        }
        for(const token of rotatedTokens)if(token!==originalToken)await denied(token);
        if(fixture){assert.equal(fixture.state.mcpToken,originalToken);assert.equal(fixture.state.mcpRestores,1);}
        console.log(JSON.stringify({original_token_restored:true,original_mcp_health:true,rotated_tokens_revoked:rotatedTokens.size}));
      }
    }finally{await env.close();}
  }
  console.log(JSON.stringify({raw_rejections:rawRejections,gui_mutations:2,clock_skew_minutes:60}));
});
