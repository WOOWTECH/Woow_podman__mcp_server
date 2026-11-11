#!/usr/bin/env python3
"""seed-if-absent assembler for /data/config.json (spec 契約補遺: GUI-owned).

config.json is BOTH the Secret-seed target AND the admin GUI's runtime state
store. So we seed initial values ONLY when /data/config.json is absent; once the
GUI writes to it, those runtime edits persist on the PVC across restarts.

Assembles the non-sensitive ConfigMap base (config.base.json) with the sensitive
Secret fragment (config.secret.json) via deep merge (sensitive wins on leaf
collisions). Runs as an initContainer using the app image (python3).

Optionally validates required config.json fields (O-1, hermes store.py:35-54):
- SEED_REQUIRED_PATHS: comma-separated dot-nested paths that must be non-empty.
- SEED_REQUIRE_FIELDS=true → exit 1 (pod stuck Init) when any are missing.
  false (pilot) → warn only. NOTE: JWT_SECRET is an ENV var, not a config.json
  field, so it is validated by the app, not here.

Paths are env-overridable so the logic is unit-testable off-cluster.
"""
import json
import os
import sys
import tempfile

TARGET = os.environ.get("SEED_TARGET", "/data/config.json")
BASE = os.environ.get("SEED_BASE", "/seed/base/config.base.json")
SECRET = os.environ.get("SEED_SECRET", "/seed/secret/config.secret.json")


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        text = fh.read().strip()
        return json.loads(text) if text else {}


def deep_merge(base, overlay):
    """Recursively merge overlay into base; overlay wins on leaf collisions."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def get_path(cfg, dotted):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def missing_required(cfg, paths):
    missing = []
    for path in paths:
        value = get_path(cfg, path)
        if value is None or value == "" or value == [] or value == {}:
            missing.append(path)
    return missing


def publish_if_absent(config):
    """Publish complete, private state without replacing a concurrent winner."""
    directory = os.path.dirname(os.path.abspath(TARGET))
    os.makedirs(directory, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=directory,
                                         prefix=".config-seed-", delete=False) as fh:
            temporary = fh.name  # created as 0600, including while writing
            json.dump(config, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(temporary, TARGET)
        except FileExistsError:
            return False
        return True
    finally:
        if temporary is not None:
            os.unlink(temporary)


def main():
    # A dangling symlink is existing unavailable state, never a fresh install.
    if os.path.lexists(TARGET):
        print(f"[seed-config] {TARGET} exists — GUI-owned, skip (seed-if-absent).")
        return 0

    # /seed/secret is always mounted (chart-managed Secret or existingSecret). A
    # missing config.secret.json means an existingSecret was supplied WITHOUT that
    # key → config.json gets no sensitive fields.
    if not os.path.exists(SECRET):
        print(
            f"[seed-config] WARNING: {SECRET} missing — seeding WITHOUT sensitive "
            "fields. If using existingSecret, ensure it carries key config.secret.json.",
            file=sys.stderr,
        )

    merged = deep_merge(load(BASE), load(SECRET))

    paths = [p for p in os.environ.get("SEED_REQUIRED_PATHS", "").split(",") if p]
    missing = missing_required(merged, paths)
    if missing:
        require = os.environ.get("SEED_REQUIRE_FIELDS", "false").lower() == "true"
        msg = "required config.json field(s) missing/empty: " + ", ".join(missing)
        if require:
            print(f"[seed-config] {msg} — failing (config.requireFields=true).", file=sys.stderr)
            return 1
        print(
            f"[seed-config] WARNING: {msg} — proceeding (config.requireFields=false, pilot).",
            file=sys.stderr,
        )

    if publish_if_absent(merged):
        print(f"[seed-config] seeded {TARGET} with {len(merged)} top-level key(s).")
    else:
        print(f"[seed-config] {TARGET} exists — GUI-owned, skip (concurrent seed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
