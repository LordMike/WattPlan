"""Schedule scoring tolerates float round-off at battery minimum and target bounds."""

import numpy as np
import pytest

from custom_components.wattplan.optimizer import mpc_power_optimizer as core
from custom_components.wattplan.optimizer.models import BatteryEntity, BatteryTarget


def _battery(target_mode: str) -> BatteryEntity:
    return BatteryEntity(
        name="house",
        initial_kwh=5.0,
        minimum_kwh=2.0,
        capacity_kwh=10.0,
        target=BatteryTarget(timeslot=1, soc_kwh=2.0, mode=target_mode, tolerance_kwh=0.0),
        charge_curve_kwh=[1.0],
        discharge_curve_kwh=[3.0],
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        throughput_cost_per_kwh=0.0,
        action_deadband_kwh=0.0,
        mode_switch_cost=0.0,
        prefer_pv_surplus_charging=False,
        can_charge_from=3,
    )


def _reasons(levels, target_mode="exact"):
    steps = len(levels) - 1
    discharge = np.maximum(-np.diff(levels), 0.0).reshape(1, steps)
    _, _, reasons, _, _ = core._score_schedule(
        prices=np.full(steps, 0.3),
        grid_export_prices=np.zeros(steps),
        solar_input=np.zeros(steps),
        usage=np.full(steps, 3.0),
        battery_entities=[_battery(target_mode)],
        comfort_entities=[],
        battery_levels=np.asarray([levels], dtype=np.float64),
        battery_charge=np.zeros((1, steps)),
        battery_discharge=discharge,
        battery_states=np.zeros((1, steps), dtype=np.int32),
        comfort_enabled=np.zeros((0, steps), dtype=np.int32),
        initial_comfort_lock_mode=np.zeros(0),
        initial_comfort_lock_remaining=np.zeros(0),
        rolling_window_slots=4,
    )
    return reasons


@pytest.mark.parametrize("noise", [-1e-9, 1e-9])
def test_round_off_at_minimum_and_exact_target_is_not_a_violation(noise):
    assert _reasons([5.0, 3.5, 2.0 + noise, 2.0 + noise]) == []


def test_real_minimum_and_target_misses_are_still_reported():
    assert _reasons([5.0, 3.5, 1.9, 1.9], target_mode="at_least") == [
        "battery_min_unmet",
        "battery_target_unmet",
    ]
    assert _reasons([5.0, 4.0, 2.1, 2.1], target_mode="at_most") == [
        "battery_target_unmet",
    ]
