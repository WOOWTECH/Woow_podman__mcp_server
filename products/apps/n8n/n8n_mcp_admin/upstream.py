"""定位映像內全域安裝的上游 n8n-mcp 套件（launcher 與工具清單共用）。"""
from __future__ import annotations

import functools
import os
import subprocess

PACKAGE_NAME = "n8n-mcp"
# 上游建議的 HTTP 入口（MCP_MODE=http、不設 USE_FIXED_HTTP → SingleSessionHTTPServer）。
# 舊的 dist/http-server.js 是 v2.31.8 起標為 deprecated 的固定 HTTP 模式（woow_paas_platform#1531）。
ENTRYPOINT = os.path.join("dist", "mcp", "index.js")


@functools.lru_cache(maxsize=1)
def package_dir() -> str:
    """以 ``npm root -g`` 動態定位（不硬編 node 全域路徑，base image 換位置時才不會靜默失效）。"""
    override = os.environ.get("N8N_MCP_PACKAGE_DIR")
    if override:
        return override
    result = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, check=True, timeout=30)
    return os.path.join(result.stdout.strip(), PACKAGE_NAME)


def entrypoint() -> str:
    return os.path.join(package_dir(), ENTRYPOINT)
