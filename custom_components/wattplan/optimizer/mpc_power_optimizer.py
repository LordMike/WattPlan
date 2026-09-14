import time

import numpy as np

from .comfort_placement import ComfortPlacementInput, place_comfort_schedules
from .models import (
    BatteryEntity,
    CalculationInput,
    ChargeSource,
    OptimizationParams,
    encode_state_blob,
    normalize_calculation_input,
)

try:
    import highspy
except ImportError:
    highspy = None


EPSILON = 1e-6
AVG_PRICE_SENTINEL = 1000.0
PRESERVE_PROBE_MIN_KWH = 0.01
PRESERVE_OBJECTIVE_TOLERANCE = 1e-7
MIP_START_MIN_LOOKAHEAD_SLOTS = 40
COMFORT_PLACEMENT_MAX_CANDIDATES_PER_ENTITY = 16
COMFORT_PLACEMENT_MAX_TOTAL_CANDIDATES = 48
COMFORT_PLACEMENT_SEARCH_SECONDS = 0.1
_AUTO_REUSE = object()


def _meets_action_deadband(amount: float, deadband: float) -> bool:
    """Return whether a positive flow reaches the inclusive action threshold."""
    return float(amount) > EPSILON and float(amount) + EPSILON >= float(deadband)


def piecewise_value_interpolated(level_array, curve):
    curve = np.asarray(curve, dtype=np.float64)
    n = len(curve)
    if n == 1:
        return np.full_like(level_array, curve[0], dtype=np.float64)

    segment_size = 100.0 / (n - 1)
    index_float = np.asarray(level_array, dtype=np.float64) / segment_size
    i_floor = np.floor(index_float).astype(int)
    i_ceil = i_floor + 1
    i_floor = np.clip(i_floor, 0, n - 1)
    i_ceil = np.clip(i_ceil, 0, n - 1)
    ratio = np.clip(index_float - i_floor, 0.0, 1.0)

    v1 = curve[i_floor]
    v2 = curve[i_ceil]
    return v1 + ratio * (v2 - v1)


def _piecewise_scalar(level, curve):
    return float(piecewise_value_interpolated(np.array([level]), curve)[0])


def _battery_power_limits(entity, level):
    if len(entity.charge_curve_kwh) == 1 and len(entity.discharge_curve_kwh) == 1:
        return max(0.0, float(entity.charge_curve_kwh[0])), max(
            0.0, float(entity.discharge_curve_kwh[0])
        )

    capacity = max(float(entity.capacity_kwh), EPSILON)
    soc = np.clip((float(level) / capacity) * 100.0, 0.0, 100.0)
    charge_limit = max(0.0, _piecewise_scalar(soc, entity.charge_curve_kwh))
    discharge_limit = max(0.0, _piecewise_scalar(soc, entity.discharge_curve_kwh))
    return charge_limit, discharge_limit


def _battery_target_bounds_kwh(entity):
    if entity.target is None:
        return None

    target_kwh = float(entity.target.soc_kwh)
    tol_kwh = float(entity.target.tolerance_kwh)
    mode = entity.target.mode

    if mode == "at_least":
        return {
            "timeslot": int(entity.target.timeslot),
            "lower_kwh": max(0.0, target_kwh - tol_kwh),
            "upper_kwh": None,
            "target_kwh": target_kwh,
            "mode": mode,
        }
    if mode == "at_most":
        return {
            "timeslot": int(entity.target.timeslot),
            "lower_kwh": None,
            "upper_kwh": min(float(entity.capacity_kwh), target_kwh + tol_kwh),
            "target_kwh": target_kwh,
            "mode": mode,
        }

    return {
        "timeslot": int(entity.target.timeslot),
        "lower_kwh": max(0.0, target_kwh - tol_kwh),
        "upper_kwh": min(float(entity.capacity_kwh), target_kwh + tol_kwh),
        "target_kwh": target_kwh,
        "mode": mode,
    }


def _charge_ingress_permissions(entity):
    flags = int(entity.can_charge_from)
    return (
        bool(flags & int(ChargeSource.GRID)),
        bool(flags & int(ChargeSource.PV)),
    )


def _initial_comfort_history(entity, rolling_window_slots):
    history_slots = max(int(rolling_window_slots) - 1, 0)
    if entity.on_history is None:
        # A legacy aggregate over W prior slots guarantees only max(count-k, 0)
        # ON observations after k oldest slots have left the window. Placing that
        # guaranteed credit at the oldest edge models those lower bounds without
        # claiming an order the caller did not provide.
        history = np.zeros(history_slots, dtype=np.int32)
        guaranteed_on = min(
            max(int(entity.on_slots_last_rolling_window) - 1, 0), history_slots
        )
        history[:guaranteed_on] = 1
        return history
    return np.asarray(entity.on_history, dtype=np.int32).copy()


def _comfort_window_deficit(history, target):
    return max(float(target) - float(np.sum(history)), 0.0)


def _fixed_comfort_schedule(
    comfort_entities,
    rolling_window_slots,
    total_steps,
    initial_lock_modes,
    initial_lock_remaining,
):
    """Build a cheap, constraint-driven comfort schedule outside the battery MILP."""
    schedule = np.zeros((len(comfort_entities), total_steps), dtype=np.float64)
    history_slots = max(int(rolling_window_slots) - 1, 0)
    for i, entity in enumerate(comfort_entities):
        history = _initial_comfort_history(entity, rolling_window_slots).tolist()
        previous = bool(entity.is_on_now)
        mode = bool(initial_lock_modes[i])
        remaining = max(int(initial_lock_remaining[i]), 0)
        off_streak = 0 if previous else int(entity.off_streak_slots_now)
        for t in range(total_steps):
            target = int(entity.target_on_slots_per_rolling_window)
            max_off = int(entity.max_consecutive_off_slots)
            if remaining > 0:
                enabled = mode or off_streak >= max_off
                if enabled != mode:
                    mode = enabled
                    remaining = max(int(entity.min_consecutive_on_slots) - 1, 0)
                else:
                    remaining -= 1
            else:
                must_start_on = sum(history) < target or off_streak >= max_off
                if not must_start_on and not previous:
                    projected = history.copy()
                    projected_off = off_streak
                    first_required = None
                    for offset in range(total_steps - t):
                        if sum(projected) < target or projected_off >= max_off:
                            first_required = offset
                            break
                        if history_slots:
                            projected.append(0)
                            projected = projected[-history_slots:]
                        projected_off += 1
                    if first_required is not None:
                        slots_after_start = total_steps - (t + first_required)
                        must_start_on = slots_after_start < int(
                            entity.min_consecutive_on_slots
                        )
                enabled = must_start_on
                if previous and not enabled:
                    minimum_off = int(entity.min_consecutive_off_slots)
                    if t + minimum_off > total_steps:
                        enabled = True
                    else:
                        projected = history.copy()
                        projected_off = off_streak
                        for _ in range(minimum_off):
                            if sum(projected) < target or projected_off >= max_off:
                                enabled = True
                                break
                            if history_slots:
                                projected.append(0)
                                projected = projected[-history_slots:]
                            projected_off += 1
                        if not enabled:
                            first_required = None
                            for offset in range(minimum_off, total_steps - t):
                                if sum(projected) < target or projected_off >= max_off:
                                    first_required = offset
                                    break
                                if history_slots:
                                    projected.append(0)
                                    projected = projected[-history_slots:]
                                projected_off += 1
                            if first_required is not None:
                                slots_after_start = total_steps - (t + first_required)
                                enabled = slots_after_start < int(
                                    entity.min_consecutive_on_slots
                                )
                if enabled != previous:
                    minimum = (
                        int(entity.min_consecutive_on_slots)
                        if enabled
                        else int(entity.min_consecutive_off_slots)
                    )
                    mode = enabled
                    remaining = max(minimum - 1, 0)
            schedule[i, t] = float(enabled)
            off_streak = 0 if enabled else off_streak + 1
            if history_slots:
                history.append(int(enabled))
                history = history[-history_slots:]
            previous = enabled
    return schedule


def _build_reuse_plan(
    previous_state,
    grid_import_prices,
    grid_export_prices,
    solar_input,
    usage,
    total_steps,
    battery_entities,
    comfort_entities,
    rolling_window_slots,
    expected_fingerprint,
):
    if previous_state is None:
        return None

    if previous_state.entity_fingerprint != expected_fingerprint:
        return None

    old_steps = previous_state.num_steps
    old_prices = previous_state.grid_import_prices
    old_solar = previous_state.solar_input
    old_usage = previous_state.usage
    battery_charge = previous_state.battery_charge
    battery_levels = previous_state.battery_levels
    battery_charge_grid = previous_state.battery_charge_grid
    battery_charge_pv = previous_state.battery_charge_pv
    battery_discharge = previous_state.battery_discharge
    battery_preserve = previous_state.battery_preserve
    comfort_on = previous_state.comfort_on
    comfort_levels = previous_state.comfort_levels
    comfort_off_streaks = previous_state.comfort_off_streaks
    comfort_is_on = previous_state.comfort_is_on
    comfort_history = previous_state.comfort_history
    comfort_lock_mode = previous_state.comfort_lock_mode
    comfort_lock_remaining = previous_state.comfort_lock_remaining

    num_battery = len(battery_entities)
    num_comfort = len(comfort_entities)
    if battery_levels is not None and battery_levels.shape != (
        num_battery,
        old_steps + 1,
    ):
        return None
    if battery_charge.shape[0] != num_battery:
        return None
    if battery_discharge.shape[0] != num_battery:
        return None
    if battery_charge_grid.shape[0] != num_battery:
        return None
    if battery_charge_pv.shape[0] != num_battery:
        return None
    if battery_preserve.shape[0] != num_battery:
        return None
    if comfort_on.shape[0] != num_comfort:
        return None
    if comfort_levels is None or comfort_levels.shape != (
        num_comfort,
        old_steps + 1,
    ):
        return None
    if comfort_off_streaks is None or comfort_off_streaks.shape != (
        num_comfort,
        old_steps + 1,
    ):
        return None
    if comfort_is_on is None or comfort_is_on.shape != (
        num_comfort,
        old_steps + 1,
    ):
        return None
    expected_history_shape = (
        num_comfort,
        old_steps + 1,
        max(int(rolling_window_slots) - 1, 0),
    )
    if num_comfort > 0 and (
        comfort_history is None or comfort_history.shape != expected_history_shape
    ):
        return None
    if comfort_lock_mode.shape[0] != num_comfort:
        return None
    if comfort_lock_remaining.shape[0] != num_comfort:
        return None

    actual_battery_levels = np.asarray(
        [float(entity.initial_kwh) for entity in battery_entities],
        dtype=np.float64,
    )
    actual_comfort_modes = np.asarray(
        [1 if entity.is_on_now else 0 for entity in comfort_entities],
        dtype=np.int32,
    )
    actual_comfort_levels = np.asarray(
        [
            _comfort_window_deficit(
                _initial_comfort_history(entity, rolling_window_slots),
                entity.target_on_slots_per_rolling_window,
            )
            for entity in comfort_entities
        ],
        dtype=np.float64,
    )
    actual_comfort_history = np.asarray(
        [
            _initial_comfort_history(entity, rolling_window_slots)
            for entity in comfort_entities
        ],
        dtype=np.int32,
    )
    actual_comfort_off_streaks = np.asarray(
        [
            0.0 if entity.is_on_now else float(entity.off_streak_slots_now)
            for entity in comfort_entities
        ],
        dtype=np.float64,
    )
    best_forecast_offset = None
    best_forecast_overlap = 0
    best_offset = None
    best_overlap = 0
    for offset_steps in range(old_steps):
        overlap_steps = min(total_steps, old_steps - offset_steps)
        if overlap_steps <= 0:
            continue

        if not np.allclose(
            old_prices[offset_steps : offset_steps + overlap_steps],
            grid_import_prices[:overlap_steps],
            atol=1e-9,
            rtol=0.0,
        ):
            continue
        if not np.allclose(
            previous_state.grid_export_prices[
                offset_steps : offset_steps + overlap_steps
            ],
            grid_export_prices[:overlap_steps],
            atol=1e-9,
            rtol=0.0,
        ):
            continue
        if not np.allclose(
            old_solar[offset_steps : offset_steps + overlap_steps],
            solar_input[:overlap_steps],
            atol=1e-9,
            rtol=0.0,
        ):
            continue
        if not np.allclose(
            old_usage[offset_steps : offset_steps + overlap_steps],
            usage[:overlap_steps],
            atol=1e-9,
            rtol=0.0,
        ):
            continue
        if overlap_steps > best_forecast_overlap:
            best_forecast_offset = offset_steps
            best_forecast_overlap = overlap_steps

        battery_state_matches = battery_levels is not None and np.allclose(
            battery_levels[:, offset_steps],
            actual_battery_levels,
            atol=1e-6,
            rtol=0.0,
        )
        comfort_state_matches = (
            np.array_equal(
                comfort_is_on[:, offset_steps].astype(np.int32),
                actual_comfort_modes,
            )
            and np.allclose(
                comfort_levels[:, offset_steps],
                actual_comfort_levels,
                atol=1e-6,
                rtol=0.0,
            )
            and (
                num_comfort == 0
                or np.array_equal(
                    comfort_history[:, offset_steps, :], actual_comfort_history
                )
            )
            and np.allclose(
                comfort_off_streaks[:, offset_steps],
                actual_comfort_off_streaks,
                atol=1e-6,
                rtol=0.0,
            )
        )
        if not battery_state_matches or not comfort_state_matches:
            # The longest matching forecast overlap defines elapsed time. A
            # later coincidental state match must not invent a shorter window.
            break

        if overlap_steps > best_overlap:
            best_offset = offset_steps
            best_overlap = overlap_steps
        break

    if best_forecast_offset is None or best_forecast_overlap <= 0:
        return None

    lock_offset = best_offset if best_offset is not None else best_forecast_offset
    reusable_steps = int(best_overlap)
    if best_offset is not None and best_overlap < total_steps:
        # Appended forecast data can change decisions anywhere inside a later
        # MPC window. Re-solve the request while carrying only runtime locks.
        reusable_steps = 0
    reuse_plan = {
        "overlap_steps": reusable_steps,
        "initial_comfort_lock_mode": comfort_lock_mode[:, lock_offset].copy(),
        "initial_comfort_lock_remaining": comfort_lock_remaining[
            :, lock_offset
        ].copy(),
    }
    if best_offset is None or reusable_steps <= 0:
        return reuse_plan

    reuse_plan.update(
        {
            "battery_charge": battery_charge[
                :, best_offset : best_offset + best_overlap
            ],
            "battery_charge_grid": battery_charge_grid[
                :, best_offset : best_offset + best_overlap
            ],
            "battery_charge_pv": battery_charge_pv[
                :, best_offset : best_offset + best_overlap
            ],
            "battery_discharge": battery_discharge[
                :, best_offset : best_offset + best_overlap
            ],
            "battery_preserve": battery_preserve[
                :, best_offset : best_offset + best_overlap
            ],
            "comfort_on": comfort_on[:, best_offset : best_offset + best_overlap],
            "comfort_lock_mode": comfort_lock_mode[
                :, best_offset : best_offset + best_overlap
            ],
            "comfort_lock_remaining": comfort_lock_remaining[
                :, best_offset : best_offset + best_overlap
            ],
        }
    )
    return reuse_plan


class _SparseRow:
    def __init__(self):
        self.values = {}

    def __getitem__(self, index):
        return self.values.get(int(index), 0.0)

    def __setitem__(self, index, value):
        index = int(index)
        value = float(value)
        if abs(value) > EPSILON:
            self.values[index] = value
        else:
            self.values.pop(index, None)


def _solve_lp(
    objective,
    A_ub,
    b_ub,
    A_eq,
    b_eq,
    bounds,
    integrality=None,
    mip_start=None,
):
    if highspy is None:
        raise RuntimeError("highspy is required but not installed")

    n_vars = len(objective)
    col_lower = np.array(
        [-highspy.kHighsInf if lb is None else float(lb) for lb, _ in bounds],
        dtype=np.float64,
    )
    col_upper = np.array(
        [highspy.kHighsInf if ub is None else float(ub) for _, ub in bounds],
        dtype=np.float64,
    )

    ub_rows = 0 if A_ub is None else len(A_ub)
    eq_rows = 0 if A_eq is None else len(A_eq)
    total_rows = ub_rows + eq_rows
    sparse_rows = (
        total_rows > 0
        and isinstance((A_ub if ub_rows else A_eq)[0], _SparseRow)
    )

    if sparse_rows:
        a_all = None
        ub_bounds = (
            np.asarray(b_ub, dtype=np.float64)
            if ub_rows
            else np.zeros(0, dtype=np.float64)
        )
        eq_bounds = (
            np.asarray(b_eq, dtype=np.float64)
            if eq_rows
            else np.zeros(0, dtype=np.float64)
        )
        row_lower = np.concatenate(
            (
                np.full(ub_rows, -highspy.kHighsInf, dtype=np.float64),
                eq_bounds,
            )
        )
        row_upper = np.concatenate((ub_bounds, eq_bounds))
    elif total_rows == 0:
        a_all = np.zeros((0, n_vars), dtype=np.float64)
        row_lower = np.zeros(0, dtype=np.float64)
        row_upper = np.zeros(0, dtype=np.float64)
    elif ub_rows > 0 and eq_rows > 0:
        a_all = np.vstack((A_ub, A_eq))
        row_lower = np.concatenate(
            (
                np.full(ub_rows, -highspy.kHighsInf, dtype=np.float64),
                np.asarray(b_eq, dtype=np.float64),
            )
        )
        row_upper = np.concatenate(
            (
                np.asarray(b_ub, dtype=np.float64),
                np.asarray(b_eq, dtype=np.float64),
            )
        )
    elif ub_rows > 0:
        a_all = np.asarray(A_ub, dtype=np.float64)
        row_lower = np.full(ub_rows, -highspy.kHighsInf, dtype=np.float64)
        row_upper = np.asarray(b_ub, dtype=np.float64)
    else:
        a_all = np.asarray(A_eq, dtype=np.float64)
        row_lower = np.asarray(b_eq, dtype=np.float64)
        row_upper = np.asarray(b_eq, dtype=np.float64)

    start = np.zeros(n_vars + 1, dtype=np.int32)
    if sparse_rows:
        columns = [[] for _ in range(n_vars)]
        rows = [*(A_ub or []), *(A_eq or [])]
        for row_index, row in enumerate(rows):
            for col_index, value in row.values.items():
                columns[col_index].append((row_index, value))
        counts = np.fromiter((len(column) for column in columns), dtype=np.int32)
        np.cumsum(counts, out=start[1:], dtype=np.int32)
        index = np.fromiter(
            (row for column in columns for row, _ in column), dtype=np.int32
        )
        values = np.fromiter(
            (value for column in columns for _, value in column), dtype=np.float64
        )
    else:
        a_dense = np.asarray(a_all, dtype=np.float64)
        nz_rows, nz_cols = np.nonzero(np.abs(a_dense) > EPSILON)
        if nz_rows.size:
            order = np.argsort(nz_cols, kind="stable")
            np.cumsum(
                np.bincount(nz_cols, minlength=n_vars),
                out=start[1:],
                dtype=np.int32,
            )
            index = nz_rows[order].astype(np.int32, copy=False)
            values = a_dense[nz_rows[order], nz_cols[order]].astype(
                np.float64, copy=False
            )
        else:
            index = np.zeros(0, dtype=np.int32)
            values = np.zeros(0, dtype=np.float64)

    lp = highspy.HighsLp()
    lp.num_col_ = int(n_vars)
    lp.num_row_ = int(total_rows)
    lp.col_cost_ = np.asarray(objective, dtype=np.float64)
    lp.col_lower_ = col_lower
    lp.col_upper_ = col_upper
    lp.row_lower_ = row_lower
    lp.row_upper_ = row_upper

    lp.a_matrix_.num_col_ = int(n_vars)
    lp.a_matrix_.num_row_ = int(total_rows)
    lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
    lp.a_matrix_.start_ = start
    lp.a_matrix_.index_ = index
    lp.a_matrix_.value_ = values
    if integrality is not None:
        lp.integrality_ = integrality

    highs = highspy.Highs()
    highs.setOptionValue("output_flag", False)
    highs.passModel(lp)
    mip_start_status = None
    if mip_start is not None:
        start_indices, start_values = mip_start
        highs.setOptionValue("mip_max_start_nodes", 10)
        mip_start_status = highs.setSolution(
            len(start_indices),
            np.asarray(start_indices, dtype=np.int32),
            np.asarray(start_values, dtype=np.float64),
        )
        if mip_start_status != highspy.HighsStatus.kOk:
            highs = highspy.Highs()
            highs.setOptionValue("output_flag", False)
            highs.passModel(lp)
    highs.run()
    model_status = highs.getModelStatus()
    info = highs.getInfo()

    class _HighspyResult:
        def __init__(self, success, x, objective_value=None):
            self.success = success
            self.x = x
            self.objective_value = objective_value
            self.mip_start_status = mip_start_status
            self.mip_node_count = int(info.mip_node_count)
            self.mip_dual_bound = float(info.mip_dual_bound)
            self.mip_gap = float(info.mip_gap)
            self.solver_runtime = float(highs.getRunTime())
            self.num_variables = int(n_vars)
            self.num_integer_variables = int(
                sum(
                    value != highspy.HighsVarType.kContinuous
                    for value in (integrality or [])
                )
            )
            self.num_rows = int(total_rows)
            self.num_nonzeros = int(values.size)

    if model_status != highspy.HighsModelStatus.kOptimal:
        return _HighspyResult(False, None)

    solution = highs.getSolution()
    if not solution.value_valid:
        return _HighspyResult(False, None)

    return _HighspyResult(
        True,
        np.asarray(solution.col_value, dtype=np.float64),
        float(highs.getObjectiveValue()),
    )


class _IndexBuilder:
    def __init__(self):
        self.offset = 0

    def add(self, count):
        start = self.offset
        self.offset += count
        return slice(start, self.offset)


def _extract_mip_start(
    x, horizon, battery_vars, grid_import_mode, pv_surplus_mode
):
    return {
        "horizon": horizon,
        "battery": [
            {
                name: x[var[name]].copy() if var[name] is not None else None
                for name in ("charge_mode", "charge_active", "discharge_active")
            }
            for var in battery_vars
        ],
        "grid_import_mode": x[grid_import_mode].copy(),
        "pv_surplus_mode": x[pv_surplus_mode].copy(),
    }


def _shift_mip_start(
    mip_start,
    horizon,
    battery_vars,
    grid_import_mode,
    pv_surplus_mode,
    *,
    omit_first=False,
):
    if not isinstance(mip_start, dict):
        return None
    previous_horizon = mip_start.get("horizon")
    previous_battery = mip_start.get("battery")
    if (
        not isinstance(previous_horizon, int)
        or previous_horizon <= 1
        or not isinstance(previous_battery, list)
        or len(previous_battery) != len(battery_vars)
    ):
        return None
    overlap = min(horizon, previous_horizon - 1)
    if overlap <= 0:
        return None

    start_indices = []
    start_values = []
    for b, var in enumerate(battery_vars):
        if not isinstance(previous_battery[b], dict):
            return None
        for name in ("charge_mode", "charge_active", "discharge_active"):
            current = var[name]
            previous = previous_battery[b].get(name)
            if current is None or previous is None:
                continue
            previous = np.asarray(previous, dtype=np.float64)
            if previous.shape != (previous_horizon,):
                return None
            first = 1 if not omit_first else 2
            count = overlap if not omit_first else max(overlap - 1, 0)
            current_start = current.start + (1 if omit_first else 0)
            start_indices.extend(range(current_start, current_start + count))
            start_values.extend(previous[first : first + count])

    for name, current in (
        ("grid_import_mode", grid_import_mode),
        ("pv_surplus_mode", pv_surplus_mode),
    ):
        previous = np.asarray(mip_start.get(name), dtype=np.float64)
        if previous.shape != (previous_horizon,):
            return None
        first = 1 if not omit_first else 2
        count = overlap if not omit_first else max(overlap - 1, 0)
        current_start = current.start + (1 if omit_first else 0)
        start_indices.extend(range(current_start, current_start + count))
        start_values.extend(previous[first : first + count])

    if not start_indices:
        return None
    return start_indices, start_values


def _use_mip_starts(battery_entities, lookahead_slots):
    return int(lookahead_slots) >= MIP_START_MIN_LOOKAHEAD_SLOTS and any(
        float(entity.action_deadband_kwh) > EPSILON for entity in battery_entities
    )


def _solve_mpc_step(
    base_timeslot,
    prices_h,
    grid_export_prices_h,
    usage_h,
    solar_h,
    battery_entities,
    battery_levels_now,
    battery_states_now,
    forced_discharge_first=None,
    mip_start=None,
    return_mip_start=False,
):
    horizon = len(prices_h)
    num_battery = len(battery_entities)
    idx = _IndexBuilder()
    battery_vars = []
    for entity in battery_entities:
        has_deadband = float(entity.action_deadband_kwh) > 0.0
        battery_vars.append(
            {
                "charge_grid": idx.add(horizon),
                "charge_pv": idx.add(horizon),
                "discharge": idx.add(horizon),
                "charge_mode": idx.add(horizon),
                "charge_active": idx.add(horizon) if has_deadband else None,
                "discharge_active": idx.add(horizon) if has_deadband else None,
                "level": idx.add(horizon + 1),
                "min_slack": idx.add(horizon),
                "target_under": idx.add(1),
                "target_over": idx.add(1),
            }
        )

    grid_import = idx.add(horizon)
    grid_export = idx.add(horizon)
    grid_import_mode = idx.add(horizon)
    pv_surplus_mode = idx.add(horizon)
    n_vars = idx.offset

    bounds = [(0.0, None) for _ in range(n_vars)]
    integrality = [highspy.HighsVarType.kContinuous for _ in range(n_vars)]
    objective = np.zeros(n_vars, dtype=np.float64)
    max_grid_charge = sum(
        max(float(value) for value in entity.charge_curve_kwh)
        for entity in battery_entities
        if _charge_ingress_permissions(entity)[0]
    )

    for t in range(horizon):
        objective[grid_import.start + t] = float(prices_h[t])
        objective[grid_export.start + t] = -float(grid_export_prices_h[t])
        bounds[grid_import.start + t] = (
            0.0,
            float(usage_h[t]) + max_grid_charge,
        )
        bounds[grid_export.start + t] = (
            0.0,
            max(float(solar_h[t]), 0.0),
        )
        bounds[grid_import_mode.start + t] = (0.0, 1.0)
        bounds[pv_surplus_mode.start + t] = (0.0, 1.0)
        integrality[grid_import_mode.start + t] = highspy.HighsVarType.kInteger
        integrality[pv_surplus_mode.start + t] = highspy.HighsVarType.kInteger

    penalty_battery_min = 0.0
    penalty_battery_target = 5000.0

    A_eq = []
    b_eq = []
    A_ub = []
    b_ub = []

    for b, entity in enumerate(battery_entities):
        var = battery_vars[b]
        charge_limit, discharge_limit = _battery_power_limits(
            entity, battery_levels_now[b]
        )
        capacity = float(entity.capacity_kwh)
        charge_eff = float(entity.charge_efficiency)
        discharge_eff = float(entity.discharge_efficiency)
        can_charge_from_grid, can_charge_from_pv = _charge_ingress_permissions(entity)
        throughput_penalty = float(entity.throughput_cost_per_kwh)
        mode_switch_cost = float(entity.mode_switch_cost)
        action_deadband = float(entity.action_deadband_kwh)
        previous_state = int(battery_states_now[b]) if battery_states_now.size else 0

        for t in range(horizon):
            grid_upper = charge_limit if can_charge_from_grid else 0.0
            pv_upper = charge_limit if can_charge_from_pv else 0.0
            bounds[var["charge_grid"].start + t] = (0.0, grid_upper)
            bounds[var["charge_pv"].start + t] = (0.0, pv_upper)
            bounds[var["discharge"].start + t] = (0.0, discharge_limit)
            bounds[var["charge_mode"].start + t] = (0.0, 1.0)
            integrality[var["charge_mode"].start + t] = (
                highspy.HighsVarType.kInteger
            )
            if action_deadband > 0.0:
                bounds[var["charge_active"].start + t] = (0.0, 1.0)
                bounds[var["discharge_active"].start + t] = (0.0, 1.0)
                integrality[var["charge_active"].start + t] = (
                    highspy.HighsVarType.kInteger
                )
                integrality[var["discharge_active"].start + t] = (
                    highspy.HighsVarType.kInteger
                )
            # Keep minimum state-of-charge as a hard floor.
            bounds[var["min_slack"].start + t] = (0.0, 0.0)
            bounds[var["target_under"].start] = (0.0, None)
            bounds[var["target_over"].start] = (0.0, None)
            objective[var["charge_grid"].start + t] += throughput_penalty
            objective[var["charge_pv"].start + t] += throughput_penalty
            objective[var["discharge"].start + t] += throughput_penalty
            objective[var["min_slack"].start + t] += penalty_battery_min
            objective[var["target_under"].start] += penalty_battery_target
            objective[var["target_over"].start] += penalty_battery_target
            if t == 0 and mode_switch_cost > 0.0:
                if previous_state != 1:
                    objective[var["charge_grid"].start + t] += mode_switch_cost
                    objective[var["charge_pv"].start + t] += mode_switch_cost
                if previous_state != 2:
                    objective[var["discharge"].start + t] += mode_switch_cost

            row = _SparseRow()
            row[var["charge_grid"].start + t] = 1.0
            row[var["charge_pv"].start + t] = 1.0
            row[var["charge_mode"].start + t] = -charge_limit
            A_ub.append(row)
            b_ub.append(0.0)

            row = _SparseRow()
            row[var["discharge"].start + t] = 1.0
            row[var["charge_mode"].start + t] = discharge_limit
            A_ub.append(row)
            b_ub.append(discharge_limit)

            if action_deadband > 0.0:
                # Commands are neutral or large enough to survive application.
                row = _SparseRow()
                row[var["charge_grid"].start + t] = 1.0
                row[var["charge_pv"].start + t] = 1.0
                row[var["charge_active"].start + t] = -charge_limit
                A_ub.append(row)
                b_ub.append(0.0)

                row = _SparseRow()
                row[var["charge_grid"].start + t] = -1.0
                row[var["charge_pv"].start + t] = -1.0
                row[var["charge_active"].start + t] = action_deadband
                A_ub.append(row)
                b_ub.append(0.0)

                row = _SparseRow()
                row[var["discharge"].start + t] = 1.0
                row[var["discharge_active"].start + t] = -discharge_limit
                A_ub.append(row)
                b_ub.append(0.0)

                row = _SparseRow()
                row[var["discharge"].start + t] = -1.0
                row[var["discharge_active"].start + t] = action_deadband
                A_ub.append(row)
                b_ub.append(0.0)

        for t in range(horizon + 1):
            bounds[var["level"].start + t] = (0.0, capacity)

        row = _SparseRow()
        row[var["level"].start] = 1.0
        A_eq.append(row)
        b_eq.append(float(battery_levels_now[b]))

        for t in range(horizon):
            row = _SparseRow()
            row[var["level"].start + t + 1] = 1.0
            row[var["level"].start + t] = -1.0
            row[var["charge_grid"].start + t] = -charge_eff
            row[var["charge_pv"].start + t] = -charge_eff
            row[var["discharge"].start + t] = 1.0 / discharge_eff
            A_eq.append(row)
            b_eq.append(0.0)

            row = _SparseRow()
            row[var["level"].start + t + 1] = -1.0
            row[var["min_slack"].start + t] = -1.0
            A_ub.append(row)
            b_ub.append(-float(entity.minimum_kwh))

        target = _battery_target_bounds_kwh(entity)
        if target is not None:
            local_level_idx = target["timeslot"] - base_timeslot + 1
            if 1 <= local_level_idx <= horizon:
                if target["lower_kwh"] is not None:
                    row = _SparseRow()
                    row[var["level"].start + local_level_idx] = -1.0
                    row[var["target_under"].start] = -1.0
                    A_ub.append(row)
                    b_ub.append(-float(target["lower_kwh"]))
                if target["upper_kwh"] is not None:
                    row = _SparseRow()
                    row[var["level"].start + local_level_idx] = 1.0
                    row[var["target_over"].start] = -1.0
                    A_ub.append(row)
                    b_ub.append(float(target["upper_kwh"]))

        forced_discharge = (
            0.0
            if forced_discharge_first is None
            else float(forced_discharge_first.get(b, 0.0))
        )
        if forced_discharge > EPSILON and horizon > 0:
            row = _SparseRow()
            row[var["discharge"].start] = -1.0
            A_ub.append(row)
            b_ub.append(-forced_discharge)

    for t in range(horizon):
        import_upper = float(usage_h[t]) + max_grid_charge
        export_upper = max(float(solar_h[t]), 0.0)

        # Signed tariffs need explicit site direction rather than objective clamps.
        row = _SparseRow()
        row[grid_import.start + t] = 1.0
        row[grid_import_mode.start + t] = -import_upper
        A_ub.append(row)
        b_ub.append(0.0)

        row = _SparseRow()
        row[grid_export.start + t] = 1.0
        row[grid_import_mode.start + t] = export_upper
        A_ub.append(row)
        b_ub.append(export_upper)

        row = _SparseRow()
        row[grid_import.start + t] = -1.0
        row[grid_export.start + t] = 1.0

        for b in range(num_battery):
            row[battery_vars[b]["charge_grid"].start + t] += 1.0
            row[battery_vars[b]["charge_pv"].start + t] += 1.0
            row[battery_vars[b]["discharge"].start + t] -= 1.0

        A_eq.append(row)
        b_eq.append(float(solar_h[t]) - float(usage_h[t]))

        # Grid-labeled battery energy must be backed by actual grid import.
        row = _SparseRow()
        row[grid_import.start + t] = -1.0
        for b in range(num_battery):
            row[battery_vars[b]["charge_grid"].start + t] = 1.0
        A_ub.append(row)
        b_ub.append(0.0)

        # Battery discharge may serve modeled load, not charging or export loops.
        row = _SparseRow()
        for b in range(num_battery):
            row[battery_vars[b]["discharge"].start + t] = 1.0
        A_ub.append(row)
        b_ub.append(float(usage_h[t]))

        # PV charging and export share only surplus after household/comfort load.
        surplus_upper = max(float(solar_h[t]), 0.0)
        row = _SparseRow()
        row[grid_export.start + t] = 1.0
        for b in range(num_battery):
            row[battery_vars[b]["charge_pv"].start + t] = 1.0
        row[pv_surplus_mode.start + t] = -surplus_upper
        A_ub.append(row)
        b_ub.append(0.0)

        row = _SparseRow()
        row[grid_export.start + t] = 1.0
        for b in range(num_battery):
            row[battery_vars[b]["charge_pv"].start + t] = 1.0
        surplus_relaxation = float(usage_h[t])
        row[pv_surplus_mode.start + t] = surplus_relaxation
        A_ub.append(row)
        b_ub.append(
            float(solar_h[t]) - float(usage_h[t]) + surplus_relaxation
        )

    shifted_mip_start = _shift_mip_start(
        mip_start,
        horizon,
        battery_vars,
        grid_import_mode,
        pv_surplus_mode,
        omit_first=True,
    )
    result = _solve_lp(
        objective=objective,
        A_ub=A_ub or None,
        b_ub=np.asarray(b_ub, dtype=np.float64) if b_ub else None,
        A_eq=A_eq or None,
        b_eq=np.asarray(b_eq, dtype=np.float64) if b_eq else None,
        bounds=bounds,
        integrality=integrality,
        mip_start=shifted_mip_start,
    )

    if not result.success:
        return None

    x = result.x
    charge_grid_cmd = np.array(
        [x[battery_vars[b]["charge_grid"].start] for b in range(num_battery)],
        dtype=np.float64,
    )
    charge_pv_cmd = np.array(
        [x[battery_vars[b]["charge_pv"].start] for b in range(num_battery)],
        dtype=np.float64,
    )
    discharge_cmd = np.array(
        [x[battery_vars[b]["discharge"].start] for b in range(num_battery)],
        dtype=np.float64,
    )
    solve_result = {
        "charge": charge_grid_cmd + charge_pv_cmd,
        "charge_grid": charge_grid_cmd,
        "charge_pv": charge_pv_cmd,
        "discharge": discharge_cmd,
        "objective_value": float(result.objective_value),
    }
    if return_mip_start:
        solve_result["mip_start"] = _extract_mip_start(
            x, horizon, battery_vars, grid_import_mode, pv_surplus_mode
        )
    return solve_result


def _battery_available_discharge_kwh(entity: BatteryEntity, level: float) -> float:
    discharge_eff = float(entity.discharge_efficiency)
    return max(float(level) - float(entity.minimum_kwh), 0.0) * discharge_eff


def _battery_preserve_probe_kwh(entity: BatteryEntity, level: float) -> float:
    _, discharge_limit = _battery_power_limits(entity, level)
    available = _battery_available_discharge_kwh(entity, level)
    action_deadband = max(float(entity.action_deadband_kwh), EPSILON)
    requested_probe = max(action_deadband, PRESERVE_PROBE_MIN_KWH)
    return min(available, discharge_limit, requested_probe)


def _slot_modeled_load_kwh(
    timeslot: int,
    *,
    usage,
    comfort_entities,
    comfort_cmd,
) -> float:
    comfort_load = 0.0
    for i, comfort in enumerate(comfort_entities):
        if float(comfort_cmd[i]) >= 0.5:
            comfort_load += float(comfort.power_usage_kwh)
    return float(usage[timeslot]) + comfort_load


def _objective_is_worse(counterfactual_objective, base_objective) -> bool:
    tolerance = max(
        PRESERVE_OBJECTIVE_TOLERANCE,
        abs(float(base_objective)) * 1e-9,
    )
    return float(counterfactual_objective) > float(base_objective) + tolerance


def _apply_controls_step(
    t,
    controls,
    prices,
    grid_export_prices,
    solar_input,
    usage,
    battery_entities,
    comfort_entities,
    battery_levels,
    comfort_levels,
    comfort_off_streaks,
    total_steps,
    enforce_action_deadband=True,
):
    num_battery = len(battery_entities)
    num_comfort = len(comfort_entities)

    comfort_enabled = np.zeros(num_comfort, dtype=np.int32)
    comfort_energy = 0.0
    next_comfort_levels = comfort_levels.copy()
    next_comfort_off_streaks = comfort_off_streaks.copy()

    for i, entity in enumerate(comfort_entities):
        desired = float(controls["comfort_on"][i])
        remaining_on_slots = max(float(comfort_levels[i]), 0.0)
        off_streak_slots = max(float(comfort_off_streaks[i]), 0.0)
        enabled = desired >= 0.5 or off_streak_slots >= float(
            entity.max_consecutive_off_slots
        )
        comfort_enabled[i] = 1 if enabled else 0

        if enabled:
            # ON slot consumes one unit from the remaining daily ON requirement.
            next_comfort_levels[i] = max(remaining_on_slots - 1.0, 0.0)
            next_comfort_off_streaks[i] = 0.0
            comfort_energy += float(entity.power_usage_kwh)
        else:
            # OFF slot keeps requirement unchanged and extends OFF streak.
            next_comfort_levels[i] = remaining_on_slots
            next_comfort_off_streaks[i] = off_streak_slots + 1.0

    charge_amounts = np.zeros(num_battery, dtype=np.float64)
    charge_grid_amounts = np.zeros(num_battery, dtype=np.float64)
    charge_pv_amounts = np.zeros(num_battery, dtype=np.float64)
    discharge_requests = np.zeros(num_battery, dtype=np.float64)
    next_battery_levels = battery_levels.copy()
    modeled_demand = float(usage[t]) + comfort_energy
    pv_surplus_remaining = max(float(solar_input[t]) - modeled_demand, 0.0)

    for i, entity in enumerate(battery_entities):
        level = float(battery_levels[i])
        charge_limit, discharge_limit = _battery_power_limits(entity, level)
        charge_eff = float(entity.charge_efficiency)
        discharge_eff = float(entity.discharge_efficiency)
        action_deadband = float(entity.action_deadband_kwh)
        can_charge_from_grid, can_charge_from_pv = _charge_ingress_permissions(entity)

        requested_grid = max(
            float(controls.get("charge_grid", np.zeros(num_battery))[i]), 0.0
        )
        requested_pv = max(
            float(controls.get("charge_pv", np.zeros(num_battery))[i]), 0.0
        )
        requested_discharge = max(float(controls["discharge"][i]), 0.0)

        if not can_charge_from_grid:
            requested_grid = 0.0
        if not can_charge_from_pv:
            requested_pv = 0.0

        total_requested = requested_grid + requested_pv
        max_charge_input_by_capacity = (
            max(float(entity.capacity_kwh) - level, 0.0) / charge_eff
            if charge_eff > EPSILON
            else 0.0
        )
        max_charge = min(charge_limit, max_charge_input_by_capacity)
        if total_requested > EPSILON and max_charge > 0.0:
            scale = min(1.0, max_charge / total_requested)
            requested_grid *= scale
            requested_pv *= scale

            actual_pv = min(requested_pv, pv_surplus_remaining)
            pv_surplus_remaining = max(pv_surplus_remaining - actual_pv, 0.0)
            actual_grid = requested_grid

            if enforce_action_deadband and not _meets_action_deadband(
                actual_grid + actual_pv, action_deadband
            ):
                pv_surplus_remaining += actual_pv
                actual_grid = 0.0
                actual_pv = 0.0

            if (
                bool(entity.prefer_pv_surplus_charging)
                and can_charge_from_pv
                and pv_surplus_remaining > EPSILON
            ):
                extra_capacity = max(max_charge - (actual_grid + actual_pv), 0.0)
                extra_pv = min(extra_capacity, pv_surplus_remaining)
                if not enforce_action_deadband or _meets_action_deadband(
                    actual_grid + actual_pv + extra_pv, action_deadband
                ):
                    actual_pv += extra_pv
                    pv_surplus_remaining = max(pv_surplus_remaining - extra_pv, 0.0)

            charge_grid_amounts[i] = actual_grid
            charge_pv_amounts[i] = actual_pv
            charge_amounts[i] = actual_grid + actual_pv
        elif (
            bool(entity.prefer_pv_surplus_charging)
            and can_charge_from_pv
            and max_charge > 0.0
            and pv_surplus_remaining > EPSILON
        ):
            extra_pv = min(max_charge, pv_surplus_remaining)
            if not enforce_action_deadband or _meets_action_deadband(
                extra_pv, action_deadband
            ):
                charge_pv_amounts[i] = extra_pv
                charge_amounts[i] = extra_pv
                pv_surplus_remaining = max(pv_surplus_remaining - extra_pv, 0.0)

        min_level = float(entity.minimum_kwh)
        available_for_discharge = max(level - min_level, 0.0) * discharge_eff
        discharge_request = min(
            requested_discharge, discharge_limit, available_for_discharge
        )
        if not enforce_action_deadband or _meets_action_deadband(
            discharge_request, action_deadband
        ):
            discharge_requests[i] = discharge_request

    demand_before_discharge = (
        modeled_demand + float(np.sum(charge_amounts)) - float(solar_input[t])
    )

    total_discharge_request = float(np.sum(discharge_requests))
    if demand_before_discharge <= 0.0 or total_discharge_request <= 0.0:
        discharge_amounts = np.zeros(num_battery, dtype=np.float64)
    elif total_discharge_request > demand_before_discharge:
        scale = demand_before_discharge / total_discharge_request
        discharge_amounts = discharge_requests * scale
    else:
        discharge_amounts = discharge_requests

    for i, entity in enumerate(battery_entities):
        if enforce_action_deadband and not _meets_action_deadband(
            discharge_amounts[i], float(entity.action_deadband_kwh)
        ):
            discharge_amounts[i] = 0.0

    battery_states = np.zeros(num_battery, dtype=np.int32)
    for i, entity in enumerate(battery_entities):
        charge_eff = float(entity.charge_efficiency)
        discharge_eff = float(entity.discharge_efficiency)
        delta = charge_amounts[i] * charge_eff - (
            discharge_amounts[i] / discharge_eff if discharge_eff > EPSILON else 0.0
        )
        level = float(battery_levels[i])
        next_level = np.clip(level + delta, 0.0, float(entity.capacity_kwh))
        next_battery_levels[i] = next_level

        if delta > EPSILON:
            battery_states[i] = 1
        elif delta < -EPSILON:
            battery_states[i] = 2
        else:
            battery_states[i] = 0

    return (
        next_battery_levels,
        next_comfort_levels,
        next_comfort_off_streaks,
        battery_states,
        comfort_enabled,
        charge_grid_amounts,
        charge_pv_amounts,
        discharge_amounts,
        max(
            float(solar_input[t])
            - (modeled_demand + float(np.sum(charge_pv_amounts))),
            0.0,
        ),
    )


def _fixed_policy_controls_step(
    t,
    *,
    battery_policies,
    comfort_cmd,
    usage,
    solar_input,
    battery_entities,
    comfort_entities,
    battery_levels,
    comfort_off_streaks,
):
    """Translate named battery policies into controls for current slot physics."""
    num_battery = len(battery_entities)
    actual_comfort = np.asarray(comfort_cmd, dtype=np.float64).copy()
    for i, entity in enumerate(comfort_entities):
        if float(comfort_off_streaks[i]) >= float(entity.max_consecutive_off_slots):
            actual_comfort[i] = 1.0

    modeled_demand = _slot_modeled_load_kwh(
        t,
        usage=usage,
        comfort_entities=comfort_entities,
        comfort_cmd=actual_comfort,
    )
    remaining_surplus = max(float(solar_input[t]) - modeled_demand, 0.0)
    remaining_deficit = max(modeled_demand - float(solar_input[t]), 0.0)
    charge_grid = np.zeros(num_battery, dtype=np.float64)
    charge_pv = np.zeros(num_battery, dtype=np.float64)
    discharge = np.zeros(num_battery, dtype=np.float64)
    charge_limits = np.zeros(num_battery, dtype=np.float64)

    # PV is finite, so preserve the runtime's stable input-order allocation.
    for i, entity in enumerate(battery_entities):
        level = float(battery_levels[i])
        charge_limit, _ = _battery_power_limits(entity, level)
        charge_limits[i] = charge_limit
        _, can_charge_from_pv = _charge_ingress_permissions(entity)
        if not can_charge_from_pv or remaining_surplus <= EPSILON:
            continue
        charge_eff = float(entity.charge_efficiency)
        capacity_input = (
            max(float(entity.capacity_kwh) - level, 0.0) / charge_eff
            if charge_eff > EPSILON
            else 0.0
        )
        charge_pv[i] = min(charge_limit, capacity_input, remaining_surplus)
        remaining_surplus = max(remaining_surplus - charge_pv[i], 0.0)

    discharge_requests = np.zeros(num_battery, dtype=np.float64)
    if remaining_deficit > EPSILON:
        for i, entity in enumerate(battery_entities):
            if battery_policies[i] != "self_consume":
                continue
            _, discharge_limit = _battery_power_limits(
                entity, float(battery_levels[i])
            )
            discharge_requests[i] = min(
                discharge_limit,
                _battery_available_discharge_kwh(entity, float(battery_levels[i])),
            )
        total_requested = float(np.sum(discharge_requests))
        if total_requested > EPSILON:
            discharge = discharge_requests * min(
                1.0, remaining_deficit / total_requested
            )

    for i, entity in enumerate(battery_entities):
        if battery_policies[i] != "grid_charge":
            continue
        can_charge_from_grid, _ = _charge_ingress_permissions(entity)
        if not can_charge_from_grid:
            continue
        charge_eff = float(entity.charge_efficiency)
        capacity_after_pv = float(battery_levels[i]) + charge_pv[i] * charge_eff
        capacity_input = (
            max(float(entity.capacity_kwh) - capacity_after_pv, 0.0) / charge_eff
            if charge_eff > EPSILON
            else 0.0
        )
        charge_grid[i] = min(
            max(charge_limits[i] - charge_pv[i], 0.0), capacity_input
        )

    return {
        "charge": charge_grid + charge_pv,
        "charge_grid": charge_grid,
        "charge_pv": charge_pv,
        "discharge": discharge,
        "comfort_on": actual_comfort,
    }


def _run_mpc(
    prices,
    grid_export_prices,
    solar_input,
    usage,
    battery_entities,
    comfort_entities,
    rolling_window_slots,
    reuse_plan,
    lookahead_slots,
    infer_battery_preserve_policy,
    policy_tail_start=None,
    battery_policy_override=None,
    fixed_comfort_override=None,
):
    num_battery = len(battery_entities)
    num_comfort = len(comfort_entities)
    total_steps = len(prices)

    battery_levels = np.zeros((num_battery, total_steps + 1), dtype=np.float64)
    comfort_levels = np.zeros((num_comfort, total_steps + 1), dtype=np.float64)
    comfort_off_streaks = np.zeros((num_comfort, total_steps + 1), dtype=np.float64)
    battery_states = np.zeros((num_battery, total_steps), dtype=np.int32)
    comfort_enabled = np.zeros((num_comfort, total_steps), dtype=np.int32)
    battery_charge = np.zeros((num_battery, total_steps), dtype=np.float64)
    battery_charge_grid = np.zeros((num_battery, total_steps), dtype=np.float64)
    battery_charge_pv = np.zeros((num_battery, total_steps), dtype=np.float64)
    battery_discharge = np.zeros((num_battery, total_steps), dtype=np.float64)
    battery_preserve = np.zeros((num_battery, total_steps), dtype=np.bool_)
    grid_export = np.zeros(total_steps, dtype=np.float64)
    comfort_on = np.zeros((num_comfort, total_steps), dtype=np.float64)
    comfort_lock_mode_series = np.zeros((num_comfort, total_steps), dtype=np.float64)
    comfort_lock_remaining_series = np.zeros(
        (num_comfort, total_steps), dtype=np.float64
    )
    history_slots = max(int(rolling_window_slots) - 1, 0)
    comfort_history = np.zeros(
        (num_comfort, total_steps + 1, history_slots), dtype=np.int32
    )

    for i, entity in enumerate(battery_entities):
        battery_levels[i, 0] = float(entity.initial_kwh)
    for i, entity in enumerate(comfort_entities):
        initial_history = _initial_comfort_history(entity, rolling_window_slots)
        comfort_history[i, 0] = initial_history
        comfort_levels[i, 0] = _comfort_window_deficit(
            initial_history,
            entity.target_on_slots_per_rolling_window,
        )
        if bool(entity.is_on_now):
            comfort_off_streaks[i, 0] = 0.0
        else:
            comfort_off_streaks[i, 0] = float(entity.off_streak_slots_now)

    successful_solves = 0
    reused_steps = int(reuse_plan["overlap_steps"]) if reuse_plan is not None else 0
    tail_start = (
        int(policy_tail_start) if policy_tail_start is not None else total_steps
    )
    policy_reused_tail_steps = int(
        reuse_plan.get("policy_reused_tail_steps", 0)
        if reuse_plan is not None
        else 0
    )
    comfort_lock_mode = np.zeros(num_comfort, dtype=np.int32)
    comfort_lock_remaining = np.zeros(num_comfort, dtype=np.int32)
    for i, entity in enumerate(comfort_entities):
        if bool(entity.is_on_now):
            comfort_lock_mode[i] = 1
            comfort_lock_remaining[i] = 0
        else:
            comfort_lock_mode[i] = 0
            comfort_lock_remaining[i] = max(
                int(entity.min_consecutive_off_slots)
                - int(entity.off_streak_slots_now),
                0,
            )
    if reuse_plan is not None:
        prior_modes = reuse_plan.get("initial_comfort_lock_mode")
        prior_remaining = reuse_plan.get("initial_comfort_lock_remaining")
        if prior_modes is not None and prior_remaining is not None:
            for i, entity in enumerate(comfort_entities):
                actual_mode = 1 if entity.is_on_now else 0
                if int(prior_modes[i]) != actual_mode:
                    continue
                comfort_lock_mode[i] = actual_mode
                comfort_lock_remaining[i] = max(
                    int(comfort_lock_remaining[i]),
                    int(prior_remaining[i]),
                    0,
                )

    if fixed_comfort_override is None:
        fixed_comfort_on = _fixed_comfort_schedule(
            comfort_entities,
            rolling_window_slots,
            total_steps,
            comfort_lock_mode,
            comfort_lock_remaining,
        )
    else:
        fixed_comfort_on = np.asarray(
            fixed_comfort_override, dtype=np.float64
        ).copy()
        if fixed_comfort_on.shape != (num_comfort, total_steps):
            raise ValueError("fixed comfort schedule shape mismatch")
        if not np.all((fixed_comfort_on == 0.0) | (fixed_comfort_on == 1.0)):
            raise ValueError("fixed comfort schedules must contain only 0/1 values")
    if reused_steps and num_comfort and not np.array_equal(
        fixed_comfort_on[:, :reused_steps],
        reuse_plan["comfort_on"][:, :reused_steps],
    ):
        # For example, shortening a legacy untimed request can move a terminal
        # comfort run. Old battery controls were solved for different demand.
        reused_steps = 0
    comfort_usage = np.sum(
        fixed_comfort_on * np.asarray(
            [entity.power_usage_kwh for entity in comfort_entities], dtype=np.float64
        )[:, None],
        axis=0,
    )
    initial_comfort_lock_mode = comfort_lock_mode.copy()
    initial_comfort_lock_remaining = comfort_lock_remaining.copy()
    use_mip_starts = policy_tail_start is None and _use_mip_starts(
        battery_entities, min(lookahead_slots, total_steps)
    )
    primary_mip_start = None

    for t in range(total_steps):
        horizon = min(lookahead_slots, total_steps - t)

        replay_policy_tail = t >= tail_start
        if replay_policy_tail:
            comfort_cmd = fixed_comfort_on[:, t].copy()
            controls = _fixed_policy_controls_step(
                t,
                battery_policies=[row[t] for row in battery_policy_override],
                comfort_cmd=comfort_cmd,
                usage=usage,
                solar_input=solar_input,
                battery_entities=battery_entities,
                comfort_entities=comfort_entities,
                battery_levels=battery_levels[:, t],
                comfort_off_streaks=comfort_off_streaks[:, t],
            )
            if infer_battery_preserve_policy:
                battery_preserve[:, t] = np.asarray(
                    [row[t] == "preserve" for row in battery_policy_override],
                    dtype=np.bool_,
                )
        elif t < reused_steps:
            controls = {
                "charge": reuse_plan["battery_charge"][:, t].copy(),
                "charge_grid": reuse_plan["battery_charge_grid"][:, t].copy(),
                "charge_pv": reuse_plan["battery_charge_pv"][:, t].copy(),
                "discharge": reuse_plan["battery_discharge"][:, t].copy(),
                "comfort_on": fixed_comfort_on[:, t].copy(),
            }
            if infer_battery_preserve_policy:
                battery_preserve[:, t] = reuse_plan["battery_preserve"][:, t].astype(
                    np.bool_
                )
            if num_comfort > 0:
                comfort_lock_mode = reuse_plan["comfort_lock_mode"][:, t].astype(
                    np.int32
                )
                comfort_lock_remaining = reuse_plan["comfort_lock_remaining"][
                    :, t
                ].astype(np.int32)
        elif num_battery == 0:
            controls = {
                "charge": np.zeros(0, dtype=np.float64),
                "charge_grid": np.zeros(0, dtype=np.float64),
                "charge_pv": np.zeros(0, dtype=np.float64),
                "discharge": np.zeros(0, dtype=np.float64),
                "comfort_on": fixed_comfort_on[:, t].copy(),
            }
        else:
            usage_h = usage[t : t + horizon].astype(np.float64, copy=True)
            if num_comfort:
                usage_h += comfort_usage[t : t + horizon]

            solve_result = _solve_mpc_step(
                base_timeslot=t,
                prices_h=prices[t : t + horizon],
                grid_export_prices_h=grid_export_prices[t : t + horizon],
                usage_h=usage_h,
                solar_h=solar_input[t : t + horizon],
                battery_entities=battery_entities,
                battery_levels_now=battery_levels[:, t],
                battery_states_now=(
                    battery_states[:, t - 1]
                    if t > 0
                    else np.zeros(num_battery, dtype=np.int32)
                ),
                mip_start=primary_mip_start if use_mip_starts else None,
                return_mip_start=use_mip_starts,
            )
            if solve_result is None:
                raise RuntimeError("MPC solve failed for softened MILP model")
            successful_solves += 1
            if use_mip_starts:
                primary_mip_start = solve_result.get("mip_start")

            controls = {
                "charge": solve_result["charge"],
                "charge_grid": solve_result["charge_grid"],
                "charge_pv": solve_result["charge_pv"],
                "discharge": solve_result["discharge"],
                "comfort_on": fixed_comfort_on[:, t].copy(),
            }
            comfort_cmd = fixed_comfort_on[:, t].copy()
            if infer_battery_preserve_policy:
                modeled_load = _slot_modeled_load_kwh(
                    t,
                    usage=usage,
                    comfort_entities=comfort_entities,
                    comfort_cmd=comfort_cmd,
                )
                pv_surplus = max(float(solar_input[t]) - modeled_load, 0.0)
                for b, entity in enumerate(battery_entities):
                    action_deadband = max(float(entity.action_deadband_kwh), EPSILON)
                    if _meets_action_deadband(
                        float(solve_result["charge_grid"][b]), action_deadband
                    ):
                        continue
                    if _meets_action_deadband(
                        float(solve_result["discharge"][b]), action_deadband
                    ):
                        continue

                    available = _battery_available_discharge_kwh(
                        entity, float(battery_levels[b, t])
                    )
                    if (
                        float(entity.minimum_kwh) > EPSILON
                        and available <= action_deadband
                    ):
                        battery_preserve[b, t] = True
                        continue

                    probe_kwh = _battery_preserve_probe_kwh(
                        entity, float(battery_levels[b, t])
                    )
                    if probe_kwh <= action_deadband:
                        continue

                    # A preserve policy is about unexpected real load, not the
                    # forecast load already in the plan. If PV surplus exists, the
                    # marginal load would consume that PV first. Only the next
                    # probe-sized slice asks whether spending battery is worse than
                    # importing and preserving it for later value.
                    counterfactual_usage_h = usage_h.copy()
                    counterfactual_usage_h[0] += pv_surplus + probe_kwh
                    preserve_baseline_objective = (
                        float(solve_result["objective_value"])
                        + float(grid_export_prices[t]) * pv_surplus
                        + float(prices[t]) * probe_kwh
                    )
                    counterfactual = _solve_mpc_step(
                        base_timeslot=t,
                        prices_h=prices[t : t + horizon],
                        grid_export_prices_h=grid_export_prices[t : t + horizon],
                        usage_h=counterfactual_usage_h,
                        solar_h=solar_input[t : t + horizon],
                        battery_entities=battery_entities,
                        battery_levels_now=battery_levels[:, t],
                        battery_states_now=(
                            battery_states[:, t - 1]
                            if t > 0
                            else np.zeros(num_battery, dtype=np.int32)
                        ),
                        forced_discharge_first={b: probe_kwh},
                    )
                    if counterfactual is None or _objective_is_worse(
                        counterfactual["objective_value"],
                        preserve_baseline_objective,
                    ):
                        battery_preserve[b, t] = True

        if num_comfort > 0:
            comfort_lock_mode_series[:, t] = comfort_lock_mode.astype(np.float64)
            comfort_lock_remaining_series[:, t] = comfort_lock_remaining.astype(
                np.float64
            )

        (
            battery_levels[:, t + 1],
            comfort_levels[:, t + 1],
            comfort_off_streaks[:, t + 1],
            battery_states[:, t],
            comfort_enabled[:, t],
            battery_charge_grid[:, t],
            battery_charge_pv[:, t],
            battery_discharge[:, t],
            grid_export[t],
        ) = _apply_controls_step(
            t=t,
            controls=controls,
            prices=prices,
            grid_export_prices=grid_export_prices,
            solar_input=solar_input,
            usage=usage,
            battery_entities=battery_entities,
            comfort_entities=comfort_entities,
            battery_levels=battery_levels[:, t],
            comfort_levels=comfort_levels[:, t],
            comfort_off_streaks=comfort_off_streaks[:, t],
            total_steps=total_steps,
            enforce_action_deadband=not replay_policy_tail,
        )
        battery_charge[:, t] = battery_charge_grid[:, t] + battery_charge_pv[:, t]
        if num_comfort > 0:
            comfort_on[:, t] = comfort_enabled[:, t].astype(np.float64)

        for i, entity in enumerate(comfort_entities):
            if history_slots > 0:
                combined = np.append(
                    comfort_history[i, t], int(comfort_enabled[i, t])
                )
                comfort_history[i, t + 1] = combined[-history_slots:]
            comfort_levels[i, t + 1] = _comfort_window_deficit(
                comfort_history[i, t + 1],
                entity.target_on_slots_per_rolling_window,
            )

        for i, entity in enumerate(comfort_entities):
            prev_enabled = (
                int(1 if entity.is_on_now else 0)
                if t == 0
                else int(comfort_enabled[i, t - 1])
            )
            now_enabled = int(comfort_enabled[i, t])

            if comfort_lock_remaining[i] > 0:
                comfort_lock_remaining[i] -= 1

            if now_enabled != prev_enabled:
                if now_enabled == 1:
                    comfort_lock_mode[i] = 1
                    comfort_lock_remaining[i] = max(
                        int(entity.min_consecutive_on_slots) - 1, 0
                    )
                else:
                    comfort_lock_mode[i] = 0
                    comfort_lock_remaining[i] = max(
                        int(entity.min_consecutive_off_slots) - 1, 0
                    )
            elif comfort_lock_remaining[i] == 0:
                comfort_lock_mode[i] = now_enabled

    return {
        "battery_levels": battery_levels,
        "comfort_levels": comfort_levels,
        "comfort_off_streaks": comfort_off_streaks,
        "battery_states": battery_states,
        "comfort_enabled": comfort_enabled,
        "battery_charge": battery_charge,
        "battery_charge_grid": battery_charge_grid,
        "battery_charge_pv": battery_charge_pv,
        "battery_discharge": battery_discharge,
        "battery_preserve": battery_preserve,
        "grid_export": grid_export,
        "comfort_on": comfort_on,
        "comfort_history": comfort_history,
        "comfort_lock_mode": comfort_lock_mode_series,
        "comfort_lock_remaining": comfort_lock_remaining_series,
        "initial_comfort_lock_mode": initial_comfort_lock_mode,
        "initial_comfort_lock_remaining": initial_comfort_lock_remaining,
        "reused_steps": reused_steps + policy_reused_tail_steps,
        "successful_solves": successful_solves,
    }


def _score_schedule(
    prices,
    grid_export_prices,
    solar_input,
    usage,
    battery_entities,
    comfort_entities,
    battery_levels,
    comfort_levels,
    battery_charge,
    battery_discharge,
    battery_states,
    comfort_enabled,
    initial_comfort_lock_mode,
    initial_comfort_lock_remaining,
    rolling_window_slots,
):
    total_steps = len(prices)
    total_price = 0.0
    total_grid_kwh = 0.0
    total_export_kwh = 0.0
    projected_cost_per_slot = []
    penalty = 0.0
    reasons = set()

    for t in range(total_steps):
        penalty_weight = 600.0 + 400.0 * (t / max(1, total_steps - 1))
        total_usage = float(usage[t])

        for i, entity in enumerate(comfort_entities):
            if comfort_enabled[i, t] == 1:
                total_usage += float(entity.power_usage_kwh)

        for i, entity in enumerate(battery_entities):
            next_level = float(battery_levels[i, t + 1])
            total_usage += float(battery_charge[i, t]) - float(battery_discharge[i, t])

            if next_level < float(entity.minimum_kwh):
                missed = float(entity.minimum_kwh) - next_level
                denom = (
                    float(entity.initial_kwh) - float(entity.minimum_kwh)
                    if float(entity.initial_kwh) > float(entity.minimum_kwh)
                    else 1.0
                )
                penalty += penalty_weight * (missed / denom)
                reasons.add("battery_min_unmet")

        net_grid_import = max(total_usage - float(solar_input[t]), 0.0)
        net_grid_export = max(float(solar_input[t]) - total_usage, 0.0)
        slot_projected_cost = (
            float(prices[t]) * net_grid_import
            - float(grid_export_prices[t]) * net_grid_export
        )
        total_price += slot_projected_cost
        total_grid_kwh += net_grid_import
        total_export_kwh += net_grid_export
        projected_cost_per_slot.append(float(slot_projected_cost))

    for i, entity in enumerate(battery_entities):
        target = _battery_target_bounds_kwh(entity)
        if target is None:
            continue

        timeslot = target["timeslot"]
        if timeslot >= total_steps:
            continue

        level_at_target = float(battery_levels[i, timeslot + 1])
        if target["lower_kwh"] is not None and level_at_target < float(
            target["lower_kwh"]
        ):
            reasons.add("battery_target_unmet")
        if target["upper_kwh"] is not None and level_at_target > float(
            target["upper_kwh"]
        ):
            reasons.add("battery_target_unmet")

    for i, entity in enumerate(comfort_entities):
        minimum_run_unmet = False
        locked_slots = min(int(initial_comfort_lock_remaining[i]), total_steps)
        locked_mode = bool(initial_comfort_lock_mode[i])
        if any(bool(comfort_enabled[i, t]) != locked_mode for t in range(locked_slots)):
            minimum_run_unmet = True

        previous = bool(entity.is_on_now)
        for t in range(total_steps):
            enabled = bool(comfort_enabled[i, t])
            if enabled != previous:
                minimum = (
                    int(entity.min_consecutive_on_slots)
                    if enabled
                    else int(entity.min_consecutive_off_slots)
                )
                if t + minimum > total_steps or any(
                    bool(value) != enabled
                    for value in comfort_enabled[i, t : t + minimum]
                ):
                    minimum_run_unmet = True
            previous = enabled
        if minimum_run_unmet:
            penalty += 1000.0
            reasons.add("comfort_min_run_unmet")

        if entity.on_history is None:
            reasons.add("comfort_history_unavailable")
        combined = np.concatenate(
            (
                _initial_comfort_history(entity, rolling_window_slots),
                comfort_enabled[i].astype(np.int32),
            )
        )
        target_on = int(entity.target_on_slots_per_rolling_window)
        for end in range(int(rolling_window_slots) - 1, combined.shape[0]):
            window = combined[end - int(rolling_window_slots) + 1 : end + 1]
            deficit = _comfort_window_deficit(window, target_on)
            if deficit > EPSILON:
                penalty += 1000.0 * deficit
                reasons.add("comfort_target_unmet")

        streak = 0
        max_streak = 0
        for t in range(total_steps):
            if comfort_enabled[i, t] == 1:
                streak = 0
            else:
                streak += 1
                max_streak = max(max_streak, streak)
        if max_streak > int(entity.max_consecutive_off_slots):
            reasons.add("comfort_max_off_unmet")

    switch_penalty = 0.0
    for i in range(len(battery_entities)):
        switch_penalty += float(battery_entities[i].mode_switch_cost) * float(
            np.sum(np.diff(battery_states[i]) != 0)
        )
    for i in range(len(comfort_entities)):
        switch_penalty += 0.05 * float(np.sum(np.diff(comfort_enabled[i]) != 0))

    avg_price = (
        (total_price / total_grid_kwh)
        if total_grid_kwh > EPSILON
        else AVG_PRICE_SENTINEL
    )
    fitness = total_price + penalty + switch_penalty
    reason_list = sorted(reasons)
    return (
        float(fitness),
        float(avg_price),
        reason_list,
        float(total_price),
        projected_cost_per_slot,
    )


def _replay_policy_cost(
    grid_import_prices,
    grid_export_prices,
    usage,
    solar_input,
    battery_entities,
    battery_modes,
    comfort_enabled,
    comfort_entities,
    optional_usage,
):
    """Replay grid cost under fixed battery modes and comfort actions."""
    total_steps = len(usage)
    battery_levels = np.asarray(
        [float(entity.initial_kwh) for entity in battery_entities],
        dtype=np.float64,
    )
    total_cost = 0.0

    for t in range(total_steps):
        total_usage = float(usage[t])
        for i, entity in enumerate(comfort_entities):
            if comfort_enabled[i, t] == 1:
                total_usage += float(entity.power_usage_kwh)
        total_usage += float(optional_usage[t])

        remaining_deficit = max(total_usage - float(solar_input[t]), 0.0)
        remaining_surplus = max(float(solar_input[t]) - total_usage, 0.0)
        charge_limits = np.zeros(len(battery_entities), dtype=np.float64)
        pv_charge = np.zeros(len(battery_entities), dtype=np.float64)

        # Preserve the runtime's stable input-order allocation of finite PV.
        for i, entity in enumerate(battery_entities):
            charge_limit, _ = _battery_power_limits(entity, battery_levels[i])
            charge_limits[i] = charge_limit
            _, can_charge_from_pv = _charge_ingress_permissions(entity)
            if not can_charge_from_pv or remaining_surplus <= EPSILON:
                continue
            capacity_input = max(
                float(entity.capacity_kwh) - battery_levels[i], 0.0
            ) / float(entity.charge_efficiency)
            pv_charge[i] = min(charge_limit, capacity_input, remaining_surplus)
            battery_levels[i] += pv_charge[i] * float(entity.charge_efficiency)
            battery_levels[i] = min(
                battery_levels[i], float(entity.capacity_kwh)
            )
            remaining_surplus -= pv_charge[i]

        discharge_requests = np.zeros(len(battery_entities), dtype=np.float64)
        if remaining_deficit > EPSILON:
            for i, entity in enumerate(battery_entities):
                if battery_modes[i][t] != "self_consume":
                    continue
                _, discharge_limit = _battery_power_limits(entity, battery_levels[i])
                available = (
                    max(battery_levels[i] - float(entity.minimum_kwh), 0.0)
                    * float(entity.discharge_efficiency)
                )
                discharge_requests[i] = min(discharge_limit, available)

            requested_discharge = float(np.sum(discharge_requests))
            if requested_discharge > EPSILON:
                # Share a limited deficit proportionally across eligible batteries.
                scale = min(1.0, remaining_deficit / requested_discharge)
                discharge = discharge_requests * scale
                remaining_deficit = max(
                    remaining_deficit - float(np.sum(discharge)), 0.0
                )
                for i, entity in enumerate(battery_entities):
                    if discharge[i] <= 0.0:
                        continue
                    battery_levels[i] -= discharge[i] / float(
                        entity.discharge_efficiency
                    )
                    battery_levels[i] = max(
                        battery_levels[i], float(entity.minimum_kwh)
                    )

        grid_charge = 0.0
        for i, entity in enumerate(battery_entities):
            if battery_modes[i][t] != "grid_charge":
                continue
            can_charge_from_grid, _ = _charge_ingress_permissions(entity)
            if not can_charge_from_grid:
                continue
            capacity_input = max(
                float(entity.capacity_kwh) - battery_levels[i], 0.0
            ) / float(entity.charge_efficiency)
            charge_input = min(
                max(charge_limits[i] - pv_charge[i], 0.0),
                capacity_input,
            )
            battery_levels[i] += charge_input * float(entity.charge_efficiency)
            battery_levels[i] = min(
                battery_levels[i], float(entity.capacity_kwh)
            )
            grid_charge += charge_input

        total_cost += (
            float(grid_import_prices[t]) * (remaining_deficit + grid_charge)
            - float(grid_export_prices[t]) * remaining_surplus
        )

    return float(total_cost)


def _direct_comfort_cost(
    grid_import_prices,
    grid_export_prices,
    usage,
    solar_input,
    comfort_entities,
    comfort_schedules,
):
    comfort_usage = np.zeros(len(usage), dtype=np.float64)
    for entity, schedule in zip(
        comfort_entities, comfort_schedules, strict=True
    ):
        comfort_usage += float(entity.power_usage_kwh) * np.asarray(
            schedule, dtype=np.float64
        )
    net = usage + comfort_usage - solar_input
    return float(
        np.sum(
            grid_import_prices * np.maximum(net, 0.0)
            - grid_export_prices * np.maximum(-net, 0.0)
        )
    )


def _effective_battery_policy_states(
    result,
    battery_entities,
    total_steps,
    battery_policy_override,
):
    states = []
    for i, entity in enumerate(battery_entities):
        row = []
        for t in range(total_steps):
            if (
                battery_policy_override is not None
                and battery_policy_override[i][t] is not None
            ):
                row.append(str(battery_policy_override[i][t]))
            else:
                row.append(_battery_schedule_state(result, entity, i, t))
        states.append(row)
    return states


def _score_result(
    result,
    *,
    grid_import_prices,
    grid_export_prices,
    solar_input,
    usage,
    battery_entities,
    comfort_entities,
    rolling_window_slots,
):
    return _score_schedule(
        prices=grid_import_prices,
        grid_export_prices=grid_export_prices,
        solar_input=solar_input,
        usage=usage,
        battery_entities=battery_entities,
        comfort_entities=comfort_entities,
        battery_levels=result["battery_levels"],
        comfort_levels=result["comfort_levels"],
        battery_charge=result["battery_charge"],
        battery_discharge=result["battery_discharge"],
        battery_states=result["battery_states"],
        comfort_enabled=result["comfort_enabled"],
        initial_comfort_lock_mode=result["initial_comfort_lock_mode"],
        initial_comfort_lock_remaining=result["initial_comfort_lock_remaining"],
        rolling_window_slots=rolling_window_slots,
    )


def _comfort_placement_inputs(
    comfort_entities,
    rolling_window_slots,
    result,
):
    return tuple(
        ComfortPlacementInput(
            name=entity.name,
            schedule=tuple(bool(value) for value in result["comfort_enabled"][i]),
            on_history=tuple(
                bool(value)
                for value in _initial_comfort_history(entity, rolling_window_slots)
            ),
            rolling_window_slots=int(rolling_window_slots),
            target_on_slots_per_rolling_window=int(
                entity.target_on_slots_per_rolling_window
            ),
            min_consecutive_on_slots=int(entity.min_consecutive_on_slots),
            min_consecutive_off_slots=int(entity.min_consecutive_off_slots),
            max_consecutive_off_slots=int(entity.max_consecutive_off_slots),
            is_on_now=bool(entity.is_on_now),
            off_streak_slots_now=(
                0 if entity.is_on_now else int(entity.off_streak_slots_now)
            ),
            lock_remaining=int(result["initial_comfort_lock_remaining"][i]),
            lock_mode=bool(result["initial_comfort_lock_mode"][i]),
        )
        for i, entity in enumerate(comfort_entities)
    )


def _comfort_replan_reuse(reuse_plan):
    if reuse_plan is None:
        return None
    clean = {
        "overlap_steps": 0,
        "initial_comfort_lock_mode": reuse_plan.get(
            "initial_comfort_lock_mode"
        ),
        "initial_comfort_lock_remaining": reuse_plan.get(
            "initial_comfort_lock_remaining"
        ),
    }
    if "policy_reused_tail_steps" in reuse_plan:
        clean["policy_reused_tail_steps"] = reuse_plan["policy_reused_tail_steps"]
    return clean


def _invalid_final_reasons(reasons):
    hard = {
        "battery_min_unmet",
        "battery_target_unmet",
        "comfort_min_run_unmet",
        "comfort_target_unmet",
        "comfort_max_off_unmet",
    }
    return sorted(hard.intersection(reasons))


def _baseline_cost_per_slot(grid_import_prices, grid_export_prices, usage, solar_input):
    net_import = np.maximum(usage - solar_input, 0.0)
    net_export = np.maximum(solar_input - usage, 0.0)
    return grid_import_prices * net_import - grid_export_prices * net_export


def _optional_entity_options(
    entity,
    grid_import_prices,
    grid_export_prices,
    usage,
    solar_input,
    battery_entities,
    battery_modes,
    comfort_enabled,
    comfort_entities,
    baseline_policy_cost,
):
    duration = entity.duration_timeslots
    profile = entity.energy_profile
    total_steps = len(grid_import_prices)
    candidates = []
    for start_timeslot in range(entity.start_min, entity.start_max + 1):
        optional_usage = np.zeros(total_steps, dtype=np.float64)
        optional_usage[start_timeslot : start_timeslot + duration] = profile
        loaded_cost = _replay_policy_cost(
            grid_import_prices=grid_import_prices,
            grid_export_prices=grid_export_prices,
            usage=usage,
            solar_input=solar_input,
            battery_entities=battery_entities,
            battery_modes=battery_modes,
            comfort_enabled=comfort_enabled,
            comfort_entities=comfort_entities,
            optional_usage=optional_usage,
        )
        incremental_cost = float(loaded_cost - baseline_policy_cost)
        candidates.append((start_timeslot, incremental_cost))

    candidates.sort(key=lambda item: (item[1], item[0]))

    selected = []
    requested = entity.options
    required_gap = entity.required_gap

    for start_timeslot, incremental_cost in candidates:
        if len(selected) >= requested:
            break
        if any(
            abs(start_timeslot - prev_start) < required_gap
            for prev_start, _ in selected
        ):
            continue
        selected.append((start_timeslot, incremental_cost))

    best = min(cost for _, cost in selected)
    return [
        {
            "start_timeslot": int(start_timeslot),
            "end_timeslot": int(start_timeslot + duration),
            "incremental_cost": float(incremental_cost),
            "delta_from_best": float(incremental_cost - best),
        }
        for start_timeslot, incremental_cost in selected
    ]


def _battery_schedule_state(
    result,
    entity: BatteryEntity,
    battery_index: int,
    timeslot: int,
) -> str:
    """Return the serialized battery policy state for one schedule slot."""
    action_deadband = float(entity.action_deadband_kwh)
    if _meets_action_deadband(
        result["battery_charge_grid"][battery_index, timeslot], action_deadband
    ):
        return "grid_charge"
    if bool(result["battery_preserve"][battery_index, timeslot]):
        return "preserve"
    return "self_consume"


def optimize_internal(
    normalized: CalculationInput,
    *,
    reuse_plan_override=_AUTO_REUSE,
    policy_tail_start=None,
    battery_policy_override=None,
    cadence_diagnostics=None,
):
    total_steps = normalized.total_steps
    grid_import_prices = normalized.grid_import_prices
    grid_export_prices = normalized.grid_export_prices
    solar_input = normalized.solar_input
    usage = normalized.usage
    battery_entities = normalized.battery_entities
    comfort_entities = normalized.comfort_entities
    optional_entities = normalized.optional_entities
    fingerprint = normalized.fingerprint
    previous_state = normalized.state
    if reuse_plan_override is _AUTO_REUSE:
        reuse_plan = _build_reuse_plan(
            previous_state=previous_state,
            grid_import_prices=grid_import_prices,
            grid_export_prices=grid_export_prices,
            solar_input=solar_input,
            usage=usage,
            total_steps=total_steps,
            battery_entities=battery_entities,
            comfort_entities=comfort_entities,
            rolling_window_slots=normalized.rolling_window_slots,
            expected_fingerprint=fingerprint,
        )
    else:
        reuse_plan = reuse_plan_override

    if policy_tail_start is not None:
        tail_start = int(policy_tail_start)
        if tail_start < 0 or tail_start > total_steps:
            raise ValueError("policy_tail_start must be within the planning horizon")
        if reuse_plan is None:
            raise ValueError("policy tail replay requires a reuse plan")
        if battery_policy_override is None or len(battery_policy_override) != len(
            battery_entities
        ):
            raise ValueError("battery policy override shape mismatch")
        valid_policies = {"grid_charge", "preserve", "self_consume"}
        for row in battery_policy_override:
            if len(row) != total_steps:
                raise ValueError("battery policy override shape mismatch")
            if any(row[t] not in valid_policies for t in range(tail_start, total_steps)):
                raise ValueError("battery policy override contains an invalid tail policy")

    start_time = time.time()
    baseline_result = _run_mpc(
        grid_import_prices,
        grid_export_prices,
        solar_input,
        usage,
        battery_entities,
        comfort_entities,
        normalized.rolling_window_slots,
        reuse_plan,
        normalized.lookahead_slots,
        normalized.infer_battery_preserve_policy,
        policy_tail_start,
        battery_policy_override,
    )
    baseline_score = _score_result(
        baseline_result,
        grid_import_prices=grid_import_prices,
        grid_export_prices=grid_export_prices,
        solar_input=solar_input,
        usage=usage,
        battery_entities=battery_entities,
        comfort_entities=comfort_entities,
        rolling_window_slots=normalized.rolling_window_slots,
    )
    result = baseline_result
    score = baseline_score
    total_successful_solves = int(baseline_result["successful_solves"])
    placement_diagnostics = None

    if comfort_entities:
        placement_inputs = _comfort_placement_inputs(
            comfort_entities,
            normalized.rolling_window_slots,
            baseline_result,
        )
        baseline_policies = _effective_battery_policy_states(
            baseline_result,
            battery_entities,
            total_steps,
            battery_policy_override,
        )
        cost_calls = 0
        placement_started = time.monotonic()

        if battery_entities:
            def evaluate_comfort_cost(schedules):
                nonlocal cost_calls
                cost_calls += 1
                return _replay_policy_cost(
                    grid_import_prices=grid_import_prices,
                    grid_export_prices=grid_export_prices,
                    usage=usage,
                    solar_input=solar_input,
                    battery_entities=battery_entities,
                    battery_modes=baseline_policies,
                    comfort_enabled=np.asarray(schedules, dtype=np.int32),
                    comfort_entities=comfort_entities,
                    optional_usage=np.zeros(total_steps, dtype=np.float64),
                )
            cost_mode = "fixed_policy_replay"
        else:
            def evaluate_comfort_cost(schedules):
                nonlocal cost_calls
                cost_calls += 1
                return _direct_comfort_cost(
                    grid_import_prices,
                    grid_export_prices,
                    usage,
                    solar_input,
                    comfort_entities,
                    schedules,
                )
            cost_mode = "direct_net_site"

        placement = place_comfort_schedules(
            placement_inputs,
            evaluate_comfort_cost,
            max_candidates_per_comfort=(
                COMFORT_PLACEMENT_MAX_CANDIDATES_PER_ENTITY
            ),
            max_total_candidates=COMFORT_PLACEMENT_MAX_TOTAL_CANDIDATES,
            minimum_improvement=EPSILON,
            should_cancel=lambda: (
                time.monotonic() - placement_started
                >= COMFORT_PLACEMENT_SEARCH_SECONDS
            ),
        )
        accepted = np.asarray(placement.schedules, dtype=np.float64)
        changed = not np.array_equal(
            accepted, baseline_result["comfort_enabled"]
        )
        fallback_reason = None
        additional_successful_solves = 0
        if changed:
            candidate_result = _run_mpc(
                grid_import_prices,
                grid_export_prices,
                solar_input,
                usage,
                battery_entities,
                comfort_entities,
                normalized.rolling_window_slots,
                _comfort_replan_reuse(reuse_plan),
                normalized.lookahead_slots,
                normalized.infer_battery_preserve_policy,
                policy_tail_start,
                battery_policy_override,
                fixed_comfort_override=accepted,
            )
            additional_successful_solves = int(
                candidate_result["successful_solves"]
            )
            total_successful_solves += additional_successful_solves
            candidate_score = _score_result(
                candidate_result,
                grid_import_prices=grid_import_prices,
                grid_export_prices=grid_export_prices,
                solar_input=solar_input,
                usage=usage,
                battery_entities=battery_entities,
                comfort_entities=comfort_entities,
                rolling_window_slots=normalized.rolling_window_slots,
            )
            invalid_reasons = _invalid_final_reasons(candidate_score[2])
            cost_tolerance = max(
                EPSILON,
                abs(float(baseline_score[3])) * 1e-9,
            )
            if not np.array_equal(candidate_result["comfort_enabled"], accepted):
                fallback_reason = "comfort_schedule_rebuilt_differently"
            elif invalid_reasons:
                fallback_reason = ",".join(invalid_reasons)
            elif float(candidate_score[3]) > float(baseline_score[3]) + cost_tolerance:
                fallback_reason = "final_cost_worsened"
            else:
                result = candidate_result
                score = candidate_score

        placement_diagnostics = {
            "status": placement.status,
            "accepted": bool(changed and fallback_reason is None),
            "cost_mode": cost_mode,
            "candidate_cost_calls": int(cost_calls),
            "candidates_considered": int(placement.candidates_considered),
            "candidates_evaluated": int(placement.candidates_evaluated),
            "candidates_rejected": int(placement.candidates_rejected),
            "accepted_moves": int(placement.accepted_moves),
            "demand_changed": bool(changed),
            "additional_planning_passes": int(changed),
            "additional_successful_solves": additional_successful_solves,
            "baseline_projected_cost": float(baseline_score[3]),
            "final_projected_cost": float(score[3]),
            "fallback_reason": fallback_reason,
            "optimality": placement.optimality,
            "violations": [
                [
                    {"code": violation.code, "slot": int(violation.slot)}
                    for violation in violations
                ]
                for violations in placement.violations
            ],
        }

    execution_time = time.time() - start_time
    fitness, avg_price, reasons, projected_cost, projected_cost_per_slot = score
    baseline_cost_array = _baseline_cost_per_slot(
        grid_import_prices=grid_import_prices,
        grid_export_prices=grid_export_prices,
        usage=usage,
        solar_input=solar_input,
    )
    baseline_cost = float(np.sum(baseline_cost_array))
    baseline_cost_per_slot = [float(v) for v in baseline_cost_array]
    per_slot = []
    for baseline_slot, projected_slot in zip(
        baseline_cost_per_slot, projected_cost_per_slot
    ):
        slot_savings_cost = baseline_slot - projected_slot
        slot_savings_pct = (
            (slot_savings_cost / baseline_slot) * 100.0
            if baseline_slot > EPSILON
            else 0.0
        )
        per_slot.append(
            {
                "baseline_cost": float(baseline_slot),
                "projected_cost": float(projected_slot),
                "projected_savings_cost": float(slot_savings_cost),
                "projected_savings_pct": float(slot_savings_pct),
            }
        )

    projected_savings_cost = baseline_cost - projected_cost
    projected_savings_pct = (
        (projected_savings_cost / baseline_cost) * 100.0
        if baseline_cost > EPSILON
        else 0.0
    )

    battery_policy_states = _effective_battery_policy_states(
        result,
        battery_entities,
        total_steps,
        battery_policy_override,
    )

    entities = []
    for i, entity in enumerate(battery_entities):
        entities.append(
            {
                "name": entity.name,
                "type": "battery",
                "schedule": [
                    {
                        "state": battery_policy_states[i][t],
                        "level": float(result["battery_levels"][i, t + 1]),
                    }
                    for t in range(total_steps)
                ],
            }
        )

    for i, entity in enumerate(comfort_entities):
        entities.append(
            {
                "name": entity.name,
                "type": "comfort",
                "schedule": [
                    {
                        "enabled": bool(result["comfort_enabled"][i, t]),
                        "level": float(result["comfort_levels"][i, t + 1]),
                    }
                    for t in range(total_steps)
                ],
            }
        )

    optional_entity_options = []
    if optional_entities:
        battery_modes = battery_policy_states
        baseline_policy_cost = _replay_policy_cost(
            grid_import_prices=grid_import_prices,
            grid_export_prices=grid_export_prices,
            usage=usage,
            solar_input=solar_input,
            battery_entities=battery_entities,
            battery_modes=battery_modes,
            comfort_enabled=result["comfort_enabled"],
            comfort_entities=comfort_entities,
            optional_usage=np.zeros(total_steps, dtype=np.float64),
        )
        for optional in optional_entities:
            optional_entity_options.append(
                {
                    "name": optional.name,
                    "options": _optional_entity_options(
                        entity=optional,
                        grid_import_prices=grid_import_prices,
                        grid_export_prices=grid_export_prices,
                        usage=usage,
                        solar_input=solar_input,
                        battery_entities=battery_entities,
                        battery_modes=battery_modes,
                        comfort_enabled=result["comfort_enabled"],
                        comfort_entities=comfort_entities,
                        baseline_policy_cost=baseline_policy_cost,
                    ),
                }
            )

    state_obj = {
        "v": 1,
        "num_steps": int(total_steps),
        "entity_fingerprint": fingerprint,
        "grid_import_price_per_kwh": grid_import_prices.tolist(),
        "grid_export_price_per_kwh": grid_export_prices.tolist(),
        "solar_input_kwh": solar_input.tolist(),
        "usage_kwh": usage.tolist(),
        "battery_levels": result["battery_levels"].tolist(),
        "battery_charge": result["battery_charge"].tolist(),
        "battery_charge_grid": result["battery_charge_grid"].tolist(),
        "battery_charge_pv": result["battery_charge_pv"].tolist(),
        "battery_discharge": result["battery_discharge"].tolist(),
        "battery_preserve": result["battery_preserve"].astype(bool).tolist(),
        "comfort_on": result["comfort_on"].tolist(),
        "comfort_history": result["comfort_history"].tolist(),
        "comfort_levels": result["comfort_levels"].tolist(),
        "comfort_off_streaks": result["comfort_off_streaks"].tolist(),
        "comfort_is_on": np.concatenate(
            (
                np.asarray(
                    [bool(entity.is_on_now) for entity in comfort_entities],
                    dtype=np.bool_,
                ).reshape(len(comfort_entities), 1),
                result["comfort_enabled"].astype(np.bool_),
            ),
            axis=1,
        ).tolist(),
        "comfort_lock_mode": result["comfort_lock_mode"].tolist(),
        "comfort_lock_remaining": result["comfort_lock_remaining"].tolist(),
    }

    response = {
        "execution_time": execution_time,
        "generations": int(total_steps),
        "fitness": float(fitness),
        "avg_price": float(avg_price),
        "projections": {
            "baseline_cost": float(baseline_cost),
            "projected_cost": float(projected_cost),
            "projected_savings_cost": float(projected_savings_cost),
            "projected_savings_pct": float(projected_savings_pct),
            "per_slot": per_slot,
        },
        "overconstrained": len(reasons) > 0,
        "suboptimal": len(reasons) > 0,
        "suboptimal_reasons": reasons,
        "problems": reasons,
        "reused_steps": int(result["reused_steps"]),
        "successful_solves": total_successful_solves,
        "entities": entities,
        "optional_entity_options": optional_entity_options,
        "state": encode_state_blob(state_obj),
    }
    if cadence_diagnostics is not None:
        response["cadence"] = cadence_diagnostics
    if placement_diagnostics is not None:
        response["comfort_placement"] = placement_diagnostics
    return response


def optimize(params: OptimizationParams):
    # Delay this import: the cadence controller delegates its solves to this module.
    from .prefix_planner import optimize as optimize_with_prefix

    return optimize_with_prefix(params)
