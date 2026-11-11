"""確認 n8n-mcp 子行程在映像內的 Node 下真的用原生 better-sqlite3，而不是靜默退回 sql.js。

n8n-mcp 把 better-sqlite3 列為 optional dependency：沒有對應 Node ABI 的預建檔時（例如它宣告的 11.x 對
Node 24／26 的 ABI 137／147），``npm ci`` 不報錯、直接略過，上游 ``createDatabaseAdapter`` 再退回
sql.js——記憶體約多一倍、沒有 FTS5，``search_nodes`` 改走 LIKE、排序變差。整個過程沒有任何錯誤，只看映像
建置成功與否看不出來。

這裡不是看檔案在不在，而是照 launcher 的做法（``database.prepare_database`` 複製種子 catalog、``build_env``
的環境）實際用上游自己的 ``dist/database/database-adapter.js`` 開那份暫存複本（不動映像內的 data/nodes.db），
確認：

- 上游選到 ``BetterSQLiteAdapter``，而且這個 node 行程確實載入了 ``better_sqlite3.node``；
- FTS5 可用，種子 catalog 的 ``nodes_fts`` 查得到資料（``search_nodes`` 靠它排序）；
- 載入的 better-sqlite3 版本等於 Dockerfile 的 ``BETTER_SQLITE3_VERSION``（＝ package.json 的 overrides）。

用法（映像建置時與沙盒內）::

    python -m n8n_mcp_admin.sqlite_check --expect-version 12.11.1 [n8n-mcp 套件目錄，預設 npm root -g]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile

from . import launcher, upstream
from .database import prepare_database

_PROBE_JS = r"""
const fs = require('fs');
const path = require('path');
const { createRequire } = require('module');
const [pkg, dbPath] = process.argv.slice(1);
const adapterFile = fs.realpathSync(path.join(pkg, 'dist', 'database', 'database-adapter.js'));
// 與上游 require('better-sqlite3') 相同的解析起點。
const upstreamRequire = createRequire(adapterFile);
const out = { node: process.version, abi: process.versions.modules };
try {
  out.betterSqlite3 = upstreamRequire('better-sqlite3/package.json').version;
} catch (error) {
  out.betterSqlite3 = null;
}
require(adapterFile).createDatabaseAdapter(dbPath).then((db) => {
  out.adapter = db.constructor.name;
  out.nativeMapped = fs.readFileSync('/proc/self/maps', 'utf8').includes('better_sqlite3.node');
  out.fts5 = db.checkFTS5Support();
  try {
    out.ftsMatch = db.prepare("SELECT count(*) AS n FROM nodes_fts WHERE nodes_fts MATCH 'webhook'").get().n > 0;
  } catch (error) {
    out.ftsMatch = false;
  }
  try {
    out.sqlite = db.prepare('SELECT sqlite_version() AS v').get().v;
  } catch (error) {
    out.sqlite = null;
  }
  db.close();
  process.stdout.write('\n' + JSON.stringify(out) + '\n');
}, (error) => {
  console.error(error);
  process.exit(1);
});
"""

# 只比對這些鍵；node／abi／sqlite 版本只列在輸出裡供建置紀錄查看。
NATIVE = {"adapter": "BetterSQLiteAdapter", "nativeMapped": True, "fts5": True, "ftsMatch": True}


def expected(version: str) -> dict:
    return {**NATIVE, "betterSqlite3": version}


def probe(package_dir: str | None = None) -> dict:
    """以 launcher 的環境、上游的 ``createDatabaseAdapter`` 開種子 catalog 的暫存複本，回報選到的 adapter。"""
    pkg = package_dir or upstream.package_dir()
    with tempfile.TemporaryDirectory() as home:
        database_path = prepare_database(pkg, os.path.join(home, "config.json"))
        env = launcher.build_env("127.0.0.1", 3000, {"PATH": os.environ.get("PATH", ""), "HOME": home})
        env["NODE_DB_PATH"] = database_path
        result = subprocess.run(
            ["node", "-e", _PROBE_JS, pkg, database_path],
            env=env, capture_output=True, text=True, timeout=120, check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"node probe failed (rc={result.returncode}): {result.stderr.strip()[-500:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the n8n-mcp child uses native better-sqlite3.")
    parser.add_argument("--expect-version", required=True, help="better-sqlite3 版本（Dockerfile 的 BETTER_SQLITE3_VERSION）")
    parser.add_argument("package_dir", nargs="?", help="n8n-mcp 套件目錄（預設 npm root -g）")
    args = parser.parse_args(argv)
    data = probe(args.package_dir)
    want = expected(args.expect_version)
    ok = all(data.get(key) == value for key, value in want.items())
    label = "native better-sqlite3" if ok else "NOT native better-sqlite3 (sql.js fallback?)"
    print(f"[n8n-mcp sqlite] {label}: {json.dumps(data, sort_keys=True)}")
    if not ok:
        print(f"[n8n-mcp sqlite] expected: {json.dumps(want, sort_keys=True)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
