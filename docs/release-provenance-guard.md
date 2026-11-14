# Product Quadlet release proof (draft)

This draft gate covers the nine MCP products under `products/`. It deliberately
has no production manifest, signing key, or expected image ID. An install or
upgrade without an independently reviewed release proof stops before changing
the env file, secrets, volume, backup, or Quadlet unit. Existing running units
are not changed by this branch.

## Release record and operator path

The release authority signs the exact bytes of a JSON manifest with an
Ed25519 key held outside the deployment host. The detached signature is the
manifest path plus `.sig`. The review process must independently establish:

1. The canonical Gitea commit is merged and the Podman catalog pins exactly
   that commit. The canonical and Podman `git -c tar.umask=0000 archive --format=tar` SHA256
   values cover their reviewed commits. The Podman commit includes the
   vendored application, Quadlet templates, installer, and verifier.
2. The image was built from that reviewed Podman commit in a controlled build
   job, with `org.opencontainers.image.revision`,
   `io.woowtech.canonical-source`, and `io.woowtech.release-build` labels.
   Labels alone are claims by the builder. Release review must check the build
   inputs and record the exact resulting local image ID for each product and
   architecture. It must not derive an expected ID from a mutable tag at
   installation time.
3. A reviewer signs the manifest only after checking source, parity, image
   build provenance, smoke results, and rollback plan. The Ed25519 **public**
   key is installed at `/etc/woowtech/mcp-release-ed25519.pub`, owned by root
   with neither the key nor a parent directory writable by the deployment
   user. The private key stays with the independent release authority.

The schema is `1`. The `canonical` record has `repository` equal to
`WOOWTECH/woow-mcp-server`, `commit`, and `archive_sha256`. The `podman` record
has `commit` and `archive_sha256`. The `images` object maps product names to
`linux/amd64` or `linux/arm64` records containing `reference`, `image_id`
(`sha256:<64 hex digits>`), `build_id`, and `labels` (including all three
required labels above). A different architecture or missing product fails.
No placeholder values or test keys are valid release evidence.

After the independent review has produced the actual manifest and image IDs,
the release authority signs it **outside the deployment host** with its
existing private key. This command signs the exact JSON bytes and creates the
detached file the installer expects:

```sh
openssl pkeyutl -sign -inkey /path/to/offline-release-private-key.pem \
  -rawin -in reviewed-release.json -out reviewed-release.json.sig
```

An authorized host administrator provisions only that authority's reviewed
**public** key. The installer rejects a key or parent directory that is
writable by the deployment user:

```sh
sudo install -d -o root -g root -m 0755 /etc/woowtech
sudo install -o root -g root -m 0644 /path/to/reviewed-release-public-key.pem \
  /etc/woowtech/mcp-release-ed25519.pub
```

Transport the reviewed manifest and its `.sig` together to the host. The
private signing key never goes to the host. These commands do not supply a
manifest, key, or image ID; those are separate reviewed release artifacts.

On a host with clean checkouts at those commits and the reviewed image
already present locally, use the appropriate install or upgrade command:

```sh
products/deploy/manage.sh emqx upgrade \
  --release-manifest /path/to/reviewed-release.json \
  --source-checkout /path/to/merged-canonical-checkout
products/deploy/headscale/scripts/upgrade.sh \
  --release-manifest /path/to/reviewed-release.json \
  --source-checkout /path/to/merged-canonical-checkout
```

The same flags apply to install. An active service on a different image must
use upgrade so the existing backup and rollback path is preserved. The
installer does not build an image; `--rebuild` fails. It renders the verified
immutable image ID into `Image=` and uses `Pull=never`.

The gate checks the signed manifest before taking a lock, repeats all checks
under the per-product lock, and repeats after rendering and before persistent
install actions. A failed proof before the lock creates no state directory.
If a proof changes while taking the lock or before persistent installation,
the newly created empty lock directory is removed with `rmdir`; a concurrent
writer's nonempty directory is left intact. An ordinary operational failure
after the proof may leave normal installer state and uses the existing
upgrade rollback path.

## Trust boundary and limits

This protects against accidental use of a wrong catalog pin, changed clean
checkout, unreviewed tag/image, forged labels, missing manifest, and an
unexpected change observed at a preflight checkpoint. It **does not provide
cryptographic protection against a malicious actor with the same UID as the
rootless deployment user**. That actor can edit `release_preflight.py`,
`manage.sh`, templates, or other executable files in the user-writable Podman
checkout before execution, and can tamper with them between checks. A
root-owned external verifier or signed, independently launched bootstrap is
required to claim resistance to that actor. The fixed root-owned public key
alone does not give that property.

The operation assumes the deployment user is trusted and that no actor edits
either checkout, the manifest, or rendered inputs during install/upgrade.
All tracked source must be clean at the reviewed commits. The third proof
checks for drift after rendering, but there remains a time-of-check/time-of-use
window before unit installation. `Image=sha256:...` closes tag swap for the
image; it does not make worktree-rendered Quadlet templates or scripts
immutable. A later release design can materialize signed source into a
root-owned read-only staging tree and execute the installer from there.

This gate does not prove that a builder's labels truthfully describe its build
inputs. The release authority must review the build record before signing.
The parity scripts check the defined canonical-to-Podman source surface, not
every possible runtime behavior. Service smoke and backend acceptance remain
separate rollout gates.

## Current release state

The Headscale DNS and shared logout changes were merged as canonical Gitea
`0e432c3ebd7a481b8994105a187f29837d941fd6`. The Podman catalog pins
that exact commit, and both direct parity checks passed against a clean
canonical checkout. The Headscale product version is `0.1.3-podman.1`.

The signing key, reviewed image identity, signed manifest, and live Quadlet
upgrade are separate release artifacts. This source branch does not include
them. The EMQX [Draft #135](https://git-prod.woowtech.io/WOOWTECH/woow-mcp-server/pulls/135)
is outside the Headscale release and must be reviewed before an EMQX image is
signed. Existing running Quadlets are not evidence that the release guard has
passed.
