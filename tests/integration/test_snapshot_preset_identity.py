"""Snapshot preset identity and recovery from unsupported stored options."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.wattplan.const import (
    CONF_ACTION_EMISSION_ENABLED,
    CONF_PLANNER_PRESET,
    CONF_PLANNING_ENABLED,
)
from custom_components.wattplan.coordinator_logic.persistence import (
    CoordinatorSnapshotStore,
)
from custom_components.wattplan.coordinator_parts import CoordinatorSnapshot
from custom_components.wattplan.entry_setup import config_fingerprint
from custom_components.wattplan.optimizer.planners.presets import DEFAULT_PRESET
from .test_config_flow import _schema_default
from .test_integration_e2e import _entry, _setup_entry

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


class _MemoryStore:
    """Minimal Home Assistant Store stand-in for persistence contract checks."""

    def __init__(self, payload=None):
        self.payload = payload
        self.async_remove = AsyncMock(side_effect=self._remove)
        self.async_save = AsyncMock(side_effect=self._save)

    async def async_load(self):
        return self.payload

    async def _save(self, payload):
        self.payload = payload

    async def _remove(self):
        self.payload = None


def _snapshot_store(store, *, planner_preset=DEFAULT_PRESET):
    return CoordinatorSnapshotStore(
        store,
        entry_id="test-entry",
        schema_id="snapshot-v1",
        logger=logging.getLogger(__name__),
        config_fingerprint="test-fingerprint",
        planner_preset=planner_preset,
    )


def test_cache_round_trip_requires_the_same_explicit_preset():
    snapshot = CoordinatorSnapshot(
        created_at=datetime(2026, 10, 13, tzinfo=UTC),
        planner_status="ok",
        action_schedules={},
    )
    cache = _snapshot_store(_MemoryStore(), planner_preset="beam-2")
    payload = cache.restore_payload(
        snapshot=snapshot,
        last_success_at=snapshot.created_at,
        last_duration_ms=20,
        last_run_timings=None,
    )
    assert payload["planner_preset"] == "beam-2"
    assert cache.async_restore_payload(payload).planner_preset == "beam-2"

    another_preset = _snapshot_store(_MemoryStore(), planner_preset="greedy-8")
    assert another_preset.async_restore_payload(payload) is None
    missing_identity = dict(payload)
    missing_identity.pop("planner_preset")
    assert cache.async_restore_payload(missing_identity) is None


def test_config_fingerprint_covers_setup_options_and_subentries():
    entry = SimpleNamespace(
        data={"slot_minutes": 15, "hours_to_plan": 48},
        options={CONF_PLANNER_PRESET: DEFAULT_PRESET},
        subentries={"battery-a": SimpleNamespace(subentry_type="battery", data={"capacity_kwh": 8})},
    )
    initial = config_fingerprint(entry)
    for attribute, changed in (
        ("data", {**entry.data, "hours_to_plan": 72}),
        ("options", {**entry.options, CONF_PLANNER_PRESET: "beam-2"}),
        (
            "subentries",
            {"battery-a": SimpleNamespace(subentry_type="battery", data={"capacity_kwh": 12})},
        ),
    ):
        changed_entry = SimpleNamespace(
            data=entry.data, options=entry.options, subentries=entry.subentries
        )
        setattr(changed_entry, attribute, changed)
        assert config_fingerprint(changed_entry) != initial


async def test_unknown_stored_preset_is_repaired_and_options_picker_recovers(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture,
):
    entry = _entry(
        title="Recovery Home",
        subentries_data=[],
        options={
            CONF_PLANNER_PRESET: "removed-preset-id",
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
    )
    with caplog.at_level(logging.WARNING, logger="custom_components.wattplan.entry_setup"):
        await _setup_entry(hass, entry)

    assert entry.options[CONF_PLANNER_PRESET] == DEFAULT_PRESET
    assert "Unknown planner preset 'removed-preset-id'" in caplog.text
    coordinator = entry.runtime_data.coordinator
    assert coordinator.data.diagnostics["optimizer"]["planner_preset"] == DEFAULT_PRESET
    assert coordinator.restore_payload()["planner_preset"] == DEFAULT_PRESET

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "planner_core"}
    )
    assert _schema_default(result, CONF_PLANNER_PRESET) == DEFAULT_PRESET
