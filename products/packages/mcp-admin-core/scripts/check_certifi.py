"""檢查映像內的 certifi，包含 pip 與 ensurepip wheel 的 vendored 副本。"""
import ast
import json
from pathlib import Path
import ssl
import sys
import zipfile

MINIMUM = (2026, 10, 23)
# GHSA-6q48-6c7w-9gw2：2026.10.19 移除 emSign ECC Root CA - G3，2026.10.23 再移除同業者的 C1／C3。
DISTRUSTED_ROOTS = ("emSign ECC Root CA - G3", "emSign Root CA - C1", "emSign ECC Root CA - C3")


def check_source(source, location):
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            version = ast.literal_eval(node.value)
            if tuple(map(int, version.split("."))) < MINIMUM:
                raise ValueError(f"受影響的 certifi：{location} ({version})")
            return {"path": str(location), "version": version}
    raise ValueError(f"無法確認 certifi 版本：{location}")


def inspect_copies(roots):
    copies = []
    for root in roots:
        for path in sorted(root.rglob("certifi/__init__.py")):
            copies.append(check_source(path.read_text(), path))
        for path in sorted(root.rglob("*.whl")):
            with zipfile.ZipFile(path) as wheel:
                for name in wheel.namelist():
                    if name.endswith("certifi/__init__.py"):
                        copies.append(check_source(wheel.read(name).decode(), f"{path}!/{name}"))
    return copies


def distrusted_roots(bundle):
    """回傳 CA bundle 內仍存在的已撤除根憑證名稱。"""
    return [label for label in DISTRUSTED_ROOTS if label in bundle]


def main():
    import certifi

    # venv 的依賴與 Python 基底都查；不只檢查 import 到的那一份。
    roots = sorted({Path(sys.prefix) / "lib", Path(sys.base_prefix) / "lib"})
    copies = inspect_copies(roots)
    if not copies:
        raise ValueError("映像未找到 certifi")
    bundle = Path(certifi.where()).read_text()
    found = distrusted_roots(bundle)
    if found:
        raise ValueError(f"certifi 仍信任已撤除的 emSign 根憑證：{'、'.join(found)}")
    ssl.create_default_context(cafile=certifi.where())
    print(json.dumps({"certifi_copies": copies, "tls_bundle_load": True}))


if __name__ == "__main__":
    main()
