// node --test packages/mcp-admin-ui/src/ownerDefaults.test.js
import test from 'node:test';
import assert from 'node:assert/strict';
import { ownerDefaultsMessage, tokenSentence } from './ownerDefaults.js';

const decision = '負責人 2026-10-25「預設全開」';
const AUTO = /MCP token 已在第一次啟動時自動產生，完整網址見 Tokens 頁/;
const autoIssued = { token_state: 'active', token_auto_issued: true };

test('migrated keeps the one-time migration wording', () => {
  const text = ownerDefaultsMessage({ decision, applied: 'migrated', source: 'existing_config', at: '2026-10-25T09:00:00+00:00' });
  assert.match(text, /既有設定已於 2026-10-25T09:00:00\+00:00 一次性改成全開/);
});

test('existing config that needed no change says it was already in use, not a new install', () => {
  const text = ownerDefaultsMessage({ decision, applied: 'fresh_install', source: 'existing_config', at: 't' });
  assert.match(text, /既有設定本來就是全開/);
  assert.match(text, /已經有 MCP token、連線設定或登入紀錄，t 第一次以這一版啟動時只寫上標記、沒有改動/);
  assert.doesNotMatch(text, /新安裝|新開通/);
});

test('an untouched deploy seed (a new PaaS service) is not called an existing config or an upgrade', () => {
  const text = ownerDefaultsMessage({ decision, applied: 'fresh_install', source: 'deploy_seed', at: 't', ...autoIssued });
  assert.match(text, /部署時寫入的設定本來就是全開/);
  assert.match(text, /t 第一次啟動時寫上標記（當時還沒有 MCP token、連線設定或登入紀錄，新開通的服務就是這樣）/);
  assert.match(text, AUTO);
  assert.doesNotMatch(text, /既有設定|升級/);
});

test('a migrated deploy seed still reports the one-time change', () => {
  const text = ownerDefaultsMessage({ decision, applied: 'migrated', source: 'deploy_seed', at: 't' });
  assert.match(text, /一次性改成全開/);
});

test('config created by the admin process is a new install', () => {
  const text = ownerDefaultsMessage({ decision, applied: 'fresh_install', source: 'new_config', ...autoIssued });
  assert.match(text, /新安裝即全開/);
  assert.match(text, AUTO);
});

test('old markers without source use a neutral wording', () => {
  const text = ownerDefaultsMessage({ decision, applied: 'fresh_install', source: null, at: null });
  assert.match(text, /新安裝，或既有設定本來就沒有限制/);
  assert.equal(ownerDefaultsMessage(null), null);
});

// 審查第三輪 G4：「已自動產生」只看後端給的 token 事實，不看 source。
test('the auto-issue sentence needs a token that was really issued automatically', () => {
  for (const source of ['deploy_seed', 'new_config']) {
    // 管理員自己輪替過（或第二輪映像開出後自己 Rotate）：現在的 token 不是自動發的
    const rotated = ownerDefaultsMessage({ decision, applied: 'fresh_install', source, token_state: 'active', token_auto_issued: false });
    assert.doesNotMatch(rotated, /自動產生/);
    // 後端沒給 token 事實（舊版 admin）：不提 token
    const legacy = ownerDefaultsMessage({ decision, applied: 'fresh_install', source });
    assert.doesNotMatch(legacy, /MCP token 已|目前還沒有 MCP token/);
  }
});

test('a revoked token tells the admin to reissue it instead of pointing at a URL', () => {
  const text = ownerDefaultsMessage({ decision, applied: 'fresh_install', source: 'deploy_seed', at: 't',
    token_state: 'revoked', token_auto_issued: false });
  assert.match(text, /MCP token 已撤銷，用戶端都連不上；要恢復請到 Tokens 頁 Rotate Token 重發/);
  assert.doesNotMatch(text, AUTO);
  const migrated = ownerDefaultsMessage({ decision, applied: 'migrated', source: 'existing_config', at: 't', token_state: 'revoked' });
  assert.match(migrated, /MCP token 已撤銷/);
});

test('no token yet (for example the first write failed) says so', () => {
  const text = ownerDefaultsMessage({ decision, applied: 'fresh_install', source: 'deploy_seed', at: 't',
    token_state: 'unconfigured', token_auto_issued: false });
  assert.match(text, /目前還沒有 MCP token，用戶端連不上；請到 Tokens 頁 Rotate Token 產生/);
  assert.doesNotMatch(text, AUTO);
});

test('tokenSentence ignores an auto flag without an active token', () => {
  assert.equal(tokenSentence({ token_state: 'revoked', token_auto_issued: true }).includes('已撤銷'), true);
  assert.equal(tokenSentence({ token_auto_issued: true }), '');
  assert.equal(tokenSentence(null), '');
});
