"""Causal spread selection preserves hybrid behavior and shared orchestration."""

import json

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners import adaptive_solar_policy
from custom_components.wattplan.optimizer.planners.adaptive_solar_policy import factory
from custom_components.wattplan.optimizer.planners.hybrid_policy import factory as hybrid_factory
from tests.optimizer.planner_conformance import assert_conforms, conformance_params, economic_params


@pytest.mark.parametrize("option", ["low_surplus_spread", "high_surplus_spread"])
@pytest.mark.parametrize("value", [True, -.1, 1.1, float("nan"), float("inf"), "1"])
def test_invalid_spreads(option, value):
    with pytest.raises(ValueError, match=option):
        factory(**{option: value})


@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf"), "1"])
def test_invalid_switch_ratio(value):
    with pytest.raises(ValueError, match="spread_switch_ratio"):
        factory(spread_switch_ratio=value)


@pytest.mark.parametrize("options", [{"surplus_ratio": 0}, {"energy_intervals": True},
                                    {"terminal_value_fraction": -1}, {"prerefill_guard": 1}])
def test_hybrid_validation_preserved(options):
    with pytest.raises(ValueError):
        factory(**options)


@pytest.mark.parametrize("params", conformance_params())
def test_shared_contract(params):
    assert_conforms("custom_components.wattplan.optimizer.planners.adaptive_solar_policy:factory", params)


@pytest.mark.parametrize("threshold", [.5, 1.0])
@pytest.mark.parametrize("high_spread", [0, .5])
@pytest.mark.parametrize("offset", [-.01, 0, .01])
def test_selection_matches_one_fixed_hybrid(threshold, high_spread, offset):
    solar = 3 * (threshold + offset)
    params = economic_params(usage=(0, 1, 0, 0), prices=(.1, 1, 1, 1), solar=(0, 0, solar, 0))
    spread = high_spread if offset >= 0 else 1.0
    result = factory(spread_switch_ratio=threshold, high_surplus_spread=high_spread)(params)
    direct = hybrid_factory(energy_intervals=6, pv_scenario_spread=spread)(params)
    diagnostics = result["diagnostics"]["adaptive_solar"]
    assert diagnostics["forecast_surplus_ratio"] == pytest.approx(threshold + offset)
    assert diagnostics["selected_pv_scenario_spread"] == spread
    assert result["diagnostics"]["hybrid_gate"] == direct["diagnostics"]["hybrid_gate"]
    assert result["entities"] == direct["entities"]
    assert result["projections"] == direct["projections"]


def test_ac_headroom_uses_charge_efficiency():
    payload = economic_params(solar=(0, 3)).model_dump()
    payload["battery_entities"][0]["charge_efficiency"] = .5
    result = factory()(OptimizationParams(**payload))
    diagnostics = result["diagnostics"]["adaptive_solar"]
    assert diagnostics["ac_storage_headroom_kwh"] == 6
    assert diagnostics["forecast_surplus_ratio"] == pytest.approx(1 / 3)
    assert diagnostics["selected_regime"] == "low_surplus"


def test_deficit_can_dominate_headroom():
    params = economic_params(usage=(0, 5, 0, 0), prices=(.1, 1, 1, 1), solar=(0, 0, 2, 0))
    diagnostics = factory()(params)["diagnostics"]["adaptive_solar"]
    assert diagnostics["ratio_denominator_kwh"] == 5
    assert diagnostics["forecast_surplus_ratio"] == .4
    assert diagnostics["selected_regime"] == "low_surplus"


@pytest.mark.parametrize("preference", ["throughput_cost_per_kwh", "mode_switch_cost", "action_deadband_kwh"])
def test_preferences_keep_hybrid_value_fallback(preference):
    params = economic_params(usage=(0, 0, 0, 0), prices=(1, 1, 1, 1), solar=(0, 0, 10, 0))
    payload = params.model_dump()
    payload[preference] = .1
    result = factory()(OptimizationParams(**payload))
    assert result["diagnostics"]["hybrid_gate"]["forced_value_reason"] == "battery_preferences"


def test_fleet_selection_reports_summed_headroom_and_keeps_joint_fallback():
    payload = economic_params(usage=(0, 0, 0, 0), prices=(1, 1, 1, 1), solar=(0, 0, 4, 0)).model_dump()
    payload["battery_entities"].append({**payload["battery_entities"][0], "name": "second"})
    result = factory()(OptimizationParams(**payload))
    assert result["diagnostics"]["adaptive_solar"]["ac_storage_headroom_kwh"] == 6
    assert result["diagnostics"]["adaptive_solar"]["selected_regime"] == "high_surplus"
    assert result["diagnostics"]["hybrid_gate"]["forced_value_reason"] == "joint_or_empty_battery_request"
    assert result["diagnostics"]["forecast_adjustments_applied"] is False


@pytest.mark.parametrize("solar, initial, ratio, regime", [
    ((0, 0, 0, 0), 3, 0.0, "low_surplus"),
    ((0, 1, 0, 0), 3, None, "high_surplus"),
])
def test_zero_denominator_is_safe_and_json_serializable(solar, initial, ratio, regime):
    params = economic_params(usage=(0, 0, 0, 0), prices=(1, 1, 1, 1), solar=solar, initial=initial)
    result = factory()(params)
    diagnostics = result["diagnostics"]["adaptive_solar"]
    assert diagnostics["forecast_surplus_ratio"] == ratio
    assert diagnostics["ratio_unbounded"] is (ratio is None)
    assert diagnostics["selected_regime"] == regime
    json.dumps(result, allow_nan=False)


def test_only_next_24_hours_enter_selection():
    params = economic_params(prices=[1] * 100, usage=[0] * 100,
                             solar=[0] * 96 + [10] * 4)
    result = factory()(params)
    adaptive = result["diagnostics"]["adaptive_solar"]
    hybrid = result["diagnostics"]["hybrid_gate"]
    assert adaptive["gate_forecast_slots"] == hybrid["gate_forecast_slots"] == 96
    assert adaptive["gate_forecast_hours"] == 24
    assert adaptive["forecast_surplus_kwh"] == hybrid["forecast_surplus_kwh"] == 0
    assert adaptive["selected_regime"] == "low_surplus"


def test_passive_gate_and_target_fallback_preserved():
    params = economic_params(usage=(0, 0, 0, 0), prices=(1, 1, 1, 1), solar=(0, 0, 10, 0))
    passive = factory()(params)
    assert passive["diagnostics"]["hybrid_gate"]["selected_policy"] == "self_consumption"
    payload = params.model_dump()
    payload["battery_entities"][0]["target"] = {"timeslot": 0, "soc_kwh": 1}
    targeted = factory()(OptimizationParams(**payload))
    assert targeted["diagnostics"]["hybrid_gate"]["forced_value_reason"] == "battery_target"
    assert targeted["entities"][0]["schedule"][0]["state"] == "grid_charge"


def test_prepares_comfort_and_runs_selected_raw_hybrid_once(monkeypatch):
    from custom_components.wattplan.optimizer.planners import comfort

    prepared_calls, planner_calls = [], []
    prepare = comfort.prepare_comfort
    original_factory = adaptive_solar_policy.hybrid_factory

    def prepare_once(params):
        prepared_calls.append(params)
        return prepare(params)

    def tracked_factory(**options):
        wrapped = original_factory(**options)
        raw = wrapped.__wrapped__

        def tracked(params):
            planner_calls.append(options["pv_scenario_spread"])
            return raw(params)

        wrapped.__wrapped__ = tracked
        return wrapped

    monkeypatch.setattr(comfort, "prepare_comfort", prepare_once)
    monkeypatch.setattr(adaptive_solar_policy, "hybrid_factory", tracked_factory)
    result = factory()(economic_params())
    assert len(prepared_calls) == 1
    assert planner_calls == [1.0]
    assert result["diagnostics"]["adaptive_solar"]["evaluated_policy_count"] == 1
    assert result["diagnostics"]["hybrid_gate"]["evaluated_policy_count"] == 1


def test_spread_selection_includes_prepared_comfort_demand():
    params = economic_params(usage=(0, 0, 0, 0), prices=(1, 1, 1, 1), solar=(1, 1, 1, 1))
    payload = params.model_dump()
    payload["rolling_window_slots"] = 4
    payload["comfort_entities"] = [{
        "name": "heater", "target_on_slots_per_rolling_window": 2,
        "max_consecutive_off_slots": 2, "power_usage_kwh": .5,
        "is_on_now": False, "off_streak_slots_now": 0,
        "on_history": [True, False, True],
    }]
    bare = factory(spread_switch_ratio=1.2)(params)
    with_comfort = factory(spread_switch_ratio=1.2)(OptimizationParams(**payload))
    assert bare["diagnostics"]["adaptive_solar"]["selected_regime"] == "high_surplus"
    adaptive = with_comfort["diagnostics"]["adaptive_solar"]
    hybrid = with_comfort["diagnostics"]["hybrid_gate"]
    assert adaptive["selected_regime"] == "low_surplus"
    assert adaptive["forecast_surplus_kwh"] == hybrid["forecast_surplus_kwh"]
    assert sum(e["type"] == "comfort" for e in with_comfort["entities"]) == 1


def test_does_not_depend_on_calendar_date():
    params = economic_params(usage=(0, 1, 0, 0), prices=(.1, 1, 1, 1), solar=(0, .4, 0, 0))
    shifted = params.model_copy(update={"plan_start": params.plan_start.replace(month=1, year=2025)})
    plan = factory()
    first, second = plan(params), plan(shifted)
    assert first["entities"] == second["entities"]
    assert first["projections"] == second["projections"]
    for key in ("adaptive_solar", "hybrid_gate", "pv_scenario_factors"):
        assert first["diagnostics"][key] == second["diagnostics"][key]
