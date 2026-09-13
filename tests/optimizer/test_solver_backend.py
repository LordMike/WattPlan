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


def _mip_start(horizon, batteries):
    return {
        "horizon": horizon,
        "battery": batteries,
        "grid_import_mode": np.arange(100, 100 + horizon, dtype=np.float64),
        "pv_surplus_mode": np.arange(200, 200 + horizon, dtype=np.float64),
    }


def _battery_start(horizon, offset, *, deadband):
    return {
        "charge_mode": np.arange(offset, offset + horizon, dtype=np.float64),
        "charge_active": (
            np.arange(offset + 20, offset + 20 + horizon, dtype=np.float64)
            if deadband
            else None
        ),
        "discharge_active": (
            np.arange(offset + 40, offset + 40 + horizon, dtype=np.float64)
            if deadband
            else None
        ),
    }


def test_production_mip_start_omits_current_and_appended_terminal_slot():
    previous = _mip_start(4, [_battery_start(4, 0, deadband=True)])
    battery_vars = [{
        "charge_mode": slice(0, 4),
        "charge_active": slice(4, 8),
        "discharge_active": slice(8, 12),
    }]

    indices, values = optimizer._shift_mip_start(
        previous,
        4,
        battery_vars,
        slice(12, 16),
        slice(16, 20),
        omit_first=True,
    )

    assert indices == [1, 2, 5, 6, 9, 10, 13, 14, 17, 18]
    assert values == pytest.approx(
        [2, 3, 22, 23, 42, 43, 102, 103, 202, 203]
    )


def test_production_mip_start_maps_entire_shrinking_tail_after_current_slot():
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
        omit_first=True,
    )

    assert indices == [1, 2, 4, 5, 7, 8, 10, 11]
    assert values == pytest.approx(
        [0.0, 1.0, 1.0, 0.0, 1.0, 0.0, 0.0, 1.0]
    )


@pytest.mark.parametrize(
    ("previous_horizon", "horizon", "expected"),
    [(2, 1, None), (2, 2, None), (3, 2, ([1, 3, 5], [2.0, 102.0, 202.0]))],
)
def test_production_mip_start_handles_short_horizons(
    previous_horizon, horizon, expected
):
    result = optimizer._shift_mip_start(
        _mip_start(
            previous_horizon,
            [_battery_start(previous_horizon, 0, deadband=False)],
        ),
        horizon,
        [{
            "charge_mode": slice(0, horizon),
            "charge_active": None,
            "discharge_active": None,
        }],
        slice(horizon, horizon * 2),
        slice(horizon * 2, horizon * 3),
        omit_first=True,
    )

    if expected is None:
        assert result is None
    else:
        indices, values = result
        assert indices == expected[0]
        assert values == pytest.approx(expected[1])


def test_production_mip_start_supports_mixed_battery_deadband_layouts():
    previous = _mip_start(
        4,
        [
            _battery_start(4, 0, deadband=True),
            _battery_start(4, 60, deadband=False),
        ],
    )
    indices, values = optimizer._shift_mip_start(
        previous,
        4,
        [
            {
                "charge_mode": slice(0, 4),
                "charge_active": slice(4, 8),
                "discharge_active": slice(8, 12),
            },
            {
                "charge_mode": slice(12, 16),
                "charge_active": None,
                "discharge_active": None,
            },
        ],
        slice(16, 20),
        slice(20, 24),
        omit_first=True,
    )

    assert indices == [1, 2, 5, 6, 9, 10, 13, 14, 17, 18, 21, 22]
    assert values == pytest.approx(
        [2, 3, 22, 23, 42, 43, 62, 63, 102, 103, 202, 203]
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


def test_sparse_solver_accepts_inequality_only_rows():
    upper = optimizer._SparseRow()
    upper[0] = 1.0
    result = optimizer._solve_lp(
        objective=np.asarray([-1.0]),
        A_ub=[upper],
        b_ub=np.asarray([1.0]),
        A_eq=None,
        b_eq=None,
        bounds=[(0.0, 2.0)],
    )

    assert result.success
    assert result.objective_value == pytest.approx(-1.0)
    assert result.x == pytest.approx([1.0])


def test_sparse_solver_accepts_equality_only_rows():
    equality = optimizer._SparseRow()
    equality[0] = 1.0
    result = optimizer._solve_lp(
        objective=np.asarray([1.0]),
        A_ub=None,
        b_ub=None,
        A_eq=[equality],
        b_eq=np.asarray([1.0]),
        bounds=[(0.0, 2.0)],
    )

    assert result.success
    assert result.objective_value == pytest.approx(1.0)
    assert result.x == pytest.approx([1.0])


def test_unrestricted_solver_model_still_runs():
    result = optimizer._solve_lp(
        objective=np.asarray([-1.0]),
        A_ub=None,
        b_ub=None,
        A_eq=None,
        b_eq=None,
        bounds=[(0.0, 2.0)],
    )

    assert result.success
    assert result.objective_value == pytest.approx(-2.0)
    assert result.x == pytest.approx([2.0])


def test_partial_mip_start_does_not_constrain_final_solution():
    link = optimizer._SparseRow()
    link[0] = -1.0
    link[1] = 2.0
    integrality = [
        highspy.HighsVarType.kContinuous,
        highspy.HighsVarType.kInteger,
    ]
    kwargs = {
        "objective": np.asarray([1.0, 0.0]),
        "A_ub": [link],
        "b_ub": np.asarray([0.0]),
        "A_eq": None,
        "b_eq": None,
        "bounds": [(0.0, 2.0), (0.0, 1.0)],
        "integrality": integrality,
    }

    cold = optimizer._solve_lp(**kwargs)
    warm = optimizer._solve_lp(**kwargs, mip_start=([1], [1.0]))

    assert cold.success and warm.success
    assert warm.mip_start_status == highspy.HighsStatus.kOk
    assert warm.objective_value == pytest.approx(cold.objective_value)
    assert warm.x[1] == pytest.approx(0.0)


def test_accepted_mip_start_with_no_feasible_completion_is_not_constraining():
    link = optimizer._SparseRow()
    link[0] = -1.0
    link[1] = 2.0
    result = optimizer._solve_lp(
        objective=np.asarray([0.0, -1.0]),
        A_ub=[link],
        b_ub=np.asarray([0.0]),
        A_eq=None,
        b_eq=None,
        bounds=[(0.0, 1.0), (0.0, 1.0)],
        integrality=[
            highspy.HighsVarType.kContinuous,
            highspy.HighsVarType.kInteger,
        ],
        mip_start=([1], [1.0]),
    )

    assert result.success
    assert result.mip_start_status == highspy.HighsStatus.kOk
    assert result.objective_value == pytest.approx(0.0)
    assert result.x == pytest.approx([0.0, 0.0])


def test_non_ok_mip_start_submission_retries_cold(monkeypatch):
    real_highs = highspy.Highs
    instances = []

    class RejectFirstStart:
        def __init__(self):
            self.delegate = real_highs()
            instances.append(self)

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        def setSolution(self, *args):
            return highspy.HighsStatus.kError

    monkeypatch.setattr(optimizer.highspy, "Highs", RejectFirstStart)
    result = optimizer._solve_lp(
        objective=np.asarray([1.0]),
        A_ub=None,
        b_ub=None,
        A_eq=None,
        b_eq=None,
        bounds=[(0.0, 1.0)],
        mip_start=([0], [1.0]),
    )

    assert result.success
    assert result.objective_value == pytest.approx(0.0)
    assert result.mip_start_status == highspy.HighsStatus.kError
    assert len(instances) == 2
