"""存檔／測試連線的 SSRF 判定（mcp_admin_core.util.ssrf，Python 重寫）與上游 n8n-mcp 的實際判定一致。

core 的 ``tests/test_ssrf.py`` 拿 ``fixtures/ssrf_upstream_n8n_mcp_2.96.2.json``（以真的上游 2.96.2 記錄的判定表）比對
Python 的結果；這裡在有上游套件時（``N8N_MCP_PACKAGE_DIR``，與 ``test_real_upstream_package_when_available`` 相同）
直接用 node 跑上游 ``SSRFProtection.validateWebhookUrl``，確認判定表仍與套件一致。升 n8n-mcp 時這裡失敗＝上游改了
規則：先更新判定表，再改 ssrf.py 讓 core 的比對測試通過。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURE = (Path(__file__).resolve().parents[3] / "packages" / "mcp-admin-core" / "tests" / "fixtures"
           / "ssrf_upstream_n8n_mcp_2.96.2.json")

NODE_SCRIPT = r"""
const { createRequire } = require('node:module');
const path = require('node:path');
const [pkg, casesJson] = process.argv.slice(1);
const req = createRequire(path.join(pkg, 'package.json'));
process.env.LOG_LEVEL = 'error';
const { SSRFProtection } = req(path.join(pkg, 'dist', 'utils', 'ssrf-protection.js'));
(async () => {
  const out = {};
  for (const url of JSON.parse(casesJson)) {
    out[url] = {};
    for (const mode of ['strict', 'moderate', 'permissive']) {
      process.env.WEBHOOK_SECURITY_MODE = mode;
      out[url][mode] = (await SSRFProtection.validateWebhookUrl(url)).valid;
    }
  }
  console.log(JSON.stringify(out));
})();
"""


def test_upstream_package_still_decides_like_the_recorded_table():
    pkg = os.environ.get("N8N_MCP_PACKAGE_DIR")
    if not pkg or not os.path.isfile(os.path.join(pkg, "dist", "utils", "ssrf-protection.js")):
        pytest.skip("n8n-mcp 套件不在本機（設 N8N_MCP_PACKAGE_DIR 才會跑）")
    node = shutil.which("node")
    if node is None:
        pytest.skip("沒有 node")
    version = json.loads(Path(pkg, "package.json").read_text())["version"]
    recorded = json.loads(FIXTURE.read_text())["decisions"]
    result = subprocess.run([node, "-e", NODE_SCRIPT, pkg, json.dumps(list(recorded))], capture_output=True,
                            text=True, timeout=120, check=True)
    upstream = json.loads(result.stdout.strip().splitlines()[-1])
    changed = {url: {"recorded": recorded[url], "upstream": upstream[url]}
               for url in recorded if upstream[url] != recorded[url]}
    assert changed == {}, f"n8n-mcp {version} 的 SSRF 判定與記錄的 2.96.2 判定表不同"
