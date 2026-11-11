"""防止只升 app 的 certifi，漏掉基底或 ensurepip wheel。"""
import importlib.util
from pathlib import Path
import zipfile

import pytest

spec = importlib.util.spec_from_file_location(
    "check_certifi", Path(__file__).parents[1] / "scripts" / "check_certifi.py"
)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.mark.parametrize("bundled", [False, True])
def test_rejects_old_vendor_even_when_app_is_fixed(tmp_path, bundled):
    app = tmp_path / "app" / "certifi" / "__init__.py"
    app.parent.mkdir(parents=True)
    app.write_text('__version__ = "2026.10.23"')
    vendor = "pip/_vendor/certifi/__init__.py"
    if bundled:
        with zipfile.ZipFile(tmp_path / "pip.whl", "w") as wheel:
            wheel.writestr(vendor, '__version__ = "2024.08.30"')
    else:
        path = tmp_path / vendor
        path.parent.mkdir(parents=True)
        path.write_text('__version__ = "2024.08.30"')
    with pytest.raises(ValueError, match="2024.08.30"):
        checker.inspect_copies([tmp_path])


def test_accepts_fixed_certifi(tmp_path):
    path = tmp_path / "certifi" / "__init__.py"
    path.parent.mkdir()
    path.write_text('__version__ = "2026.10.23"')
    assert checker.inspect_copies([tmp_path]) == [
        {"path": str(path), "version": "2026.10.23"}
    ]


def test_rejects_previous_floor(tmp_path):
    """2026.10.19 仍帶 emSign Root CA - C1／ECC Root CA - C3，低於新下限。"""
    path = tmp_path / "certifi" / "__init__.py"
    path.parent.mkdir()
    path.write_text('__version__ = "2026.10.19"')
    with pytest.raises(ValueError, match="2026.10.19"):
        checker.inspect_copies([tmp_path])


def test_flags_each_distrusted_emsign_root():
    bundle = "\n".join(f'# Label: "{label}"' for label in checker.DISTRUSTED_ROOTS)
    assert checker.distrusted_roots(bundle) == list(checker.DISTRUSTED_ROOTS)
    assert checker.distrusted_roots('# Label: "emSign Root CA - G1"') == []


def test_locked_certifi_bundle_has_no_distrusted_roots():
    import certifi

    assert checker.distrusted_roots(Path(certifi.where()).read_text()) == []
