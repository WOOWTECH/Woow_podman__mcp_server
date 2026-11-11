// node --test packages/mcp-admin-ui/src/mcpUrl.test.js
// Tokens 頁直接顯示完整 MCP 網址（第三輪 G4）；沒揭露時只有佔位、看不到 token。
import test from 'node:test';
import assert from 'node:assert/strict';
import { TOKEN_PLACEHOLDER, claudeCodeAddCommand, mcpUrl } from './mcpUrl.js';

test('the full MCP URL is the page origin plus /private_<token>/mcp', () => {
  assert.equal(mcpUrl('https://paas-cs-1.woowtech.io', 'abc123'), 'https://paas-cs-1.woowtech.io/private_abc123/mcp');
  assert.equal(mcpUrl('https://paas-cs-1.woowtech.io/', 'abc123'), 'https://paas-cs-1.woowtech.io/private_abc123/mcp');
});

test('without a revealed token only the placeholder appears', () => {
  for (const token of [null, undefined, '']) {
    const url = mcpUrl('https://h.example', token);
    assert.equal(url, `https://h.example/private_${TOKEN_PLACEHOLDER}/mcp`);
    assert.doesNotMatch(url, /private_(?!<TOKEN>)/);
  }
});

test('the Claude Code command uses the same URL', () => {
  const url = mcpUrl('https://h.example', 'tok');
  assert.equal(claudeCodeAddCommand('my-odoo-mcp', url), 'claude mcp add --transport http my-odoo-mcp "https://h.example/private_tok/mcp"');
});
