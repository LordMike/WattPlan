"""Check the integration and HACS metadata files."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads(
    (ROOT / "custom_components/wattplan/manifest.json").read_text(encoding="utf-8")
)
HACS = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))


def test_manifest_declares_runtime_dependencies() -> None:
    """The integration imports recorder and energy, so startup order must be declared.

    Both are soft: the code degrades gracefully without the recorder, so it is
    an after-dependency rather than a hard dependency.
    """
    assert {"recorder", "energy"} <= set(MANIFEST["after_dependencies"])
    assert MANIFEST["issue_tracker"].endswith("/WattPlan/issues")


def test_manifest_keys_follow_hassfest_order() -> None:
    """Hassfest expects domain, name, then the remaining keys alphabetically."""
    keys = list(MANIFEST)
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])


def test_hacs_declares_minimum_home_assistant_version() -> None:
    """OptionsFlowWithReload needs Home Assistant 2025.8, so HACS must say so."""
    parts = [int(part) for part in HACS["homeassistant"].split(".")]
    assert tuple(parts) >= (2025, 8, 0)


def test_reproduction_retention_description_mentions_disk_use() -> None:
    """The retention setting warns about disk use and both string files agree."""
    descriptions = []
    for relative in ("strings.json", "translations/en.json"):
        strings = json.loads(
            (ROOT / "custom_components/wattplan" / relative).read_text(encoding="utf-8")
        )
        descriptions.append(
            strings["options"]["step"]["troubleshooting"]["data_description"][
                "planner_reproduction_retention_days"
            ]
        )
    assert descriptions[0] == descriptions[1]
    assert "10 MB" in descriptions[0]
