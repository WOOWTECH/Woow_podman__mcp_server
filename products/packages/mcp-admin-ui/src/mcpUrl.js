// Tokens 頁的 MCP 連線網址與 Claude Code 指令（第三輪 G4：管理頁直接看得到完整網址）。純 JS、不含 JSX，方便 node --test 直接測。
//
// 閘道路徑是 /private_<token>/mcp（Streamable HTTP）。token 還沒揭露時以 <TOKEN> 佔位——真的 token 只在按「顯示」
// 之後 60 秒內、視窗沒離開時出現（#85，TokenManager 的 hideRevealedToken），複製則一律向後端揭露後才組網址。

export const TOKEN_PLACEHOLDER = '<TOKEN>';

/** 完整 MCP 網址：origin（例如 https://paas-cs-xxxx.woowtech.io）＋ /private_<token>/mcp；沒有 token 就用佔位。 */
export function mcpUrl(origin, token) {
  const base = String(origin || '').replace(/\/+$/, '');
  return `${base}/private_${token || TOKEN_PLACEHOLDER}/mcp`;
}

/** Claude Code 的註冊指令（同一個網址）。 */
export function claudeCodeAddCommand(serverName, url) {
  return `claude mcp add --transport http ${serverName} "${url}"`;
}
