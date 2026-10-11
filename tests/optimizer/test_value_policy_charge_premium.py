"""Grid charging risk premium changes search economics, never physical reporting."""

from itertools import product

import pytest

from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from custom_components.wattplan.optimizer.planners.hybrid_policy import factory as hybrid_factory
from custom_components.wattplan.optimizer.planners.value_policy import factory
from tests.optimizer.test_dynamic_programming_planner import policies, request


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
@pytest.mark.parametrize("premium", [-0.01, True, False, float("nan"), float("inf"), "0.1", None])
def test_invalid_premium_is_rejected(planner, premium):
    with pytest.raises(ValueError, match="grid_charge_premium_per_kwh"):
        planner(grid_charge_premium_per_kwh=premium)


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
@pytest.mark.parametrize("spread", [0.0, 1.0])
def test_explicit_zero_preserves_entire_default_result(planner, spread, monkeypatch):
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.shared.perf_counter", lambda: 0.0)
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.reporting.perf_counter", lambda: 0.0)
    params = request(initial=1.25, pv=(0.25, 0.5, 0.0, 0.25))
    options = dict(energy_intervals=6, pv_scenario_spread=spread)
    assert planner(**options)(params) == planner(**options, grid_charge_premium_per_kwh=0.0)(params)


def test_premium_can_reject_marginal_grid_trade():
    params = request(prices=(0.1, 0.12, 0.0, 0.0))
    ordinary = factory(energy_intervals=3)(params)
    cautious = factory(energy_intervals=3, grid_charge_premium_per_kwh=0.03)(params)
    assert policies(ordinary)[0][0] == "grid_charge"
    assert policies(cautious)[0][0] == "self_consume"
    assert cautious["projections"]["projected_cost"] == pytest.approx(0.12)


@pytest.mark.parametrize("params", [request(), request(initial=1.0),
    request(pv=(0.5, 0.0, 0.0, 0.0)),
    request(prices=(-0.3, 0.2, 1.0, 0.0)),
    request(target=dict(timeslot=0, soc_kwh=1.0, mode="at_least"))])
def test_aligned_deterministic_dispatch_matches_premium_exhaustive_oracle(params):
    premium = 0.3

    def score(actions):
        replay = evaluate_schedule(params, [actions])
        return (replay.target_miss_kwh, replay.objective + premium *
                sum(sum(step.grid_charge) for step in replay.steps))

    oracle = min(score(actions) for actions in
                 product(("self_consume", "preserve", "grid_charge"), repeat=4))
    result = factory(energy_intervals=6, grid_charge_premium_per_kwh=premium)(params)
    assert score(policies(result)[0]) == pytest.approx(oracle)


def test_scenarios_average_incremental_branch_grid_charge_and_preserve_reporting():
    params = request(prices=(0.1, 0.0, 0.0, 0.0), usage=(0.5, 0.0, 0.0, 0.0),
                     pv=(0.5, 0.0, 0.0, 0.0),
                     target=dict(timeslot=0, soc_kwh=1.0, mode="at_least"))
    before = params.model_dump()
    result = factory(energy_intervals=6, pv_scenario_spread=1.0,
                     grid_charge_premium_per_kwh=0.2)(params)
    # Low PV: household import .5 + battery import 1. High PV: PV charges
    # .5 and grid charges .5. Expected tariff cost .1, premium .2 * .75.
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx((0.0, 0.25))
    assert policies(result)[0][0] == "grid_charge"
    replay = evaluate_schedule(params, policies(result))
    assert result["projections"]["projected_cost"] == pytest.approx(0.1)
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(replay.score)
    assert [entry["level"] for entry in result["entities"][0]["schedule"]] == pytest.approx(
        [step.levels[0] for step in replay.steps])
    assert params.model_dump() == before


@pytest.mark.parametrize("spread", [0.0, 1.0])
def test_household_import_and_pv_only_charging_never_receive_premium(spread):
    params = request(ingress=2, prices=(0.4, 1.0, 0.0, 0.0),
                     usage=(1.0, 1.0, 0.0, 0.0), pv=(0.5, 0.0, 0.0, 0.0))
    ordinary = factory(energy_intervals=6, pv_scenario_spread=spread)(params)
    cautious = factory(energy_intervals=6, pv_scenario_spread=spread,
                       grid_charge_premium_per_kwh=100.0)(params)
    assert ordinary["entities"] == cautious["entities"]
    assert ordinary["projections"] == cautious["projections"]
    if spread:
        assert ordinary["diagnostics"]["initial_value_policy_expected_score"] == cautious["diagnostics"]["initial_value_policy_expected_score"]


def test_target_priority_survives_large_premium():
    params = request(target=dict(timeslot=0, soc_kwh=1.0, mode="at_least"))
    result = factory(energy_intervals=3, grid_charge_premium_per_kwh=100.0)(params)
    assert policies(result)[0][0] == "grid_charge"
    assert result["diagnostics"]["evaluation"]["score"][0] == 0.0
    assert result["projections"]["projected_cost"] == pytest.approx(0.1)


def test_fleet_fallback_discloses_premium_not_applied():
    params = request(batteries=2)
    ordinary = factory()(params)
    cautious = factory(grid_charge_premium_per_kwh=0.1)(params)
    assert ordinary["entities"] == cautious["entities"]
    assert ordinary["projections"] == cautious["projections"]
    assert cautious["diagnostics"]["value_policy_fallback"] == "bounded_joint_state_dp"
    assert cautious["diagnostics"]["grid_charge_premium_applied"] is False


def test_target_repair_discloses_premium_not_applied():
    params = request(initial=2.1, prices=(1.2, 0.3, 1.2, 0.3),
                     usage=(0.0, 0.0, 1.1, 1.1), pv=(0.0, 0.2, 0.0, 0.2),
                     target=dict(timeslot=3, soc_kwh=1.1, tolerance_kwh=0.02, mode="exact"))
    result = factory(energy_intervals=4, grid_charge_premium_per_kwh=0.01)(params)
    assert result["diagnostics"]["target_repair"]["improved"] is True
    assert result["diagnostics"]["target_repair"]["grid_charge_premium_applied"] is False
    replay = evaluate_schedule(params, policies(result))
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(replay.score)


def test_hybrid_value_branch_forwards_premium():
    params = request(prices=(0.1, 0.12, 0.0, 0.0))
    options = dict(energy_intervals=6, pv_scenario_spread=1.0, grid_charge_premium_per_kwh=0.03)
    direct = factory(**options)(params)
    hybrid = hybrid_factory(**options)(params)
    assert hybrid["entities"] == direct["entities"]
    assert hybrid["projections"] == direct["projections"]
    assert hybrid["diagnostics"]["hybrid_gate"]["grid_charge_premium_applied"] is True


def test_hybrid_passive_branch_discloses_unused_premium():
    params = request(pv=(0.0, 0.0, 10.0, 0.0))
    ordinary = hybrid_factory()(params)
    cautious = hybrid_factory(grid_charge_premium_per_kwh=0.1)(params)
    assert cautious["diagnostics"]["hybrid_gate"]["selected_policy"] == "self_consumption"
    assert cautious["diagnostics"]["hybrid_gate"]["grid_charge_premium_applied"] is False
    assert cautious["entities"] == ordinary["entities"]
    assert cautious["projections"] == ordinary["projections"]
