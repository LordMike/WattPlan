"""Forecast-only gating chooses one policy while retaining constrained features."""
import pytest

from custom_components.wattplan.optimizer.planners.hybrid_policy import _prerefill_opportunity, factory
from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners.value_policy import factory as value_factory
from tests.optimizer.planner_conformance import assert_conforms, conformance_params, economic_params


@pytest.mark.parametrize("ratio", [0, -1, True, float("nan"), float("inf")])
def test_invalid_ratio(ratio):
    with pytest.raises(ValueError):
        factory(surplus_ratio=ratio)


@pytest.mark.parametrize("params", conformance_params())
def test_shared_contract(params):
    assert_conforms("custom_components.wattplan.optimizer.planners.hybrid_policy:factory", params)


def test_solar_surplus_selects_passive_without_force_charging():
    params = economic_params(usage=(0, 1, 0, 0), prices=(.1, 1, 1, 1), solar=(0, 0, 5, 0))
    result = factory(surplus_ratio=1)(params)
    assert result["diagnostics"]["hybrid_gate"]["selected_policy"] == "self_consumption"
    assert result["entities"][0]["schedule"][0]["state"] == "self_consume"


def test_low_solar_retains_cheap_grid_purchase():
    result = factory()(economic_params())
    assert result["diagnostics"]["hybrid_gate"]["selected_policy"] == "value_policy"
    assert result["entities"][0]["schedule"][0]["state"] == "grid_charge"


def test_target_forces_value_even_with_abundant_later_pv():
    params = economic_params(usage=(0, 0, 0, 0), prices=(1, 1, 1, 1),
        solar=(0, 0, 10, 0), target={"timeslot":0,"soc_kwh":1})
    result = factory()(params)
    assert result["diagnostics"]["hybrid_gate"]["forced_value_reason"] == "battery_target"
    assert result["entities"][0]["schedule"][0]["state"] == "grid_charge"
    assert "battery_target_unmet" not in result["suboptimal_reasons"]


def test_negative_tariffs_keep_economic_dispatch():
    params = economic_params(usage=(0, 0, 0, 0), prices=(-.1, 1, 1, 1), solar=(0, 0, 10, 0))
    result = factory()(params)
    assert result["diagnostics"]["hybrid_gate"]["forced_value_reason"] == "negative_tariff"
    assert result["entities"][0]["schedule"][0]["state"] == "grid_charge"


@pytest.mark.parametrize("credit", [-1, True, float("inf")])
def test_terminal_credit_uses_value_factory_validation(credit):
    with pytest.raises(ValueError):
        factory(terminal_value_fraction=credit)


def test_terminal_credit_passes_to_value_search_without_reporting_credit():
    params = economic_params(usage=(0, 0, 0, 0), prices=(.1, 1, 1, 1))
    ordinary = factory()(params)
    credited = factory(terminal_value_fraction=.5)(params)
    assert ordinary["entities"][0]["schedule"][0]["state"] == "self_consume"
    assert credited["entities"][0]["schedule"][0]["state"] == "grid_charge"
    assert credited["projections"]["projected_cost"] == pytest.approx(.1)
    assert credited["diagnostics"]["evaluation"]["terminal_energy_credit"] == 0


@pytest.mark.parametrize("spread", [-.1, 1.1, float("nan")])
def test_pv_scenario_spread_uses_value_validation(spread):
    with pytest.raises(ValueError):
        factory(pv_scenario_spread=spread)


def test_pv_scenario_value_branch_matches_direct_value_with_original_physics():
    params = economic_params(usage=(0, 1, 0, 0), prices=(.1, 1, 1, 1), solar=(0, .4, 0, 0))
    before = params.model_dump()
    result = factory(pv_scenario_spread=.5)(params)
    direct = value_factory(energy_intervals=4, pv_scenario_spread=.5)(params)
    assert result["diagnostics"]["hybrid_gate"]["selected_policy"] == "value_policy"
    assert result["diagnostics"]["pv_scenario_factors"] == [.5, 1.5]
    assert result["entities"] == direct["entities"]
    assert result["projections"] == direct["projections"]
    assert params.model_dump() == before


@pytest.mark.parametrize("guard", [0, 1, None, "true"])
def test_prerefill_guard_requires_boolean(guard):
    with pytest.raises(ValueError):
        factory(prerefill_guard=guard)


def test_prerefill_guard_catches_cheap_buy_before_dear_load_and_later_pv():
    params = economic_params(prices=(.1, 1, 1, 1), usage=(0, 1, 0, 0), solar=(0, 0, 5, 0))
    ordinary = factory()(params)
    guarded = factory(prerefill_guard=True)(params)
    assert ordinary["entities"][0]["schedule"][0]["state"] == "self_consume"
    assert guarded["entities"][0]["schedule"][0]["state"] == "grid_charge"
    assert guarded["diagnostics"]["hybrid_gate"]["forced_value_reason"] == "dear_deficit_before_pv_refill"
    assert guarded["diagnostics"]["hybrid_gate"]["prerefill_probe"]["earlier_buy_slot"] == 0


def test_prerefill_guard_allows_pv_refill_before_dear_load():
    params = economic_params(prices=(.1, 1, 1, 1), usage=(0, 0, 1, 0), solar=(0, 5, 0, 0))
    guarded = factory(prerefill_guard=True)(params)
    assert guarded["diagnostics"]["hybrid_gate"]["selected_policy"] == "self_consumption"
    assert guarded["diagnostics"]["hybrid_gate"]["prerefill_probe"]["refill_slot"] == 1


def test_prerefill_probe_honors_permissions_and_half_slot_headroom():
    payload = economic_params(prices=(.1, 1, 1, 1), usage=(0, 1, 0, 0), solar=(0, 0, 5, 0)).model_dump()
    payload["battery_entities"][0]["can_charge_from"] = 2
    assert not _prerefill_opportunity(OptimizationParams(**payload), 4)["reject_passive"]
    params = economic_params(prices=(.1, 1, 1, 1), usage=(.1, 3, 0, 0), solar=(0, 0, 5, 0), initial=2.8)
    assert not _prerefill_opportunity(params, 4)["reject_passive"]


def test_prerefill_guard_does_not_depend_on_calendar_date():
    params = economic_params(prices=(.1, 1, 1, 1), usage=(0, 1, 0, 0), solar=(0, 0, 5, 0))
    changed = params.model_copy(update={"plan_start": params.plan_start.replace(month=1)})
    original = factory(prerefill_guard=True)(params)
    shifted = factory(prerefill_guard=True)(changed)
    assert original["entities"] == shifted["entities"]
    assert original["diagnostics"]["hybrid_gate"] == shifted["diagnostics"]["hybrid_gate"]
