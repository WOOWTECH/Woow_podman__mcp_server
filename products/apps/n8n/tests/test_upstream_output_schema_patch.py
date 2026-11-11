"""The pinned npm artifact must describe object-valued workflow issue details."""

import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from n8n_mcp_admin import upstream_output_schema_patch as patch

FIXTURE = Path(__file__).parent / "fixtures/n8n-mcp-2.96.2-tools.js.gz"
FIXTURE_SHA256 = "f1f99d4d1f8b9416e8036695047271eeffd08669ded30fbe349fb3feeda86a03"


@pytest.fixture
def package(tmp_path):
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256
    raw = gzip.decompress(FIXTURE.read_bytes())
    assert hashlib.sha256(raw).hexdigest() == patch.ORIGINAL_SHA256
    target = tmp_path / patch.REL
    target.parent.mkdir(parents=True)
    target.write_bytes(raw)
    (tmp_path / "package.json").write_text(json.dumps({"version": patch.VERSION}))
    return tmp_path


def test_only_two_exact_schema_literals_change(package):
    original = (package / patch.REL).read_bytes()
    assert patch.patch_package(package) is True
    updated = (package / patch.REL).read_bytes()
    assert hashlib.sha256(updated).hexdigest() == patch.PATCHED_SHA256
    assert updated.replace(patch.AFTER, patch.BEFORE) == original
    assert patch.patch_package(package) is False


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
def test_workflow_error_and_warning_schema_accept_object_details(package):
    patch.patch_package(package)
    script = """
const tools=require(process.argv[1]).n8nDocumentationToolsFinal;
const workflow=tools.find(x=>x.name==='validate_workflow').outputSchema;
const node=tools.find(x=>x.name==='validate_node').outputSchema;
console.log(JSON.stringify({
  error:workflow.properties.errors.items.properties.details.type,
  warning:workflow.properties.warnings.items.properties.details.type,
  node:node.properties.errors.items.properties.details?.type ?? null,
  names:tools.map(x=>x.name)
}));
"""
    done = subprocess.run(["node", "-e", script, str(package / patch.REL)],
                          capture_output=True, text=True, check=True, timeout=20)
    result = json.loads(done.stdout)
    assert result["error"] == result["warning"] == "object"
    assert result["names"] == ["tools_documentation", "search_nodes", "get_node",
                               "validate_node", "get_template", "search_templates",
                               "validate_workflow"]


def test_unreviewed_version_or_bytes_refused(package):
    target = package / patch.REL
    before = target.read_bytes()
    (package / "package.json").write_text('{"version":"2.96.3"}')
    with pytest.raises(ValueError, match="version"):
        patch.patch_package(package)
    assert target.read_bytes() == before
    (package / "package.json").write_text(json.dumps({"version": patch.VERSION}))
    target.write_bytes(before + b"\n// unknown\n")
    changed = target.read_bytes()
    with pytest.raises(ValueError, match="bytes"):
        patch.patch_package(package)
    assert target.read_bytes() == changed
