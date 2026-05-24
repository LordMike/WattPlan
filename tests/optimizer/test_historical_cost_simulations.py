"""Tests for pure historical cost simulation helpers."""

from custom_components.wattplan.historical_cost.simulations import (
    BatterySimulationConfig,
    no_battery_cost,
    simulate_self_consumption_slot,
)
import pytest


def test_no_battery_cost_uses_usage_pv_and_prices() -> None:
    """No-battery scenario should recompute flows from usage and PV facts."""
    assert no_battery_cost(
        usage=1.5,
        pv=1.0,
        import_price=2.0,
        export_price=0.5,
    ) == pytest.approx(1.0)
    assert no_battery_cost(
        usage=0.5,
        pv=1.0,
        import_price=2.0,
        export_price=0.5,
    ) == pytest.approx(-0.25)


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
