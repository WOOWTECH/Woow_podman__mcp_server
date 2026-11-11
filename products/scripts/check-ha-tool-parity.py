#!/usr/bin/env python3
"""Compare the PaaS/Podman tool names with the pinned HA Add-on source."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


PRODUCTS = ("emqx", "hermes", "n8n", "odoo", "litellm", "nextcloud", "opendesign", "headscale")
HA_SOURCE_COMMIT = "1b45bdfa5338170d2b397ef5940bb4c241f8b343"
DEPLOYMENT_ONLY_TOOLS = {"headscale": {"headscale_platform"}}
WORKSPACE = Path(__file__).resolve().parents[1]

PAAS_CODE = """
import importlib
import json
import sys
from pathlib import Path

products = ('emqx', 'hermes', 'n8n', 'odoo', 'litellm', 'nextcloud', 'opendesign', 'headscale')
result = {}
for product in products:
    sys.path.insert(0, str(Path('apps') / product))
    registry = importlib.import_module(f'{product}_mcp_admin.tool_registry')
    result[product] = registry.get_all_tool_names()
print(json.dumps(result))
"""

HA_CODE = """
import json
import sys

sys.path.insert(0, 'packages/mcp-admin-core')
from mcp_admin_core.surface import full_tools

products = ('emqx', 'hermes', 'n8n', 'odoo', 'litellm', 'nextcloud', 'opendesign', 'headscale')
print(json.dumps({product: list(full_tools(product)) for product in products}))
"""


def tool_names(code: str, directory: Path) -> dict[str, list[str]]:
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=directory, capture_output=True, text=True, check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"tool import failed in {directory}: {completed.stderr[-2000:]}")
    result = json.loads(completed.stdout)
    if set(result) != set(PRODUCTS):
        raise RuntimeError(f"unexpected product catalog in {directory}: {sorted(result)}")
    for product, names in result.items():
        if len(names) != len(set(names)):
            raise RuntimeError(f"duplicate {product} tool names in {directory}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-checkout", type=Path, required=True)
    args = parser.parse_args()
    ha_checkout = args.ha_checkout.resolve(strict=True)
    revision = subprocess.check_output(
        ["git", "-C", str(ha_checkout), "rev-parse", "HEAD"], text=True,
    ).strip()
    if revision != HA_SOURCE_COMMIT:
        raise SystemExit(f"HA source must be pinned to {HA_SOURCE_COMMIT}; got {revision}")

    paas = tool_names(PAAS_CODE, WORKSPACE)
    ha = tool_names(HA_CODE, ha_checkout)
    failed = False
    for product in PRODUCTS:
        paas_only = set(paas[product]) - set(ha[product])
        ha_only = set(ha[product]) - set(paas[product])
        allowed = DEPLOYMENT_ONLY_TOOLS.get(product, set())
        print(
            f"{product}: PaaS/Podman {len(paas[product])}, HA {len(ha[product])}; "
            f"PaaS only {sorted(paas_only)}, HA only {sorted(ha_only)}"
        )
        if paas_only != allowed or ha_only:
            failed = True
    if failed:
        raise SystemExit("HA/PaaS tool-name parity failed")
    print(f"HA/PaaS tool-name parity passed at {HA_SOURCE_COMMIT}")


if __name__ == "__main__":
    main()
