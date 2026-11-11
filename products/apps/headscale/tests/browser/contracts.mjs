// Read-only schema/accessibility regression; also runs against --static-root from ba494dd.
import {start,go,run,assert} from './common.mjs';
await run('array-contracts',async()=>{
  const env=await start(),{page,errors}=env;
  try{
    await go(page,'routes');const routes=page.locator('[name="routes"]');
    assert.equal(await routes.getAttribute('required'),null,'required array property does not imply minItems');
    await routes.fill('');assert(await routes.evaluate(n=>n.checkValidity()),'Empty routes are valid');
    await go(page,'devices');await page.locator('.task-picker').selectOption({label:'設定標籤'});
    const tags=page.locator('[name="tags"]');
    for(const value of ['', ' \n , \t ',Array(129).fill('tag:test').join('\n')]){
      await tags.fill(value);assert.equal(await tags.evaluate(n=>n.checkValidity()),false,'minItems/maxItems must validate parsed array');
    }
    await tags.fill('tag:test');assert(await tags.evaluate(n=>n.checkValidity()));
    await go(page,'pairing');
    const keys=page.locator('section.card').filter({has:page.getByRole('heading',{name:'裝置加入金鑰',exact:true})});
    await keys.locator('.task-picker').selectOption({label:'建立'});
    await keys.locator('[name="tags"]').fill('');assert(await keys.locator('[name="tags"]').evaluate(n=>n.checkValidity()),'Optional preauth tags may be empty');
    assert.deepEqual(errors,[]);
  }finally{await env.close();}
});
