"""Config entry removal and migration lifecycle tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from custom_components.wattplan import async_migrate_entry, async_remove_entry
from custom_components.wattplan.config_identity import config_fingerprint
from custom_components.wattplan.const import (
    CONF_OPTIMIZER_LOOKAHEAD_SLOTS,
    CONF_PLANNER_PRESET,
    DOMAIN,
    LEGACY_OPTIMIZER_LOOKAHEAD_SLOTS,
)
from custom_components.wattplan.coordinator import STORAGE_VERSION, snapshot_storage_key
from tests.common import MockConfigEntry

from .test_integration_e2e import _entry

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def private_config_dir(hass: HomeAssistant, tmp_path: Path) -> None:
    """Keep reproduction files out of the shared test configuration directory."""
    hass.config.config_dir = str(tmp_path)

STORE_KEY_PREFIXES = (
    "wattplan.snapshot.",
    "wattplan.history.",
    "wattplan.targets.",
)


def _seed_entry_data(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry_id: str
) -> Path:
    """Create the stores and reproduction files WattPlan keeps for one entry."""
    for prefix in STORE_KEY_PREFIXES:
        hass_storage[f"{prefix}{entry_id}"] = {
            "version": 1,
            "minor_version": 1,
            "key": f"{prefix}{entry_id}",
            "data": {},
        }
    directory = Path(hass.config.path("wattplan_reproductions", entry_id))
    directory.mkdir(parents=True)
    (directory / "2026-01-01.jsonl").write_text("{}\n", encoding="utf-8")
    return directory


async def test_removing_entry_deletes_stores_and_reproductions(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Deleting a config entry leaves none of its persisted data behind."""
    entry = _entry(title="Home", subentries_data=[])
    other = _entry(title="Other", subentries_data=[])
    entry.add_to_hass(hass)
    other.add_to_hass(hass)
    directory = _seed_entry_data(hass, hass_storage, entry.entry_id)
    other_directory = _seed_entry_data(hass, hass_storage, other.entry_id)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    for prefix in STORE_KEY_PREFIXES:
        assert f"{prefix}{entry.entry_id}" not in hass_storage
        assert f"{prefix}{other.entry_id}" in hass_storage
    assert not directory.exists()
    assert (other_directory / "2026-01-01.jsonl").exists()


async def test_removing_entry_without_stored_data_is_harmless(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Entries that never stored anything can still be removed."""
    entry = _entry(title="Home", subentries_data=[])
    entry.add_to_hass(hass)

    await async_remove_entry(hass, entry)

    assert not hass_storage
    assert not Path(hass.config.path("wattplan_reproductions")).exists()


async def test_migration_rejects_future_versions(hass: HomeAssistant) -> None:
    """An entry written by a newer WattPlan must not be migrated."""
    entry = MockConfigEntry(domain=DOMAIN, title="Home", version=2, minor_version=1)
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is False
    assert entry.version == 2
    assert entry.minor_version == 1


async def test_migration_adds_legacy_lookahead_to_old_entries(
    hass: HomeAssistant,
) -> None:
    """Version 1.1 entries keep their historical lookahead and planner."""
    entry = _entry(title="Home", subentries_data=[], minor_version=1)
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True
    assert entry.minor_version == 3
    assert entry.options[CONF_OPTIMIZER_LOOKAHEAD_SLOTS] == LEGACY_OPTIMIZER_LOOKAHEAD_SLOTS
    assert entry.options[CONF_PLANNER_PRESET] == "highs"


@pytest.mark.parametrize("preset", [None, "beam-2"])
async def test_migration_preserves_explicit_and_implicit_planner_choices(hass, preset):
    entry = _entry(title="Home", subentries_data=[], minor_version=2)
    entry.add_to_hass(hass)
    if preset is not None:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PLANNER_PRESET: preset}
        )
    before = dict(entry.options)
    assert await async_migrate_entry(hass, entry) is True
    assert entry.minor_version == 3
    assert entry.options == {**before, CONF_PLANNER_PRESET: preset or "highs"}
    assert await async_migrate_entry(hass, entry) is True
    assert entry.options == {**before, CONF_PLANNER_PRESET: preset or "highs"}


@pytest.mark.parametrize(
    ("cached_preset", "matching_fingerprint"),
    [("highs", True), ("highs", False), ("beam-2", True), (None, True)],
)
async def test_implicit_planner_migration_only_preserves_matching_cached_identity(
    hass, cached_preset, matching_fingerprint,
):
    entry = _entry(title="Home", subentries_data=[], minor_version=2)
    entry.add_to_hass(hass)
    cached = {
        "planner_preset": cached_preset,
        "config_fingerprint": config_fingerprint(entry) if matching_fingerprint else "other-config",
        "snapshot": {"planner_message": "Retained plan"},
    }
    store = Store(hass, STORAGE_VERSION, snapshot_storage_key(entry.entry_id), private=True)
    await store.async_save(cached)
    assert await async_migrate_entry(hass, entry) is True
    expected = dict(cached)
    if cached_preset == "highs" and matching_fingerprint:
        expected["config_fingerprint"] = config_fingerprint(entry)
    assert await store.async_load() == expected
    assert entry.options[CONF_PLANNER_PRESET] == "highs"
    assert await async_migrate_entry(hass, entry) is True
    assert await store.async_load() == expected
