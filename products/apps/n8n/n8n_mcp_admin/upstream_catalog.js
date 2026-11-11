#!/usr/bin/env node
// 從上游 n8n-mcp 套件讀出 tools/list 的來源定義（woow_paas_platform#1531）。
//
// tools/list 回傳的就是 dist/mcp/tools.js 的 n8nDocumentationToolsFinal＋dist/mcp/tools-n8n-manager.js
// 的 n8nManagementTools（後者在設定好 n8n API 後才出現）；可依操作停用的工具與合法操作名則是
// TOOL_OPERATION_PARAM＋tool-policy.getValidOperations()（＝DISABLED_TOOL_OPERATIONS 的比對基準）。
// 直接讀這些定義，管理頁在還沒填連線、子行程沒啟動時也能看到完整清單並預先停用。
//
// 用法：node upstream_catalog.js <n8n-mcp 套件目錄>；輸出單行 JSON 到 stdout。
'use strict';

const path = require('path');

const pkgDir = process.argv[2];
if (!pkgDir) {
  process.stderr.write('usage: upstream_catalog.js <n8n-mcp package dir>\n');
  process.exit(2);
}

// 上游模組載入時可能寫 log 到 stdout，避免污染輸出
const out = process.stdout.write.bind(process.stdout);
console.log = console.error;
console.info = console.error;
console.warn = console.error;
process.env.MCP_MODE = process.env.MCP_MODE || 'stdio';
process.env.N8N_MCP_TELEMETRY_DISABLED = 'true';

const req = (rel) => require(path.join(pkgDir, rel));
const version = req('package.json').version;
const docs = req('dist/mcp/tools.js').n8nDocumentationToolsFinal;
const mgr = req('dist/mcp/tools-n8n-manager.js');
const policy = req('dist/mcp/tool-policy.js');

const lower = (xs) => [...xs].map((x) => String(x).toLowerCase());

function pick(tool, group) {
  const param = mgr.TOOL_OPERATION_PARAM[tool.name] || null;
  return {
    name: tool.name,
    group,
    description: String(tool.description || ''),
    annotations: tool.annotations || {},
    operation_param: param,
    operation_default: mgr.TOOL_OPERATION_DEFAULT[tool.name] || null,
    operations: param ? lower(policy.getValidOperations(tool.name)) : [],
    destructive_operations: lower(mgr.DESTRUCTIVE_TOOL_OPERATIONS[tool.name] || []),
  };
}

const tools = [
  ...docs.map((t) => pick(t, 'documentation')),
  ...mgr.n8nManagementTools.map((t) => pick(t, 'management')),
];
out(JSON.stringify({ package: 'n8n-mcp', version, tools }) + '\n');
