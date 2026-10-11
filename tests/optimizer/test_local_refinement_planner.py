"""Robust tariff acceptance preserves physics and deadline priority."""

import pytest

from custom_components.wattplan.optimizer.planners.local_refinement import factory
from custom_components.wattplan.optimizer.planners.local_search import factory as local
from tests.optimizer.planner_conformance import (
    assert_conforms, battery_policies, conformance_params, economic_params,
)


@pytest.mark.parametrize("options", [
    {"grid_margin_per_kwh": -1}, {"grid_margin_per_kwh": float("nan")},
    {"grid_margin_per_kwh": True}, {"preserve_fraction": -0.1},
    {"preserve_fraction": 1.1}, {"preserve_fraction": True},
    {"tariff_fraction": -0.1}, {"tariff_fraction": float("nan")},
    {"tariff_fraction": True},
])
def test_invalid_options(options):
    with pytest.raises(ValueError):
        factory(**options)


@pytest.mark.parametrize("params", conformance_params())
def test_full_shared_contract(params):
    assert_conforms(
        "custom_components.wattplan.optimizer.planners.local_refinement:factory",
        params, {"grid_margin_per_kwh": 0.3, "preserve_fraction": 0.15},
    )


def test_zero_margin_matches_three_seed_local_search():
    params = economic_params()
    assert battery_policies(factory()(params)) == battery_policies(local(max_evaluations=3)(params))


def test_margin_rejects_marginal_grid_trade():
    params = economic_params(prices=(0.9, 1), usage=(0, 1))
    ordinary = factory()(params)
    robust = factory(grid_margin_per_kwh=0.2)(params)
    assert ordinary["entities"][0]["schedule"][0]["state"] == "grid_charge"
    assert robust["entities"][0]["schedule"][0]["state"] == "self_consume"
    assert robust["diagnostics"]["rejected_grid_margin"] == 1


def test_margin_does_not_prevent_immediate_target_recovery():
    params = economic_params(
        prices=(10, 1, 1, 1), usage=(0, 0, 0, 0),
        target={"timeslot": 0, "soc_kwh": 1},
    )
    result = factory(grid_margin_per_kwh=100)(params)
    assert result["entities"][0]["schedule"][0]["state"] == "grid_charge"
    assert "battery_target_unmet" not in result["suboptimal_reasons"]


def test_tariff_quantile_can_buy_two_cheap_slots_for_later_demand():
    params = economic_params(prices=(0.1, 0.2, 1, 1), usage=(0, 0, 1, 1))
    minimum = factory()(params)
    broader = factory(tariff_fraction=0.4)(params)
    assert broader["entities"][0]["schedule"][1]["state"] == "grid_charge"
    assert broader["projections"]["projected_cost"] < minimum["projections"]["projected_cost"]


@pytest.mark.parametrize("preserve_fraction", [None, 0.8])
def test_exact_target_keeps_feasible_self_consumption_seed(preserve_fraction):
    params = economic_params(prices=(.1, .8, .8, .8), usage=(0, .1, .1, .5),
        solar=(.3, .3, .3, .3),
        target={"timeslot":3,"soc_kwh":.5,"mode":"exact","tolerance_kwh":.02})
    original = local(max_evaluations=3)(params)
    result = factory(preserve_fraction=preserve_fraction)(params)
    assert original["diagnostics"]["evaluation"]["score"] == pytest.approx([0, 0])
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(
        original["diagnostics"]["evaluation"]["score"])
    assert "battery_target_unmet" not in result["suboptimal_reasons"]
    assert result["diagnostics"]["evaluations"] <= 5
