"""Mobility metadata, isolated DC accounting and service-first shared replay."""

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams, normalize_calculation_input
from custom_components.wattplan.optimizer.physics import transition, transition_at
from custom_components.wattplan.optimizer.planners.contract import PlannerCapabilities
from custom_components.wattplan.optimizer.planners.evaluation import build_result, evaluate_schedule
from custom_components.wattplan.optimizer.planners.registry import BUILTINS, create_planner
from tests.optimizer.planner_conformance import battery_params as request


def mobility_request(**fields):
    raw = request(initial=2.0, usage=(0.0,) * 4).model_dump()
    raw["battery_entities"][0].update(fields)
    return OptimizationParams(**raw)


@pytest.mark.parametrize("fields", [
    dict(connected_slots=[1, True, True, True]), dict(connected_slots=[True]),
    dict(external_energy_kwh=[0.0]), dict(external_energy_kwh=[True, 0, 0, 0]),
    dict(external_energy_kwh=[float("nan"), 0, 0, 0]), dict(external_energy_kwh=[-1, 0, 0, 0]),
    dict(departure_requirements=[dict(timeslot=True, minimum_kwh=1)]),
    dict(departure_requirements=[dict(timeslot=4, minimum_kwh=1)]),
    dict(departure_requirements=[dict(timeslot=0, minimum_kwh=float("inf"))]),
    dict(departure_requirements=[dict(timeslot=0, minimum_kwh=4)]),
])
def test_strict_metadata_validation(fields):
    with pytest.raises(ValueError):
        mobility_request(**fields)


@pytest.mark.parametrize("mode", ["self_consume", "preserve", "grid_charge"])
def test_disconnected_battery_has_no_home_exchange(mode):
    params = mobility_request(connected_slots=[False] * 4)
    battery = params.battery_entities[0]
    for usage, pv in ((2.0, 0.0), (0.0, 2.0)):
        step = transition_at([battery], [2.0], [mode], slot=0, usage=usage, pv=pv)
        assert step.levels == [2.0]
        assert step.grid_charge == step.pv_charge == step.discharge == [0.0]
        assert step.grid_import == usage
        assert step.grid_export == pv


def test_disconnected_ev_excluded_from_fleet_allocation():
    house = request(initial=1.0).battery_entities[0]
    ev = mobility_request(connected_slots=[False] * 4).battery_entities[0]
    step = transition_at([ev, house], [2.0, 1.0], ["self_consume"] * 2,
                         slot=0, usage=1.0, pv=0.0)
    assert step.discharge == [0.0, 1.0]
    assert step.levels == [2.0, 0.0]
    charged = transition_at([ev, house], [2.0, 1.0], ["preserve"] * 2,
                           slot=0, usage=0.0, pv=1.0)
    assert charged.pv_charge == [0.0, 1.0]


def test_dc_draw_reports_served_shortfall_and_never_bills_household():
    params = mobility_request(external_energy_kwh=[3.0, 0, 0, 0])
    step = transition_at(params.battery_entities, [2.0], ["preserve"], slot=0, usage=0, pv=0)
    assert step.levels_before_external == [2.0]
    assert step.levels == [0.0]
    assert step.external_energy == [2.0]
    assert step.external_shortfall == [1.0]
    assert step.grid_import == step.grid_export == step.loss == step.charge == 0.0
    result = build_result(params, [["preserve"] * 4])
    assert result["projections"]["mobility_served_kwh"] == 2.0
    assert result["projections"]["mobility_unserved_kwh"] == 1.0
    assert result["projections"]["service_feasible"] is False
    assert result["projections"]["projected_savings_comparable"] is False


def test_departure_before_draw_and_existing_target_after_draw():
    params = mobility_request(external_energy_kwh=[2, 0, 0, 0],
        departure_requirements=[dict(timeslot=0, minimum_kwh=2)])
    score = evaluate_schedule(params, [["preserve"] * 4])
    assert score.departure_violations[0]["soc_kwh"] == 2.0
    assert score.score == (0.0, 0.0, 0.0)
    raw = params.model_dump()
    raw["battery_entities"][0]["target"] = dict(timeslot=0, soc_kwh=1)
    score = evaluate_schedule(OptimizationParams(**raw), [["preserve"] * 4])
    assert score.score == (0.0, 1.0, 0.0)


def test_mobility_service_outranks_cheaper_household_tariff():
    params = mobility_request(departure_requirements=[dict(timeslot=0, minimum_kwh=2)])
    params.usage_kwh[0], params.grid_import_price_per_kwh[0] = 1.0, 20.0
    feasible = evaluate_schedule(params, [["preserve"] * 4])
    cheap = evaluate_schedule(params, [["self_consume"] * 4])
    assert feasible.tariff_cost > cheap.tariff_cost
    assert feasible.score < cheap.score


def test_fractional_home_steps_apply_dc_draw_once_at_boundary():
    params = mobility_request(external_energy_kwh=[1.0, 0, 0, 0])
    first = transition_at(params.battery_entities, [2.0], ["preserve"], slot=0,
        usage=0, pv=0, slot_fraction=0.5, apply_external=False)
    final = transition_at(params.battery_entities, first.levels, ["preserve"], slot=0,
        usage=0, pv=0, slot_fraction=0.5, apply_external=True)
    assert first.levels == [2.0]
    assert final.levels == [1.0]
    assert final.external_energy == [1.0]


def test_inactive_arrays_keep_transition_score_and_result_identical(monkeypatch):
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.reporting.perf_counter", lambda: 0)
    params = request(initial=2.0, usage=(0.0,) * 4)
    inert = mobility_request(connected_slots=[True] * 4, external_energy_kwh=[0] * 4)
    old = transition(params.battery_entities, [2], ["preserve"], usage=0, pv=0)
    assert transition_at(inert.battery_entities, [2], ["preserve"], slot=0, usage=0, pv=0) == old
    assert evaluate_schedule(inert, [["preserve"] * 4]).score == (0, 0)
    assert build_result(params, [["preserve"] * 4]) == build_result(inert, [["preserve"] * 4])
    PlannerCapabilities().validate(inert)


@pytest.mark.parametrize("name", [name for name in BUILTINS if not create_planner(name).capabilities.mobility])
def test_unsupported_planners_reject_active_inputs(name):
    with pytest.raises(ValueError, match="mobility"):
        create_planner(name)(mobility_request(connected_slots=[False] * 4))


def test_internal_conversion_preserves_mobility_metadata():
    params = mobility_request(connected_slots=[False, True, True, True],
        external_energy_kwh=[1, 0, 0, 0], departure_requirements=[dict(timeslot=1, minimum_kwh=2)])
    converted = normalize_calculation_input(params)
    battery = converted.battery_entities[0]
    assert battery.connected_slots == params.battery_entities[0].connected_slots
    assert battery.external_energy_kwh == [1, 0, 0, 0]
    assert battery.departure_requirements[0].minimum_kwh == 2


@pytest.mark.parametrize("draw", [-1.0, float("nan"), float("inf"), True])
def test_physical_wrapper_rejects_unvalidated_invalid_draw(draw):
    params = mobility_request()
    params.battery_entities[0].external_energy_kwh = [draw, 0, 0, 0]
    with pytest.raises(ValueError, match="finite and nonnegative"):
        transition_at(params.battery_entities, [2.0], ["preserve"], slot=0, usage=0, pv=0)
