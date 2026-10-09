"""Planner factory isolation, common scoring, and deterministic dataset gates."""

import base64
from copy import deepcopy
import json
import subprocess
import sys

import pytest

from custom_components.wattplan.optimizer.models import (
    OptimizationParams,
    encode_state_blob,
)
from custom_components.wattplan.optimizer.planners import create_planner
from custom_components.wattplan.optimizer.planners.budget import SearchBudget
from custom_components.wattplan.optimizer.planners.contract import PlannerCapabilities
from custom_components.wattplan.optimizer.planners.evaluation import (
    build_result,
    evaluate_schedule,
    target_miss,
)
from custom_components.wattplan.optimizer.planners.registry import resolve_factory
from custom_components.wattplan.optimizer.simulation.dataset import load_dataset
from scripts.generate_simulation_data import main as generate, records_for_day
from tests.optimizer.planner_conformance import assert_conforms, conformance_params


def state_factory(*, marker=1):
    def plan(params):
        result = build_result(
            params,
            [["preserve"] * len(params.usage_kwh) for _ in params.battery_entities],
        )
        result["seen_state"] = params.state
        result["state"] = encode_state_blob({"v": 1, "marker": marker})
        return result

    return plan


@pytest.mark.parametrize("factory", ["highs", "self-consumption"])
@pytest.mark.parametrize("params", conformance_params())
def test_builtins_conform(factory, params):
    assert_conforms(factory, params)


def test_state_is_namespaced_and_reset_on_options_or_implementation_change():
    params = conformance_params()[1]
    path = "tests.optimizer.test_planner_contract:state_factory"
    planner = create_planner(path)
    first = planner(params)
    second = planner(params.model_copy(update={"state": first["state"]}))
    assert second["seen_state"] == encode_state_blob({"v": 1, "marker": 1})
    changed = create_planner(path, {"marker": 2})(
        params.model_copy(update={"state": first["state"]})
    )
    assert changed["seen_state"] is None
    assert changed["diagnostics"]["planner"]["state_reset_reason"]
    foreign = create_planner("self-consumption")(
        params.model_copy(update={"state": first["state"]})
    )
    assert foreign["diagnostics"]["planner"]["state_reset_reason"]
    legacy = planner(params.model_copy(update={"state": encode_state_blob({"v": 1})}))
    assert legacy["seen_state"] is None


def test_highs_accepts_legacy_state_and_aliases_share_identity(monkeypatch):
    import custom_components.wattplan.optimizer as optimizer

    captured = []
    legacy = encode_state_blob({"v": 1, "legacy": True})

    def fake(params):
        captured.append(params.state)
        return {"state": legacy}

    monkeypatch.setattr(optimizer, "optimize", fake)
    params = conformance_params()[1].model_copy(update={"state": legacy})
    first = create_planner("highs")(params)
    create_planner(
        "custom_components.wattplan.optimizer.simulation.adapters:highs_factory"
    )(params.model_copy(update={"state": first["state"]}))
    assert captured == [legacy, legacy]
    envelope = json.loads(base64.urlsafe_b64decode(first["state"]))
    assert envelope["planner_state"]["factory"] == resolve_factory("highs")


@pytest.mark.parametrize("options", [[], 0, False, "bad"])
def test_non_object_options_rejected(options):
    with pytest.raises(ValueError, match="object"):
        create_planner("self-consumption", options)


def test_factory_options_and_capabilities_fail_explicitly():
    with pytest.raises(TypeError, match="unexpected keyword"):
        create_planner("self-consumption", {"unknown": 1})
    params = conformance_params()[1].model_copy(update={"comfort_entities": [object()]})
    with pytest.raises(ValueError, match="comfort"):
        PlannerCapabilities().validate(params)
    result = create_planner("self-consumption")(conformance_params()[2])
    assert any(
        "not optimized" in note for note in result["diagnostics"]["planner"]["notes"]
    )
    assert any(
        "solver_time_limit_seconds" in note
        for note in result["diagnostics"]["planner"]["notes"]
    )


def test_evaluator_hand_calculation_and_target_priority():
    payload = conformance_params()[1].model_dump()
    payload.update(
        grid_import_price_per_kwh=[-1, 1, 3, 2],
        grid_export_price_per_kwh=[0] * 4,
        usage_kwh=[0, 0, 1, 0],
        solar_input_kwh=[0] * 4,
        throughput_cost_per_kwh=0.2,
        mode_switch_cost=0.1,
    )
    payload["battery_entities"][0].update(
        initial_kwh=0,
        minimum_kwh=0,
        charge_efficiency=0.8,
        discharge_efficiency=0.5,
        target={"timeslot": 1, "soc_kwh": 0.8, "mode": "exact"},
    )
    params = OptimizationParams(**payload)
    policies = [["grid_charge", "preserve", "self_consume", "preserve"]]
    score = evaluate_schedule(params, policies)
    assert score.tariff_cost == pytest.approx(-1 + 0.6 * 3)
    assert score.throughput_kwh == pytest.approx(1.4)
    assert score.mode_switches == 4  # idle -> charge -> idle -> discharge -> idle
    assert score.score == pytest.approx((0, 0.8 + 1.4 * 0.2 + 4 * 0.1))
    assert score.steps[-1].levels == [0]
    missed = evaluate_schedule(params, [["preserve"] * 4])
    assert missed.target_miss_kwh == 0.8
    assert score.score < missed.score
    assert (
        build_result(params, policies, evaluation=score)["diagnostics"]["evaluation"][
            "terminal_energy_credit"
        ]
        == 0
    )
    assert target_miss(params.battery_entities[0].target, 0.8 - 1e-8) == 0


def test_evaluator_reports_below_reserve_and_rejects_forbidden_actions():
    params = conformance_params()[2]
    policies = [["self_consume"] * 4 for _ in params.battery_entities]
    score = evaluate_schedule(params, policies)
    assert score.reserve_shortfalls[0]["initial_shortfall_kwh"] == pytest.approx(0.1)
    policies[1][0] = "grid_charge"
    with pytest.raises(ValueError, match="forbidden"):
        evaluate_schedule(params, policies)


def test_search_budget_has_exact_count_and_separate_time_bound(monkeypatch):
    budget = SearchBudget(2)
    assert budget.take() and budget.take() and not budget.take()
    assert budget.diagnostics()["evaluations"] == 2
    assert budget.termination_reason == "evaluation_limit"
    timed = SearchBudget(100, 1)
    monkeypatch.setattr(
        "custom_components.wattplan.optimizer.planners.budget.time.perf_counter",
        lambda: timed._start + 2,
    )
    assert not timed.take()
    assert timed.termination_reason == "search_time_limit"
    for invalid in (0, -1, True, "1", float("nan")):
        with pytest.raises(ValueError):
            SearchBudget(2, invalid)


def test_synthetic_generator_full_day_is_reproducible_and_not_perfect_forecast(
    tmp_path,
):
    from datetime import date

    first = records_for_day(date(2026, 6, 15))
    assert first == records_for_day(date(2026, 6, 15))
    assert len(first) == 192 and all(row["synthetic"] for row in first)
    assert any(
        first[i]["request"]["optimizer_params"]["usage_kwh"][0]
        != first[i + 1]["actuals"][0]["usage_kwh"]
        for i in range(0, 192, 2)
    )
    assert generate([str(tmp_path / "data")]) == 0
    dataset = load_dataset(tmp_path / "data")
    assert dataset.validate_coverage()["slots"] == 96
    assert dataset.range_info()["hours"] == 24
    assert any("synthetic" in warning for warning in dataset.warnings)
    original = deepcopy(dataset.events[0].params)
    assert_conforms("self-consumption", OptimizationParams(**original))
    assert dataset.events[0].params == original


def test_lightweight_planner_import_does_not_load_highs():
    code = "from custom_components.wattplan.optimizer.planners import create_planner; create_planner('self-consumption'); import sys; assert 'highspy' not in sys.modules; assert 'custom_components.wattplan.optimizer.mpc_power_optimizer' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True, timeout=30)
