import time

import numpy as np

from .models import (
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


MPC_HORIZON = 22
EPSILON = 1e-6
AVG_PRICE_SENTINEL = 1000.0


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


def _charge_source_permissions(entity):
    flags = int(entity.can_charge_from)
    return (
        bool(flags & int(ChargeSource.GRID)),
        bool(flags & int(ChargeSource.PV)),
    )


def _build_reuse_plan(
    previous_state,
    grid_import_prices,
    grid_export_prices,
    solar_input,
    usage,
    total_steps,
    num_battery,
    num_comfort,
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
    battery_charge_grid = previous_state.battery_charge_grid
    battery_charge_pv = previous_state.battery_charge_pv
    battery_discharge = previous_state.battery_discharge
    comfort_on = previous_state.comfort_on
    comfort_lock_mode = previous_state.comfort_lock_mode
    comfort_lock_remaining = previous_state.comfort_lock_remaining

    if battery_charge.shape[0] != num_battery:
        return None
    if battery_discharge.shape[0] != num_battery:
        return None
    if battery_charge_grid.shape[0] != num_battery:
        return None
    if battery_charge_pv.shape[0] != num_battery:
        return None
    if comfort_on.shape[0] != num_comfort:
        return None
    if comfort_lock_mode.shape[0] != num_comfort:
        return None
    if comfort_lock_remaining.shape[0] != num_comfort:
        return None

    best_offset = None
    best_overlap = 0
    for offset_steps in range(old_steps):
        overlap_steps = min(total_steps, old_steps - offset_steps)
        if overlap_steps <= best_overlap:
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

        best_offset = offset_steps
        best_overlap = overlap_steps

    if best_offset is None or best_overlap <= 0:
        return None

    return {
        "overlap_steps": int(best_overlap),
        "battery_charge": battery_charge[:, best_offset : best_offset + best_overlap],
        "battery_charge_grid": battery_charge_grid[
            :, best_offset : best_offset + best_overlap
        ],
        "battery_charge_pv": battery_charge_pv[
            :, best_offset : best_offset + best_overlap
        ],
        "battery_discharge": battery_discharge[
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


def _solve_lp(objective, A_ub, b_ub, A_eq, b_eq, bounds, integrality=None):
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

    ub_rows = 0 if A_ub is None else int(A_ub.shape[0])
    eq_rows = 0 if A_eq is None else int(A_eq.shape[0])
    total_rows = ub_rows + eq_rows

    if total_rows == 0:
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

    a_dense = np.asarray(a_all, dtype=np.float64)
    start = np.zeros(n_vars + 1, dtype=np.int32)
    index_parts = []
    value_parts = []
    nnz = 0
    for col in range(n_vars):
        col_vals = a_dense[:, col]
        nz_rows = np.flatnonzero(np.abs(col_vals) > EPSILON)
        if nz_rows.size > 0:
            index_parts.append(nz_rows.astype(np.int32, copy=False))
            value_parts.append(col_vals[nz_rows].astype(np.float64, copy=False))
            nnz += int(nz_rows.size)
        start[col + 1] = nnz
    if index_parts:
        index = np.concatenate(index_parts)
        values = np.concatenate(value_parts)
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
    highs.run()
    model_status = highs.getModelStatus()

    class _HighspyResult:
        def __init__(self, success, x):
            self.success = success
            self.x = x

    if model_status != highspy.HighsModelStatus.kOptimal:
        return _HighspyResult(False, None)

    solution = highs.getSolution()
    if not solution.value_valid:
        return _HighspyResult(False, None)

    return _HighspyResult(
        True,
        np.asarray(solution.col_value, dtype=np.float64),
    )


class _IndexBuilder:
    def __init__(self):
        self.offset = 0

    def add(self, count):
        start = self.offset
        self.offset += count
        return slice(start, self.offset)


def _solve_mpc_step(
    base_timeslot,
    prices_h,
    grid_export_prices_h,
    usage_h,
    solar_h,
    battery_entities,
    comfort_entities,
    battery_levels_now,
    comfort_levels_now,
    prev_comfort_on,
    comfort_off_streaks_now,
    remaining_steps_total,
):
    horizon = len(prices_h)
    num_battery = len(battery_entities)
    num_comfort = len(comfort_entities)
    idx = _IndexBuilder()
    battery_vars = []
    for _ in range(num_battery):
        battery_vars.append(
            {
                "charge_grid": idx.add(horizon),
                "charge_pv": idx.add(horizon),
                "discharge": idx.add(horizon),
                "level": idx.add(horizon + 1),
                "min_slack": idx.add(horizon),
                "target_under": idx.add(1),
                "target_over": idx.add(1),
            }
        )

    comfort_vars = []
    for _ in range(num_comfort):
        comfort_vars.append(
            {
                "on": idx.add(horizon),
                "level": idx.add(horizon + 1),
                "switch_abs": idx.add(horizon),
                "target_slack": idx.add(1),
            }
        )

    grid_import = idx.add(horizon)
    grid_export = idx.add(horizon)
    n_vars = idx.offset

    bounds = [(0.0, None) for _ in range(n_vars)]
    integrality = [highspy.HighsVarType.kContinuous for _ in range(n_vars)]
    objective = np.zeros(n_vars, dtype=np.float64)

    for t in range(horizon):
        objective[grid_import.start + t] = max(float(prices_h[t]), 0.0)
        objective[grid_export.start + t] = -max(float(grid_export_prices_h[t]), 0.0)
        bounds[grid_export.start + t] = (
            0.0,
            max(float(solar_h[t]) - float(usage_h[t]), 0.0),
        )

    penalty_battery_min = 0.0
    penalty_battery_target = 5000.0
    penalty_comfort_target = 4000.0
    penalty_switch = 0.0
    throughput_penalty = 0.0

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
        can_charge_from_grid, can_charge_from_pv = _charge_source_permissions(entity)

        for t in range(horizon):
            grid_upper = charge_limit if can_charge_from_grid else 0.0
            pv_upper = charge_limit if can_charge_from_pv else 0.0
            bounds[var["charge_grid"].start + t] = (0.0, grid_upper)
            bounds[var["charge_pv"].start + t] = (0.0, pv_upper)
            bounds[var["discharge"].start + t] = (0.0, discharge_limit)
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

            row = np.zeros(n_vars, dtype=np.float64)
            row[var["charge_grid"].start + t] = 1.0
            row[var["charge_pv"].start + t] = 1.0
            A_ub.append(row)
            b_ub.append(charge_limit)

        for t in range(horizon + 1):
            bounds[var["level"].start + t] = (0.0, capacity)

        row = np.zeros(n_vars, dtype=np.float64)
        row[var["level"].start] = 1.0
        A_eq.append(row)
        b_eq.append(float(battery_levels_now[b]))

        for t in range(horizon):
            row = np.zeros(n_vars, dtype=np.float64)
            row[var["level"].start + t + 1] = 1.0
            row[var["level"].start + t] = -1.0
            row[var["charge_grid"].start + t] = -charge_eff
            row[var["charge_pv"].start + t] = -charge_eff
            row[var["discharge"].start + t] = 1.0 / discharge_eff
            A_eq.append(row)
            b_eq.append(0.0)

            row = np.zeros(n_vars, dtype=np.float64)
            row[var["level"].start + t + 1] = -1.0
            row[var["min_slack"].start + t] = -1.0
            A_ub.append(row)
            b_ub.append(-float(entity.minimum_kwh))

        target = _battery_target_bounds_kwh(entity)
        if target is not None:
            local_level_idx = target["timeslot"] - base_timeslot + 1
            if 1 <= local_level_idx <= horizon:
                if target["lower_kwh"] is not None:
                    row = np.zeros(n_vars, dtype=np.float64)
                    row[var["level"].start + local_level_idx] = -1.0
                    row[var["target_under"].start] = -1.0
                    A_ub.append(row)
                    b_ub.append(-float(target["lower_kwh"]))
                if target["upper_kwh"] is not None:
                    row = np.zeros(n_vars, dtype=np.float64)
                    row[var["level"].start + local_level_idx] = 1.0
                    row[var["target_over"].start] = -1.0
                    A_ub.append(row)
                    b_ub.append(float(target["upper_kwh"]))

    for c, entity in enumerate(comfort_entities):
        var = comfort_vars[c]
        max_off = int(entity.max_consecutive_off_slots)
        limit_window = max_off + 1
        remaining_now = float(comfort_levels_now[c])
        min_on_this_window = max(
            0.0,
            remaining_now - max(float(remaining_steps_total - horizon), 0.0),
        )

        for t in range(horizon):
            bounds[var["on"].start + t] = (0.0, 1.0)
            bounds[var["switch_abs"].start + t] = (0.0, None)
            integrality[var["on"].start + t] = highspy.HighsVarType.kInteger
            objective[var["switch_abs"].start + t] += penalty_switch

        bounds[var["target_slack"].start] = (0.0, None)
        objective[var["target_slack"].start] += penalty_comfort_target

        for t in range(horizon + 1):
            bounds[var["level"].start + t] = (-float(horizon + 24), None)

        row = np.zeros(n_vars, dtype=np.float64)
        row[var["level"].start] = 1.0
        A_eq.append(row)
        b_eq.append(float(comfort_levels_now[c]))

        for t in range(horizon):
            row = np.zeros(n_vars, dtype=np.float64)
            row[var["level"].start + t + 1] = 1.0
            row[var["level"].start + t] = -1.0
            row[var["on"].start + t] = 1.0
            A_eq.append(row)
            b_eq.append(0.0)

            if t == 0:
                row = np.zeros(n_vars, dtype=np.float64)
                row[var["on"].start] = 1.0
                row[var["switch_abs"].start] = -1.0
                A_ub.append(row)
                b_ub.append(float(prev_comfort_on[c]))

                row = np.zeros(n_vars, dtype=np.float64)
                row[var["on"].start] = -1.0
                row[var["switch_abs"].start] = -1.0
                A_ub.append(row)
                b_ub.append(-float(prev_comfort_on[c]))
            else:
                row = np.zeros(n_vars, dtype=np.float64)
                row[var["on"].start + t] = 1.0
                row[var["on"].start + t - 1] = -1.0
                row[var["switch_abs"].start + t] = -1.0
                A_ub.append(row)
                b_ub.append(0.0)

        row = np.zeros(n_vars, dtype=np.float64)
        for t in range(horizon):
            row[var["on"].start + t] = -1.0
        row[var["target_slack"].start] = -1.0
        A_ub.append(row)
        b_ub.append(-float(min_on_this_window))

        initial_window = max(
            1, limit_window - int(max(0.0, comfort_off_streaks_now[c]))
        )
        initial_window = min(initial_window, horizon)
        row = np.zeros(n_vars, dtype=np.float64)
        for t in range(initial_window):
            row[var["on"].start + t] = -1.0
        A_ub.append(row)
        b_ub.append(-1.0)

        if horizon >= limit_window:
            for start in range(0, horizon - limit_window + 1):
                row = np.zeros(n_vars, dtype=np.float64)
                for t in range(start, start + limit_window):
                    row[var["on"].start + t] = -1.0
                A_ub.append(row)
                b_ub.append(-1.0)

    for t in range(horizon):
        row = np.zeros(n_vars, dtype=np.float64)
        row[grid_import.start + t] = -1.0
        row[grid_export.start + t] = 1.0

        for b in range(num_battery):
            row[battery_vars[b]["charge_grid"].start + t] += 1.0
            row[battery_vars[b]["charge_pv"].start + t] += 1.0
            row[battery_vars[b]["discharge"].start + t] -= 1.0

        for c, entity in enumerate(comfort_entities):
            row[comfort_vars[c]["on"].start + t] += float(entity.power_usage_kwh)

        A_eq.append(row)
        b_eq.append(float(solar_h[t]) - float(usage_h[t]))

    for t in range(horizon):
        row = np.zeros(n_vars, dtype=np.float64)
        for b in range(num_battery):
            row[battery_vars[b]["charge_pv"].start + t] = 1.0
        A_ub.append(row)
        b_ub.append(max(float(solar_h[t]) - float(usage_h[t]), 0.0))

    result = _solve_lp(
        objective=objective,
        A_ub=np.asarray(A_ub, dtype=np.float64) if A_ub else None,
        b_ub=np.asarray(b_ub, dtype=np.float64) if b_ub else None,
        A_eq=np.asarray(A_eq, dtype=np.float64) if A_eq else None,
        b_eq=np.asarray(b_eq, dtype=np.float64) if b_eq else None,
        bounds=bounds,
        integrality=integrality,
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
    comfort_cmd = np.array(
        [x[comfort_vars[c]["on"].start] for c in range(num_comfort)],
        dtype=np.float64,
    )

    return {
        "charge": charge_grid_cmd + charge_pv_cmd,
        "charge_grid": charge_grid_cmd,
        "charge_pv": charge_pv_cmd,
        "discharge": discharge_cmd,
        "comfort_on": comfort_cmd,
    }


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
    pv_surplus_remaining = max(float(solar_input[t]) - float(usage[t]), 0.0)

    for i, entity in enumerate(battery_entities):
        level = float(battery_levels[i])
        charge_limit, discharge_limit = _battery_power_limits(entity, level)
        charge_eff = float(entity.charge_efficiency)
        discharge_eff = float(entity.discharge_efficiency)
        can_charge_from_grid, can_charge_from_pv = _charge_source_permissions(entity)

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

            charge_grid_amounts[i] = actual_grid
            charge_pv_amounts[i] = actual_pv
            charge_amounts[i] = actual_grid + actual_pv

        min_level = float(entity.minimum_kwh)
        available_for_discharge = max(level - min_level, 0.0) * discharge_eff
        discharge_requests[i] = min(
            requested_discharge, discharge_limit, available_for_discharge
        )

    demand_before_discharge = (
        float(usage[t]) + float(np.sum(charge_amounts)) - float(solar_input[t])
    )

    total_discharge_request = float(np.sum(discharge_requests))
    if demand_before_discharge <= 0.0 or total_discharge_request <= 0.0:
        discharge_amounts = np.zeros(num_battery, dtype=np.float64)
    elif total_discharge_request > demand_before_discharge:
        scale = demand_before_discharge / total_discharge_request
        discharge_amounts = discharge_requests * scale
    else:
        discharge_amounts = discharge_requests

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
            - (
                float(usage[t])
                + comfort_energy
                + float(np.sum(charge_pv_amounts))
            ),
            0.0,
        ),
    )


def _run_mpc(
    prices,
    grid_export_prices,
    solar_input,
    usage,
    battery_entities,
    comfort_entities,
    reuse_plan,
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
    grid_export = np.zeros(total_steps, dtype=np.float64)
    comfort_on = np.zeros((num_comfort, total_steps), dtype=np.float64)
    comfort_lock_mode_series = np.zeros((num_comfort, total_steps), dtype=np.float64)
    comfort_lock_remaining_series = np.zeros(
        (num_comfort, total_steps), dtype=np.float64
    )

    for i, entity in enumerate(battery_entities):
        battery_levels[i, 0] = float(entity.initial_kwh)
    for i, entity in enumerate(comfort_entities):
        # We represent comfort level as remaining required ON slots in horizon context.
        comfort_levels[i, 0] = max(
            float(entity.target_on_slots_per_rolling_window)
            - float(entity.on_slots_last_rolling_window),
            0.0,
        )
        if bool(entity.is_on_now):
            comfort_off_streaks[i, 0] = 0.0
        else:
            comfort_off_streaks[i, 0] = float(entity.off_streak_slots_now)

    successful_solves = 0
    reused_steps = int(reuse_plan["overlap_steps"]) if reuse_plan is not None else 0
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

    for t in range(total_steps):
        horizon = min(MPC_HORIZON, total_steps - t)

        if t < reused_steps:
            controls = {
                "charge": reuse_plan["battery_charge"][:, t].copy(),
                "charge_grid": reuse_plan["battery_charge_grid"][:, t].copy(),
                "charge_pv": reuse_plan["battery_charge_pv"][:, t].copy(),
                "discharge": reuse_plan["battery_discharge"][:, t].copy(),
                "comfort_on": reuse_plan["comfort_on"][:, t].copy(),
            }
            if num_comfort > 0:
                comfort_lock_mode = reuse_plan["comfort_lock_mode"][:, t].astype(
                    np.int32
                )
                comfort_lock_remaining = reuse_plan["comfort_lock_remaining"][
                    :, t
                ].astype(np.int32)
        else:
            locked_indices = [
                i for i in range(num_comfort) if int(comfort_lock_remaining[i]) > 0
            ]
            unlocked_indices = [
                i for i in range(num_comfort) if int(comfort_lock_remaining[i]) <= 0
            ]

            fixed_on_profile = np.zeros((num_comfort, horizon), dtype=np.float64)
            for i in locked_indices:
                fixed_on_profile[i, :] = float(comfort_lock_mode[i])

            usage_h = usage[t : t + horizon].astype(np.float64, copy=True)
            for i in locked_indices:
                usage_h += (
                    float(comfort_entities[i].power_usage_kwh) * fixed_on_profile[i]
                )

            prev_comfort_on = (
                comfort_enabled[:, t - 1].astype(np.float64)
                if t > 0
                else np.asarray(
                    [1.0 if e.is_on_now else 0.0 for e in comfort_entities],
                    dtype=np.float64,
                )
            )

            solve_result = _solve_mpc_step(
                base_timeslot=t,
                prices_h=prices[t : t + horizon],
                grid_export_prices_h=grid_export_prices[t : t + horizon],
                usage_h=usage_h,
                solar_h=solar_input[t : t + horizon],
                battery_entities=battery_entities,
                comfort_entities=[comfort_entities[i] for i in unlocked_indices],
                battery_levels_now=battery_levels[:, t],
                comfort_levels_now=comfort_levels[unlocked_indices, t]
                if unlocked_indices
                else np.zeros(0, dtype=np.float64),
                prev_comfort_on=prev_comfort_on[unlocked_indices]
                if unlocked_indices
                else np.zeros(0, dtype=np.float64),
                comfort_off_streaks_now=comfort_off_streaks[unlocked_indices, t]
                if unlocked_indices
                else np.zeros(0, dtype=np.float64),
                remaining_steps_total=total_steps - t,
            )
            if solve_result is None:
                raise RuntimeError("MPC solve failed for softened MILP model")
            successful_solves += 1

            comfort_cmd = np.zeros(num_comfort, dtype=np.float64)
            for i in locked_indices:
                comfort_cmd[i] = float(comfort_lock_mode[i])
            for pos, i in enumerate(unlocked_indices):
                comfort_cmd[i] = float(solve_result["comfort_on"][pos])

            controls = {
                "charge": solve_result["charge"],
                "charge_grid": solve_result["charge_grid"],
                "charge_pv": solve_result["charge_pv"],
                "discharge": solve_result["discharge"],
                "comfort_on": comfort_cmd,
            }

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
        )
        battery_charge[:, t] = battery_charge_grid[:, t] + battery_charge_pv[:, t]
        if num_comfort > 0:
            comfort_on[:, t] = comfort_enabled[:, t].astype(np.float64)

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
        "battery_states": battery_states,
        "comfort_enabled": comfort_enabled,
        "battery_charge": battery_charge,
        "battery_charge_grid": battery_charge_grid,
        "battery_charge_pv": battery_charge_pv,
        "battery_discharge": battery_discharge,
        "grid_export": grid_export,
        "comfort_on": comfort_on,
        "comfort_lock_mode": comfort_lock_mode_series,
        "comfort_lock_remaining": comfort_lock_remaining_series,
        "reused_steps": reused_steps,
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
        remaining_on_slots = float(comfort_levels[i, -1])
        if remaining_on_slots > EPSILON:
            penalty += 1000.0 * remaining_on_slots
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
        switch_penalty += 0.05 * float(np.sum(np.diff(battery_states[i]) != 0))
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


def _baseline_net_import(
    usage,
    solar_input,
    battery_levels,
    comfort_enabled,
    comfort_entities,
):
    total_steps = len(usage)
    baseline = np.zeros(total_steps, dtype=np.float64)

    for t in range(total_steps):
        total_usage = float(usage[t])
        for i, entity in enumerate(comfort_entities):
            if comfort_enabled[i, t] == 1:
                total_usage += float(entity.power_usage_kwh)

        battery_delta = float(np.sum(battery_levels[:, t + 1] - battery_levels[:, t]))
        net = total_usage + battery_delta - float(solar_input[t])
        baseline[t] = net

    return baseline


def _baseline_cost_per_slot(grid_import_prices, grid_export_prices, usage, solar_input):
    net_import = np.maximum(usage - solar_input, 0.0)
    net_export = np.maximum(solar_input - usage, 0.0)
    return grid_import_prices * net_import - grid_export_prices * net_export


def _optional_entity_options(entity, grid_import_prices, baseline_net_import):
    duration = entity.duration_timeslots
    profile = entity.energy_profile
    candidates = []
    for start_timeslot in range(entity.start_min, entity.start_max + 1):
        base_slice = baseline_net_import[start_timeslot : start_timeslot + duration]
        loaded_slice = base_slice + profile
        price_slice = grid_import_prices[start_timeslot : start_timeslot + duration]
        base_cost = np.where(
            price_slice < 0.0,
            price_slice * base_slice,
            price_slice * np.maximum(base_slice, 0.0),
        )
        loaded_cost = np.where(
            price_slice < 0.0,
            price_slice * loaded_slice,
            price_slice * np.maximum(loaded_slice, 0.0),
        )
        incremental_cost = float(np.sum(loaded_cost - base_cost))
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


def _battery_schedule_charge_source(result, battery_index: int, timeslot: int) -> int:
    """Return a normalized charge source bitmask for one battery schedule slot."""
    battery_state = int(result["battery_states"][battery_index, timeslot])
    if battery_state == 0:
        return 0

    return int(
        (1 if result["battery_charge_grid"][battery_index, timeslot] > EPSILON else 0)
        | (2 if result["battery_charge_pv"][battery_index, timeslot] > EPSILON else 0)
    )


def optimize_internal(normalized: CalculationInput):
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
    reuse_plan = _build_reuse_plan(
        previous_state=previous_state,
        grid_import_prices=grid_import_prices,
        grid_export_prices=grid_export_prices,
        solar_input=solar_input,
        usage=usage,
        total_steps=total_steps,
        num_battery=len(battery_entities),
        num_comfort=len(comfort_entities),
        expected_fingerprint=fingerprint,
    )

    start_time = time.time()
    result = _run_mpc(
        grid_import_prices,
        grid_export_prices,
        solar_input,
        usage,
        battery_entities,
        comfort_entities,
        reuse_plan,
    )
    execution_time = time.time() - start_time

    fitness, avg_price, reasons, projected_cost, projected_cost_per_slot = (
        _score_schedule(
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
        )
    )
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

    entities = []
    battery_state_name = {0: "hold", 1: "charge", 2: "discharge"}
    for i, entity in enumerate(battery_entities):
        entities.append(
            {
                "name": entity.name,
                "type": "battery",
                "schedule": [
                    {
                        "state": battery_state_name[
                            int(result["battery_states"][i, t])
                        ],
                        "charge_source": _battery_schedule_charge_source(
                            result, i, t
                        ),
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

    baseline_net_import = _baseline_net_import(
        usage=usage,
        solar_input=solar_input,
        battery_levels=result["battery_levels"],
        comfort_enabled=result["comfort_enabled"],
        comfort_entities=comfort_entities,
    )
    optional_entity_options = []
    for optional in optional_entities:
        optional_entity_options.append(
            {
                "name": optional.name,
                "options": _optional_entity_options(
                    entity=optional,
                    grid_import_prices=grid_import_prices,
                    baseline_net_import=baseline_net_import,
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
        "battery_charge": result["battery_charge"].tolist(),
        "battery_charge_grid": result["battery_charge_grid"].tolist(),
        "battery_charge_pv": result["battery_charge_pv"].tolist(),
        "battery_discharge": result["battery_discharge"].tolist(),
        "comfort_on": result["comfort_on"].tolist(),
        "comfort_lock_mode": result["comfort_lock_mode"].tolist(),
        "comfort_lock_remaining": result["comfort_lock_remaining"].tolist(),
    }

    return {
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
        "successful_solves": int(result["successful_solves"]),
        "entities": entities,
        "optional_entity_options": optional_entity_options,
        "state": encode_state_blob(state_obj),
    }


def optimize(params: OptimizationParams):
    return optimize_internal(normalize_calculation_input(params))
