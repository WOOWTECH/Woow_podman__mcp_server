#!/usr/bin/env python3
"""Validated, non-secret catalog consumed by the shared Quadlet installer."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CATALOG = Path(__file__).resolve().parents[1] / "catalog.json"
NAME = re.compile(r"[a-z][a-z0-9-]{1,31}\Z")
MODULE = re.compile(r"[a-z][a-z0-9_]+\.main:app\Z")


def load() -> dict:
    data = json.loads(CATALOG.read_text())
    products = data["products"]
    assert len(set(products)) == len(products)
    ports = set()
    for name, item in products.items():
        assert NAME.fullmatch(name), name
        assert NAME.fullmatch(item["package"]), name
        assert MODULE.fullmatch(item["module"]), name
        port = item["default_port"]
        assert isinstance(port, int) and 1024 <= port <= 65535 and port not in ports, name
        ports.add(port)
        if name != "headscale":
            assert (CATALOG.parents[1] / "apps" / name / "Dockerfile").is_file(), name
    return data


def main() -> None:
    data = load()
    if sys.argv[1:] == ["list"]:
        for name, item in data["products"].items():
            print(f"{name}\t{item['default_port']}\t{item['package']}")
    elif len(sys.argv) == 3 and sys.argv[1] == "get":
        product = data["products"].get(sys.argv[2])
        if product is None:
            raise SystemExit(f"unknown product: {sys.argv[2]}")
        fields = (
            product["package"], product["module"], str(product["default_port"]),
            product.get("image_version", data["image_version"]), product.get("installer", "shared"),
        )
        sys.stdout.buffer.write("\0".join(fields).encode() + b"\0")
    else:
        raise SystemExit("usage: catalog.py list | get PRODUCT")


if __name__ == "__main__":
    main()
