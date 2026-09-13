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
