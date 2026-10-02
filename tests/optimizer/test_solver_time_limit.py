"""HiGHS time limit handling: limits are passed down and incumbents accepted."""

from datetime import UTC, datetime

import numpy as np
import pytest

highspy = pytest.importorskip("highspy")

from custom_components.wattplan.optimizer import (
    OptimizationParams,
    optimize,
)
from custom_components.wattplan.optimizer import mpc_power_optimizer as optimizer


def test_time_limited_solve_is_usable_only_with_a_feasible_incumbent() -> None:
    status = highspy.HighsModelStatus
    feasible = highspy.kSolutionStatusFeasible
    none = highspy.kSolutionStatusNone

    assert optimizer._solution_usable(status.kOptimal, feasible)
    assert optimizer._solution_usable(status.kTimeLimit, feasible)
    assert not optimizer._solution_usable(status.kTimeLimit, none)
    assert not optimizer._solution_usable(status.kInfeasible, feasible)


def test_solve_lp_accepts_a_time_limit() -> None:
    result = optimizer._solve_lp(
        objective=np.asarray([1.0, 2.0]),
        A_ub=None,
        b_ub=None,
        A_eq=np.asarray([[1.0, 1.0]]),
        b_eq=np.asarray([3.0]),
        bounds=[(0.0, None), (0.0, None)],
        time_limit_seconds=5.0,
    )

    assert result.success
    assert result.objective_value == pytest.approx(3.0)


def test_request_time_limit_reaches_every_solve(monkeypatch) -> None:
    limits = []
    original = optimizer._solve_lp

    def capture(*args, **kwargs):
        limits.append(kwargs.get("time_limit_seconds"))
        return original(*args, **kwargs)

    monkeypatch.setattr(optimizer, "_solve_lp", capture)
    horizon = 8
    result = optimize(
        OptimizationParams(
            plan_start=datetime(2026, 9, 23, tzinfo=UTC),
            slot_minutes=60,
            grid_import_price_per_kwh=[0.2, 0.4] * (horizon // 2),
            grid_export_price_per_kwh=[0.05] * horizon,
            solar_input_kwh=[0.0] * horizon,
            usage_kwh=[0.3] * horizon,
            lookahead_slots=horizon,
            solver_time_limit_seconds=7.5,
            battery_entities=[
                {
                    "name": "house",
                    "initial_kwh": 2.0,
                    "minimum_kwh": 1.0,
                    "capacity_kwh": 5.0,
                    "charge_curve_kwh": [2.0],
                    "discharge_curve_kwh": [2.0],
                    "can_charge_from": 3,
                }
            ],
            comfort_entities=[],
        )
    )

    assert result["successful_solves"] > 0
    assert limits
    assert set(limits) == {7.5}


def test_default_time_limit_is_bounded() -> None:
    params = OptimizationParams(
        grid_import_price_per_kwh=[0.2] * 4,
        solar_input_kwh=[0.0] * 4,
        usage_kwh=[0.1] * 4,
        battery_entities=[],
        comfort_entities=[],
    )
    assert params.solver_time_limit_seconds == pytest.approx(20.0)
    with pytest.raises(ValueError):
        OptimizationParams(
            grid_import_price_per_kwh=[0.2] * 4,
            solver_time_limit_seconds=0,
            battery_entities=[],
            comfort_entities=[],
        )
