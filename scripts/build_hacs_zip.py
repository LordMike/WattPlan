#!/usr/bin/env python3
"""Build a HACS-compatible release artifact for WattPlan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_ROOT = REPO_ROOT / "src" / "custom_components" / "wattplan"
MANIFEST_PATH = INTEGRATION_ROOT / "manifest.json"


def _manifest_version() -> str:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"manifest version missing in {MANIFEST_PATH}")
    return version


def _build_zip(output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(INTEGRATION_ROOT.rglob("*")):
            if file_path.is_dir():
                continue
            archive_name = file_path.relative_to(REPO_ROOT).as_posix()
            archive.write(file_path, archive_name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version-label",
        help="Label to use in the artifact filename. Defaults to manifest version.",
    )
    parser.add_argument(
        "--output-dir",
        default="dist",
        help="Directory for the generated zip file.",
    )
    parser.add_argument(
        "--validate-manifest-version",
        help="Fail if the manifest version does not match this exact value.",
    )
    args = parser.parse_args()

    manifest_version = _manifest_version()
    if (
        args.validate_manifest_version is not None
        and manifest_version != args.validate_manifest_version
    ):
        raise SystemExit(
            f"manifest version {manifest_version!r} does not match "
            f"{args.validate_manifest_version!r}"
        )

    version_label = args.version_label or manifest_version
    output_path = REPO_ROOT / args.output_dir / f"wattplan-{version_label}.zip"
    _build_zip(output_path)
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
