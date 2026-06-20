"""Tests for pure historical cost simulation helpers."""

from custom_components.wattplan.historical_cost.simulations import (
    BatterySimulationConfig,
    grid_only_cost,
    simulate_self_consumption_slot,
)
import pytest


def test_grid_only_cost_uses_usage_and_import_price() -> None:
    """Grid-only scenario should price all usage as grid import."""
    assert grid_only_cost(
        usage=1.5,
        import_price=2.0,
    ) == pytest.approx(3.0)
    assert grid_only_cost(
        usage=0.5,
        import_price=2.0,
    ) == pytest.approx(1.0)


def test_self_consumption_uses_batteries_in_configured_order() -> None:
    """Self-consumption should charge and discharge batteries in configured order."""
    batteries = [
        BatterySimulationConfig(
            subentry_id="first",
            minimum_kwh=0.0,
            capacity_kwh=2.0,
            max_charge_kwh=1.0,
            max_discharge_kwh=1.0,
            charge_efficiency=1.0,
            discharge_efficiency=1.0,
            can_charge_from_pv=True,
        ),
        BatterySimulationConfig(
            subentry_id="second",
            minimum_kwh=0.0,
            capacity_kwh=2.0,
            max_charge_kwh=1.0,
            max_discharge_kwh=1.0,
            charge_efficiency=1.0,
            discharge_efficiency=1.0,
            can_charge_from_pv=True,
        ),
    ]

    charged = simulate_self_consumption_slot(
        usage=0.0,
        pv=1.5,
        batteries=batteries,
        soc_by_battery={"first": 0.0, "second": 0.0},
    )

    assert charged.grid_export == pytest.approx(0.0)
    assert charged.soc_by_battery["first"] == pytest.approx(1.0)
    assert charged.soc_by_battery["second"] == pytest.approx(0.5)

    discharged = simulate_self_consumption_slot(
        usage=1.5,
        pv=0.0,
        batteries=batteries,
        soc_by_battery=charged.soc_by_battery,
    )

    assert discharged.grid_import == pytest.approx(0.0)
    assert discharged.soc_by_battery["first"] == pytest.approx(0.0)
    assert discharged.soc_by_battery["second"] == pytest.approx(0.0)


def test_self_consumption_discharge_limit_is_delivered_energy() -> None:
    """Discharge power limit should cap delivered energy, not SoC draw."""
    result = simulate_self_consumption_slot(
        usage=1.0,
        pv=0.0,
        batteries=[
            BatterySimulationConfig(
                subentry_id="battery",
                minimum_kwh=0.0,
                capacity_kwh=2.0,
                max_charge_kwh=1.0,
                max_discharge_kwh=1.0,
                charge_efficiency=1.0,
                discharge_efficiency=0.8,
                can_charge_from_pv=True,
            )
        ],
        soc_by_battery={"battery": 2.0},
    )

    assert result.grid_import == pytest.approx(0.0)
    assert result.soc_by_battery["battery"] == pytest.approx(0.75)
