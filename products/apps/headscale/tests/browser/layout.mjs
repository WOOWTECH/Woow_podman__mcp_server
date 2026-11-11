// Read-only external run: --base-url ORIGIN --credentials-file /private/state.json
// Tests all 10 pages and every enabled form variant at 320/390/768/1440 CSS px.
import {start,go,pages,screenshot,run,assert} from './common.mjs';
await run('layout',async()=>{
  const env=await start(),{page,errors}=env;
  let variants=0,controls=0;
  async function geometry(){
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Document must fit viewport');
    assert.notEqual(await page.locator('body').evaluate(n=>getComputedStyle(n).overflowX),'hidden','Do not hide page overflow');
    const result=await page.evaluate(()=>{
      const targets=[...document.querySelectorAll('button,input,select,textarea,summary,#navigation a,.brand')].filter(n=>n.checkVisibility());
      for(const n of targets){
        n.scrollIntoView({block:'center',inline:'nearest'});
        const r=n.getBoundingClientRect();
        if(r.left<-.5||r.right>innerWidth+.5||r.width<1)return {ok:false};
        const hit=document.elementFromPoint(r.left+r.width/2,r.top+Math.min(r.height/2,20));
        if(!hit||!(n.contains(hit)||hit.contains(n)))return {ok:false};
      }
      return {ok:true,count:targets.length};
    });
    assert(result.ok,'Control must be reachable without clipping or overlap');controls+=result.count;
  }
  try{
    for(const width of [320,390,768,1440]){
      await page.setViewportSize({width,height:900});
      for(const name of pages){
        await go(page,name);await geometry();
        const pickers=page.locator('.task-picker');
        for(let i=0;i<await pickers.count();i++){
          const picker=pickers.nth(i),count=await picker.locator('option').count();
          for(let j=0;j<count;j++){await picker.selectOption(String(j));await geometry();variants++;}
        }
        if(name==='setup'){
          await page.getByText('PaaS 服務整合（選填）',{exact:true}).click();await geometry();
        }
        if(name==='mcp'){
          const checkboxes=page.locator('.check-row input[type="checkbox"]');assert.equal(await checkboxes.count(),12);
          for(let i=0;i<12;i++){
            const box=checkboxes.nth(i),id=await box.getAttribute('id');assert(id);
            const label=page.locator(`label[for="${id}"]`);const title=(await label.innerText()).split('\n')[0];assert(title);
            assert.equal(await page.getByRole('checkbox',{name:title,exact:false}).count()>=1,true);
            const checked=await box.isChecked();await label.click();assert.equal(await box.isChecked(),!checked);
            await box.focus();await page.keyboard.press('Space');assert.equal(await box.isChecked(),checked);
            assert(await box.evaluate(n=>n===document.activeElement),'Keyboard focus remains visible on checkbox');
          }
          await page.getByText('依操作調整權限',{exact:true}).click();await geometry();
        }
        await page.evaluate(()=>scrollTo(0,0));await screenshot(page,`${name}-${width}`);
      }
    }
    assert.deepEqual(errors,[]);console.log(JSON.stringify({pages:40,form_variants:variants,controls,permission_checks:48,screenshots:40}));
  }finally{await env.close();}
});
