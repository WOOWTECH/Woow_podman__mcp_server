#!/usr/bin/env python3
"""Fail closed before a rootless Quadlet installer changes persistent state.

The installer and CLI use the fixed external release key. Isolated tests call
the verifier function directly with a disposable test key. Only an
independently signed manifest can supply an expected image identity.

``--unsigned`` is the owner-approved exception (2026-11-14): no manifest and no
signature. The Podman checkout must still be clean and its HEAD already on
origin/main, and the local image must carry that commit and the catalog pin in
its labels; its ID is rendered as usual.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import subprocess
import sys
import tempfile


TRUST_KEY = Path("/etc/woowtech/mcp-release-ed25519.pub")
CANONICAL_REPOSITORY = "WOOWTECH/woow-mcp-server"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
BUILD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,127}\Z")
MAIN_REF = "refs/remotes/origin/main"


class ReleaseError(Exception):
    """An install cannot prove its source or image identity."""


def fail(message: str) -> None:
    raise ReleaseError(message)


def run(*args: str, input_bytes: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(args, input=input_bytes, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        fail(f"release proof command failed: {args[0]}")
    return result.stdout


def unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            fail(f"duplicate release manifest field: {key}")
        result[key] = value
    return result


def hex_value(value: object, label: str, *, commit: bool = False) -> str:
    if not isinstance(value, str) or not (COMMIT if commit else HEX64).fullmatch(value):
        fail(f"invalid {label}")
    return value


def signed_manifest(path: Path, trust_key: Path) -> dict:
    if not path.is_file():
        fail("reviewed release manifest is missing")
    signature = Path(f"{path}.sig")
    if not signature.is_file() or not trust_key.is_file():
        fail("release manifest signature or trusted public key is missing")
    if trust_key == TRUST_KEY:
        # The installer never supplies a different key. Reject a key or parent
        # directory writable by the deployment user, including ACLs and symlink
        # tricks. Mode bits alone do not reveal an ACL grant to this process.
        for component in (trust_key, *trust_key.parents):
            meta = component.lstat()
            if (meta.st_uid != 0 or meta.st_mode & 0o022 or component.is_symlink()
                    or os.access(component, os.W_OK, effective_ids=True)):
                fail("release trust anchor is not root-owned and read-only")
    # Verify the exact bytes read later. A local caller may choose the manifest
    # path, but the installer always uses the independent /etc trust anchor.
    data = path.read_bytes()
    if len(data) > 1_000_000:
        fail("release manifest is too large")
    try:
        with tempfile.NamedTemporaryFile(mode="wb") as exact_input:
            exact_input.write(data)
            exact_input.flush()
            verified = subprocess.run(
                ("openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(trust_key),
                 "-rawin", "-sigfile", str(signature), "-in", exact_input.name),
                capture_output=True, check=False,
            )
    except OSError:
        fail("OpenSSL is required to verify the release manifest")
    if verified.returncode != 0:
        fail("release manifest signature is invalid")
    try:
        manifest = json.loads(data, object_pairs_hook=unique_pairs)
    except (UnicodeError, ValueError):
        fail("release manifest JSON is invalid")
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        fail("unsupported release manifest schema")
    return manifest


def git(repo: Path, *args: str) -> str:
    return run("git", "-C", str(repo), *args).decode().strip()


def archive_sha256(repo: Path, commit: str) -> str:
    # Archive bytes include all tracked paths, not merely a self-reported label.
    try:
        child = subprocess.Popen(
            ("git", "-C", str(repo), "-c", "tar.umask=0000",
             "archive", "--format=tar", commit),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except OSError:
        fail("git is required to verify release content")
    digest = hashlib.sha256()
    assert child.stdout is not None
    for block in iter(lambda: child.stdout.read(1024 * 1024), b""):
        digest.update(block)
    child.stdout.close()
    assert child.stderr is not None
    child.stderr.read()
    if child.wait() != 0:
        fail("pinned Git archive could not be read")
    return digest.hexdigest()


def require_sha256(actual: str, expected: object, label: str) -> None:
    if actual != hex_value(expected, label):
        fail(f"{label} does not match reviewed release manifest")


def verify_sources(repo: Path, source: Path, manifest: dict, catalog: dict, product: str) -> None:
    canonical = manifest.get("canonical")
    podman = manifest.get("podman")
    if not isinstance(canonical, dict) or not isinstance(podman, dict):
        fail("release manifest source records are missing")
    if canonical.get("repository") != CANONICAL_REPOSITORY:
        fail("release manifest canonical repository is wrong")
    source_commit = hex_value(canonical.get("commit"), "canonical commit", commit=True)
    podman_commit = hex_value(podman.get("commit"), "Podman commit", commit=True)
    if source_commit != catalog.get("paas_source_commit"):
        fail("catalog pin differs from reviewed canonical commit")
    if git(repo, "rev-parse", "HEAD") != podman_commit:
        fail("Podman checkout differs from reviewed release commit")
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        fail("Podman checkout has unreviewed local changes")
    if git(source, "rev-parse", "HEAD") != source_commit:
        fail("canonical checkout HEAD differs from reviewed source commit")
    if git(source, "status", "--porcelain", "--untracked-files=all"):
        fail("canonical checkout has unreviewed local changes")
    if git(source, "cat-file", "-t", source_commit) != "commit":
        fail("canonical commit object is unavailable")
    require_sha256(archive_sha256(source, source_commit), canonical.get("archive_sha256"),
                   "canonical source content")
    require_sha256(archive_sha256(repo, podman_commit), podman.get("archive_sha256"),
                   "Podman source content")
    checker = "check-headscale-parity.py" if product == "headscale" else "check-all-parity.py"
    try:
        result = subprocess.run(
            (sys.executable, str(repo / "products/scripts" / checker),
             "--source-checkout", str(source)),
            capture_output=True, check=False,
        )
    except OSError:
        fail("canonical-to-Podman parity checker could not run")
    if result.returncode != 0:
        fail("canonical-to-Podman source parity failed")


def host_platform() -> str:
    machine = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if platform.system() != "Linux" or machine is None:
        fail("unsupported host platform for reviewed image")
    return f"linux/{machine}"


def inspect_local(reference: str) -> dict:
    try:
        inspected = json.loads(run("podman", "image", "inspect", "--format", "{{json .}}", reference))
    except (UnicodeError, ValueError):
        fail("local image inspection is invalid")
    if not isinstance(inspected, dict):
        fail("local image inspection is invalid")
    actual_id = inspected.get("Id", "")
    if isinstance(actual_id, str) and not actual_id.startswith("sha256:"):
        actual_id = f"sha256:{actual_id}"
    inspected["Id"] = actual_id
    return inspected


def verify_image(manifest: dict, product: str, reference: str, podman_commit: str,
                 canonical_commit: str) -> str:
    platform_id = host_platform()
    images = manifest.get("images")
    if not isinstance(images, dict):
        fail("release manifest has no image records")
    by_product = images.get(product)
    entry = by_product.get(platform_id) if isinstance(by_product, dict) else None
    if not isinstance(entry, dict):
        fail("reviewed image identity is missing for this product/platform")
    if entry.get("reference") != reference:
        fail("reviewed image reference differs from catalog/version")
    image_id = entry.get("image_id")
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        fail("reviewed immutable image ID is missing")
    hex_value(image_id[7:], "reviewed immutable image ID")
    build_id = entry.get("build_id")
    if not isinstance(build_id, str) or not BUILD_ID.fullmatch(build_id):
        fail("reviewed build identity is missing")
    required = {
        "org.opencontainers.image.revision": podman_commit,
        "io.woowtech.canonical-source": canonical_commit,
        "io.woowtech.release-build": build_id,
    }
    labels = entry.get("labels")
    if not isinstance(labels, dict) or any(labels.get(k) != v for k, v in required.items()):
        fail("release manifest lacks required build labels")
    inspected = inspect_local(reference)
    if inspected["Id"] != image_id:
        fail("local image tag resolves to an unreviewed image ID")
    if f"{inspected.get('Os')}/{inspected.get('Architecture')}" != platform_id:
        fail("local image platform differs from reviewed manifest")
    actual_labels = inspected.get("Labels")
    if not isinstance(actual_labels, dict) or any(actual_labels.get(k) != v for k, v in labels.items()):
        fail("local image labels differ from reviewed manifest")
    # Render the immutable ID into Quadlet. Retagging cannot redirect a unit to
    # a different image after verification.
    return image_id


def release_reference(repo: Path, product: str) -> tuple[dict, str]:
    catalog_path = repo / "products/deploy/catalog.json"
    try:
        catalog = json.loads(catalog_path.read_text())
    except (OSError, ValueError):
        fail("Podman catalog is unavailable")
    if product not in catalog.get("products", {}):
        fail("unknown catalog product")
    version = (repo / "products/deploy/headscale/VERSION").read_text().strip() \
        if product == "headscale" else catalog["image_version"]
    return catalog, f"localhost/woow-{product}-mcp-admin:{version}"


def verify(repo: Path, source: Path, manifest_path: Path, trust_key: Path, product: str) -> str:
    manifest = signed_manifest(manifest_path, trust_key)
    catalog, reference = release_reference(repo, product)
    verify_sources(repo, source, manifest, catalog, product)
    return verify_image(manifest, product, reference, manifest["podman"]["commit"],
                        manifest["canonical"]["commit"])


def verify_unsigned(repo: Path, product: str) -> str:
    """No signed manifest: accept only an image built from this clean checkout."""
    catalog, reference = release_reference(repo, product)
    platform_id = host_platform()
    commit = git(repo, "rev-parse", "HEAD")
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        fail("Podman checkout has local changes")
    # Only merged commits ship. The remote-tracking ref is not refreshed here: a stale or
    # missing origin/main refuses the release until the operator fetches.
    try:
        merged = subprocess.run(("git", "-C", str(repo), "merge-base", "--is-ancestor",
                                 commit, MAIN_REF), capture_output=True, check=False)
    except OSError:
        fail("git is required to verify release content")
    if merged.returncode != 0:
        fail("Podman checkout HEAD is not on origin/main; fetch and check out a merged commit")
    inspected = inspect_local(reference)
    image_id = inspected["Id"]
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        fail("local image ID is invalid")
    hex_value(image_id[7:], "local image ID")
    if f"{inspected.get('Os')}/{inspected.get('Architecture')}" != platform_id:
        fail("local image platform differs from this host")
    labels = inspected.get("Labels")
    if not isinstance(labels, dict):
        fail("local image has no release labels")
    if labels.get("org.opencontainers.image.revision") != commit:
        fail("local image was not built from this Podman checkout")
    if labels.get("io.woowtech.canonical-source") != catalog.get("paas_source_commit"):
        fail("local image was built for another canonical pin")
    build_id = labels.get("io.woowtech.release-build")
    if not isinstance(build_id, str) or not BUILD_ID.fullmatch(build_id):
        fail("local image has no release build label")
    return image_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--source-checkout", type=Path)
    parser.add_argument("--release-manifest", type=Path)
    parser.add_argument("--product", required=True)
    parser.add_argument("--unsigned", action="store_true",
                        help="owner-approved release without a signed manifest")
    args = parser.parse_args()
    if args.unsigned and (args.release_manifest or args.source_checkout):
        parser.error("--unsigned takes no --release-manifest or --source-checkout")
    if not args.unsigned and (args.release_manifest is None or args.source_checkout is None):
        parser.error("--release-manifest and --source-checkout are required")
    try:
        if args.unsigned:
            image_id = verify_unsigned(args.repo, args.product)
        else:
            image_id = verify(args.repo, args.source_checkout, args.release_manifest,
                              TRUST_KEY, args.product)
    except (ReleaseError, OSError, KeyError, TypeError) as error:
        print(f"release preflight: {error}", file=sys.stderr)
        return 1
    print(image_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
