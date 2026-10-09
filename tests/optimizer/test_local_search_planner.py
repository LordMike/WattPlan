"""Local-search economics, request isolation, physics and exact budget checks."""

from copy import deepcopy

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners import create_planner
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from custom_components.wattplan.optimizer.planners.local_search import factory
from tests.optimizer.planner_conformance import assert_conforms, conformance_params

PATH = "custom_components.wattplan.optimizer.planners.local_search:factory"


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
    import custom_components.wattplan.optimizer.planners.local_search as module

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


def test_duplicate_candidates_cannot_cause_unbounded_restarts():
    result = factory(max_evaluations=200, max_restarts=100000)(request())
    diagnostic = result["diagnostics"]
    assert diagnostic["candidate_attempts"] <= diagnostic["candidate_attempt_limit"]
    assert diagnostic["candidate_attempt_limit"] == 1600
    assert diagnostic["evaluations"] <= 81  # Three actions to four slots.
    assert diagnostic["termination_reason"] == "candidate_attempt_limit"


def test_local_search_import_does_not_load_highs():
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "from custom_components.wattplan.optimizer.planners.local_search import factory; "
            "factory(); import sys; assert 'highspy' not in sys.modules; "
            "assert 'custom_components.wattplan.optimizer.mpc_power_optimizer' not in sys.modules",
        ],
        check=True,
        timeout=30,
    )
