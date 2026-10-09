"""Cross-entropy economics, request isolation, physics and exact budget checks."""

from copy import deepcopy

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners import create_planner
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from custom_components.wattplan.optimizer.planners.cross_entropy import factory
from tests.optimizer.planner_conformance import assert_conforms, conformance_params

PATH = "custom_components.wattplan.optimizer.planners.cross_entropy:factory"


def request(*, prices=(0.1, 1.0), usage=(0, 1), solar=None, initial=0, target=None):
    extra = max(0, 4 - len(prices))
    solar = list(solar or [0] * len(prices)) + [0] * extra
    prices = list(prices) + [max(prices)] * extra
    usage = list(usage) + [0] * extra
    return OptimizationParams(
        plan_start="2026-06-15T00:00:00+00:00",
        slot_minutes=15,
        grid_import_price_per_kwh=list(prices),
        grid_export_price_per_kwh=[0] * len(prices),
        usage_kwh=list(usage),
        solar_input_kwh=solar,
        comfort_entities=[],
        battery_entities=[
            {
                "name": "house",
                "initial_kwh": initial,
                "minimum_kwh": 0,
                "capacity_kwh": 3,
                "charge_curve_kwh": [1],
                "discharge_curve_kwh": [1],
                "can_charge_from": 3,
                "target": target,
            }
        ],
    )


def policies(result):
    return [
        [action["state"] for action in entity["schedule"]]
        for entity in result["entities"]
        if entity["type"] == "battery"
    ]


@pytest.mark.parametrize("params", conformance_params())
def test_shared_conformance(params):
    result = assert_conforms(PATH, params, {"max_evaluations": 60})
    assert result["state"] is None
    assert result["diagnostics"]["evaluation"]["terminal_energy_credit"] == 0
    assert result["diagnostics"]["evaluations"] <= 60


@pytest.mark.parametrize(
    "options",
    [
        {"max_evaluations": 0},
        {"max_evaluations": True},
        {"max_seconds": 0},
        {"max_seconds": float("nan")},
        {"seed": True},
        {"seed": 1.5},
        {"max_restarts": -1},
        {"max_restarts": True},
        {"unknown": 1},
    ],
)
def test_invalid_options(options):
    with pytest.raises((ValueError, TypeError)):
        factory(**options)


def test_cheap_charge_then_expensive_discharge():
    params = request()
    result = factory(max_evaluations=60)(params)
    assert result["projections"]["projected_cost"] == pytest.approx(0.1)
    assert policies(result)[0][:2] == ["grid_charge", "self_consume"]
    assert result["projections"]["final_soc_kwh"] == {"house": 0}


def test_negative_import_and_export_prices_use_shared_forecast_cost():
    params = request(prices=(-1, 2), usage=(0, 1), solar=(0.5, 0))
    params.grid_export_price_per_kwh = [-2, -0.5, 0, 0]
    result = factory(max_evaluations=80)(params)
    evaluated = evaluate_schedule(params, policies(result))
    assert evaluated.tariff_cost < 0
    assert result["projections"]["projected_cost"] == evaluated.tariff_cost


@pytest.mark.parametrize(
    "mode, initial, goal, usage",
    [
        ("at_least", 0, 2, (0, 0)),
        ("exact", 0, 1, (0, 0)),
        ("at_most", 2, 0, (1, 1)),
        ("exact", 2, 0, (1, 1)),
    ],
)
def test_targets_take_priority_over_tariff_cost(mode, initial, goal, usage):
    params = request(
        prices=(10, 10),
        usage=usage,
        initial=initial,
        target={"timeslot": 1, "soc_kwh": goal, "mode": mode},
    )
    result = factory(max_evaluations=100)(params)
    assert result["diagnostics"]["evaluation"]["score"][0] == 0


def test_unreachable_target_reports_miss_and_deadline_is_inclusive():
    params = request(
        prices=(100, -100),
        usage=(0, 0),
        target={"timeslot": 0, "soc_kwh": 2, "mode": "exact"},
    )
    result = factory(max_evaluations=60)(params)
    check = result["diagnostics"]["evaluation"]["target_checks"][0]
    assert check["soc_kwh"] == 1
    assert check["miss_kwh"] == 1


@pytest.mark.parametrize("count", [1, 2, 7, 40])
def test_no_hidden_evaluations_and_build_result_reuses_evaluation(monkeypatch, count):
    import custom_components.wattplan.optimizer.planners.cross_entropy as module

    calls = []
    original = module.evaluate_schedule

    def recorded(params, candidate):
        calls.append(deepcopy(candidate))
        return original(params, candidate)

    monkeypatch.setattr(module, "evaluate_schedule", recorded)
    result = factory(max_evaluations=count)(request())
    assert len(calls) == result["diagnostics"]["evaluations"] <= count
    assert (
        result["diagnostics"]["evaluation"]["score"]
        <= result["diagnostics"]["baseline_score"]
    )


def test_tiny_time_budget_still_counts_one_complete_baseline():
    result = factory(max_evaluations=100, max_seconds=1e-12)(request())
    assert result["diagnostics"]["evaluations"] == 1
    assert result["diagnostics"]["termination_reason"] == "search_time_limit"
    assert len(result["entities"][0]["schedule"]) == 4


def test_long_horizon_many_batteries_has_work_cap():
    params = request(prices=(0.1,) * 672, usage=(0,) * 672)
    payload = params.model_dump()
    payload["battery_entities"] = [
        {**payload["battery_entities"][0], "name": f"battery-{index}"}
        for index in range(16)
    ]
    result = factory(max_evaluations=10000)(OptimizationParams(**payload))
    assert result["diagnostics"]["effective_max_evaluations"] == 18
    assert result["diagnostics"]["evaluations"] <= 18
    assert all(len(entity["schedule"]) == 672 for entity in result["entities"])


def test_determinism_input_isolation_and_changed_soc_forecast_target():
    params = conformance_params()[2]
    before = params.model_dump(mode="json")
    planner = create_planner(PATH, {"max_evaluations": 120, "seed": 11})
    first = planner(params)
    second = planner(params)
    assert first["entities"] == second["entities"]
    assert first["diagnostics"]["evaluation"] == second["diagnostics"]["evaluation"]
    assert params.model_dump(mode="json") == before
    changed = request(
        prices=(5, -3),
        usage=(1, 0),
        initial=2,
        target={"timeslot": 0, "soc_kwh": 1, "mode": "exact"},
    )
    after = planner(changed)
    assert after["diagnostics"]["evaluation"]["score"][0] == 0
    assert (
        after["projections"]["projected_cost"]
        == evaluate_schedule(changed, policies(after)).tariff_cost
    )
    assert any(
        "lookahead_slots" in note for note in after["diagnostics"]["planner"]["notes"]
    )


def test_preferences_and_nonconstant_curves_preserve_shared_objective():
    params = conformance_params()[1]
    params.battery_entities[0].charge_curve_kwh = [1, 0.5, 0.2]
    params.battery_entities[0].discharge_curve_kwh = [0.2, 0.8, 1]
    params.throughput_cost_per_kwh = 2
    params.mode_switch_cost = 0.7
    result = factory(max_evaluations=80)(params)
    evaluated = evaluate_schedule(params, policies(result))
    assert result["diagnostics"]["evaluation"]["score"] == list(evaluated.score)
    assert evaluated.score <= evaluate_schedule(params, [["self_consume"] * 4]).score


def test_collapsed_distribution_cannot_cause_unbounded_sampling():
    result = factory(
        max_evaluations=200, guidance=1.0, probability_floor=1e-12
    )(request())
    diagnostic = result["diagnostics"]
    assert diagnostic["candidate_attempts"] <= diagnostic["candidate_attempt_limit"]
    assert diagnostic["candidate_attempt_limit"] == 1600
    assert diagnostic["evaluations"] <= 81  # Three actions to four slots.
    assert diagnostic["termination_reason"] == "candidate_attempt_limit"


def test_cross_entropy_import_does_not_load_highs():
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "from custom_components.wattplan.optimizer.planners.cross_entropy import factory; "
            "factory(); import sys; assert 'highspy' not in sys.modules; "
            "assert 'custom_components.wattplan.optimizer.mpc_power_optimizer' not in sys.modules",
        ],
        check=True,
        timeout=30,
    )


def test_elite_update_is_smoothed_masked_and_normalized():
    from custom_components.wattplan.optimizer.planners.cross_entropy import _update

    distributions = [[[0.7, 0.3, 0.0]]]
    elite = [((0, 0), [["preserve"]])]
    _update(distributions, elite, smoothing=0.5, floor=0.1, allowed=[(0, 1)])
    assert distributions[0][0] == pytest.approx([0.38, 0.62, 0])
    assert sum(distributions[0][0]) == pytest.approx(1)


def test_cem_samples_and_updates_multiple_generations():
    params = request(
        prices=(0.1, 1, 0.2, 2, 0.3, 3), usage=(0, 1, 0, 1, 0, 1)
    )
    result = factory(
        max_evaluations=100, population_size=4, guidance=0.65, block_slots=1
    )(params)
    assert result["diagnostics"]["generations"] >= 2
    assert result["diagnostics"]["candidate_attempts"] > 0


@pytest.mark.parametrize("options", [
    {"population_size": True}, {"population_size": 1}, {"block_slots": 0},
    {"block_slots": 673}, {"elite_fraction": 0}, {"elite_fraction": float("nan")},
    {"smoothing": 0}, {"probability_floor": 0.34}, {"guidance": 0},
    {"guidance": True}, {"probability_floor": float("inf")},
])
def test_invalid_cem_options(options):
    with pytest.raises((ValueError, TypeError)):
        factory(**options)


def test_rounding_fallback_never_emits_forbidden_grid_action():
    from custom_components.wattplan.optimizer.planners.cross_entropy import _categorical

    assert _categorical([0.5, 0.4999999999999998, 0], 0.9999999999999999, (0, 1)) == 1


def test_floor_applies_to_one_hot_initial_guidance():
    params = request(
        prices=(0.1, 1, 0.2, 2, 0.3, 3), usage=(0, 1, 0, 1, 0, 1)
    )
    result = factory(
        max_evaluations=100, population_size=4, guidance=1,
        probability_floor=1 / 3, block_slots=1,
    )(params)
    assert result["diagnostics"]["generations"] >= 2


def test_all_reported_levels_equal_replayed_levels_exactly():
    params = conformance_params()[2]
    result = factory(max_evaluations=60)(params)
    evaluated = evaluate_schedule(params, policies(result))
    for index, entity in enumerate(result["entities"]):
        assert [action["level"] for action in entity["schedule"]] == [
            step.levels[index] for step in evaluated.steps
        ]
    assert result["projections"]["projected_cost"] == evaluated.tariff_cost


@pytest.mark.parametrize("count", [1, 48])
@pytest.mark.parametrize("mode,initial,goal,usage", [
    ("at_least", 0, 1, (0, 0, 0, 0)),
    ("exact", 0, 1, (0, 0, 0, 0)),
    ("at_most", 1, 0, (1, 0, 0, 0)),
    ("exact", 1, 0, (1, 0, 0, 0)),
    ("at_least", 1, 0.5, (1, 0, 0, 0)),
])
def test_target_seed_handles_earliest_expensive_deadline(count, mode, initial, goal, usage):
    params = request(prices=(10, 0.01, 0.01, 0.01), usage=usage, initial=initial,
                     target={"timeslot": 0, "soc_kwh": goal, "mode": mode})
    actual = factory(max_evaluations=count, seed=0)(params)
    assert actual["diagnostics"]["evaluation"]["score"][0] == 0
    assert actual["diagnostics"]["evaluations"] <= count
    assert actual["diagnostics"]["seed_candidates"][0].startswith("target-")


@pytest.mark.parametrize("count", [1, 48])
def test_reserve_recovers_immediately_under_tiny_and_selected_budgets(count):
    params = request(prices=(10, 0.01, 0.01, 0.01), usage=(0,) * 4)
    battery = params.battery_entities[0]
    battery.minimum_kwh = 1.0
    battery.charge_curve_kwh = [0.4]
    actual = factory(max_evaluations=count, seed=0)(params)
    assert policies(actual)[0][:3] == ["grid_charge"] * 3
    assert [slot["level"] for slot in actual["entities"][0]["schedule"]][:3] == pytest.approx([0.4, 0.8, 1.2])
    assert actual["projections"]["projected_cost"] < 5
    assert actual["fitness"] > 1000
    assert "battery_min_unmet" in actual["problems"]
    assert actual["diagnostics"]["evaluation"]["score"] == list(evaluate_schedule(params, policies(actual)).score)


def test_pv_only_reserve_recovery_respects_mask_and_real_energy():
    params = request(prices=(10,) * 4, usage=(0,) * 4, solar=(0.4,) * 4)
    battery = params.battery_entities[0]
    battery.minimum_kwh = 1.0
    battery.can_charge_from = 2
    battery.charge_curve_kwh = [0.4]
    actual = factory(max_evaluations=1)(params)
    assert "grid_charge" not in policies(actual)[0]
    assert [slot["level"] for slot in actual["entities"][0]["schedule"]] == pytest.approx([0.4, 0.8, 1.2, 1.6])
    assert actual["projections"]["projected_cost"] == 0


def test_direct_and_registered_cem_support_comfort_optional_and_ha_reporting():
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
    direct = factory(max_evaluations=48, seed=0)(params)
    registered = create_planner("cross-entropy", {"max_evaluations": 48, "seed": 0})(params)
    assert params.model_dump() == before
    assert direct["entities"] == registered["entities"]
    assert direct["projections"] == registered["projections"]
    assert direct["optional_entity_options"][0]["options"]
    assert len(direct["energy_flows"]["per_slot"]) == 8
    assert direct["execution_time"] > 0
    assert not direct["suboptimal"]
    replay = evaluate_schedule(prepare_comfort(params).params, policies(direct))
    assert direct["projections"]["projected_cost"] == pytest.approx(replay.tariff_cost)
    assert factory().capabilities.comfort
    assert factory().capabilities.optional_loads == "supported"
