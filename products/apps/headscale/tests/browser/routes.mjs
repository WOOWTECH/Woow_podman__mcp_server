// Local: PLAYWRIGHT_MODULE=/path/to/playwright CHROMIUM_PATH=/path/to/chrome node routes.mjs
// Isolated candidate (only after coordinator readiness): add --base-url http://127.0.0.1:19984
// --credentials-file /private/state-or-location.json --isolated --node-id OWN_TEST_NODE_ID
// The supplied node's original approved routes are restored on success. Failure stops writes.
import {start,go,task,fill,submit,screenshot,run,assert,options} from './common.mjs';
await run('routes',async()=>{
  const env=await start({mutations:true}),{page,fixture,errors}=env;
  try{
    const nodeId=options['node-id']||(fixture?'201':null);assert(nodeId,'External run requires --node-id');
    const writes=[],reads=[];
    page.on('request',r=>{if(r.url().endsWith('/tools/headscale_routes_replace'))writes.push(r.postDataJSON().arguments);if(r.url().endsWith('/tools/headscale_nodes')&&r.postDataJSON()?.arguments.operation==='get')reads.push(r);});
    await go(page,'routes');const card=task(page,'headscale_routes_replace');await fill(card,'node_id',nodeId);
    const response=page.waitForResponse(r=>r.url().endsWith('/tools/headscale_nodes'));
    await card.getByRole('button',{name:'讀取目前核准路由',exact:true}).click();
    const original=(await (await response).json()).node.approvedRoutes;assert(Array.isArray(original));
    const two=['10.20.0.0/16','192.168.20.0/24'];
    await fill(card,'routes',two.join('\n'));
    await submit(page,card,{confirmation:text=>{assert(text.includes(nodeId));assert(text.includes('目前核准'));}});
    assert.deepEqual(writes.at(-1).routes,two);assert((await card.locator('.readback').innerText()).includes(two[1]));
    const before=writes.length;
    await fill(card,'routes','');
    assert.equal(await card.locator('[name="routes"]').getAttribute('required'),null,'Required JSON array must allow []');
    await submit(page,card,{accept:false,confirmation:text=>{assert(text.includes('撤銷全部'));assert(text.includes(nodeId));for(const route of two)assert(text.includes(route));}});
    assert.equal(writes.length,before,'Cancel cannot send a mutation');assert((await page.locator('#notice').innerText()).includes('已取消'));
    let confirmed=false;
    page.once('dialog',async d=>{assert(d.message().includes('撤銷全部'));confirmed=true;await d.accept();});
    await card.getByRole('button',{name:'撤銷全部核准路由',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#notice')?.textContent==='已讀回確認：全部核准路由已撤銷。');
    assert(confirmed);assert.deepEqual(writes.at(-1),{node_id:nodeId,routes:[]});assert(reads.length>=5,'Read before confirmation and after write');
    assert((await card.locator('.readback').innerText()).includes('無（全部已撤銷）'));
    assert((await card.locator('.readback').innerText()).includes('裝置公告路由'));
    await fill(card,'routes',two[0]);await submit(page,card);assert.deepEqual(writes.at(-1).routes,[two[0]]);
    assert((await card.locator('.readback').innerText()).includes(two[0]));
    const good=writes.length;
    for(const bad of ['[]','not-a-cidr']){
      await fill(card,'routes',bad);await card.locator('button[type="submit"]').click();
      assert.equal(await card.locator('[name="routes"]').evaluate(n=>n.checkValidity()),false);assert.equal(writes.length,good);
    }
    await fill(card,'routes','10.20.0.0/999');await submit(page,card);
    assert.equal(await page.locator('#notice').evaluate(n=>n.classList.contains('error')),true,'Upstream invalid CIDR error is visible');
    assert.equal(await card.locator('button[type="submit"]').isEnabled(),true,'Rejected write stays editable');
    await screenshot(page,'routes-invalid');
    if(fixture){
      await fill(card,'routes',two.join('\n'));fixture.delay(200);
      let writesBefore=writes.length;
      page.once('dialog',d=>d.accept());
      await card.locator('form').evaluate(f=>{f.requestSubmit();f.requestSubmit();f.dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));});
      await page.waitForFunction(()=>document.querySelector('form[aria-busy="true"]'));
      assert.equal(await card.locator('[name="node_id"]').isDisabled(),true);
      await page.waitForFunction(()=>!document.querySelector('form[aria-busy="true"]'));
      assert.equal(writes.length,writesBefore+1,'Duplicate submit events must send one write');fixture.delay(0);
      // Consume the first get as the successful pre-confirm read; fail only postwrite read.
      fixture.fault({name:'headscale_nodes',op:'get',response:{node:structuredClone(fixture.state.nodes[0])}});
      fixture.fault({name:'headscale_nodes',op:'get',response:{status:503,body:{detail:'READBACK_UNAVAILABLE'}}});
      await fill(card,'routes','');await submit(page,card);
      assert((await page.locator('#notice').innerText()).includes('讀回尚未確認'));
      assert.equal(await card.locator('button[type="submit"]').isDisabled(),true);
      writesBefore=writes.length;
      await card.getByRole('button',{name:'重新讀回目前狀態'}).click();await page.waitForFunction(()=>!document.querySelector('[data-tool="headscale_routes_replace"] button[type="submit"]').disabled);
      assert.equal(writes.length,writesBefore,'Retry readback cannot repeat mutation');
      assert.equal(await card.locator('button[type="submit"]').isEnabled(),true);
      // A lost mutation response may still have changed server state.
      fixture.fault({name:'headscale_routes_replace',afterWrite:true,response:{status:409,body:{detail:{code:'HEADSCALE_UPSTREAM_TIMEOUT',outcome:'unknown'}}}});
      await fill(card,'routes',two[0]);await submit(page,card);
      assert((await page.locator('#notice').innerText()).includes('寫入結果未確認'));
      assert.equal(await card.locator('button[type="submit"]').isDisabled(),true);
      writesBefore=writes.length;await card.getByRole('button',{name:'重新讀回目前狀態'}).click();await page.waitForFunction(()=>!document.querySelector('[data-tool="headscale_routes_replace"] button[type="submit"]').disabled);assert.equal(writes.length,writesBefore);
      assert((await card.locator('.readback').innerText()).includes(two[0]));
      // Concurrency/mismatch must not produce a success banner.
      fixture.fault({name:'headscale_nodes',op:'get',response:{node:structuredClone(fixture.state.nodes[0])}});
      fixture.fault({name:'headscale_nodes',op:'get',response:{node:{...fixture.state.nodes[0],approvedRoutes:two}}});
      await fill(card,'routes','');await submit(page,card);
      assert((await page.locator('#notice').innerText()).includes('不一致'));
      await card.getByRole('button',{name:'重新讀回目前狀態'}).click();await page.waitForFunction(()=>!document.querySelector('[data-tool="headscale_routes_replace"] button[type="submit"]').disabled);
    }
    await fill(card,'routes',original.join('\n'));await submit(page,card);
    assert.equal(await page.locator('#notice').evaluate(n=>n.classList.contains('error')),false);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({checks:'empty-array,cancel,confirm,readback,retain-one,invalid-CIDR,restore',faults:!!fixture,writes:writes.length,reads:reads.length}));
  }finally{await env.close();}
});
