"""n8n-mcp／MCP TS SDK／better-sqlite3 的版本以專用 lock 為準，其餘釘點要一起改（漏改在單元測試就失敗，不必等映像建置）。

釘點：``upstream-dependencies/package.json``（相依、overrides 與 allowScripts）、``package-lock.json``、Dockerfile 的
``ARG N8N_MCP_VERSION``／``N8N_MCP_SDK_VERSION``／``BETTER_SQLITE3_VERSION``（建置時核對的期望值，也是 release train
清冊讀的位置）、兩個建置期修補的 ``VERSION`` 守線，以及修補測試用的 fixture manifest。另外確認 Dockerfile 要求的
Node 版本落在上游 n8n-mcp 與 better-sqlite3 的 ``engines.node`` 範圍內，且各映像的 Node stage 與 CI 用同一版。
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from n8n_mcp_admin import upstream_direct_patch, upstream_result_patch, upstream_output_schema_patch

APP = Path(__file__).resolve().parents[1]
REPO = APP.parents[1]
DOCKERFILE = (APP / "Dockerfile").read_text()


def _lock_packages() -> dict:
    return json.loads((APP / "upstream-dependencies" / "package-lock.json").read_text())["packages"]


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _satisfies(version: str, spec: str) -> bool:
    """只支援上游 engines 用到的 ``^x.y.z``、``>=x.y.z``、``x.x`` 與 ``||``。"""
    current = _version(version)
    for clause in (c.strip() for c in spec.split("||")):
        if clause.startswith("^"):
            low = _version(clause[1:])
            if current[0] == low[0] and current >= low:
                return True
        elif clause.startswith(">="):
            if current >= _version(clause[2:]):
                return True
        elif re.fullmatch(r"\d+\.x", clause):
            if current[0] == int(clause[:-2]):
                return True
        else:
            raise AssertionError(f"unsupported engines clause: {clause}")
    return False


def _dockerfile_node_version() -> str:
    asserted = re.findall(r'test "\$\(node --version\)" = v(\d+\.\d+\.\d+)', DOCKERFILE)
    runtime = re.findall(r"^FROM node:(\d+\.\d+\.\d+)-bookworm-slim@sha256:[0-9a-f]{64} AS node-runtime$",
                         DOCKERFILE, re.MULTILINE)
    assert asserted and runtime and set(asserted) == set(runtime)
    return asserted[0]


def test_all_n8n_mcp_pins_agree() -> None:
    package = json.loads((APP / "upstream-dependencies" / "package.json").read_text())
    version = package["dependencies"]["n8n-mcp"]
    sdk = package["overrides"]["@modelcontextprotocol/sdk"]
    sqlite = package["overrides"]["better-sqlite3"]
    lock = _lock_packages()
    assert lock[""]["dependencies"]["n8n-mcp"] == version
    assert lock["node_modules/n8n-mcp"]["version"] == version
    assert lock["node_modules/@modelcontextprotocol/sdk"]["version"] == sdk
    assert lock["node_modules/better-sqlite3"]["version"] == sqlite
    # npm 11 起 install script 要明列核可（目前只警告、日後會擋）；只核可釘住的這一版，換版要重新核可。
    assert package["allowScripts"] == {f"better-sqlite3@{sqlite}": True}
    args = dict(re.findall(r"^ARG (N8N_MCP_VERSION|N8N_MCP_SDK_VERSION|BETTER_SQLITE3_VERSION)=(\S+)$",
                           DOCKERFILE, re.MULTILINE))
    assert args == {"N8N_MCP_VERSION": version, "N8N_MCP_SDK_VERSION": sdk, "BETTER_SQLITE3_VERSION": sqlite}
    assert upstream_direct_patch.VERSION == upstream_result_patch.VERSION == upstream_output_schema_patch.VERSION == version
    assert DOCKERFILE.index('RUN python /tmp/upstream_policy_patch.py') < DOCKERFILE.index('RUN python /tmp/upstream_output_schema_patch.py')
    fixtures = APP / "tests" / "fixtures"
    for manifest in ("manifest.json", "result-patch-manifest.json"):
        assert json.loads((fixtures / manifest).read_text())["version"] == version


def test_dockerfile_node_satisfies_upstream_engines() -> None:
    node = _dockerfile_node_version()
    lock = _lock_packages()
    for package in ("n8n-mcp", "better-sqlite3"):
        engines = lock[f"node_modules/{package}"]["engines"]["node"]
        assert _satisfies(node, engines), (package, node, engines)


def test_node_version_agrees_across_dockerfiles_and_ci() -> None:
    """九個映像的 Node stage（前端 build 與執行環境）與 CI 的 setup-node 同一版、同一組 digest。"""
    node = _dockerfile_node_version()
    images: dict[str, set[str]] = {}
    for dockerfile in sorted(REPO.glob("apps/*/Dockerfile")):
        text = dockerfile.read_text()
        for line in re.findall(r"^FROM node:.*$", text, re.MULTILINE):
            match = re.fullmatch(r"FROM node:(\S+)@(sha256:[0-9a-f]{64}) AS \S+", line)
            assert match, (dockerfile.parent.name, line)  # 一律以多架構 index digest 固定
            images.setdefault(match[1], set()).add(match[2])
        for asserted in re.findall(r'test "\$\(node --version\)" = v(\S+) ', text):
            assert asserted == node, (dockerfile.parent.name, asserted)
    assert set(images) == {f"{node}-alpine", f"{node}-bookworm-slim"}, images
    assert all(len(digests) == 1 for digests in images.values()), images
    ci = (REPO.parent / ".github" / "workflows" / "all-products.yml").read_text()
    assert re.findall(r"node-version: '([^']+)'", ci) == [node]


def test_engines_helper() -> None:
    spec = "^22.23.4 || ^24.22.1 || >=26.12.1"
    assert _satisfies("22.23.4", spec) and _satisfies("24.22.1", spec) and _satisfies("26.12.1", spec)
    assert not _satisfies("22.23.3", spec) and not _satisfies("20.20.2", spec) and not _satisfies("23.0.0", spec)
    majors = "20.x || 22.x || 23.x || 24.x || 25.x || 26.x"
    assert _satisfies("26.12.1", majors) and _satisfies("24.22.1", majors) and _satisfies("20.0.0", majors)
    assert not _satisfies("21.7.3", majors) and not _satisfies("27.0.0", majors)
