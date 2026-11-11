// Exercise the external-target code path without contacting any real backend.
// Includes private pointer loading, GUI login, independent bearer-only oracle,
// fresh registration IDs, and a native-state disagreement that must fail.
import {spawn} from 'node:child_process';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomBytes} from 'node:crypto';
import assert from 'node:assert/strict';
import {controlledServer} from './controlled-server.mjs';

async function child(args,script='forms'){
  const process=spawn(globalThis.process.execPath,[new URL('./'+script+'.mjs',import.meta.url).pathname,...args],{stdio:['ignore','pipe','pipe']});
  let out='',err='';process.stdout.on('data',d=>out+=d);process.stderr.on('data',d=>err+=d);
  const code=await new Promise((resolve,reject)=>{process.on('error',reject);process.on('close',resolve);});
  // Never forward child output or exception messages (they can contain secrets).
  return {code,records:(out+err).trim().split('\n').filter(Boolean).map(s=>JSON.parse(s))};
}
let fixture,dir;
try{
  dir=await mkdtemp(join(tmpdir(),'headscale-workflow-harness-'));
  const credentials=join(dir,'state.json'),pointer=join(dir,'location.json'),pendingFile=join(dir,'pending.json');
  await writeFile(credentials,JSON.stringify({admin_password:'fixture-admin-password',headscale_api_key:'fixture-native-key',mcp_auth_token:'fixture-original-mcp-token'}),{mode:0o600});
  await writeFile(pointer,JSON.stringify({path:credentials}),{mode:0o600});
  fixture=await controlledServer({debugEnabled:true,requireLogin:true,mcpSSE:true});
  const args=['--base-url',fixture.base,'--oracle-url',fixture.base,'--credentials-file',pointer,'--isolated','--debug-nodes','--tag','tag:test'];
  const first=await child([...args,'--output',join(dir,'synthetic')]);
  assert.equal(first.code,0);const synthetic=first.records.find(r=>r.positive_form_variants);
  assert.equal(synthetic.positive_form_variants,26);assert.equal(synthetic.native_operations,25);assert.equal(synthetic.cleanup_confirmed,true);
  const pending=Object.fromEntries(['register','legacy','approve','reject'].map(op=>[op,'hskey-authreq-'+randomBytes(18).toString('base64url')]));
  for(const id of Object.values(pending))fixture.pending(id);
  await writeFile(pendingFile,JSON.stringify(pending),{mode:0o600});
  const second=await child([...args,'--pending-file',pendingFile,'--backfill','--output',join(dir,'pending')]);
  assert.equal(second.code,0);const complete=second.records.find(r=>r.positive_form_variants);
  assert.equal(complete.positive_form_variants,29);assert.equal(complete.native_operations,28);
  assert.equal(complete.registrations.filter(r=>r.source==='dedicated-client').length,2);
  assert.equal(fixture.state.users.length,1);assert.equal(fixture.state.nodes.length,1);assert.equal(fixture.state.preAuthKeys.length,0);assert.equal(fixture.state.apiKeys.length,0);
  assert.equal(fixture.state.mcpRotations,0);assert.equal(fixture.state.mcpRestarts,0);
  // Preflight, list, then create: only the third native user read disagrees with
  // the successful GUI response/readback. This must prevent a false pass.
  fixture.nativeFault({path:'/api/v1/user',after:3,response:{users:[]}});
  const negative=await child([...args,'--output',join(dir,'mismatch')]);
  assert.equal(negative.code,1);assert(negative.records.some(r=>r.failed_phase==='headscale_users:create'));
  assert(fixture.nativeCalls.every(c=>c.method==='GET'));
  const coreArgs=['--base-url',fixture.base,'--credentials-file',pointer,'--isolated','--output',join(dir,'core')];
  const core=await child(coreArgs,'mcp-actions');assert.equal(core.code,0);
  assert(core.records.some(r=>r.raw_rejections===5&&r.gui_mutations===2));
  assert(core.records.some(r=>r.original_token_restored&&r.original_mcp_health&&r.rotated_tokens_revoked===1));
  fixture.failNextRestart();
  const interrupted=await child(coreArgs,'mcp-actions');assert.equal(interrupted.code,1);
  assert(interrupted.records.some(r=>r.original_token_restored&&r.original_mcp_health&&r.rotated_tokens_revoked===1));
  assert.equal(fixture.state.mcpToken,'fixture-original-mcp-token');assert.equal(fixture.state.mcpRestores,2);
  assert.equal(fixture.state.mcpRotations,2);assert.equal(fixture.state.mcpRestarts,1);
  console.log(JSON.stringify({suite:'workflow-harness',status:'PASS',target:'local-controlled-only',external_path_forms:synthetic.positive_form_variants,with_pending_and_backfill_forms:complete.positive_form_variants,native_disagreement_rejected:true,core_success_restored:true,core_failure_restored:true,mcp_sse_verified:true}));
}catch(error){console.error(JSON.stringify({suite:'workflow-harness',status:'FAIL',error_type:error.name,location:error.stack?.split('\n').find(line=>/workflow-harness.mjs:\d+/.test(line))?.trim()}));process.exitCode=1;}
finally{await fixture?.close();if(dir)await rm(dir,{recursive:true,force:true});}
