"""Joint seed construction, matched baseline retention and target fallback."""

import pytest

from custom_components.wattplan.optimizer.planners import mobility_hybrid_seeds, mobility_policy
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from custom_components.wattplan.optimizer.planners.registry import create_planner
from custom_components.wattplan.optimizer.planners.presets import run_preset
from tests.optimizer.test_mobility_policy import request, policies, independent_score


@pytest.mark.parametrize("params", [
    request(connected=(True, True, False), draws=(0., 0., 1.)),
    request(prices=(.2, 5., .1, 5.), connected=(True, False, True, False),
        draws=(0., 1., 0., 1.), departures=[dict(timeslot=0, minimum_kwh=1.), dict(timeslot=2, minimum_kwh=1.)], house=True),
    request(prices=(5., .1, 4., 0.), usage=(1., 0., 1., 0.), pv=(0., 1., 0., 0.), house=True),
    request(initial=1., departures=[], draws=[0.]*4, house=True),
])
def test_four_full_joint_candidates_preserve_matched_baseline_and_inputs(params):
    before = params.model_dump()
    baseline = mobility_policy.baseline_factory()(params)
    result = mobility_hybrid_seeds.factory()(params)
    evaluation = evaluate_schedule(params, policies(result))
    baseline_score = evaluate_schedule(params, policies(baseline)).score
    diagnostics = result["diagnostics"]
    assert diagnostics["evaluations"] == 4
    assert len(diagnostics["candidate_scores"]) == 4
    assert diagnostics["candidate_scores"][0]["score"] == pytest.approx(baseline_score)
    assert evaluation.score <= baseline_score
    assert evaluation.score == min(tuple(row["score"]) for row in diagnostics["candidate_scores"])
    physical_score = independent_score(params, policies(result))
    assert (physical_score if evaluation.mobility_active else physical_score[1:]) == pytest.approx(evaluation.score)
    assert result["projections"]["projected_cost"] == pytest.approx(evaluation.tariff_cost)
    assert params.model_dump() == before


def test_joint_pv_competition_changes_mobile_actions_not_spliced_levels(monkeypatch):
    params = request(prices=(1., 1., 1., 1.), pv=(1., 0., 0., 0.), house=True,
        departures=[dict(timeslot=1, minimum_kwh=1.)])
    calls = []
    real = mobility_hybrid_seeds.transition_at
    def observed(batteries, levels, actions, **kwargs):
        step = real(batteries, levels, actions, **kwargs)
        calls.append((kwargs["slot"], tuple(actions), step))
        return step
    monkeypatch.setattr(mobility_hybrid_seeds, "transition_at", observed)
    result = mobility_hybrid_seeds.factory()(params)
    assert len(calls) == 4 * len(params.usage_kwh)
    for slot, actions, step in calls:
        assert sum(step.pv_charge) <= params.solar_input_kwh[slot]
    # House gets first PV allocation; EV's first action still buys its own service.
    assert calls[0][2].pv_charge == [1., 0.]
    assert calls[0][2].grid_charge == [0., 1.]
    assert result["projections"]["service_feasible"] is True
    calls.clear()
    reversed_params = params.model_copy(update={"battery_entities": list(reversed(params.battery_entities))})
    reversed_result = mobility_hybrid_seeds.factory()(reversed_params)
    assert calls[0][2].pv_charge == [1., 0.]
    assert calls[0][2].grid_charge == [0., 0.]
    reversed_baseline = mobility_policy.baseline_factory()(reversed_params)
    assert reversed_result["diagnostics"]["candidate_scores"][0]["score"] == pytest.approx(
        evaluate_schedule(reversed_params, policies(reversed_baseline)).score)


def test_declared_all_true_mobile_remains_preserved_without_trip():
    params = request(initial=2., usage=(1., 1., 0., 0.), departures=[], draws=[0.]*4)
    result = mobility_hybrid_seeds.factory()(params)
    assert policies(result) == [["preserve"] * 4]
    assert result["projections"]["projected_cost"] == 5.1


def test_40_kwh_trip_service_and_disconnection_are_checked():
    params = request(prices=(1., .8, .6, .4, .2, .1, 2., 2.),
        connected=[True]*6 + [False]*2, draws=[0.]*6 + [40., 0.],
        departures=[], house=True)
    payload = params.model_dump()
    payload["battery_entities"][-1].update(capacity_kwh=80., charge_curve_kwh=[7.4],
        discharge_curve_kwh=[7.4], departure_requirements=[dict(timeslot=5, minimum_kwh=40.)])
    params = type(params)(**payload)
    result = mobility_hybrid_seeds.factory()(params)
    evaluation = evaluate_schedule(params, policies(result))
    assert evaluation.mobility_miss_kwh == 0
    assert sum(step.external_energy[-1] for step in evaluation.steps if step.external_energy) == 40.
    for step in evaluation.steps[6:]:
        assert step.grid_charge[-1] == step.pv_charge[-1] == step.discharge[-1] == 0


@pytest.mark.parametrize("target_mode", ["at_least", "at_most", "exact"])
def test_mobile_ordinary_target_uses_disclosed_whole_fleet_fallback(target_mode):
    params = request(prices=(.1, 3., 1., 0.), departures=[], house=True)
    payload = params.model_dump()
    payload["battery_entities"][-1]["target"] = dict(timeslot=1, soc_kwh=1., mode=target_mode)
    payload["battery_entities"][-1]["charge_curve_kwh"] = [1., 1., .5]
    payload["battery_entities"][-1]["discharge_curve_kwh"] = [.2, .5, 1.]
    params = type(params)(**payload)
    result = mobility_hybrid_seeds.factory()(params)
    assert result["diagnostics"]["mobility_hybrid_fallback"] == "ordinary_mobile_target"
    assert result["diagnostics"]["max_expansions"] == 1500
    assert evaluate_schedule(params, policies(result)).target_miss_kwh == 0


def test_comfort_prepared_once_and_raw_stationary_request_is_metadata_free(monkeypatch):
    from custom_components.wattplan.optimizer.planners import comfort
    from tests.optimizer.test_live_planner_parity import request as live_request
    from types import SimpleNamespace
    original = comfort.prepare_comfort
    prepared = []
    def prepare(params):
        prepared.append(params)
        return original(params)
    monkeypatch.setattr(comfort, "prepare_comfort", prepare)
    raw_calls = []
    original_hybrid = mobility_hybrid_seeds.hybrid_factory
    def hybrid(**options):
        raw = original_hybrid(**options).__wrapped__
        def recorded(params):
            raw_calls.append(params)
            return raw(params)
        return SimpleNamespace(__wrapped__=recorded)
    monkeypatch.setattr(mobility_hybrid_seeds, "hybrid_factory", hybrid)
    params = live_request(optional=False)
    vehicle = request(departures=[]).battery_entities[0].model_copy(update={
        "connected_slots": [True]*8, "external_energy_kwh": [0.]*8})
    params = params.model_copy(update={"battery_entities": [*params.battery_entities, vehicle]})
    result = create_planner("mobility-hybrid-seeds")(params)
    assert len(prepared) == 1
    assert result["diagnostics"]["stationary_hybrid"] is not None
    assert len(raw_calls) == 1
    assert raw_calls[0].state is None
    assert len(raw_calls[0].battery_entities) == 1
    assert raw_calls[0].battery_entities[0].connected_slots is None
    assert raw_calls[0].comfort_entities == []
    assert raw_calls[0].usage_kwh == pytest.approx([
        flow["base_load_kwh"] + flow["comfort_load_kwh"]
        for flow in result["energy_flows"]["per_slot"]])
    assert sum(raw_calls[0].usage_kwh) > sum(params.usage_kwh)
    assert run_preset(request(), "mobility-hybrid-seeds-4-grid4")["diagnostics"]["evaluations"] == 4


@pytest.mark.parametrize("value", [True, 0, -1, 1.5])
def test_grid_validation_inherited_from_hybrid(value):
    with pytest.raises(ValueError):
        mobility_hybrid_seeds.factory(energy_intervals=value)
