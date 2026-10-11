"""Threshold gating and independent shared-physics conformance."""
import pytest

from custom_components.wattplan.optimizer.planners.price_reserve import factory
from tests.optimizer.planner_conformance import assert_conforms, conformance_params, economic_params, battery_policies


@pytest.mark.parametrize("options", [
    {"charge_quantile": -0.1}, {"discharge_quantile": 1.1}, {"pv_reserve": True},
    {"max_fill": float("nan")}, {"margin": -1}, {"margin": float("inf")},
    {"reserve_slots": 0}, {"reserve_slots": 97}, {"reserve_slots": True},
    {"demand_reserve": 1}, {"grid_enabled": 0},
    {"future_price_margin": -1}, {"future_price_margin": float("nan")},
])
def test_invalid_options(options):
    with pytest.raises(ValueError):
        factory(**options)


@pytest.mark.parametrize("params", conformance_params())
def test_complete_physical_reports_without_input_mutation(params):
    assert_conforms("custom_components.wattplan.optimizer.planners.price_reserve:factory", params)


def test_losses_reject_weak_grid_arbitrage():
    params = economic_params(prices=(1, 1.1), usage=(0, 1))
    params.battery_entities[0].charge_efficiency = 0.9
    params.battery_entities[0].discharge_efficiency = 0.9
    result = factory(charge_quantile=0, discharge_quantile=1, margin=0)(params)
    assert "grid_charge" not in battery_policies(result)[0]


def test_profitable_trade_and_solar_headroom_gate():
    params = economic_params(prices=(0.1, 2, 2, 2), usage=(0, 1, 0, 0))
    result = factory(charge_quantile=0, discharge_quantile=1, margin=0)(params)
    assert battery_policies(result)[0][0] == "grid_charge"
    params.solar_input_kwh = [0, 4, 0, 0]
    result = factory(charge_quantile=0, discharge_quantile=1, margin=0, pv_reserve=1)(params)
    assert battery_policies(result)[0][0] != "grid_charge"


@pytest.mark.parametrize("mode, initial, goal, usage", [
    ("at_least", 0, 2, (0, 0, 0, 0)), ("exact", 0, 1, (0, 0, 0, 0)),
    ("at_most", 2, 0, (1, 1, 0, 0)), ("exact", 2, 0, (1, 1, 0, 0)),
])
def test_feasible_targets_precede_tariffs(mode, initial, goal, usage):
    params = economic_params(prices=(10, 10, -100, -100), usage=usage, initial=initial,
                             target={"timeslot": 1, "soc_kwh": goal, "mode": mode})
    result = factory()(params)
    assert result["diagnostics"]["evaluation"]["score"][0] == 0

@pytest.mark.parametrize("initial, expected", [(1, "preserve"), (2, "self_consume")])
def test_demand_reserve_protects_only_needed_higher_price_energy(initial, expected):
    params = economic_params(prices=(1, 2, 2, 2), usage=(1, 1, 0, 0), initial=initial)
    result = factory(discharge_quantile=1, demand_reserve=True, future_price_margin=0,
                     grid_enabled=False)(params)
    assert battery_policies(result)[0][0] == expected


def test_demand_reserve_stops_at_meaningful_permitted_pv_refill():
    params = economic_params(prices=(1, 2, 2, 2), usage=(1, 0, 1, 0),
                             solar=(0, 1, 0, 0), initial=1)
    options = dict(discharge_quantile=1, demand_reserve=True, future_price_margin=0, grid_enabled=False)
    assert battery_policies(factory(**options)(params))[0][0] == "self_consume"
    params.battery_entities[0].can_charge_from = 1
    assert battery_policies(factory(**options)(params))[0][0] == "preserve"
    params.battery_entities[0].can_charge_from = 3
    params.solar_input_kwh[1] = 0.01
    assert battery_policies(factory(**options)(params))[0][0] == "preserve"


def test_demand_reserve_scan_cap_and_future_margin():
    params = economic_params(prices=(1, 1, 2, 2), usage=(1, 0, 1, 0), initial=1)
    options = dict(discharge_quantile=1, demand_reserve=True, future_price_margin=0, grid_enabled=False)
    assert battery_policies(factory(reserve_slots=1, **options)(params))[0][0] == "self_consume"
    assert battery_policies(factory(reserve_slots=2, **options)(params))[0][0] == "preserve"
    options["future_price_margin"] = 2
    assert battery_policies(factory(reserve_slots=2, **options)(params))[0][0] == "self_consume"


@pytest.mark.parametrize("params", conformance_params())
def test_demand_reserve_physical_reports_and_mandatory_recovery(params):
    result = assert_conforms("custom_components.wattplan.optimizer.planners.price_reserve:factory", params,
                             dict(demand_reserve=True, grid_enabled=False))
    if params.battery_entities and params.battery_entities[0].initial_kwh < params.battery_entities[0].minimum_kwh:
        assert battery_policies(result)[0][0] == "grid_charge"
