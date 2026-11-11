"""n8n-mcp 工具回 {success:false} 時 MCP 結果要 isError=true（商用驗收 DATA-NMCP-005-2）。

上游 n8n-mcp 2.96.2 把 n8n API 錯誤包成 ``{success:false, error:…, code:'API_ERROR'}`` 文字，
``CallToolResult.isError`` 卻是 false——例如對已發佈的 workflow 呼叫 n8n_delete_workflow，
依賴 isError 判斷的 client 會當成成功。建置時修補 ``dist/mcp/server.js`` 的工具結果組裝處，
並在 n8n_delete_workflow 的描述註明「已發佈需先 deactivate」。

測試用的是 npm 上真的 2.96.2 檔案（fixtures/*.gz，雜湊見 result-patch-manifest.json）。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
import stat

import pytest

from n8n_mcp_admin import upstream_result_patch as patch

FIXTURES = Path(__file__).parent / "fixtures"
MANIFEST = json.loads((FIXTURES / "result-patch-manifest.json").read_text())
SERVER = Path("dist/mcp/server.js")
TOOLS = Path("dist/mcp/tools-n8n-manager.js")


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """把釘版的上游檔案解到暫存目錄（不跑 npm scripts）。"""
    (tmp_path / "package.json").write_text(json.dumps({"version": MANIFEST["version"]}))
    for rel, meta in MANIFEST["files"].items():
        data = gzip.decompress((FIXTURES / meta["fixture"]).read_bytes())
        assert hashlib.sha256(data).hexdigest() == meta["sha256"]
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o644)
    return tmp_path


def _undo(package: Path, rel: Path) -> bytes:
    data = (package / rel).read_bytes()
    for before, after in patch.FILES[rel].replacements:
        assert data.count(after) == 1
        data = data.replace(after, before, 1)
    return data


def test_patch_touches_only_the_reviewed_spots(package: Path) -> None:
    originals = {rel: (package / rel).read_bytes() for rel in patch.FILES}
    assert patch.patch_package(package) is True
    for rel, spec in patch.FILES.items():
        result = (package / rel).read_bytes()
        assert hashlib.sha256(result).hexdigest() == spec.patched_sha256
        assert _undo(package, rel) == originals[rel]          # 其餘位元組完全不變
        assert stat.S_IMODE((package / rel).stat().st_mode) == 0o644


def test_success_false_maps_to_is_error(package: Path) -> None:
    patch.patch_package(package)
    server = (package / SERVER).read_text()
    assert "result.success === false" in server
    assert "mcpResponse.isError = true;" in server


def test_delete_workflow_description_mentions_deactivate_first(package: Path) -> None:
    patch.patch_package(package)
    tools = (package / TOOLS).read_text()
    start = tools.index("name: 'n8n_delete_workflow'")
    description = tools[start: tools.index("inputSchema", start)]
    assert "deactivate" in description and "deactivateWorkflow" in description
    assert "This action cannot be undone." in description          # 原句保留


def test_idempotent_and_does_not_rewrite(package: Path) -> None:
    assert patch.patch_package(package) is True
    stats = {rel: (package / rel).stat() for rel in patch.FILES}
    assert patch.patch_package(package) is False
    for rel, before in stats.items():
        after = (package / rel).stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_unknown_version_rejected_before_mutation(package: Path) -> None:
    (package / "package.json").write_text('{"version":"2.96.3"}')
    before = {rel: (package / rel).read_bytes() for rel in patch.FILES}
    with pytest.raises(ValueError, match="version"):
        patch.patch_package(package)
    assert {rel: (package / rel).read_bytes() for rel in patch.FILES} == before


def test_unknown_bytes_in_any_file_rejected_before_any_mutation(package: Path) -> None:
    """第二個檔案內容不對時，第一個檔案也不能先被改掉（全有或全無）。"""
    altered = (package / TOOLS).read_bytes() + b"\n// unreviewed change\n"
    (package / TOOLS).write_bytes(altered)
    server_before = (package / SERVER).read_bytes()
    with pytest.raises(ValueError, match="bytes"):
        patch.patch_package(package)
    assert (package / SERVER).read_bytes() == server_before
    assert (package / TOOLS).read_bytes() == altered


def test_missing_file_fails(package: Path) -> None:
    (package / SERVER).unlink()
    with pytest.raises(FileNotFoundError):
        patch.patch_package(package)


# ---------------------------------------------------------------------------
# 行為：把修補後的結果組裝段落拿出來用 node 真的跑一次
# ---------------------------------------------------------------------------
_HARNESS = """
const build = new Function('name', 'result', 'responseText', 'structuredContent', 'resultMeta', %s);
const cases = [
  ['n8n_delete_workflow', {success: false, error: 'Cannot delete a published workflow. Unpublish it before deleting.', code: 'API_ERROR'}],
  ['n8n_delete_workflow', {success: true, data: {id: '1', deleted: true}}],
  ['tools_documentation', 'plain text result'],
  ['validate_node', {valid: false, errors: [{message: 'x'}]}],
  ['n8n_list_workflows', {data: []}],
];
const out = cases.map(([name, result]) => {
  const r = build(name, result, JSON.stringify(result), name.startsWith('validate_') ? result : null, {});
  return {name, isError: r.isError === true, text: r.content[0].text};
});
console.log(JSON.stringify(out));
"""


def _response_block(server_js: str) -> str:
    start = server_js.index("const mcpResponse = {")
    end = server_js.index("return mcpResponse;", start) + len("return mcpResponse;")
    return server_js[start:end]


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_patched_result_block_sets_is_error_only_for_success_false(package: Path) -> None:
    patch.patch_package(package)
    block = _response_block((package / SERVER).read_text())
    proc = subprocess.run(["node", "-e", _HARNESS % json.dumps(block)], capture_output=True, text=True,
                          check=True, timeout=30)
    flags = {(c["name"], i): c["isError"] for i, c in enumerate(json.loads(proc.stdout))}
    assert flags == {
        ("n8n_delete_workflow", 0): True,     # n8n 拒絕 → isError
        ("n8n_delete_workflow", 1): False,
        ("tools_documentation", 2): False,
        ("validate_node", 3): False,          # 驗證結果 valid:false 是答案，不是工具失敗
        ("n8n_list_workflows", 4): False,
    }
