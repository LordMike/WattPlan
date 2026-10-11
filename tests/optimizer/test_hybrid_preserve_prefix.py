"""End-matched passive-prefix comparisons through original shared physics."""

import pytest

from custom_components.wattplan.optimizer.physics import EPSILON, transition
from custom_components.wattplan.optimizer.planners import hybrid_policy, self_consumption
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from tests.optimizer.test_dynamic_programming_planner import policies, request


def proof_request(*, prices=(0.1, 5.0, 0.0, 0.0), usage=(1.0, 1.0, 0.0, 0.0)):
    params = request(initial=1.0, prices=prices, usage=usage, pv=(0.0, 0.0, 10.0, 0.0))
    params.slot_minutes = 60
    params.battery_entities[0].capacity_kwh = 1.0
    return params


def independent_prefix(params, actions):
    """Replay fixed actions directly, retaining tariff and state at every slot."""
    level = params.battery_entities[0].initial_kwh
    total, rows = 0.0, []
    for slot, action in enumerate(actions):
        step = transition(params.battery_entities, [level], [action],
            usage=params.usage_kwh[slot], pv=params.solar_input_kwh[slot])
        total += (step.grid_import * params.grid_import_price_per_kwh[slot]
                  - step.grid_export * params.grid_export_price_per_kwh[slot])
        level = step.levels[0]
        rows.append((level, total))
    return rows


@pytest.mark.parametrize("value", [0, 1, None, 0.0, "true"])
def test_flag_requires_boolean(value):
    with pytest.raises(ValueError, match="first_preserve_prefix"):
        hybrid_policy.factory(first_preserve_prefix=value)


@pytest.mark.parametrize("params", [proof_request(), request(), request(batteries=2)])
def test_disabled_full_output_parity(params, monkeypatch):
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.shared.perf_counter", lambda: 0.0)
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.reporting.perf_counter", lambda: 0.0)
    from custom_components.wattplan.optimizer.planners.budget import SearchBudget
    diagnostics = SearchBudget.diagnostics
    monkeypatch.setattr(SearchBudget, "diagnostics", lambda self: {**diagnostics(self), "search_seconds": 0.0})
    options = dict(prerefill_guard=True, include_initial_knot=True, energy_intervals=6,
        pv_scenario_spread=1.0, paired_demand_scenario_spread=0.25,
        paired_demand_scenario_slots=12, grid_charge_premium_per_kwh=0.03)
    assert hybrid_policy.factory(**options)(params) == hybrid_policy.factory(**options, first_preserve_prefix=False)(params)


def test_public_end_matched_proof_reports_actual_cost_five_to_point_one():
    params = proof_request()
    snapshot = params.model_dump()
    baseline = hybrid_policy.factory(prerefill_guard=True)(params)
    result = hybrid_policy.factory(prerefill_guard=True, first_preserve_prefix=True,
        grid_charge_premium_per_kwh=10.0, include_initial_knot=True,
        include_discharge_budget_knot=True, first_action_rollout=True)(params)
    assert baseline["projections"]["projected_cost"] == pytest.approx(5.0)
    assert result["projections"]["projected_cost"] == pytest.approx(0.1)
    assert policies(result) == [["preserve", "self_consume", "self_consume", "self_consume"]]
    passive = independent_prefix(params, ["self_consume"] * 4)
    refined = independent_prefix(params, policies(result)[0])
    assert passive[1][0] == refined[1][0] == 0.0
    assert passive[-1][0] == refined[-1][0] == 1.0
    assert passive[-1][1] - refined[-1][1] == pytest.approx(4.9)
    gate = result["diagnostics"]["hybrid_gate"]
    assert gate["selected_policy"] == "first_preserve_then_self_consumption"
    assert gate["first_preserve_prefix_evaluated"] is True
    assert gate["first_preserve_prefix_applied"] is True
    assert gate["first_preserve_prefix_convergence_slot"] == 1
    assert gate["first_preserve_prefix_transitions"] == 4
    assert gate["first_preserve_prefix_hypothetical_tariff_improvement"] == pytest.approx(4.9)
    assert gate["evaluated_policy_count"] == 1
    for feature in ("grid_charge_premium", "initial_knot", "discharge_budget_knot", "first_action_rollout"):
        assert gate[f"{feature}_applied"] is False
    assert params.model_dump() == snapshot
    replay = evaluate_schedule(params, policies(result))
    assert sum(sum(step.grid_charge) for step in replay.steps) == 0.0
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(replay.score)


@pytest.mark.parametrize("prices", [(5.0, 0.1, 0.0, 0.0), (0.1, 0.1, 0.0, 0.0),
    (0.1, 0.1 + EPSILON / 2, 0.0, 0.0)])
def test_worse_tie_and_sub_epsilon_improvement_keep_passive(prices):
    params = proof_request(prices=prices)
    result = hybrid_policy.factory(first_preserve_prefix=True)(params)
    assert policies(result) == [["self_consume"] * 4]
    assert result["diagnostics"]["hybrid_gate"]["first_preserve_prefix_omission_reason"] == "no_strict_tariff_improvement"


def test_identical_first_physics_skips_after_one_transition():
    params = proof_request(usage=(0.0, 1.0, 0.0, 0.0))
    result = hybrid_policy.factory(first_preserve_prefix=True)(params)
    gate = result["diagnostics"]["hybrid_gate"]
    assert gate["first_preserve_prefix_transitions"] == 1
    assert gate["first_preserve_prefix_applied"] is False
    assert gate["first_preserve_prefix_convergence_slot"] is None


@pytest.mark.parametrize("minutes,cap", [(60, 24), (120, 12), (15, 96)])
def test_no_convergence_within_relative_24_hour_cap_ignores_later_gain(minutes, cap):
    size = cap + 2
    usage, pv, prices = [0.0] * size, [0.0] * size, [0.0] * size
    usage[0], usage[cap], pv[2], prices[0], prices[cap] = 1.0, 1.0, 10.0, 0.1, 5.0
    params = request(initial=1.0, prices=prices, usage=usage, pv=pv)
    params.slot_minutes = minutes
    params.battery_entities[0].capacity_kwh = 1.0
    params.battery_entities[0].charge_curve_kwh = [0.0]
    result = hybrid_policy.factory(first_preserve_prefix=True)(params)
    gate = result["diagnostics"]["hybrid_gate"]
    assert gate["first_preserve_prefix_bound_slots"] == cap
    assert gate["first_preserve_prefix_bound_hours"] == 24.0
    assert gate["first_preserve_prefix_convergence_slot"] is None
    assert gate["first_preserve_prefix_transitions"] == 2 * cap
    assert gate["first_preserve_prefix_omission_reason"] == "no_state_convergence_within_bound"
    assert policies(result)[0][0] == "self_consume"
    passive, refined = independent_prefix(params, ["self_consume"] * size), independent_prefix(params, ["preserve"] + ["self_consume"] * (size - 1))
    assert passive[cap][0] == refined[cap][0]
    assert passive[-1][1] > refined[-1][1]


@pytest.mark.parametrize("export", [0.0, 0.3, 20.0])
def test_losses_rates_variable_curves_and_exports_match_direct_full_replay(export):
    params = proof_request(usage=(0.25, 0.5, 0.0, 0.0))
    battery = params.battery_entities[0]
    battery.minimum_kwh = 0.2
    battery.charge_efficiency = battery.discharge_efficiency = 0.8
    battery.charge_curve_kwh = [5.0, 3.0, 1.0]
    battery.discharge_curve_kwh = [0.5, 0.25, 0.5]
    params.grid_export_price_per_kwh = [export] * 4
    result = hybrid_policy.factory(first_preserve_prefix=True)(params)
    gate = result["diagnostics"]["hybrid_gate"]
    passive = independent_prefix(params, ["self_consume"] * 4)
    refined = independent_prefix(params, ["preserve"] + ["self_consume"] * 3)
    convergence = next((slot for slot in range(4) if passive[slot][0] == refined[slot][0]), None)
    accepted = convergence is not None and passive[convergence][1] - refined[convergence][1] > EPSILON
    assert gate["first_preserve_prefix_applied"] is accepted
    assert gate["first_preserve_prefix_convergence_slot"] == convergence
    actual = evaluate_schedule(params, policies(result))
    assert result["projections"]["projected_cost"] == pytest.approx(actual.tariff_cost)
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(actual.score)


@pytest.mark.parametrize("reason", ["target", "reserve", "fleet", "preference", "negative", "gate", "pv_permission", "prerefill"])
def test_existing_value_eligibility_omits_prefix(reason):
    params = proof_request()
    if reason == "target":
        params = proof_request()
        params.battery_entities[0].target = type(params.battery_entities[0]).TargetParams(timeslot=0, soc_kwh=1.0)
    elif reason == "reserve":
        params.battery_entities[0].minimum_kwh, params.battery_entities[0].initial_kwh = 0.5, 0.0
    elif reason == "fleet":
        params.battery_entities.append(params.battery_entities[0].model_copy(update={"name": "other"}))
    elif reason == "preference":
        params.mode_switch_cost = 0.01
    elif reason == "negative":
        params.grid_import_price_per_kwh[0] = -0.1
    elif reason == "gate":
        params.solar_input_kwh = [0.0] * 4
    elif reason == "pv_permission":
        params.battery_entities[0].can_charge_from = 1
    else:
        params.battery_entities[0].initial_kwh = 0.0
    result = hybrid_policy.factory(prerefill_guard=True, first_preserve_prefix=True)(params)
    ordinary = hybrid_policy.factory(prerefill_guard=True)(params)
    gate = result["diagnostics"]["hybrid_gate"]
    assert gate["selected_policy"] == "value_policy"
    assert gate["first_preserve_prefix_evaluated"] is False
    assert gate["first_preserve_prefix_transitions"] == 0
    assert gate["first_preserve_prefix_omission_reason"]
    assert result["entities"] == ordinary["entities"]


def test_one_comfort_preparation_and_one_final_result_builder(monkeypatch):
    from custom_components.wattplan.optimizer.planners import comfort
    from tests.optimizer.test_planner_comfort import request as comfort_request
    calls = dict(comfort=0, build=0, baseline=0)
    prepare, build = comfort.prepare_comfort, hybrid_policy.build_result
    def count_prepare(params):
        calls["comfort"] += 1
        return prepare(params)
    def count_build(*args, **kwargs):
        calls["build"] += 1
        return build(*args, **kwargs)
    def forbidden_baseline(*args, **kwargs):
        calls["baseline"] += 1
        raise AssertionError("accepted prefix must not complete the baseline planner")
    monkeypatch.setattr(comfort, "prepare_comfort", count_prepare)
    monkeypatch.setattr(hybrid_policy, "build_result", count_build)
    monkeypatch.setattr(self_consumption, "build_result", forbidden_baseline)
    params = proof_request()
    params.rolling_window_slots = 4
    params.comfort_entities = [comfort_request(power_usage_kwh=0.01).comfort_entities[0]]
    snapshot = params.model_dump()
    result = hybrid_policy.factory(first_preserve_prefix=True)(params)
    assert result["diagnostics"]["hybrid_gate"]["first_preserve_prefix_applied"] is True
    assert sum(flow["comfort_load_kwh"] for flow in result["energy_flows"]["per_slot"]) > 0.0
    assert params.model_dump() == snapshot
    assert calls == dict(comfort=1, build=1, baseline=0)


def test_near_equal_levels_do_not_certify_common_future():
    params = proof_request(usage=(0.5, 1.0 - 1e-12, 0.0, 0.0))
    params.battery_entities[0].charge_curve_kwh = [0.0]
    passive = independent_prefix(params, ["self_consume"] * 4)
    refined = independent_prefix(params, ["preserve"] + ["self_consume"] * 3)
    assert 0.0 < abs(passive[1][0] - refined[1][0]) < EPSILON
    result = hybrid_policy.factory(first_preserve_prefix=True)(params)
    gate = result["diagnostics"]["hybrid_gate"]
    assert gate["first_preserve_prefix_convergence_slot"] is None
    assert gate["first_preserve_prefix_applied"] is False
    assert gate["first_preserve_prefix_transitions"] == 8


def test_near_equality_continues_until_later_exact_merge():
    params = proof_request(usage=(0.5, 1.0 - 1e-12, 0.0, 0.0))
    result = hybrid_policy.factory(first_preserve_prefix=True)(params)
    gate = result["diagnostics"]["hybrid_gate"]
    assert gate["first_preserve_prefix_convergence_slot"] == 2
    assert gate["first_preserve_prefix_applied"] is True
