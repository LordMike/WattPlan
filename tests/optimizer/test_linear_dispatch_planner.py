"""LP loss accounting, deterministic rounding and physical fallback contracts."""
import pytest

from custom_components.wattplan.optimizer.planners.linear_dispatch import factory
from tests.optimizer.planner_conformance import assert_conforms, conformance_params, economic_params, battery_policies


@pytest.mark.parametrize("options", [
    {"max_slots": 1}, {"max_slots": True}, {"max_slots": 97},
    {"round_fraction": -1}, {"round_fraction": float("nan")},
    {"pv_scale": 2}, {"terminal_value_fraction": True},
    {"solver_seconds": 0}, {"solver_seconds": float("inf")},
])
def test_invalid_options(options):
    with pytest.raises(ValueError):
        factory(**options)


@pytest.mark.parametrize("params", conformance_params())
def test_complete_physical_output_and_explicit_fallback(params):
    result = assert_conforms("custom_components.wattplan.optimizer.planners.linear_dispatch:factory", params)
    assert result["diagnostics"]["integer_variables"] == 0
    if len(params.battery_entities) != 1:
        assert result["diagnostics"]["fallback_reason"] == "requires_single_battery"


def test_profitable_grid_charge_and_loss_rejection():
    params = economic_params(prices=(.1, 1), usage=(0, 1))
    result = factory()(params)
    assert battery_policies(result)[0][0] == "grid_charge"
    assert result["diagnostics"]["lp_solved_slots"] == 4
    assert result["projections"]["projected_cost"] == pytest.approx(.1)
    params.grid_import_price_per_kwh = [1, 1.1, 1.1, 1.1]
    params.battery_entities[0].charge_efficiency = .9
    params.battery_entities[0].discharge_efficiency = .9
    assert "grid_charge" not in battery_policies(factory()(params))[0]


def test_negative_import_cannot_create_a_charge_discharge_cycle():
    params = economic_params(prices=(-1, 2, 2, 2), usage=(1, 1, 0, 0), initial=1)
    result = factory()(params)
    assert battery_policies(result)[0][0] != "self_consume"
    assert result["projections"]["projected_cost"] == pytest.approx(-2)


def test_pv_only_replay_and_search_scaling_never_mutate_request():
    params = economic_params(prices=(.1, 1, 2, 2), usage=(0, 1, 1, 0), solar=(2, 0, 0, 0))
    params.battery_entities[0].can_charge_from = 2
    result = assert_conforms("custom_components.wattplan.optimizer.planners.linear_dispatch:factory", params,
                             dict(pv_scale=.5, terminal_value_fraction=.25))
    assert "grid_charge" not in battery_policies(result)[0]


@pytest.mark.parametrize("mode, initial, goal, usage", [
    ("at_least", 0, 2, (0, 0, 0, 0)), ("exact", 0, 1, (0, 0, 0, 0)),
    ("at_most", 2, 0, (1, 1, 0, 0)),
])
def test_feasible_target_fallback_precedes_prices(mode, initial, goal, usage):
    params = economic_params(prices=(10, 10, -100, -100), usage=usage, initial=initial,
                             target={"timeslot": 1, "soc_kwh": goal, "mode": mode})
    result = factory()(params)
    assert result["diagnostics"]["fallback_reason"] == "physical_target_preparation"
    assert result["diagnostics"]["evaluation"]["score"][0] == 0


def test_reusing_solver_has_no_previous_request_policy_state():
    planner = factory()
    params = economic_params()
    first = planner(params)
    planner(economic_params(prices=(10, 10), usage=(1, 1), initial=2))
    second = planner(params)
    assert battery_policies(first) == battery_policies(second)
    assert first["projections"] == second["projections"]

@pytest.mark.parametrize("params", conformance_params())
def test_marginal_dispatch_conforms(params):
    assert_conforms("custom_components.wattplan.optimizer.planners.linear_dispatch:factory", params,
                    dict(dispatch_mode="marginal"))


def test_energy_dual_sign_matches_finite_difference_initial_energy():
    import highspy
    from custom_components.wattplan.optimizer.planners.linear_dispatch import _solve_relaxation
    params = economic_params(prices=(1, 2, 2, 2), usage=(0, 1, 0, 0), initial=.4)
    battery = params.battery_entities[0]
    battery.can_charge_from = 2
    battery.discharge_efficiency = .8
    solver = highspy.Highs()
    solver.setOptionValue("output_flag", False)
    first = _solve_relaxation(params, solver, 4, 1, 0)
    delta = 1e-4
    shifted = params.model_copy(deep=True)
    shifted.battery_entities[0].initial_kwh += delta
    second = _solve_relaxation(shifted, solver, 4, 1, 0)
    derivative = (second["objective"] - first["objective"]) / delta
    assert first["energy_duals"][0] == pytest.approx(-1.6)
    assert derivative == pytest.approx(first["energy_duals"][0])


def test_marginal_action_matches_one_step_future_cost_fixture():
    from custom_components.wattplan.optimizer.physics import transition
    params = economic_params(prices=(.2, 2, 2, 2), usage=(1, 1, 0, 0), initial=.5)
    params.battery_entities[0].can_charge_from = 2
    result = factory(dispatch_mode="marginal")(params)
    # Each unit held can replace next-slot imports at price2. Hand-enumerate
    # exact physical actions using that known slope, independent of the LP.
    scores = {}
    for action in ("self_consume", "preserve"):
        step = transition(params.battery_entities, [.5], [action], usage=1, pv=0)
        scores[action] = step.grid_import * .2 - 2 * step.levels[0]
    expected = min(scores, key=scores.get)
    assert expected == "preserve"
    assert battery_policies(result)[0][0] == expected
    assert result["projections"]["projected_cost"] == pytest.approx(1.2)


def test_factory_and_non_native_fallback_do_not_import_highspy():
    import subprocess
    import sys
    code = '''import sys
from custom_components.wattplan.optimizer.planners.linear_dispatch import factory
from tests.optimizer.planner_conformance import economic_params
planner = factory(dispatch_mode="marginal")
assert "highspy" not in sys.modules
params = economic_params(target={"timeslot":1,"soc_kwh":1})
planner(params)
assert "highspy" not in sys.modules
'''
    subprocess.run([sys.executable, "-c", code], check=True)


def test_solver_deadline_is_per_request_after_cumulative_native_work():
    # Each solve has a small finite budget. Total native solver work across
    # requests must exceed that budget without causing later requests to expire.
    prices = tuple(.1 if slot % 4 == 0 else 2 for slot in range(96))
    usage = tuple(0 if slot % 4 == 0 else 1 for slot in range(96))
    params = economic_params(prices=prices, usage=usage)
    limit = .01
    planner = factory(max_slots=96, solver_seconds=limit, dispatch_mode="marginal")
    total_runtime = 0
    successes = successes_after_budget = 0
    for _ in range(120):
        result = planner(params)
        diagnostics = result["diagnostics"]
        if diagnostics["fallback_reason"] is None:
            assert diagnostics["lp_solved_slots"] == 96
            successes += 1
            if total_runtime > limit:
                successes_after_budget += 1
        else:
            # CPU contention can expire an individual request. It must not
            # permanently exhaust the planner's future requests, as a reused
            # native cumulative clock did.
            assert diagnostics["lp_model_status"] == "HighsModelStatus.kTimeLimit"
        total_runtime += diagnostics["lp_solver_runtime"]
    assert total_runtime > limit
    assert successes >= 60
    assert successes_after_budget >= 30


def test_lp_coexists_with_previously_initialized_default_highs_scheduler():
    import subprocess
    import sys
    code = '''import highspy
prior = highspy.Highs()
prior.setOptionValue("output_flag", False)
prior.addVar(0, 1)
prior.changeColCost(0, -1)
assert prior.run() == highspy.HighsStatus.kOk
from custom_components.wattplan.optimizer.planners.linear_dispatch import factory
from tests.optimizer.planner_conformance import economic_params, battery_policies
result = factory()(economic_params())
assert result["diagnostics"]["fallback_reason"] is None
assert result["diagnostics"]["lp_run_status"] == "HighsStatus.kOk"
assert battery_policies(result)[0][0] == "grid_charge"
'''
    subprocess.run([sys.executable, "-c", code], check=True)


def test_partial_pv_allows_only_remaining_grid_charge_power():
    params = economic_params(prices=(-1, 2), usage=(0, 1), solar=(.5, 0))
    params.grid_export_price_per_kwh = [-2, -.5, 0, 0]
    result = factory()(params)
    assert battery_policies(result)[0][0] == "grid_charge"
    assert result["projections"]["projected_cost"] == pytest.approx(-.5)
    # Larger surplus consumes the whole charging limit; grid import cannot
    # coexist with the remaining PV export. PV ingress permission matters too.
    params.solar_input_kwh[0] = 2
    result = assert_conforms("custom_components.wattplan.optimizer.planners.linear_dispatch:factory", params)
    assert battery_policies(result)[0][0] != "grid_charge"
    params.solar_input_kwh[0] = .5
    params.battery_entities[0].can_charge_from = 1
    result = assert_conforms("custom_components.wattplan.optimizer.planners.linear_dispatch:factory", params)
    assert battery_policies(result)[0][0] != "grid_charge"


def test_surplus_exceeding_remaining_capacity_keeps_valid_bounds_and_physics():
    params = economic_params(prices=(-1, 2), usage=(0, 1), solar=(.5, 0), initial=2.9)
    params.grid_export_price_per_kwh = [2, 0, 0, 0]
    result = assert_conforms("custom_components.wattplan.optimizer.planners.linear_dispatch:factory", params)
    assert result["diagnostics"]["lp_model_status"] == "HighsModelStatus.kOptimal"
    from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
    step = evaluate_schedule(params, battery_policies(result)).steps[0]
    assert step.grid_export > 0
    assert step.grid_import == 0
