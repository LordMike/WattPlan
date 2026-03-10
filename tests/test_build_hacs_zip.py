from __future__ import annotations

import importlib.util
from pathlib import Path
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "build_hacs_zip.py"


def _load_build_script():
    spec = importlib.util.spec_from_file_location("build_hacs_zip", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_hacs_zip_uses_hacs_archive_layout(tmp_path) -> None:
    build_hacs_zip = _load_build_script()
    output_path = tmp_path / "wattplan-test.zip"

    build_hacs_zip._build_zip(output_path)

    with zipfile.ZipFile(output_path) as archive:
        names = archive.namelist()

    assert "custom_components/wattplan/manifest.json" in names
    assert all(not name.startswith("src/") for name in names)
