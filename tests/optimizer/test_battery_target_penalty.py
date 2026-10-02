"""The battery target slack is penalized once per battery, not once per slot."""

import numpy as np
import pytest

from custom_components.wattplan.optimizer import OptimizationParams
from custom_components.wattplan.optimizer import mpc_power_optimizer as core
from custom_components.wattplan.optimizer.models import normalize_calculation_input


def _params(lookahead: int) -> OptimizationParams:
    horizon = 24
    return OptimizationParams(
        grid_import_price_per_kwh=[0.3] * horizon,
        grid_export_price_per_kwh=[0.05] * horizon,
        solar_input_kwh=[0.0] * horizon,
        usage_kwh=[0.2] * horizon,
        lookahead_slots=lookahead,
        battery_entities=[
            {
                "name": "house",
                "initial_kwh": 2.0,
                "minimum_kwh": 1.0,
                "capacity_kwh": 10.0,
                "target": {"timeslot": 6, "soc_kwh": 6.0},
                "charge_curve_kwh": [1.0],
                "discharge_curve_kwh": [1.0],
                "can_charge_from": 3,
            }
        ],
        comfort_entities=[],
    )


@pytest.mark.parametrize("lookahead", [8, 20])
def test_target_penalty_does_not_scale_with_lookahead(monkeypatch, lookahead):
    objectives = []
    original = core._solve_lp

    def recording_solve(*args, **kwargs):
        objectives.append(np.asarray(kwargs["objective"], dtype=np.float64))
        return original(*args, **kwargs)

    monkeypatch.setattr(core, "_solve_lp", recording_solve)
    result = core.optimize_internal(normalize_calculation_input(_params(lookahead)))

    assert objectives
    # The largest coefficients are the 5000 soft-minimum and target penalties.
    assert max(float(np.max(objective)) for objective in objectives) == 5000.0
    assert "battery_target_unmet" not in result["suboptimal_reasons"]
    assert result["entities"][0]["schedule"][6]["level"] >= 6.0 - 1e-6
