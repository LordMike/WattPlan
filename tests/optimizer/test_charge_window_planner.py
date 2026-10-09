"""Window economics, inclusive targets, bounded work and neutral physics."""

from copy import deepcopy

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners import create_planner
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from custom_components.wattplan.optimizer.planners.charge_window import factory
from tests.optimizer.planner_conformance import assert_conforms, conformance_params

PATH = "custom_components.wattplan.optimizer.planners.charge_window:factory"


def request(*, prices=(0.1, 0.1, 0.3, 0.3, 2, 2), usage=(0, 0, 1, 1, 1, 1), initial=0, target=None, solar=None):
    horizon = len(prices)
    return OptimizationParams(
        plan_start="2026-06-15T00:00:00+00:00", slot_minutes=15,
        grid_import_price_per_kwh=list(prices), grid_export_price_per_kwh=[0] * horizon,
        usage_kwh=list(usage), solar_input_kwh=list(solar or [0] * horizon), comfort_entities=[],
        battery_entities=[{
            "name": "house", "initial_kwh": initial, "minimum_kwh": 0, "capacity_kwh": 3,
            "charge_curve_kwh": [1], "discharge_curve_kwh": [1], "can_charge_from": 3, "target": target,
        }],
    )


def policies(result):
    return [[action["state"] for action in entity["schedule"]]
            for entity in result["entities"] if entity["type"] == "battery"]


@pytest.mark.parametrize("params", conformance_params())
def test_shared_conformance(params):
    result = assert_conforms(PATH, params, {"max_evaluations": 80})
    assert result["state"] is None
    assert result["diagnostics"]["evaluations"] <= 80


@pytest.mark.parametrize("options", [
    {"max_evaluations": 0}, {"max_evaluations": True}, {"max_seconds": 0},
    {"max_seconds": float("nan")}, {"max_seconds": float("inf")},
    {"window_candidates": 0}, {"window_candidates": 17}, {"window_candidates": True},
    {"window_candidates": 1.5}, {"unknown": 1},
])
def test_invalid_options(options):
    with pytest.raises((ValueError, TypeError)):
        factory(**options)


def test_contiguous_cheap_charge_preserved_for_expensive_release():
    params = request()
    result = factory(max_evaluations=100)(params)
    assert result["projections"]["projected_cost"] == pytest.approx(0.8)
    assert policies(result)[0] == ["grid_charge", "grid_charge", "preserve", "preserve", "self_consume", "self_consume"]
    assert result["projections"]["final_soc_kwh"] == {"house": 0}


@pytest.mark.parametrize("mode, initial, goal, usage", [
    ("at_least", 0, 2, (0, 0, 0, 0)), ("exact", 0, 1, (0, 0, 0, 0)),
    ("at_most", 2, 0, (1, 1, 0, 0)), ("exact", 2, 0, (1, 1, 0, 0)),
])
def test_feasible_deadline_precedes_economic_cost(mode, initial, goal, usage):
    params = request(prices=(10, 10, -100, -100), usage=usage, initial=initial,
                     target={"timeslot": 1, "soc_kwh": goal, "mode": mode})
    result = factory(max_evaluations=100)(params)
    assert result["diagnostics"]["evaluation"]["score"][0] == 0


def test_infeasible_inclusive_deadline_does_not_use_future_energy():
    params = request(prices=(100, -100, -100, -100), usage=(0, 0, 0, 0),
                     target={"timeslot": 0, "soc_kwh": 2, "mode": "exact"})
    check = factory(max_evaluations=80)(params)["diagnostics"]["evaluation"]["target_checks"][0]
    assert check["soc_kwh"] == 1
    assert check["miss_kwh"] == 1


def test_multi_battery_deadlines_and_pv_only_negative_tariffs():
    params = request(prices=(-1, 0.2, 3, 3), usage=(0, 0, 1, 1), solar=(0, 2, 0, 0),
                     target={"timeslot": 1, "soc_kwh": 1, "mode": "at_least"})
    payload = params.model_dump()
    payload["battery_entities"].append({**payload["battery_entities"][0], "name": "pv", "can_charge_from": 2})
    params = OptimizationParams(**payload)
    params.grid_export_price_per_kwh = [-2] * 4
    result = factory(max_evaluations=160)(params)
    assert result["diagnostics"]["evaluation"]["score"][0] == 0
    assert "grid_charge" not in policies(result)[1]
    evaluated = evaluate_schedule(params, policies(result))
    assert result["projections"]["projected_cost"] == evaluated.tariff_cost
    assert result["projections"]["final_soc_kwh"] == dict(zip((b.name for b in params.battery_entities), evaluated.steps[-1].levels))


def test_nonlinear_curves_efficiencies_preferences_and_reserve():
    params = conformance_params()[2]
    params.battery_entities[0].charge_curve_kwh = [1, 0.5, 0.2]
    params.battery_entities[0].discharge_curve_kwh = [0.2, 0.8, 1]
    params.throughput_cost_per_kwh = 0.4
    params.mode_switch_cost = 0.7
    result = factory(max_evaluations=100)(params)
    replay = evaluate_schedule(params, policies(result))
    assert result["diagnostics"]["evaluation"]["score"] == list(replay.score)
    assert result["diagnostics"]["evaluation"]["reserve_shortfalls"]
    for slot, step in enumerate(replay.steps):
        for index, entity in enumerate(result["entities"]):
            assert entity["schedule"][slot]["level"] == step.levels[index]
    assert replay.score <= evaluate_schedule(params, [["self_consume"] * 4 for _ in params.battery_entities]).score


@pytest.mark.parametrize("count", [1, 2, 7, 50])
def test_every_evaluation_counted_and_final_result_not_replayed(monkeypatch, count):
    import custom_components.wattplan.optimizer.planners.charge_window as module
    calls = []
    original = module.evaluate_schedule
    def recorded(params, candidate):
        calls.append(deepcopy(candidate))
        return original(params, candidate)
    monkeypatch.setattr(module, "evaluate_schedule", recorded)
    result = factory(max_evaluations=count)(request())
    assert len(calls) == result["diagnostics"]["evaluations"] <= count
    assert result["diagnostics"]["evaluation"]["score"] <= result["diagnostics"]["baseline_score"]


def test_tiny_deadline_still_returns_complete_baseline():
    result = factory(max_seconds=1e-12)(request())
    assert result["diagnostics"]["evaluations"] == 1
    assert result["diagnostics"]["termination_reason"] == "search_time_limit"
    assert len(result["entities"][0]["schedule"]) == 6
    assert result["diagnostics"]["limitations"]


def test_long_many_battery_requests_bound_work_and_candidate_generation():
    params = request(prices=(0.1,) * 672, usage=(0,) * 672)
    payload = params.model_dump()
    payload["battery_entities"] = [{**payload["battery_entities"][0], "name": f"battery-{i}"} for i in range(16)]
    result = factory(max_evaluations=10000)(OptimizationParams(**payload))
    assert result["diagnostics"]["effective_max_evaluations"] == 18
    assert result["diagnostics"]["evaluations"] <= 18
    assert result["diagnostics"]["candidate_attempts"] <= 144
    assert all(len(entity["schedule"]) == 672 for entity in result["entities"])


def test_deterministic_stateless_and_input_isolation():
    params = conformance_params()[2]
    before = params.model_dump(mode="json")
    planner = create_planner(PATH, {"max_evaluations": 100})
    first, second = planner(params), planner(params)
    assert first["entities"] == second["entities"]
    assert first["diagnostics"]["evaluation"] == second["diagnostics"]["evaluation"]
    assert params.model_dump(mode="json") == before
    changed = request(prices=(4, -1, 2, 1), usage=(1, 0, 1, 0), initial=2,
                      target={"timeslot": 0, "soc_kwh": 1, "mode": "exact"})
    result = planner(changed)
    assert result["diagnostics"]["evaluation"]["score"][0] == 0
    assert result["projections"]["projected_cost"] == evaluate_schedule(changed, policies(result)).tariff_cost
    assert any("lookahead_slots" in note for note in result["diagnostics"]["planner"]["notes"])


def test_import_never_loads_solver():
    import subprocess
    import sys
    subprocess.run([sys.executable, "-c",
        "from custom_components.wattplan.optimizer.planners.charge_window import factory; factory(); import sys; assert 'highspy' not in sys.modules; assert 'custom_components.wattplan.optimizer.mpc_power_optimizer' not in sys.modules"], check=True, timeout=30)


def test_charging_before_peak_survives_cheaper_windows_after_peak():
    # Every tariff threshold seed chooses useless future cheap energy. A bundled
    # early charge/release interval must cross the empty-battery plateau instead.
    params = request(prices=(1, 1.5, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1),
                     usage=(0, 1, 0, 0, 0, 0, 0, 0))
    result = factory(max_evaluations=100)(params)
    assert result["projections"]["projected_cost"] == pytest.approx(1)
    assert policies(result)[0][:2] == ["grid_charge", "self_consume"]
    assert result["diagnostics"]["accepted_changes"]["paired_window"] > 0
    assert result["projections"]["final_soc_kwh"] == {"house": 0}


def test_finer_scales_have_reserved_evaluations():
    params = request(prices=(0.2,) * 64, usage=(0,) * 64)
    result = factory(max_evaluations=40)(params)
    assert result["diagnostics"]["scales_visited"] == [64, 32, 16, 8, 4, 2, 1]
    assert result["diagnostics"]["evaluations"] <= 40


@pytest.mark.parametrize("count", [1, 96])
@pytest.mark.parametrize("mode,initial,goal,usage", [
    ("at_least", 0, 1, (0, 0, 0, 0)),
    ("exact", 0, 1, (0, 0, 0, 0)),
    ("at_most", 1, 0, (1, 0, 0, 0)),
    ("exact", 1, 0, (1, 0, 0, 0)),
])
def test_earliest_feasible_deadline_is_seeded_before_tariff_search(count, mode, initial, goal, usage):
    params = request(prices=(10, 0.01, 0.01, 0.01), usage=usage, initial=initial,
                     target={"timeslot": 0, "soc_kwh": goal, "mode": mode})
    actual = factory(max_evaluations=count)(params)
    assert actual["diagnostics"]["evaluation"]["score"][0] == 0
    assert actual["diagnostics"]["target_seed_transitions"] == 1
    assert actual["diagnostics"]["evaluations"] <= count


@pytest.mark.parametrize("count", [1, 96])
def test_reserve_recovery_starts_before_future_cheap_prices(count):
    params = request(prices=(10, 0.01, 0.01, 0.01), usage=(0, 0, 0, 0))
    params.battery_entities[0].minimum_kwh = 1.0
    params.battery_entities[0].charge_curve_kwh = [0.4]
    actual = factory(max_evaluations=count)(params)
    assert policies(actual)[0][:3] == ["grid_charge"] * 3
    assert [slot["level"] for slot in actual["entities"][0]["schedule"]][:3] == pytest.approx([0.4, 0.8, 1.2])
    assert actual["projections"]["projected_cost"] < 5
    assert actual["fitness"] > 1000
    assert "battery_min_unmet" in actual["problems"]
    assert actual["diagnostics"]["evaluation"]["score"] == list(evaluate_schedule(params, policies(actual)).score)


def test_pv_only_reserve_seed_obeys_ingress_and_curves():
    params = request(prices=(10,) * 4, usage=(0,) * 4, solar=(0.4,) * 4)
    battery = params.battery_entities[0]
    battery.minimum_kwh = 1.0
    battery.can_charge_from = 2
    battery.charge_curve_kwh = [0.4]
    actual = factory(max_evaluations=1)(params)
    assert "grid_charge" not in policies(actual)[0]
    assert [slot["level"] for slot in actual["entities"][0]["schedule"]] == pytest.approx([0.4, 0.8, 1.2, 1.6])
    assert actual["projections"]["projected_cost"] == 0


def test_direct_and_registered_window_planners_include_comfort_and_optional_loads():
    from custom_components.wattplan.optimizer.planners.comfort import prepare_comfort
    from tests.optimizer.test_planner_comfort import request as comfort_request

    raw = comfort_request().model_dump()
    raw["battery_entities"] = request(initial=0.1).model_dump()["battery_entities"]
    raw["battery_entities"][0].update(
        minimum_kwh=0.5, target={"timeslot": 3, "soc_kwh": 1.0, "mode": "at_least"}
    )
    raw["optional_entities"] = [{
        "name": "dishwasher", "duration_timeslots": 2,
        "start_before_timeslot": 8, "energy_kwh": 0.6,
    }]
    params = OptimizationParams.model_validate(raw)
    before = params.model_dump()
    direct = factory(max_evaluations=96)(params)
    registered = create_planner("charge-window", {"max_evaluations": 96})(params)
    assert params.model_dump() == before
    assert direct["entities"] == registered["entities"]
    assert direct["projections"] == registered["projections"]
    assert direct["optional_entity_options"][0]["options"]
    assert len(direct["projections"]["per_slot"]) == 8
    assert len(direct["energy_flows"]["per_slot"]) == 8
    assert direct["execution_time"] > 0
    assert not direct["suboptimal"]
    replay = evaluate_schedule(prepare_comfort(params).params, policies(direct))
    assert direct["projections"]["projected_cost"] == pytest.approx(replay.tariff_cost)
    assert factory().capabilities.comfort
    assert factory().capabilities.optional_loads == "supported"
