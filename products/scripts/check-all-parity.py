#!/usr/bin/env python3
"""Verify every shared Podman app against the pinned PaaS source commit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / "deploy/catalog.json").read_text())
PIN = CATALOG["paas_source_commit"]
APPS = tuple(name for name in CATALOG["products"] if name != "headscale")
# Retired on Podman (2026-11-14) but still vendored with the same edits: the uv and npm
# workspace locks include them and the shared core tests iterate every app.
RETIRED_APPS = ("vk",)
PATHS = tuple(f"apps/{name}" for name in APPS + RETIRED_APPS) + (
    "apps/odoo-manage/frontend", "packages/mcp-admin-core/mcp_admin_core",
    "packages/mcp-admin-core/pyproject.toml", "packages/mcp-admin-core/tests",
    "packages/mcp-admin-ui", "pyproject.toml",
    "uv.lock", "package.json", "package-lock.json",
)
IGNORE_DIRS = {"__pycache__", ".pytest_cache", "node_modules", "dist"}


def git(source: Path, *args: str) -> bytes:
    return subprocess.check_output(("git", "-C", str(source), *args))


def expected(path: str, value: bytes) -> bytes:
    app = path.split("/")[1] if path.startswith("apps/") else ""
    if app in APPS + RETIRED_APPS and path == f"apps/{app}/{app}_mcp_admin/main.py":
        for quote in (b'"', b"'"):
            before = b"deployment_kind=" + quote + b"paas" + quote
            if value.count(before) == 1:
                return value.replace(before, b"deployment_kind=" + quote + b"podman" + quote)
        raise AssertionError(f"PaaS deployment marker changed: {path}")
    if path == "apps/n8n/tests/test_upstream_pins.py":
        before_ci = b'REPO / ".gitea" / "workflows" / "ci.yml"'
        assert value.count(before_ci) == 1, "PaaS n8n CI pin check changed"
        return value.replace(before_ci, b'REPO.parent / ".github" / "workflows" / "all-products.yml"')
    if path == "packages/mcp-admin-core/tests/test_capabilities.py":
        before_kind = b'assert body["deployment"] == {"kind": "paas"}'
        assert value.count(before_kind) == 1, "PaaS deployment identity test changed"
        return value.replace(before_kind, b'assert body["deployment"] == {"kind": "podman"}')
    if path == "packages/mcp-admin-core/tests/test_security_headers.py":
        before_port = b'    c = make_app({"mcp_auth_token": "secret"})\n'
        assert value.count(before_port) == 1, "PaaS proxy test setup changed"
        isolated_port = (
            b"    # Podman hosts may already run a service on the core default child port 8000.\n"
            b"    # Reserve a currently free loopback port so this test observes the proxy's\n"
            b"    # unavailable-upstream path rather than an unrelated host service.\n"
            b"    import socket\n"
            b"    with socket.socket() as probe:\n"
            b"        probe.bind((\"127.0.0.1\", 0))\n"
            b"        child_port = probe.getsockname()[1]\n"
            b'    c = make_app({"mcp_auth_token": "secret", "mcp_server": {"port": child_port}})\n'
        )
        return value.replace(before_port, isolated_port)
    if app in APPS + RETIRED_APPS and path == f"apps/{app}/Dockerfile":
        module = f"{app}_mcp_admin.main:app"
        before = (f'CMD ["uvicorn", "{module}", "--host", "0.0.0.0", '
                  '"--port", "8080"]').encode()
        after = f'CMD ["python", "/app/bootstrap.py", "{module}"]'.encode()
        assert value.count(before) == 1, f"PaaS image startup changed: {path}"
        assert value.count(b"USER 1000:1000") == 1, f"PaaS image user changed: {path}"
        value = value.replace(b"USER 1000:1000", b"COPY deploy/shared/bootstrap.py /app/bootstrap.py\nUSER 1000:1000").replace(before, after)
        if app == "k8s":
            checksum = b"252ed5d0b7fe94837f01b982ff15ebd87b5dad67df5b5145ee30ec4f714fcfe2"
            before_download = (b"ADD --chmod=755 \\\n    --checksum=sha256:" + checksum +
                               b" \\\n    https://github.com/containers/kubernetes-mcp-server/releases/download/v${KMS_VERSION}/kubernetes-mcp-server-linux-amd64 \\\n    /usr/local/bin/kubernetes-mcp-server\n")
            assert value.count(before_download) == 1, "PaaS K8s binary download or checksum changed"
            podman_download = (
                b"# Podman 4.9 Buildah does not implement remote ADD --checksum. Download into the\n"
                b"# image build, verify the same immutable PaaS digest, then grant execute access.\n"
                b"RUN python -c \"import urllib.request; urllib.request.urlretrieve('https://github.com/containers/kubernetes-mcp-server/releases/download/v${KMS_VERSION}/kubernetes-mcp-server-linux-amd64', '/usr/local/bin/kubernetes-mcp-server')\" \\\n"
                b" && printf '%s  %s\\n' '" + checksum +
                b"' \\\n    /usr/local/bin/kubernetes-mcp-server | sha256sum -c - \\\n && chmod 755 /usr/local/bin/kubernetes-mcp-server\n"
            )
            value = value.replace(before_download, podman_download)
        return value
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-checkout", required=True, type=Path)
    args = parser.parse_args()
    git(args.source_checkout, "cat-file", "-e", f"{PIN}^{{commit}}")
    tracked = set(git(args.source_checkout, "ls-tree", "-r", "--name-only", PIN,
                      *PATHS).decode().splitlines())
    failures = []
    for path in sorted(tracked):
        local = ROOT / path
        if not local.is_file() or local.read_bytes() != expected(
                path, git(args.source_checkout, "show", f"{PIN}:{path}")):
            failures.append(path)
    extras = []
    for base in PATHS:
        location = ROOT / base
        candidates = location.rglob("*") if location.is_dir() else (location,)
        for file in candidates:
            if (file.is_file() and not any(part in IGNORE_DIRS for part in file.parts)
                    and str(file.relative_to(ROOT)) not in tracked):
                extras.append(str(file.relative_to(ROOT)))
    if failures or extras:
        print(f"parity failed: changed/missing={failures}, extra={extras}", file=sys.stderr)
        return 1
    print(f"parity ok: {len(tracked)} PaaS files at {PIN}; {len(APPS)} Podman apps and retired {', '.join(RETIRED_APPS)} differ only in image startup and deployment kind")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
