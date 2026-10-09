"""Real selected planners feed HA schedules, health, sensors and Outlook."""

import pytest

from custom_components.wattplan.const import (
    CONF_ACTION_EMISSION_ENABLED,
    CONF_PLANNER_PRESET,
    CONF_PLANNING_ENABLED,
)
from custom_components.wattplan.optimizer.planners.presets import PRESETS
from .test_integration_e2e import (
    _battery_subentry,
    _comfort_subentry,
    _entry,
    _optional_subentry,
    _run_optimize,
    _setup_entry,
    entity_registry_enabled_by_default,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.mark.parametrize("preset", PRESETS)
async def test_real_planners_publish_all_equipment_without_losing_health(
    hass, preset, entity_registry_enabled_by_default,
):
    entry = _entry(
        title="Home",
        options={CONF_PLANNER_PRESET: preset, CONF_PLANNING_ENABLED: False,
                 CONF_ACTION_EMISSION_ENABLED: False},
        subentries_data=[
            _battery_subentry(subentry_id="battery", name="battery"),
            _comfort_subentry(subentry_id="comfort", name="comfort"),
            _optional_subentry(subentry_id="optional", name="optional"),
        ],
    )
    await _setup_entry(hass, entry)
    await _run_optimize(hass)
    coordinator = entry.runtime_data.coordinator
    assert not coordinator._plan_error.has_error
    snapshot = coordinator.data
    assert len(snapshot.action_schedules["batteries"]["battery"]) == 4
    assert len(snapshot.action_schedules["comforts"]["comfort"]) == 4
    diagnostics = snapshot.diagnostics
    assert diagnostics["optionals"]["optional"]["next_start_option"]
    assert diagnostics["outlook"] is not None
    optimizer = diagnostics["optimizer"]
    assert optimizer["planner_preset"] == preset
    assert len(optimizer["projections"]["per_slot"]) == 4
    assert any(flow["comfort_load_kwh"] > 0 for flow in optimizer["energy_flows"]["per_slot"])
    # The test has no ordered recorder history; every implementation must expose
    # the conservative fallback instead of claiming that comfort was verified.
    assert "comfort_history_unavailable" in optimizer["suboptimal_reasons"]
    assert snapshot.planner_status == "degraded"
    assert hass.states.get("sensor.home_status").state == "degraded"
    assert hass.states.get("sensor.home_optional_next_start_option") is not None
