// Controlled GUI regression for profile CRUD, request scope and state clearing.
import {start,go,operation,fill,run,assert} from './common.mjs';

await run('profiles',async()=>{
  const env=await start({requireLogin:true}),{page,fixture,base,errors}=env;
  assert(fixture,'Profile writes run against the controlled server only');
  const requests=[];
  page.on('request',request=>{
    const path=new URL(request.url()).pathname;
    if(path.startsWith('/api/'))requests.push({path,method:request.method(),headers:request.headers(),cookie:request.headerValue('cookie'),request});
  });
  try{
    await page.reload();
    await page.locator('#content').getByRole('heading',{name:'目前裝置'}).waitFor();
    assert.equal(await page.locator('#profile-select').inputValue(),'default');
    assert.equal(await page.locator('#profile-delete').isDisabled(),true);
    assert.equal(await page.locator('#profile-rename').isDisabled(),true);

    const unsafeLabel='<img src=x onerror=alert(1)> Alpha';
    await page.locator('#profile-create').click();
    await page.locator('#profile-label').fill(unsafeLabel);
    await page.locator('#profile-save').click();
    await page.waitForFunction(()=>document.querySelector('#profile-select')?.value.startsWith('profile-'));
    const createdId=await page.locator('#profile-select').inputValue();
    await page.locator('#content').getByRole('heading',{name:'先連接組織的 Headscale'}).waitFor();
    assert((await page.locator('#profile-select option:checked').textContent()).includes(unsafeLabel));
    assert.equal(await page.locator('#profile-select img').count(),0,'Profile labels must be inserted as text');
    assert.equal(await page.locator('#content').getByText('目前裝置').count(),0,'New profile has no default overview');

    await page.locator('#profile-rename').click();
    await page.locator('#profile-label').fill('第二組連線');
    await page.locator('#profile-save').click();
    await page.waitForFunction(()=>document.querySelector('#profile-select option:checked')?.textContent.includes('第二組連線'));
    page.once('dialog',dialog=>dialog.dismiss());
    await page.locator('#profile-delete').click();
    assert.equal(await page.locator('#profile-select').inputValue(),createdId,'Cancelled deletion keeps the profile');
    await page.reload();
    await page.locator('#content').getByRole('heading',{name:'先連接組織的 Headscale'}).waitFor();
    assert.equal(await page.locator('#profile-select').inputValue(),createdId,'Selection survives a reload');

    await go(page,'mcp');
    const newRotation=page.waitForResponse(r=>r.url().endsWith('/api/tokens/rotate'));
    page.once('dialog',dialog=>dialog.accept());
    await page.getByRole('button',{name:'產生並顯示新連線網址'}).click();
    assert.equal((await newRotation).status(),200);
    const defaultToken=fixture.state.mcpToken;
    await page.waitForFunction(()=>document.querySelector('#content pre')?.textContent.includes('fixture-rotated-profile-'));
    await page.locator('#profile-select').selectOption('default');
    await page.locator('#content').getByRole('heading',{name:'工具權限'}).waitFor();
    assert.equal(await page.locator('#content').getByText('fixture-rotated-profile-', {exact:false}).count(),0,'Switch removes revealed token');
    assert.equal(fixture.state.mcpToken,defaultToken,'Other profile rotation leaves the default token unchanged');

    const defaultRotation=page.waitForResponse(r=>r.url().endsWith('/api/tokens/rotate'));
    page.once('dialog',dialog=>dialog.accept());
    await page.getByRole('button',{name:'產生並顯示新連線網址'}).click();
    assert.equal((await defaultRotation).status(),200,'Revision must be reloaded for the selected profile');

    await go(page,'users');
    const createUser=await operation(page,'headscale_users','create');
    await fill(createUser,'name','BrowserSwitch');
    await fill(createUser,'display_name','Browser switch check');
    fixture.delay(200);
    const readback=page.waitForRequest(r=>r.url().endsWith('/tools/headscale_users')&&r.postDataJSON()?.arguments.operation==='list');
    await createUser.locator('button[type="submit"]').click();
    await readback;
    await page.locator('#profile-select').selectOption(createdId);
    await page.locator('[data-tool="headscale_users"]').waitFor();
    await page.waitForTimeout(250);
    assert((await page.locator('#notice').innerText()).includes('未確認的請求'),'Interrupted write must direct the user to read back its state');
    assert(!(await page.locator('#notice').innerText()).includes('已收到寫入回應'),'Old readback must not show a success notice in the new profile');
    fixture.delay(0);
    await page.locator('#profile-select').selectOption('default');
    await go(page,'overview');
    fixture.delayConnection(300);
    const slow=page.waitForRequest(r=>r.url().endsWith('/api/headscale/connection')&&r.headers()['x-mcp-profile']==='default');
    await page.locator('#navigation a[href="#setup"]').click();
    await slow;
    await page.locator('#profile-select').selectOption(createdId);
    await page.getByRole('textbox',{name:'Headscale API 位址'}).waitFor();
    assert.equal(await page.getByRole('textbox',{name:'Headscale API 位址'}).inputValue(),'');
    await page.waitForTimeout(400);
    assert.equal(await page.getByRole('textbox',{name:'Headscale API 位址'}).inputValue(),'','Old response must not restore the previous connection');
    fixture.delayConnection(0);

    page.once('dialog',dialog=>dialog.accept());
    await page.locator('#profile-delete').click();
    await page.waitForFunction(()=>document.querySelector('#profile-select')?.value==='default'&&document.querySelector('#profile-select')?.options.length===1);
    assert.equal(await page.locator('#profile-delete').isDisabled(),true);
    await page.locator('#logout').click();
    await page.locator('#login').waitFor({state:'visible'});
    await page.locator('#password').fill('fixture-admin-password');
    await page.locator('#login-form button').click();
    await page.locator('#workspace').waitFor({state:'visible'});
    assert.equal(await page.locator('#profile-select').inputValue(),'default');

    assert(requests.some(r=>r.path==='/api/headscale/connection'&&r.headers['x-mcp-profile']===createdId));
    assert(requests.some(r=>r.path==='/api/headscale/catalog'&&r.headers['x-mcp-profile']===createdId));
    assert(requests.some(r=>r.path==='/api/tokens/rotate'&&r.headers['x-mcp-profile']===createdId));
    assert(requests.some(r=>r.path==='/api/headscale/catalog'&&r.headers['x-mcp-profile']==='default'));
    for(const request of requests){
      const exempt=/^\/api\/(auth|profiles)(\/|$)/.test(request.path);
      assert.equal(request.headers['x-mcp-profile']===undefined,exempt,request.path+' profile scope');
      if(request.headers.origin)assert.equal(request.headers.origin,base,request.path+' Origin');
      if(request.path!=='/api/auth/login'&&await request.request.response())
        assert((await request.cookie)?.includes('fixture-session=yes'),request.path+' session cookie');
    }
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({created:true,renamed:true,deleted:true,reload_selection:true,secret_cleared:true,stale_response_ignored:true,requests_checked:requests.length}));
  }finally{await env.close();}
});
