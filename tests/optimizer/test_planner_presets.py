"""Named configurations preserve existing tuning and isolate cached state."""

import pytest

from custom_components.wattplan.optimizer.models import encode_state_blob
from custom_components.wattplan.optimizer.planners.presets import (
    PRESETS, get_preset, lookahead_slots, run_preset,
)
from custom_components.wattplan.optimizer.planners.registry import BUILTINS, create_planner
from tests.optimizer.planner_conformance import conformance_params


def test_catalog_covers_all_algorithms_and_every_factory_accepts_its_settings():
    assert {preset.planner for preset in PRESETS.values()} == set(BUILTINS)
    for preset in PRESETS.values():
        assert callable(create_planner(preset.planner, dict(preset.options)))
    with pytest.raises(ValueError, match="Unknown planner preset"):
        get_preset("typo")


def test_highs_presets_apply_slots_and_discard_incompatible_state(monkeypatch):
    from custom_components.wattplan import optimizer

    seen = []
    legacy = encode_state_blob({"v": 1})

    def fake(params):
        seen.append((params.lookahead_slots, params.state))
        return {"state": legacy}

    monkeypatch.setattr(optimizer, "optimize", fake)
    params = conformance_params()[1].model_copy(update={"lookahead_slots": 22})
    first = run_preset(params, "highs")
    second = run_preset(params.model_copy(update={"state": first["state"]}), "highs-16")
    run_preset(params.model_copy(update={"state": second["state"]}), "highs-16")
    run_preset(params.model_copy(update={"state": second["state"]}), "highs-24")
    assert seen == [(22, None), (16, None), (16, legacy), (24, None)]
    assert second["diagnostics"]["planner"]["preset"] == "highs-16"
    assert second["diagnostics"]["planner"]["options"] == {"lookahead_slots": 16}


def test_every_named_preset_owns_algorithm_tuning_but_preserves_request(monkeypatch):
    from custom_components.wattplan.optimizer.planners import registry
    from custom_components.wattplan.optimizer.planners.presets import algorithm_settings

    baseline = conformance_params()[2].model_copy(
        update={"state": encode_state_blob({"v": 1, "unchanged": True})}
    )
    external = baseline.model_copy(update={
        "lookahead_slots": 6,
        "throughput_cost_per_kwh": 1.7,
        "action_deadband_kwh": 0.3,
        "mode_switch_cost": 2.1,
        "solver_time_limit_seconds": 7.5,
        "infer_battery_preserve_policy": False,
    })
    seen = []

    def fake_create_planner(planner, options):
        def call(params):
            seen.append((planner, options, params))
            return {}
        return call

    monkeypatch.setattr(registry, "create_planner", fake_create_planner)
    for name, preset in PRESETS.items():
        run_preset(external, name)
        planner, options, params = seen[-1]
        assert planner == preset.planner
        assert options == preset.options
        assert {
            field: getattr(params, field)
            for field in algorithm_settings(
                name, plan_slots=len(external.grid_import_price_per_kwh)
            )
        } == algorithm_settings(
            name, plan_slots=len(external.grid_import_price_per_kwh)
        )
        assert params.grid_import_price_per_kwh == external.grid_import_price_per_kwh
        assert params.grid_export_price_per_kwh == external.grid_export_price_per_kwh
        assert params.solar_input_kwh == external.solar_input_kwh
        assert params.usage_kwh == external.usage_kwh
        assert params.battery_entities == external.battery_entities
        assert params.comfort_entities == external.comfort_entities
        assert params.optional_entities == external.optional_entities
        assert params.state == external.state
        assert params.slot_minutes == external.slot_minutes


def test_highs_lookahead_ignores_legacy_user_setting():
    assert lookahead_slots("highs", 6, 96) == 22
    assert lookahead_slots("highs-16", 6, 96) == 16


def test_effective_horizon_has_unambiguous_units():
    assert lookahead_slots("highs", 22, 96) == 22
    assert lookahead_slots("highs-16", 22, 96) == 16
    assert lookahead_slots("highs-24", 22, 8) == 8
    assert lookahead_slots("greedy-8", 22, 96) == 96


@pytest.mark.parametrize("value", [True, 1, 673, "16", 1.5])
def test_highs_rejects_invalid_preset_lookahead(value):
    with pytest.raises(ValueError, match="lookahead_slots"):
        create_planner("highs", {"lookahead_slots": value})
