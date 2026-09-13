import numpy as np
import pytest

highspy = pytest.importorskip("highspy")

from custom_components.wattplan.optimizer import mpc_power_optimizer as optimizer


def test_highs_sparse_conversion_preserves_mixed_milp_rows():
    result = optimizer._solve_lp(
        objective=np.asarray([1.0, 2.0, 0.5, 0.0]),
        A_ub=np.asarray(
            [
                [1.0, 1.0, 0.0, 0.0],
                [0.0, -1.0, 1.0, 0.0],
            ]
        ),
        b_ub=np.asarray([3.0, 0.0]),
        A_eq=np.asarray([[1.0, 0.0, 1.0, 0.0]]),
        b_eq=np.asarray([2.0]),
        bounds=[(0.0, None), (0.0, 1.0), (0.0, None), (0.0, 0.0)],
        integrality=[
            highspy.HighsVarType.kContinuous,
            highspy.HighsVarType.kInteger,
            highspy.HighsVarType.kContinuous,
            highspy.HighsVarType.kContinuous,
        ],
    )

    assert result.success
    assert result.objective_value == pytest.approx(2.0)
    assert result.x == pytest.approx([2.0, 0.0, 0.0, 0.0])


def test_direct_sparse_rows_match_dense_solver_input():
    upper = optimizer._SparseRow()
    upper[0] = 1.0
    upper[1] = 1.0
    link = optimizer._SparseRow()
    link[1] = -1.0
    link[2] = 1.0
    equality = optimizer._SparseRow()
    equality[0] = 1.0
    equality[2] = 1.0

    result = optimizer._solve_lp(
        objective=np.asarray([1.0, 2.0, 0.5, 0.0]),
        A_ub=[upper, link],
        b_ub=np.asarray([3.0, 0.0]),
        A_eq=[equality],
        b_eq=np.asarray([2.0]),
        bounds=[(0.0, None), (0.0, 1.0), (0.0, None), (0.0, 0.0)],
        integrality=[
            highspy.HighsVarType.kContinuous,
            highspy.HighsVarType.kInteger,
            highspy.HighsVarType.kContinuous,
            highspy.HighsVarType.kContinuous,
        ],
    )

    assert result.success
    assert result.objective_value == pytest.approx(2.0)
    assert result.x == pytest.approx([2.0, 0.0, 0.0, 0.0])


def test_adjacent_mip_start_shifts_only_overlapping_integer_slots():
    previous = {
        "horizon": 4,
        "battery": [
            {
                "charge_mode": np.asarray([0.0, 1.0, 0.0, 1.0]),
                "charge_active": np.asarray([1.0, 0.0, 1.0, 0.0]),
                "discharge_active": None,
            }
        ],
        "grid_import_mode": np.asarray([1.0, 0.0, 1.0, 0.0]),
        "pv_surplus_mode": np.asarray([0.0, 1.0, 0.0, 1.0]),
    }
    battery_vars = [
        {
            "charge_mode": slice(0, 3),
            "charge_active": slice(3, 6),
            "discharge_active": None,
        }
    ]

    indices, values = optimizer._shift_mip_start(
        previous,
        3,
        battery_vars,
        slice(6, 9),
        slice(9, 12),
    )

    assert indices == list(range(12))
    assert values == pytest.approx(
        [1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0]
    )


def test_adjacent_mip_start_rejects_incompatible_shape():
    assert optimizer._shift_mip_start(
        {"horizon": 4, "battery": []},
        3,
        [
            {
                "charge_mode": slice(0, 3),
                "charge_active": None,
                "discharge_active": None,
            }
        ],
        slice(3, 6),
        slice(6, 9),
    ) is None
