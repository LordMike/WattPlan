"""Connection, trip energy and service reporting across mobility-capable planners."""

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners import create_planner
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from custom_components.wattplan.optimizer.planners.registry import BUILTINS
from tests.optimizer.planner_conformance import assert_conforms, battery_policies, economic_params


MOBILITY_PLANNERS = [name for name in BUILTINS if create_planner(name).capabilities.mobility]


@pytest.mark.parametrize("name", MOBILITY_PLANNERS)
@pytest.mark.parametrize("scenario", ["connected", "pv-only", "impossible"])
def test_service_and_trip_energy_are_reported_from_executable_plan(name, scenario):
    payload = economic_params(prices=(1, 0.1, 2, 1), usage=(0,) * 4).model_dump()
    payload["battery_entities"][0].update(
        connected_slots=[True, True, False, False],
        external_energy_kwh=[0, 0, 1, 0],
        departure_requirements=[{"timeslot": 1, "minimum_kwh": 1}],
    )
    if scenario == "pv-only":
        payload["battery_entities"][0]["can_charge_from"] = 2
        payload["solar_input_kwh"] = [0.5, 0.5, 0, 0]
    elif scenario == "impossible":
        payload["battery_entities"][0]["connected_slots"] = [False] * 4
    params = OptimizationParams(**payload)
    result = assert_conforms(name, params)
    replay = evaluate_schedule(params, battery_policies(result))
    projection = result["projections"]
    assert projection["service_feasible"] is (scenario != "impossible")
    assert projection["mobility_served_kwh"] == pytest.approx(0 if scenario == "impossible" else 1)
    assert projection["mobility_unserved_kwh"] == pytest.approx(1 if scenario == "impossible" else 0)
    assert projection["departure_checks"][0]["miss_kwh"] == pytest.approx(1 if scenario == "impossible" else 0)
    for step in replay.steps[2:]:
        assert step.grid_charge == step.pv_charge == step.discharge == [0]
    if scenario in ("pv-only", "impossible"):
        # Trip withdrawals do not become a household electricity bill.
        assert projection["projected_cost"] == pytest.approx(0)


@pytest.mark.parametrize("name", MOBILITY_PLANNERS)
def test_two_vehicles_cannot_share_the_same_pv_energy(name):
    payload = economic_params(prices=(1,) * 4, usage=(0,) * 4, solar=(1, 0, 0, 0)).model_dump()
    battery = payload["battery_entities"][0]
    battery.update(can_charge_from=2, connected_slots=[True] * 4,
                   departure_requirements=[{"timeslot": 0, "minimum_kwh": 1}])
    payload["battery_entities"] = [dict(battery, name="first"), dict(battery, name="second")]
    params = OptimizationParams(**payload)
    result = assert_conforms(name, params)
    assert sum(entity["schedule"][0]["level"] for entity in result["entities"]) <= 1 + 1e-6
    assert sum(row["miss_kwh"] for row in result["projections"]["departure_checks"]) >= 1 - 1e-6
    assert result["projections"]["service_feasible"] is False


@pytest.mark.parametrize("name,targets,scope,recovery", [
    ("mobility-baseline", "observed", "none", False),
    ("mobility-tariff-seeds", "observed", "mobility", False),
    ("mobility-hybrid-seeds", "optimized", "household", True),
    ("mobility-fleet-beam", "optimized", "household", True),
])
def test_mobility_planners_declare_documented_capabilities(name, targets, scope, recovery):
    capabilities = create_planner(name).capabilities
    assert capabilities.mobility
    assert (capabilities.targets, capabilities.economic_scope, capabilities.reserve_recovery) == (
        targets, scope, recovery,
    )
