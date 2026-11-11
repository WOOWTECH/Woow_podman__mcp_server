"""確認 launcher 給 n8n-mcp 子行程的環境下，上游 telemetry 與 npm 版本檢查確實是關的（woow_paas_platform#1615）。

不是看我們自己設了什麼變數，而是用 launcher 的 ``build_env()`` 實際載入上游
``dist/telemetry/config-manager.js``，問它 ``isEnabled()``——上游改了開關名稱或判斷方式時就會失敗。
並且故意模擬「有人想打開」：``~/.n8n-mcp/telemetry.json`` 寫 ``enabled: true``、繼承的環境變數寫
``N8N_MCP_TELEMETRY_DISABLED=false``，結果仍必須是關的、也不能印出上游的 first-run「Usage Telemetry」說明框。

npm 版本檢查同理：繼承 ``N8N_MCP_VERSION_CHECK_DISABLED=false``，用 launcher 的環境呼叫上游
``dist/utils/npm-version-checker.js`` 的 ``checkNpmVersion(true)``，結果必須是 ``status: "disabled"``，
而且攔截 ``fetch`` 後確認沒有發出任何請求。

用法（映像建置時與沙盒內）::

    python -m n8n_mcp_admin.telemetry_check [n8n-mcp 套件目錄，預設 npm root -g]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from . import launcher, upstream

_PROBE_JS = r"""
const path = require('path');
const pkg = process.argv[1];
const m = require(path.join(pkg, 'dist', 'telemetry', 'config-manager.js'))
  .TelemetryConfigManager.getInstance();
const out = { disabledByEnv: m.isDisabledByEnvironment(), enabled: m.isEnabled() };
let requests = 0;
globalThis.fetch = async () => {
  requests += 1;
  throw new Error('npm version check must not reach the network');
};
require(path.join(pkg, 'dist', 'utils', 'npm-version-checker.js')).checkNpmVersion(true).then((result) => {
  out.versionCheck = result.status;
  out.versionCheckRequests = requests;
  process.stdout.write('\n' + JSON.stringify(out) + '\n');
}, (error) => {
  console.error(error);
  process.exit(1);
});
"""

FIRST_RUN_BANNER = "Usage Telemetry"
EXPECTED = {"disabledByEnv": True, "enabled": False, "banner": False,
            "versionCheck": "disabled", "versionCheckRequests": 0}


def probe(package_dir: str | None = None) -> dict:
    """以 launcher 的環境跑上游 config manager 與 npm 版本檢查，回傳和 ``EXPECTED`` 同樣的鍵。"""
    pkg = package_dir or upstream.package_dir()
    with tempfile.TemporaryDirectory() as home:
        cfg_dir = os.path.join(home, ".n8n-mcp")
        os.makedirs(cfg_dir)
        with open(os.path.join(cfg_dir, "telemetry.json"), "w", encoding="utf-8") as fh:
            json.dump({"enabled": True, "userId": "telemetry-check"}, fh)
        base = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": home,
            "N8N_MCP_TELEMETRY_DISABLED": "false",
            "TELEMETRY_DISABLED": "false",
            "DISABLE_TELEMETRY": "false",
            "N8N_MCP_TELEMETRY_URL": "http://127.0.0.1:9/",
            "N8N_MCP_VERSION_CHECK_DISABLED": "false",
        }
        env = launcher.build_env("127.0.0.1", 3000, base)
        result = subprocess.run(
            ["node", "-e", _PROBE_JS, pkg], env=env, capture_output=True, text=True, timeout=60, check=False
        )
    if result.returncode != 0:
        raise RuntimeError(f"node probe failed (rc={result.returncode}): {result.stderr.strip()[-500:]}")
    data = json.loads(result.stdout.strip().splitlines()[-1])
    data["banner"] = FIRST_RUN_BANNER in (result.stdout + result.stderr)
    return data


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    data = probe(args[0] if args else None)
    ok = data == EXPECTED
    print(f"[n8n-mcp telemetry／version check] {'disabled' if ok else 'NOT disabled'}: {json.dumps(data)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
