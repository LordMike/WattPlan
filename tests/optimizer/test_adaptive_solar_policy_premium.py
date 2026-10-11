"""Independent causal premium selection keeps hybrid and reporting contracts."""

import json

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners import adaptive_solar_policy, comfort
from custom_components.wattplan.optimizer.planners.adaptive_solar_policy import factory
from custom_components.wattplan.optimizer.planners.hybrid_policy import factory as hybrid_factory
from tests.optimizer.planner_conformance import assert_conforms, conformance_params, economic_params


PREMIUM_OPTIONS = {"low_surplus_grid_charge_premium_per_kwh": 0.0,
                   "high_surplus_grid_charge_premium_per_kwh": .03}


@pytest.mark.parametrize("name", list(PREMIUM_OPTIONS))
@pytest.mark.parametrize("value", [True, -.01, float("nan"), float("inf"), "0", None])
def test_invalid_premiums(name, value):
    with pytest.raises(ValueError, match=name):
        factory(**{name: value})


@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf"), "0", None])
def test_invalid_premium_threshold(value):
    with pytest.raises(ValueError, match="premium_switch_ratio"):
        factory(premium_switch_ratio=value)


@pytest.mark.parametrize("params", conformance_params())
def test_enabled_premium_contract(params):
    assert_conforms("custom_components.wattplan.optimizer.planners.adaptive_solar_policy:factory",
                    params, PREMIUM_OPTIONS)


@pytest.mark.parametrize("ratio", [0, .009, .01, .011, .49, .5, 1.6])
def test_threshold_matches_direct_hybrid(ratio):
    params = economic_params(usage=(0, 0, 0, 0), prices=(.1, 1, 1, 1),
                             solar=(0, 0, 3 * ratio, 0))
    before = params.model_dump()
    premium = .03 if ratio >= .01 else 0
    result = factory(low_surplus_spread=1, high_surplus_spread=1, **PREMIUM_OPTIONS)(params)
    direct = hybrid_factory(energy_intervals=6, pv_scenario_spread=1,
                            grid_charge_premium_per_kwh=premium)(params)
    adaptive = result["diagnostics"]["adaptive_solar"]
    assert adaptive["selected_grid_charge_premium_per_kwh"] == premium
    assert adaptive["selected_premium_regime"] == ("high_surplus" if premium else "low_surplus")
    assert adaptive["premium_switch_ratio"] == .01
    assert result["entities"] == direct["entities"]
    assert result["projections"] == direct["projections"]
    assert result["diagnostics"]["evaluation"] == direct["diagnostics"]["evaluation"]
    assert result["diagnostics"]["hybrid_gate"] == direct["diagnostics"]["hybrid_gate"]
    assert params.model_dump() == before


@pytest.mark.parametrize("spread_threshold,premium_threshold,ratio,spread,premium", [
    (.5, .01, .005, 1, 0), (.5, .01, .1, 1, .03), (.5, .01, .6, .5, .03),
    (.01, .5, .1, .5, 0),
])
def test_independent_gates_select_one_of_four_raw_hybrids(
        monkeypatch, spread_threshold, premium_threshold, ratio, spread, premium):
    configured, calls, preparations = [], [], []
    original_factory, prepare = adaptive_solar_policy.hybrid_factory, comfort.prepare_comfort

    def tracked_factory(**options):
        configured.append((options["pv_scenario_spread"], options["grid_charge_premium_per_kwh"]))
        wrapped = original_factory(**options)
        raw = wrapped.__wrapped__

        def tracked(params):
            calls.append((options["pv_scenario_spread"], options["grid_charge_premium_per_kwh"]))
            return raw(params)

        wrapped.__wrapped__ = tracked
        return wrapped

    def tracked_prepare(params):
        preparations.append(params)
        return prepare(params)

    monkeypatch.setattr(adaptive_solar_policy, "hybrid_factory", tracked_factory)
    monkeypatch.setattr(comfort, "prepare_comfort", tracked_prepare)
    plan = factory(high_surplus_spread=.5, spread_switch_ratio=spread_threshold,
                   premium_switch_ratio=premium_threshold, **PREMIUM_OPTIONS)
    assert set(configured) == {(1, 0), (1, .03), (.5, 0), (.5, .03)}
    result = plan(economic_params(usage=(0, 0, 0, 0), prices=(1, 1, 1, 1),
                                  solar=(0, 0, 3 * ratio, 0)))
    assert len(configured) == 4  # No per-request factory construction.
    assert calls == [(spread, premium)]
    assert len(preparations) == 1
    assert result["diagnostics"]["adaptive_solar"]["evaluated_policy_count"] == 1


def test_disabled_premiums_preserve_existing_diagnostics_and_policy():
    params = economic_params(usage=(0, 1, 0, 0), prices=(.1, 1, 1, 1), solar=(0, .4, 0, 0))
    result = factory()(params)
    explicit_zero = factory(premium_switch_ratio=2, low_surplus_grid_charge_premium_per_kwh=0,
                            high_surplus_grid_charge_premium_per_kwh=0)(params)
    direct = hybrid_factory(energy_intervals=6, pv_scenario_spread=1)(params)
    assert result["entities"] == explicit_zero["entities"] == direct["entities"]
    assert result["projections"] == explicit_zero["projections"] == direct["projections"]
    assert result["diagnostics"]["adaptive_solar"] == explicit_zero["diagnostics"]["adaptive_solar"]
    assert not any("premium" in key for key in result["diagnostics"]["adaptive_solar"])
    assert result["diagnostics"]["hybrid_gate"] == direct["diagnostics"]["hybrid_gate"]


@pytest.mark.parametrize("ratio", [0, 1])
def test_equal_premiums_match_fixed_hybrid(ratio):
    params = economic_params(usage=(0, 1, 0, 0), prices=(.1, 1, 1, 1),
                             solar=(0, 0, 3 * ratio, 0))
    result = factory(low_surplus_spread=1, high_surplus_spread=1,
                     low_surplus_grid_charge_premium_per_kwh=.03,
                     high_surplus_grid_charge_premium_per_kwh=.03)(params)
    direct = hybrid_factory(energy_intervals=6, pv_scenario_spread=1,
                            grid_charge_premium_per_kwh=.03)(params)
    assert result["entities"] == direct["entities"]
    assert result["projections"] == direct["projections"]


def test_passive_gate_does_not_apply_selected_premium():
    params = economic_params(usage=(0, 0, 0, 0), prices=(1, 1, 1, 1), solar=(0, 0, 10, 0))
    result = factory(**PREMIUM_OPTIONS)(params)
    assert result["diagnostics"]["adaptive_solar"]["selected_grid_charge_premium_per_kwh"] == .03
    assert result["diagnostics"]["hybrid_gate"]["selected_policy"] == "self_consumption"
    assert result["diagnostics"]["hybrid_gate"]["grid_charge_premium_applied"] is False
    assert result["diagnostics"]["evaluation"]["score"] == factory()(params)["diagnostics"]["evaluation"]["score"]


@pytest.mark.parametrize("solar, premium, unbounded", [(0, 0, False), (1, .03, True)])
def test_zero_denominator_remains_json_safe(solar, premium, unbounded):
    params = economic_params(usage=(0, 0, 0, 0), prices=(1, 1, 1, 1),
                             solar=(0, solar, 0, 0), initial=3)
    result = factory(**PREMIUM_OPTIONS)(params)
    adaptive = result["diagnostics"]["adaptive_solar"]
    assert adaptive["selected_grid_charge_premium_per_kwh"] == premium
    assert adaptive["ratio_unbounded"] is unbounded
    json.dumps(result, allow_nan=False)


def test_premium_gate_uses_only_next24h():
    params = economic_params(prices=[1] * 100, usage=[0] * 100,
                             solar=[0] * 96 + [10] * 4)
    result = factory(**PREMIUM_OPTIONS)(params)
    assert result["diagnostics"]["adaptive_solar"]["selected_grid_charge_premium_per_kwh"] == 0


def test_premium_gate_includes_prepared_comfort_demand():
    params = economic_params(usage=(0, 0, 0, 0), prices=(1, 1, 1, 1), solar=(1, 1, 1, 1))
    payload = params.model_dump()
    payload["rolling_window_slots"] = 4
    payload["comfort_entities"] = [{
        "name": "heater", "target_on_slots_per_rolling_window": 2,
        "max_consecutive_off_slots": 2, "power_usage_kwh": .5,
        "is_on_now": False, "off_streak_slots_now": 0,
        "on_history": [True, False, True],
    }]
    plan = factory(premium_switch_ratio=1.2, **PREMIUM_OPTIONS)
    assert plan(params)["diagnostics"]["adaptive_solar"]["selected_grid_charge_premium_per_kwh"] == .03
    result = plan(OptimizationParams(**payload))
    assert result["diagnostics"]["adaptive_solar"]["selected_grid_charge_premium_per_kwh"] == 0
    assert result["diagnostics"]["adaptive_solar"]["forecast_surplus_kwh"] == result["diagnostics"]["hybrid_gate"]["forecast_surplus_kwh"]


@pytest.mark.parametrize("mode", ["target", "reserve", "negative", "preferences", "no_pv", "fleet"])
def test_constrained_branches_match_fixed_hybrid(mode):
    payload = economic_params(usage=(0, 1, 0, 0), prices=(.1, 1, 1, 1), solar=(0, 0, 4, 0)).model_dump()
    battery = payload["battery_entities"][0]
    if mode == "target":
        battery["target"] = {"timeslot": 0, "soc_kwh": 1}
    elif mode == "reserve":
        battery["minimum_kwh"] = 1
    elif mode == "negative":
        payload["grid_import_price_per_kwh"][0] = -.1
    elif mode == "preferences":
        payload["throughput_cost_per_kwh"] = .1
    elif mode == "no_pv":
        battery["can_charge_from"] = 1
    else:
        payload["battery_entities"].append({**battery, "name": "second"})
    params = OptimizationParams(**payload)
    result = factory(low_surplus_spread=1, high_surplus_spread=1, **PREMIUM_OPTIONS)(params)
    direct = hybrid_factory(energy_intervals=6, pv_scenario_spread=1,
                            grid_charge_premium_per_kwh=.03)(params)
    assert result["entities"] == direct["entities"]
    assert result["projections"] == direct["projections"]
    assert result["diagnostics"]["evaluation"] == direct["diagnostics"]["evaluation"]
    assert result["diagnostics"]["hybrid_gate"]["selected_policy"] == "value_policy"
    if mode == "fleet":
        assert result["diagnostics"]["grid_charge_premium_applied"] is False


def test_enabled_premium_has_no_date_dependency():
    params = economic_params(usage=(0, 1, 0, 0), prices=(.1, 1, 1, 1), solar=(0, .4, 2, 0))
    shifted = params.model_copy(update={"plan_start": params.plan_start.replace(month=1, year=2025)})
    plan = factory(high_surplus_spread=.5, **PREMIUM_OPTIONS)
    first, second = plan(params), plan(shifted)
    assert first["entities"] == second["entities"]
    assert first["projections"] == second["projections"]
    assert first["diagnostics"]["adaptive_solar"] == second["diagnostics"]["adaptive_solar"]
