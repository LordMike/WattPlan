"""Public request/result guarantees for every planner, independent of search."""

from copy import deepcopy

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners import create_planner
from custom_components.wattplan.optimizer.planners.registry import BUILTINS
from tests.optimizer.planner_conformance import (
    assert_conforms, assert_public_result, economic_params,
)


@pytest.mark.parametrize("name", BUILTINS)
@pytest.mark.parametrize("ingress", [0, 1, 2])
def test_sources_curves_and_reserve_do_not_create_energy(name, ingress):
    params = economic_params(
        prices=(-0.2, 0.1, 0.8, 0.4), usage=(0.3, 0.3, 1.0, 0.4),
        solar=(0, 1.5, 0, 0.2), initial=1,
    )
    battery = params.battery_entities[0]
    battery.minimum_kwh = 0.2
    battery.can_charge_from = ingress
    battery.charge_curve_kwh = [1, 0.5, 0.2]
    battery.discharge_curve_kwh = [0.2, 0.5, 1]
    battery.charge_efficiency = 0.9
    battery.discharge_efficiency = 0.8
    result = assert_conforms(name, params)
    levels = [1, *[point["level"] for point in result["entities"][0]["schedule"]]]
    assert min(levels) >= 0.2 - 1e-6
    if ingress == 0:
        assert all(after <= before + 1e-6 for before, after in zip(levels, levels[1:]))
    if not ingress & 1:
        assert all(flow["grid_charge_kwh"] == pytest.approx(0)
                   for flow in result["energy_flows"]["per_slot"])


@pytest.mark.parametrize("name", BUILTINS)
def test_no_equipment_and_zero_baseline_have_complete_finite_reports(name):
    params = economic_params(prices=(0,) * 4, usage=(0,) * 4)
    params.battery_entities = []
    params.slot_minutes = 60
    result = assert_conforms(name, params)
    assert result["entities"] == []
    assert not result["suboptimal"]
    for key in ("baseline_cost", "projected_cost", "projected_savings_cost", "projected_savings_pct"):
        assert result["projections"][key] == 0


@pytest.mark.parametrize("name", BUILTINS)
def test_optional_recommendations_are_advisory_and_use_signed_tariffs(name):
    params = economic_params(prices=(-1, -1, 1, 1), usage=(0,) * 4)
    params.battery_entities = []
    baseline = create_planner(name)(params)
    payload = params.model_dump()
    payload["optional_entities"] = [{
        "name": "washer", "duration_timeslots": 2, "energy_kwh": [0.5, 1.5],
        "start_before_timeslot": 4, "options": 2,
    }]
    result = assert_conforms(name, OptimizationParams(**payload))
    assert result["entities"] == baseline["entities"]
    assert result["projections"] == baseline["projections"]
    assert result["energy_flows"] == baseline["energy_flows"]
    options = result["optional_entity_options"][0]["options"]
    assert [option["start_timeslot"] for option in options] == [0, 2]
    assert [option["incremental_cost"] for option in options] == pytest.approx([-2, 2])


@pytest.mark.parametrize("corruption", [
    "missing_field", "over_capacity", "wrong_cost",
])
def test_public_assertions_reject_malformed_or_inconsistent_results(corruption):
    params = economic_params()
    result = deepcopy(create_planner("self-consumption")(params))
    if corruption == "missing_field":
        del result["fitness"]
    elif corruption == "over_capacity":
        result["entities"][0]["schedule"][0]["level"] = 4
    elif corruption == "wrong_cost":
        result["projections"]["projected_cost"] += 1
    with pytest.raises((AssertionError, KeyError, ValueError)):
        assert_public_result(params, result)
