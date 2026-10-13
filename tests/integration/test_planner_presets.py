"""Production selection, state invalidation and reproduction identity."""

import json
import asyncio
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.wattplan.const import (
    CONF_ACTION_EMISSION_ENABLED, CONF_HOURS_TO_PLAN,
    CONF_OPTIMIZER_LOOKAHEAD_HOURS, CONF_PLANNER_PRESET, CONF_PLANNING_ENABLED,
    CONF_PLANNER_REPRODUCTION_RETENTION_DAYS,
    CONF_RECORD_PLANNER_REPRODUCTIONS,
    CONF_SLOT_MINUTES,
    CONF_SHOW_EXPERIMENTAL_PLANNERS,
)
from custom_components.wattplan.optimizer.planners.presets import (
    PRESETS,
    available_preset_names,
    run_preset,
)
from custom_components.wattplan.flows.source_shared import _validate_core_data
from custom_components.wattplan.flows.main import WattPlanConfigFlow
from custom_components.wattplan.coordinator_parts import CycleTrigger, PlanningStageError
from .test_config_flow import (
    _create_basic_entry,
    _schema_default,
    _serialized_schema_field,
)
from .test_integration_e2e import (
    _battery_subentry, _entry, _fake_optimize_with_entities, _run_optimize, _setup_entry,
    entity_registry_enabled_by_default,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def test_options_save_preset_and_keep_it_selected(hass, mock_setup_entry):
    entry = await _create_basic_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "troubleshooting"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_RECORD_PLANNER_REPRODUCTIONS: False,
            CONF_SHOW_EXPERIMENTAL_PLANNERS: True,
            CONF_PLANNER_REPRODUCTION_RETENTION_DAYS: 14,
        },
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "planner_core"}
    )
    assert _schema_default(result, CONF_PLANNER_PRESET) == "highs"
    assert "beam-2" in {
        option["value"]
        for option in _serialized_schema_field(result, CONF_PLANNER_PRESET)["selector"]["select"]["options"]
    }
    assert "local-search-3" in {
        option["value"]
        for option in _serialized_schema_field(result, CONF_PLANNER_PRESET)["selector"]["select"]["options"]
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {
            CONF_PLANNER_PRESET: "beam-2", CONF_SLOT_MINUTES: "60",
            CONF_HOURS_TO_PLAN: "24",
        },
    )
    assert result["type"] is FlowResultType.MENU
    assert entry.options[CONF_PLANNER_PRESET] == "beam-2"
    assert CONF_PLANNER_PRESET not in entry.data
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "planner_core"}
    )
    assert _schema_default(result, CONF_PLANNER_PRESET) == "beam-2"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PLANNER_PRESET: "beam-2",
            CONF_SLOT_MINUTES: "60",
            CONF_HOURS_TO_PLAN: "24",
        },
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "troubleshooting"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_RECORD_PLANNER_REPRODUCTIONS: False,
            CONF_SHOW_EXPERIMENTAL_PLANNERS: False,
            CONF_PLANNER_REPRODUCTION_RETENTION_DAYS: 14,
        },
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "planner_core"}
    )
    assert _schema_default(result, CONF_PLANNER_PRESET) == "beam-2"
    choices = {
        option["value"]
        for option in _serialized_schema_field(result, CONF_PLANNER_PRESET)["selector"]["select"]["options"]
    }
    assert choices == {"highs", "self-consumption", "local-search-3", "beam-2"}


async def test_hidden_current_experimental_preset_remains_selectable(
    hass, mock_setup_entry
):
    entry = await _create_basic_entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PLANNER_PRESET: "beam-2",
            CONF_SHOW_EXPERIMENTAL_PLANNERS: False,
        },
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "planner_core"}
    )
    assert _schema_default(result, CONF_PLANNER_PRESET) == "beam-2"
    choices = {
        option["value"]
        for option in _serialized_schema_field(result, CONF_PLANNER_PRESET)["selector"]["select"]["options"]
    }
    assert choices == {"highs", "self-consumption", "local-search-3", "beam-2"}


async def test_options_recovers_unknown_preset_without_loaded_runtime(
    hass, mock_setup_entry
):
    entry = await _create_basic_entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_PLANNER_PRESET: "removed-preset"},
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert entry.options[CONF_PLANNER_PRESET] == "highs"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "planner_core"}
    )
    assert _schema_default(result, CONF_PLANNER_PRESET) == "highs"


def test_experimental_preset_filter_keeps_hidden_current_selection():
    stable = available_preset_names()
    assert stable == ("highs", "self-consumption", "local-search-3")
    assert "beam-2" not in stable
    assert "beam-2" in available_preset_names(current="beam-2")
    assert set(available_preset_names(show_experimental=True)) == set(PRESETS)


def test_irrelevant_legacy_lookahead_does_not_block_fixed_presets():
    data = {CONF_SLOT_MINUTES: 60, CONF_HOURS_TO_PLAN: 24,
            CONF_OPTIMIZER_LOOKAHEAD_HOURS: 0.1}
    assert _validate_core_data(data)
    assert not _validate_core_data({**data, CONF_PLANNER_PRESET: "beam-2"})
    assert _validate_core_data({**data, CONF_PLANNER_PRESET: "typo"}) == {
        CONF_PLANNER_PRESET: "unknown_planner_preset"
    }


@pytest.mark.parametrize("preset", list(PRESETS))
async def test_every_preset_runs_through_live_coordinator(
    hass: HomeAssistant, preset: str, entity_registry_enabled_by_default: None,
):
    entry = _entry(
        title="Home",
        options={CONF_PLANNER_PRESET: preset, CONF_PLANNING_ENABLED: False,
                 CONF_ACTION_EMISSION_ENABLED: False},
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
    )
    await _setup_entry(hass, entry)
    await _run_optimize(hass)
    coordinator = entry.runtime_data.coordinator
    assert coordinator.data is not None
    assert not coordinator._plan_error.has_error
    diagnostics = coordinator.data.diagnostics["optimizer"]
    assert diagnostics["planner_preset"] == preset
    assert diagnostics["planner"] == PRESETS[preset].planner
    assert diagnostics["diagnostics"]["planner"]["preset"] == preset
    assert diagnostics["planner_options"] == PRESETS[preset].options
    duration = hass.states.get("sensor.home_last_run_duration")
    assert duration is not None
    assert duration.attributes["planner_preset"] == preset


async def test_changing_preset_reloads_and_drops_incompatible_cached_plan(
    hass: HomeAssistant, entity_registry_enabled_by_default: None,
):
    entry = _entry(
        title="Home", subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
        options={CONF_PLANNER_PRESET: "greedy-8", CONF_PLANNING_ENABLED: False,
                 CONF_ACTION_EMISSION_ENABLED: False},
    )
    await _setup_entry(hass, entry)
    await _run_optimize(hass)
    previous = entry.runtime_data.coordinator
    with patch("custom_components.wattplan.entry_setup.async_try_initial_plan"):
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PLANNER_PRESET: "beam-2"}
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.coordinator is not previous
    assert entry.runtime_data.optimizer_state is None


async def test_search_and_placement_diagnostics_survive_ha_projection(
    hass: HomeAssistant, entity_registry_enabled_by_default: None,
):
    entry = _entry(title="Home", subentries_data=[
        _battery_subentry(subentry_id="battery", name="battery")
    ])
    await _setup_entry(hass, entry)

    def result(params):
        return {
            **_fake_optimize_with_entities(params),
            "execution_time": 0.125,
            "diagnostics": {"search": {"evaluations": 8, "elapsed_seconds": 0.12}},
            "comfort_placement": {"accepted_moves": 2},
        }

    with patch("custom_components.wattplan.optimizer.optimize", side_effect=result):
        await _run_optimize(hass)
    diagnostics = entry.runtime_data.coordinator.data.diagnostics["optimizer"]
    assert diagnostics["execution_time_s"] == 0.125
    assert diagnostics["diagnostics"]["search"] == {
        "evaluations": 8, "elapsed_seconds": 0.12
    }
    assert diagnostics["comfort_placement"] == {"accepted_moves": 2}


@pytest.mark.parametrize("preset", ["highs", "highs-16", "beam-2"])
@pytest.mark.parametrize("as_json", [False, True])
async def test_input_export_preserves_selected_algorithm_and_existing_model_field(
    hass: HomeAssistant, preset, as_json,
):
    entry = _entry(
        title="Home", subentries_data=[],
        options={CONF_PLANNER_PRESET: preset, CONF_PLANNING_ENABLED: False,
                 CONF_ACTION_EMISSION_ENABLED: False},
    )
    await _setup_entry(hass, entry)
    response = await hass.services.async_call(
        "wattplan", "export_planner_input", {"as_json": as_json},
        blocking=True, return_response=True,
    )
    assert response["planner_preset"] == preset
    assert response["planner"] == PRESETS[preset].planner
    assert response["planner_options"] == PRESETS[preset].options
    model = json.loads(response["model_json"]) if as_json else response["model"]
    assert model["usage_kwh"]
    assert ("model_json" if as_json else "model") in response


@pytest.mark.parametrize("preset", ["highs", "highs-16", "beam-2"])
async def test_setup_summary_describes_selected_settings(hass, preset):
    flow = WattPlanConfigFlow()
    flow.hass = hass
    flow._core = {"name": "Home", CONF_SLOT_MINUTES: 15, CONF_HOURS_TO_PLAN: 48}
    flow._entry_options = {CONF_PLANNER_PRESET: preset, "optimizer_lookahead_slots": 44}
    flow._sources = {}
    summary = await flow.async_step_setup_complete()
    placeholders = summary["description_placeholders"]
    assert placeholders["planner_preset"] == PRESETS[preset].label
    assert placeholders["search_settings"] == (
        "HiGHS lookahead: 11 hours" if preset == "highs" else "Fixed by the selected preset"
    )
    assert "lookahead_hours" not in placeholders


@pytest.mark.parametrize("phase", ["acquisition", "executor"])
@pytest.mark.parametrize("lifecycle", ["reload", "shutdown"])
async def test_obsolete_plan_cannot_update_runtime_publish_or_persist(
    hass, entity_registry_enabled_by_default, phase, lifecycle,
):
    entry = _entry(title="Home", subentries_data=[
        _battery_subentry(subentry_id="battery", name="battery")
    ])
    await _setup_entry(hass, entry)
    original_runtime = entry.runtime_data
    old = original_runtime.coordinator
    old_snapshot, old_state = old.data, original_runtime.optimizer_state
    request = await old.async_build_planner_input_export()
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_request(*args):
        started.set()
        await release.wait()
        return request

    executor = hass.async_add_executor_job

    async def delayed_executor(target, *args):
        if target is run_preset:
            started.set()
            await release.wait()
        return await executor(target, *args)

    boundary = (
        patch.object(old, "_async_build_planning_request", side_effect=delayed_request)
        if phase == "acquisition"
        else patch.object(hass, "async_add_executor_job", side_effect=delayed_executor)
    )
    with (
        boundary,
        patch.object(old, "async_persist_snapshot") as persist,
        patch.object(old.planner_history, "async_record") as history,
        patch("custom_components.wattplan.optimizer.optimize", side_effect=_fake_optimize_with_entities) as optimizer,
    ):
        pending = asyncio.create_task(old.async_plan(trigger=CycleTrigger.SERVICE))
        try:
            await started.wait()
            if lifecycle == "reload":
                with patch("custom_components.wattplan.entry_setup.async_try_initial_plan"):
                    hass.config_entries.async_update_entry(
                        entry, options={**entry.options, CONF_PLANNER_PRESET: "beam-2"}
                    )
                    await hass.async_block_till_done(wait_background_tasks=True)
                assert entry.runtime_data.coordinator is not old
                assert entry.runtime_data.optimizer_state is None
            else:
                await old.async_shutdown()
            release.set()
            with pytest.raises(PlanningStageError, match="configuration changed"):
                await pending
            assert original_runtime.optimizer_state == old_state
            assert old.data is old_snapshot
            if lifecycle == "reload":
                assert entry.runtime_data.optimizer_state is None
            persist.assert_not_called()
            history.assert_not_called()
            # Acquisition/worker wait is cancelled before obsolete work executes.
            assert optimizer.call_count == 0
        finally:
            release.set()
            if not pending.done():
                await pending
