"""DP oracle comparisons, unrounded trajectories and deterministic bounded work."""

from itertools import product

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners.dynamic_programming import (
    _State, _advance, _key, factory,
)
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from tests.optimizer.planner_conformance import assert_conforms, conformance_params

PATH = "custom_components.wattplan.optimizer.planners.dynamic_programming:factory"


def request(*, prices=(0.1, 1.0, 0.0, 0.0), usage=(0.0, 1.0, 0.0, 0.0),
            initial=0.0, target=None, pv=None, ingress=3, batteries=1):
    return OptimizationParams(
        plan_start="2026-06-15T00:00:00+00:00", slot_minutes=15,
        grid_import_price_per_kwh=list(prices), grid_export_price_per_kwh=[0.0] * len(prices),
        usage_kwh=list(usage), solar_input_kwh=list(pv or [0.0] * len(prices)),
        comfort_entities=[], battery_entities=[dict(
            name=f"battery-{i}", initial_kwh=initial, minimum_kwh=0.0, capacity_kwh=3.0,
            charge_curve_kwh=[1.0], discharge_curve_kwh=[1.0], can_charge_from=ingress,
            target=target,
        ) for i in range(batteries)],
    )


def policies(result):
    return [[a["state"] for a in b["schedule"]] for b in result["entities"]
            if b["type"] == "battery"]


def exhaustive_score(params):
    choices = [tuple(product(("self_consume", "preserve", "grid_charge")
                             if b.can_charge_from & 1 else ("self_consume", "preserve"),
                             repeat=len(params.usage_kwh))) for b in params.battery_entities]
    return min(evaluate_schedule(params, candidate).score for candidate in product(*choices))


@pytest.mark.parametrize("params", [
    request(),
    request(prices=(-0.3, 0.2, 1.0, 0.0), usage=(0.0, 0.0, 1.0, 0.0)),
    request(initial=1.0, batteries=2, usage=(1.0, 1.0, 1.0, 1.0)),
    request(ingress=2, pv=(1.0, 0.0, 1.0, 0.0)),
    request(target=dict(timeslot=1, soc_kwh=2.0, mode="at_least")),
    request(initial=2.0, usage=(1.0, 1.0, 0.0, 0.0),
            target=dict(timeslot=1, soc_kwh=0.0, mode="at_most")),
    request(target=dict(timeslot=1, soc_kwh=1.0, mode="exact")),
    request(target=dict(timeslot=1, soc_kwh=3.0, mode="exact")),
])
def test_fine_unpruned_dp_matches_tiny_exhaustive_oracle(params):
    result = factory(soc_step_kwh=1e-9, max_states=10000, max_expansions=100000)(params)
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(exhaustive_score(params))
    assert result["diagnostics"]["frontier_cells_pruned"] == 0
    assert result["diagnostics"]["tail_transitions"] == 0
    assert result["diagnostics"]["termination_reason"] == "search_complete"


def test_physical_behavior_history_is_part_of_bellman_cell():
    assert _key(_State((1.0,), (1,)), 0.5) != _key(_State((1.0,), (0,)), 0.5)
    params = request(initial=1.0, prices=(0.2, 0.4, 0.8, -0.1), usage=(0.2, 0.4, 1.0, 0.0))
    params.mode_switch_cost = 0.37
    params.throughput_cost_per_kwh = 0.13
    params.action_deadband_kwh = 0.4
    result = factory(soc_step_kwh=1e-9, max_states=10000)(params)
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(exhaustive_score(params))


def test_coarse_cells_never_round_physical_soc_or_cost():
    params = conformance_params()[2]
    params.battery_entities[0].charge_curve_kwh = [0.83, 0.57, 0.12]
    params.battery_entities[0].discharge_curve_kwh = [0.12, 0.41, 0.79]
    params.battery_entities[0].target.mode = "exact"
    params.battery_entities[0].target.tolerance_kwh = 0.15
    result = assert_conforms(PATH, params, {"soc_step_kwh": 10.0})
    evaluation = evaluate_schedule(params, policies(result))
    assert result["diagnostics"]["merged_candidates"] > 0
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(evaluation.score)
    for index, entity in enumerate(result["entities"]):
        assert [a["level"] for a in entity["schedule"]] == pytest.approx(
            [step.levels[index] for step in evaluation.steps])
    assert result["diagnostics"]["evaluation"]["reserve_shortfalls"][0]["initial_shortfall_kwh"] == pytest.approx(0.1)


def test_infeasible_targets_are_reported_without_manufacturing_energy():
    params = request(target=dict(timeslot=0, soc_kwh=3.0, mode="exact"))
    result = factory()(params)
    assert result["entities"][0]["schedule"][0]["level"] == pytest.approx(1.0)
    assert result["diagnostics"]["evaluation"]["score"][0] == pytest.approx(2.0)


def test_negative_prices_and_pv_only_ingress():
    result = factory()(request(prices=(-0.3, 0.0, 0.0, 0.0), usage=(0.0,) * 4))
    assert result["projections"]["projected_cost"] == pytest.approx(-0.3)
    assert result["diagnostics"]["evaluation"]["terminal_energy_credit"] == 0.0
    pv = factory()(request(ingress=2, pv=(1.0, 0.0, 0.0, 0.0)))
    assert "grid_charge" not in policies(pv)[0]
    assert pv["projections"]["projected_cost"] == pytest.approx(0.0)


def test_tiny_budget_completes_large_joint_state_space_repeatably():
    params = request(prices=(0.1,) * 672, usage=(0.5,) * 672, batteries=20)
    plan = factory(soc_step_kwh=1e-9, max_states=10**9, max_expansions=1,
                   max_joint_actions=10**9)
    before = params.model_dump()
    first, second = plan(params), plan(params)
    assert first["entities"] == second["entities"]
    assert first["projections"] == second["projections"]
    assert params.model_dump() == before
    assert first["state"] is None
    assert all(len(b["schedule"]) == 672 for b in first["entities"])
    diag = first["diagnostics"]
    assert diag["expansions"] == diag["evaluations"] == diag["joint_actions_considered"] == 1
    assert diag["tail_transitions"] == 671
    assert diag["replay_transitions"] == 672
    assert diag["joint_actions_pruned_per_state"] == 3 ** 20 - 1
    assert diag["termination_reason"] == "evaluation_limit"
    assert diag["peak_candidate_cells"] <= 1


def test_frontier_and_work_limits_are_explicit():
    result = factory(max_states=1, max_expansions=7)(request())
    diag = result["diagnostics"]
    assert diag["frontier_cells_pruned"] > 0
    assert diag["expansions"] == 7
    assert diag["searched_slots"] == 3
    assert diag["tail_transitions"] == 1
    assert diag["approximate"] is True


def test_cooperative_deadline_preserves_full_horizon(monkeypatch):
    from custom_components.wattplan.optimizer.planners import budget
    monkeypatch.setattr(budget.time, "perf_counter", lambda: 1e100)
    result = factory(max_seconds=1.0)(request())
    assert result["diagnostics"]["expansions"] == 0
    assert result["diagnostics"]["tail_transitions"] == 4
    assert result["diagnostics"]["termination_reason"] == "search_time_limit"
    assert len(result["entities"][0]["schedule"]) == 4


@pytest.mark.parametrize("option,value", [
    ("soc_step_kwh", 0), ("soc_step_kwh", True), ("soc_step_kwh", float("nan")),
    ("soc_step_kwh", float("inf")), ("max_states", 0), ("max_states", True),
    ("max_expansions", -1), ("max_joint_actions", 1.5),
    ("max_seconds", 0), ("max_seconds", float("inf")),
])
def test_invalid_options(option, value):
    with pytest.raises(ValueError):
        factory(**{option: value})


def test_strict_keyword_only_options():
    with pytest.raises(TypeError):
        factory(0.5)
    with pytest.raises(TypeError):
        factory(unknown=1)


def test_extremely_fine_cell_width_does_not_overflow():
    key = _key(_State((1.0,), (0,)), 5e-324)
    assert isinstance(key[0][0], int)
    assert key[0][0] > 10 ** 300


def test_tiny_budget_retains_last_slot_target_and_reports_tail():
    params = request(target=dict(timeslot=3, soc_kwh=3.0, mode="at_least"))
    result = factory(max_expansions=1)(params)
    assert result["diagnostics"]["tail_transitions"] == 3
    assert result["diagnostics"]["evaluation"]["target_checks"][0]["timeslot"] == 3
    assert result["diagnostics"]["evaluation"]["score"][0] == 0.0


def test_prefix_reserve_penalty_matches_full_evaluation_without_billing_penalty():
    params = request(initial=0.1, usage=(0.0,) * 4)
    params.battery_entities[0].minimum_kwh = 1.0
    params.battery_entities[0].charge_curve_kwh = [0.2]
    state = _State((0.1,), (0,))
    for slot in range(4):
        state = _advance(params, state, ("grid_charge",), slot)
    evaluation = evaluate_schedule(params, [["grid_charge"] * 4])
    assert state.score == pytest.approx(evaluation.score)
    assert evaluation.objective - evaluation.tariff_cost == pytest.approx(8000.0)
    actual = factory()(params)
    assert actual["projections"]["projected_cost"] < 1.0
    assert actual["fitness"] > 1000.0
    assert "battery_min_unmet" in actual["problems"]


@pytest.mark.parametrize("budget", [1, 10000])
def test_reserve_recovery_starts_now_despite_expensive_power_and_tiny_budget(budget):
    params = request(prices=(10.0, 0.0, 0.0, 0.0), usage=(0.0,) * 4)
    params.battery_entities[0].minimum_kwh = 1.0
    params.battery_entities[0].charge_curve_kwh = [0.4]
    actual = factory(max_expansions=budget)(params)
    assert policies(actual)[0][:3] == ["grid_charge"] * 3
    assert [slot["level"] for slot in actual["entities"][0]["schedule"]][:3] == pytest.approx([0.4, 0.8, 1.2])
    assert actual["diagnostics"]["evaluation"]["score"] == pytest.approx(
        evaluate_schedule(params, policies(actual)).score
    )


def test_recovery_never_invents_grid_ingress_for_pv_only_battery():
    params = request(ingress=2, usage=(0.0,) * 4, pv=(0.4,) * 4)
    params.battery_entities[0].minimum_kwh = 1.0
    actual = factory(max_expansions=1)(params)
    assert "grid_charge" not in policies(actual)[0]
    assert [slot["level"] for slot in actual["entities"][0]["schedule"]] == pytest.approx([0.4, 0.8, 1.2, 1.6])
    assert actual["projections"]["projected_cost"] == 0.0
