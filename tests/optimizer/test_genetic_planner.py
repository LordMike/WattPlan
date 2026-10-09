"""Genetic search budgets, structured seeds, physical agreement and diversity."""

from copy import deepcopy

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from custom_components.wattplan.optimizer.planners.genetic import factory
from tests.optimizer.planner_conformance import assert_conforms, conformance_params

PATH = "custom_components.wattplan.optimizer.planners.genetic:factory"


def request(*, prices=(0.1, 1), usage=(0, 1), initial=0, target=None, horizon=None):
    horizon = horizon or max(4, len(prices))
    payload = conformance_params()[1].model_dump()
    payload.update(grid_import_price_per_kwh=list(prices) + [max(prices)] * (horizon - len(prices)),
                   grid_export_price_per_kwh=[0] * horizon,
                   usage_kwh=list(usage) + [0] * (horizon - len(usage)),
                   solar_input_kwh=[0] * horizon)
    payload["battery_entities"][0].update(initial_kwh=initial, minimum_kwh=0,
                                         charge_efficiency=1, discharge_efficiency=1, target=target)
    return OptimizationParams(**payload)


def policies(result):
    return [[a["state"] for a in e["schedule"]] for e in result["entities"]]


@pytest.mark.parametrize("params", conformance_params())
def test_shared_conformance(params):
    result = assert_conforms(PATH, params, {"max_evaluations": 48})
    assert result["state"] is None
    assert result["diagnostics"]["evaluations"] <= 48


@pytest.mark.parametrize("options", [
    {"max_evaluations": 0}, {"max_evaluations": True},
    {"population_size": 1}, {"population_size": True},
    {"generations": -1}, {"generations": True},
    {"elite_count": 0}, {"elite_count": 8}, {"elite_count": True},
    {"tournament_size": 0}, {"tournament_size": 9},
    {"mutation_rate": float("nan")}, {"mutation_rate": True}, {"mutation_rate": -1},
    {"crossover_rate": 1.1}, {"crossover_rate": float("inf")},
    {"seed": True}, {"seed": 1.5}, {"max_seconds": 0},
    {"guidance_slots": 0}, {"guidance_slots": True},
    {"max_seconds": float("nan")}, {"unknown": 1},
])
def test_invalid_options(options):
    with pytest.raises((ValueError, TypeError)):
        factory(**options)


def test_charge_before_expensive_window_and_preserve_existing_energy():
    result = factory(max_evaluations=8)(request())
    assert policies(result)[0][:2] == ["grid_charge", "self_consume"]
    assert result["projections"]["projected_cost"] == pytest.approx(0.1)
    result = factory(max_evaluations=8)(request(usage=(1, 1), initial=1))
    assert policies(result)[0][1] == "self_consume"
    assert result["projections"]["projected_cost"] < 1


@pytest.mark.parametrize("mode, initial, goal, usage", [
    ("at_least", 0, 2, (0, 0)), ("exact", 0, 1, (0, 0)),
    ("at_most", 2, 0, (1, 1)), ("exact", 2, 0, (1, 1)),
])
def test_feasible_deadlines_take_priority(mode, initial, goal, usage):
    params = request(prices=(10, 10), usage=usage, initial=initial,
                     target={"timeslot": 1, "soc_kwh": goal, "mode": mode})
    result = factory(max_evaluations=12)(params)
    assert result["diagnostics"]["evaluation"]["score"][0] == 0


def test_long_deadline_seed_is_not_random_gene_lottery():
    params = request(prices=(10,) * 144, usage=(0,) * 144,
                     target={"timeslot": 95, "soc_kwh": 3, "mode": "exact"})
    result = factory(max_evaluations=4)(params)
    assert result["diagnostics"]["evaluation"]["score"][0] == 0
    assert len(policies(result)[0]) == 144


def test_unreachable_target_inclusive_deadline():
    params = request(prices=(100, -100), usage=(0, 0),
                     target={"timeslot": 0, "soc_kwh": 2, "mode": "exact"})
    result = factory(max_evaluations=12)(params)
    check = result["diagnostics"]["evaluation"]["target_checks"][0]
    assert check["soc_kwh"] == 1
    assert check["miss_kwh"] == 1


@pytest.mark.parametrize("count", [1, 2, 7, 48])
def test_all_full_evaluations_are_counted(monkeypatch, count):
    import custom_components.wattplan.optimizer.planners.genetic as module
    calls = []
    original = module.evaluate_schedule

    def recorded(params, candidate):
        calls.append(deepcopy(candidate))
        return original(params, candidate)

    monkeypatch.setattr(module, "evaluate_schedule", recorded)
    result = factory(max_evaluations=count)(request())
    assert len(calls) == result["diagnostics"]["evaluations"] <= count
    assert result["diagnostics"]["unique_evaluated"] == len(calls)
    assert result["diagnostics"]["evaluation"]["score"] <= result["diagnostics"]["baseline_score"]


def test_tiny_deadline_has_complete_counted_baseline():
    result = factory(max_seconds=1e-12)(request(horizon=144))
    assert result["diagnostics"]["evaluations"] == 1
    assert result["diagnostics"]["termination_reason"] == "search_time_limit"
    assert len(policies(result)[0]) == 144


def test_saturated_pv_only_batteries_have_valid_modes_and_exact_projection():
    params = conformance_params()[2]
    params.battery_entities[0].initial_kwh = params.battery_entities[0].capacity_kwh
    params.battery_entities[0].charge_curve_kwh = [1, 0.5, 0]
    params.battery_entities[0].discharge_curve_kwh = [0.2, 0.8, 1]
    params.throughput_cost_per_kwh = 0.2
    params.mode_switch_cost = 0.3
    result = factory(max_evaluations=40)(params)
    assert "grid_charge" not in policies(result)[1]
    evaluation = evaluate_schedule(params, policies(result))
    assert result["diagnostics"]["evaluation"]["score"] == list(evaluation.score)
    assert result["projections"]["projected_cost"] == evaluation.tariff_cost
    for index, entity in enumerate(result["entities"]):
        assert [a["level"] for a in entity["schedule"]] == [
            s.levels[index] for s in evaluation.steps
        ]


def test_negative_prices_remain_forecast_only():
    params = request(prices=(-1, 2), usage=(0, 1))
    params.solar_input_kwh[0] = 0.5
    params.grid_export_price_per_kwh[0] = -2
    result = factory(max_evaluations=24)(params)
    assert result["projections"]["projected_cost"] < 0


def test_fixed_seed_fresh_every_cycle_and_population_operators():
    params = request(prices=(0.1, 1, 0.2, 2) * 8, usage=(0.2,) * 32, initial=1)
    before = params.model_dump()
    planner = factory(max_evaluations=96, seed=19)
    first, second = planner(params), planner(params)
    assert first["entities"] == second["entities"]
    assert first["diagnostics"]["evaluation"] == second["diagnostics"]["evaluation"]
    diagnostic = first["diagnostics"]
    assert diagnostic["crossovers"] > 0
    assert diagnostic["mutations"] > 0
    assert diagnostic["generations"] > 0
    assert diagnostic["diversity"] > 1
    assert params.model_dump() == before
    params.battery_entities[0].initial_kwh = 0
    changed = planner(params)
    assert changed["projections"]["projected_cost"] == pytest.approx(
        evaluate_schedule(params, policies(changed)).tariff_cost
    )


def test_small_action_space_and_no_mutation_cannot_hang():
    params = request(prices=(1,) * 4, usage=(0,) * 4, initial=3)
    result = factory(max_evaluations=100000, generations=100000, population_size=100,
                     mutation_rate=0, crossover_rate=0)(params)
    assert result["diagnostics"]["evaluations"] <= 81
    # Seeds plus two bounded fill attempts.
    assert result["diagnostics"]["candidate_attempts"] <= 1605
    assert result["diagnostics"]["termination_reason"] == "diversity_attempt_limit"


def test_empty_batteries_do_not_try_to_fill_population():
    result = factory(max_evaluations=100000, generations=100000)(conformance_params()[0])
    assert result["entities"] == []
    assert result["diagnostics"]["evaluations"] == 1
    assert result["diagnostics"]["termination_reason"] == "no_batteries"



def test_efficiency_tolerance_target_and_observed_reserve_shortfall():
    params = request(prices=(10, 10), usage=(0, 0), initial=0.1,
                     target={"timeslot": 1, "soc_kwh": 2, "mode": "exact", "tolerance_kwh": 0.11})
    params.battery_entities[0].minimum_kwh = 0.2
    params.battery_entities[0].charge_efficiency = 0.9
    result = factory(max_evaluations=12)(params)
    evaluation = result["diagnostics"]["evaluation"]
    assert evaluation["score"][0] == 0
    assert evaluation["target_checks"][0]["soc_kwh"] == pytest.approx(1.9)
    assert evaluation["reserve_shortfalls"] == [{"battery": "house", "initial_shortfall_kwh": 0.1}]


@pytest.mark.parametrize("budget", [1, 16, 48])
def test_first_counted_seed_recovers_reserve_immediately(budget):
    params = request(prices=(10, 9, 8, 1), usage=(0,) * 4)
    params.battery_entities[0].minimum_kwh = 2.0
    result = factory(max_evaluations=budget)(params)
    assert policies(result)[0][:2] == ["grid_charge", "grid_charge"]
    assert result["diagnostics"]["evaluations"] <= budget
    assert result["diagnostics"]["reserve_seed_transitions"] == 2
    assert result["diagnostics"]["seed_transitions"] >= 2
    assert result["diagnostics"]["evaluation"]["reserve_recovery_penalty"] == 5000.0
    assert result["suboptimal_reasons"] == ["battery_min_unmet"]


def test_expired_budget_returns_complete_reserve_recovery():
    params = request(prices=(10, 9, 8, 1), usage=(0,) * 4)
    params.battery_entities[0].minimum_kwh = 1.0
    result = factory(max_seconds=1e-12)(params)
    assert result["diagnostics"]["evaluations"] == 1
    assert policies(result)[0][0] == "grid_charge"
    assert result["diagnostics"]["reserve_seed_transitions"] == 1
    assert len(policies(result)[0]) == 4
    assert not result["suboptimal"]


def test_single_evaluation_respects_pv_only_recovery():
    params = request(prices=(10, 9, 8, 1), usage=(0,) * 4)
    params.battery_entities[0].minimum_kwh = 1.0
    params.battery_entities[0].can_charge_from = 2
    params.solar_input_kwh = [0.0, 1.0, 0.0, 0.0]
    result = factory(max_evaluations=1)(params)
    assert "grid_charge" not in policies(result)[0]
    assert [slot["level"] for slot in result["entities"][0]["schedule"]] == [0.0, 1.0, 1.0, 1.0]
    assert result["diagnostics"]["reserve_seed_transitions"] == 2
    assert result["projections"]["projected_cost"] == 0.0


@pytest.mark.parametrize("preset", ["genetic-fast", "genetic-standard"])
@pytest.mark.parametrize("mode", ["at_least", "exact"])
def test_published_presets_meet_immediate_expensive_target(preset, mode):
    from custom_components.wattplan.optimizer.planners.presets import run_preset

    params = request(prices=(10, 1, 0, 0), usage=(0,) * 4,
                     target={"timeslot": 0, "soc_kwh": 1, "mode": mode})
    result = run_preset(params, preset)
    assert policies(result)[0][0] == "grid_charge"
    assert result["diagnostics"]["evaluation"]["target_checks"][0]["miss_kwh"] == 0.0
    assert not result["suboptimal"]


@pytest.mark.parametrize("registry", [False, True])
def test_comfort_optional_and_full_reporting_via_factory_and_registry(registry):
    from custom_components.wattplan.optimizer.comfort_placement import check_comfort_schedule
    from custom_components.wattplan.optimizer.models import ComfortEntityParams, OptionalEntityParams
    from custom_components.wattplan.optimizer.planners import create_planner
    from custom_components.wattplan.optimizer.planners.comfort import prepare_comfort

    params = request(prices=(0.2,) * 8, usage=(0.5,) * 8, initial=2)
    params.rolling_window_slots = 4
    params.comfort_entities = [ComfortEntityParams(
        name="heater", target_on_slots_per_rolling_window=1,
        max_consecutive_off_slots=3, power_usage_kwh=0.5,
        is_on_now=False, off_streak_slots_now=2, on_history=[True, False, False],
    )]
    params.optional_entities = [OptionalEntityParams(
        name="washer", duration_timeslots=1, energy_kwh=0.5,
        start_before_timeslot=8, options=3,
    )]
    before = params.model_dump()
    planner = create_planner("genetic", {"max_evaluations": 16}) if registry else factory(max_evaluations=16)
    result = planner(params)
    assert params.model_dump() == before
    assert planner.capabilities.comfort
    assert planner.capabilities.optional_loads == "supported"
    assert "rolling_window_slots" not in result["diagnostics"]["ignored_preferences"]
    prepared = prepare_comfort(params)
    comfort = next(entity for entity in result["entities"] if entity["type"] == "comfort")
    enabled = tuple(slot["enabled"] for slot in comfort["schedule"])
    assert not check_comfort_schedule(prepared.inputs[0], enabled)
    assert len(result["optional_entity_options"][0]["options"]) == 3
    assert len(result["projections"]["per_slot"]) == 8
    assert result["projections"]["baseline_cost"] == pytest.approx(0.8)
    for flow, on in zip(result["energy_flows"]["per_slot"], enabled, strict=True):
        assert flow["base_load_kwh"] == 0.5
        assert flow["comfort_load_kwh"] == 0.5 * on
    assert result["execution_time"] >= result["diagnostics"]["reporting"]["reporting_seconds"]
    assert not result["suboptimal"]


def test_exact_discrete_miss_is_reported_without_feasibility_claim():
    params = request(prices=(1,) * 4, usage=(0,) * 4,
                     target={"timeslot": 0, "soc_kwh": 0.4, "mode": "exact"})
    result = factory(max_evaluations=16)(params)
    assert result["suboptimal_reasons"] == ["battery_target_unmet"]
    assert result["diagnostics"]["evaluation"]["constraint_status"] == "observed_plan_violations_not_infeasibility_proof"
