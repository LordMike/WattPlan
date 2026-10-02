"""Coarse-tail MPC cadence: near-term exactness, solve counts, replay guards."""

import numpy as np
import pytest

pytest.importorskip("highspy")

from custom_components.wattplan.optimizer import OptimizationParams
from custom_components.wattplan.optimizer import mpc_power_optimizer as core
from custom_components.wattplan.optimizer.models import BatteryEntity
from tests.optimizer.benchmark_cases import build_case

SLOTS = 56
LOOKAHEAD = 20
CASES = ["battery-zero-pv", "mixed-batteries-pv"]
KEYS = (
    "battery_charge_grid",
    "battery_charge_pv",
    "battery_discharge",
    "battery_preserve",
)


COARSE_FINE_MINUTES = core.FINE_HORIZON_MINUTES


def _run(monkeypatch, payload, fine_minutes=COARSE_FINE_MINUTES):
    """Run the planner; return (result, first _run_mpc output)."""
    monkeypatch.setattr(core, "FINE_HORIZON_MINUTES", fine_minutes)
    captured = []
    original = core._run_mpc

    def spy(*args, **kwargs):
        value = original(*args, **kwargs)
        captured.append(value)
        return value

    monkeypatch.setattr(core, "_run_mpc", spy)
    normalized = core.normalize_calculation_input(OptimizationParams(**payload))
    result = core.optimize_internal(normalized, reuse_plan_override=None)
    return result, captured[0]


@pytest.mark.parametrize("case", CASES)
def test_near_term_matches_every_slot_baseline(monkeypatch, case):
    payload = build_case(case, SLOTS, LOOKAHEAD)
    fine_slots, _ = core._solve_cadence(payload["slot_minutes"])
    _, base = _run(monkeypatch, payload, 10**9)
    _, coarse = _run(monkeypatch, payload)

    for key in KEYS:
        np.testing.assert_array_equal(
            np.asarray(base[key])[:, :fine_slots],
            np.asarray(coarse[key])[:, :fine_slots],
            err_msg=key,
        )


def test_coarse_tail_makes_fewer_solves_but_covers_fine_and_block_starts(monkeypatch):
    payload = build_case("battery-zero-pv", SLOTS, LOOKAHEAD)
    fine_slots, block_slots = core._solve_cadence(payload["slot_minutes"])
    block_starts = len(range(fine_slots, SLOTS, block_slots))

    base, _ = _run(monkeypatch, payload, 10**9)
    coarse, _ = _run(monkeypatch, payload)

    assert base["successful_solves"] >= SLOTS
    assert coarse["successful_solves"] < base["successful_solves"]
    assert coarse["successful_solves"] >= fine_slots + block_starts


@pytest.mark.parametrize(
    ("slot_minutes", "expected"), [(15, (32, 4)), (30, (16, 2)), (60, (8, 1))]
)
def test_solve_cadence(slot_minutes, expected):
    assert core._solve_cadence(slot_minutes) == expected


def test_hourly_slots_match_baseline_exactly(monkeypatch):
    payload = build_case("battery-zero-pv", 24, 12)
    payload["slot_minutes"] = 60
    base, _ = _run(monkeypatch, payload, 10**9)
    coarse, _ = _run(monkeypatch, payload)

    assert coarse["successful_solves"] == base["successful_solves"]
    assert coarse["projections"] == base["projections"]
    assert coarse["entities"] == base["entities"]


def test_shift_mip_start_maps_previous_index_to_current_index():
    horizon = 5
    previous = {
        "horizon": 6,
        "battery": [
            {
                "charge_mode": np.arange(0, 6, dtype=np.float64),
                "charge_active": None,
                "discharge_active": None,
            }
        ],
        "grid_import_mode": np.arange(100, 106, dtype=np.float64),
        "pv_surplus_mode": np.arange(200, 206, dtype=np.float64),
    }
    battery_vars = [
        {
            "charge_mode": slice(0, horizon),
            "charge_active": None,
            "discharge_active": None,
        }
    ]
    grid, pv = slice(5, 10), slice(10, 15)

    indices, values = core._shift_mip_start(
        previous, horizon, battery_vars, grid, pv, shift=2
    )
    # overlap = min(5, 6 - 2) = 4; current i takes previous i + shift.
    assert indices == [0, 1, 2, 3, 5, 6, 7, 8, 10, 11, 12, 13]
    assert values == pytest.approx(
        [2, 3, 4, 5, 102, 103, 104, 105, 202, 203, 204, 205]
    )

    indices, values = core._shift_mip_start(
        previous, horizon, battery_vars, grid, pv, omit_first=True, shift=2
    )
    # With omit_first, current i (>= 1) takes previous i + shift (+1 skipped slot).
    assert indices == [1, 2, 3, 6, 7, 8, 11, 12, 13]
    assert values == pytest.approx([3, 4, 5, 103, 104, 105, 203, 204, 205])


def test_shift_mip_start_rejects_shift_beyond_previous_horizon():
    previous = {"horizon": 2, "battery": [], "grid_import_mode": None}
    assert core._shift_mip_start(
        previous, 3, [], slice(0, 3), slice(3, 6), shift=2
    ) is None


def _battery(**overrides):
    values = dict(
        name="b",
        initial_kwh=5.0,
        minimum_kwh=0.0,
        capacity_kwh=10.0,
        target=None,
        charge_curve_kwh=[2.0],
        discharge_curve_kwh=[2.0],
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        throughput_cost_per_kwh=0.0,
        action_deadband_kwh=0.0,
        mode_switch_cost=0.0,
        prefer_pv_surplus_charging=False,
        can_charge_from=3,
    )
    values.update(overrides)
    return BatteryEntity(**values)


def _plan(charge_grid=0.0, charge_pv=0.0, discharge=0.0, horizon=4):
    def arr(value):
        return np.full((1, horizon), value, dtype=np.float64)

    return {
        "plan_charge_grid": arr(charge_grid),
        "plan_charge_pv": arr(charge_pv),
        "plan_discharge": arr(discharge),
    }


def _replay(plan, level, t=18, solved_t=17, entity=None):
    return core._coarse_replay_step(
        t,
        (plan, solved_t),
        fine_slots=16,
        block_slots=4,
        battery_entities=[entity or _battery()],
        battery_levels=np.array([level]),
        infer_battery_preserve_policy=False,
    )


def test_replay_returns_planned_controls_when_physics_allows():
    replay = _replay(_plan(charge_grid=2.0), level=5.0)
    assert replay is not None
    controls, _ = replay
    assert controls["charge_grid"][0] == pytest.approx(2.0)
    assert controls["charge"][0] == pytest.approx(2.0)


@pytest.mark.parametrize(
    ("t", "coarse"),
    [(5, True), (16, True), (20, True), (18, False)],
)
def test_replay_solves_in_fine_horizon_block_start_or_without_plan(t, coarse):
    plan = (_plan(charge_grid=2.0), 17) if coarse else None
    assert (
        core._coarse_replay_step(
            t, plan, 16, 4, [_battery()], np.array([5.0]), False
        )
        is None
    )


def test_replay_falls_back_when_planned_charge_would_be_clipped():
    # Only 1.0 kWh of room left but 2.0 kWh planned: slot physics would clip it.
    assert _replay(_plan(charge_pv=2.0), level=9.0) is None
    assert _replay(_plan(charge_grid=2.0), level=9.0) is None


def test_replay_falls_back_when_grid_charge_has_no_headroom():
    assert _replay(_plan(charge_grid=0.5), level=9.9) is None


def test_replay_falls_back_when_discharge_exceeds_available_energy():
    assert _replay(_plan(discharge=2.0), level=1.0) is None
