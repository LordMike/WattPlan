"""Relaxed linear battery dispatch rounded into executable inverter policies.

A single sparse HiGHS LP optimizes continuous grid/PV charge, delivered deficit
energy and end-of-slot storage over max_slots. It has no integer variables.
Stored-energy conservation includes both efficiencies; power, capacity and
reserve constraints bound the relaxation. Negative-import slots forbid discharge
so the LP cannot earn money through lossy simultaneous grid charge/discharge.
PV surplus reserves charging power before grid ingress. PV-ineligible batteries
cannot grid charge while surplus remains; eligible batteries can use power left
after absorbing that surplus, matching the shared PV-first rule.
Positive-price power sharing uses a convex time-sharing constraint, not a claim
that the inverter can execute both directions at once.

The grid-charge fraction is rounded at round_fraction; a positive discharge
becomes self-consumption and all other slots preserve. Optional marginal mode
instead tests each legal named action using exact shared physics and scores
immediate forecast cost plus the next LP energy-balance dual times carried energy.
The dual is a local cost-to-go slope at the relaxed path, not a global value
function; full-power actions can cross its valid region. The final LP slot uses
negative terminal credit as its future slope. Exact economic ties favor the first
self-consumption action. Immediate switching cost is included in marginal mode,
while the LP does not model future switches. This can overbuy energy,
miss small profitable purchases, or exhaust storage sooner than the continuous
solution. Curves use their maximum rate in the relaxation; shared replay applies
actual SoC-dependent curves and headroom eligibility. The LP may choose to export
PV which shared replay instead automatically stores. Consequently neither its
objective nor fractional levels are published as executable projections. One
full unchanged-request evaluation reports the accepted policy and its physics.
Slots beyond the bounded LP horizon use self-consumption, with no reporting credit
for residual energy. Optional terminal value and PV scaling are explicit search
assumptions only; no actual future observations or previous request state enter.

Targets, multiple batteries and initial reserve shortfalls use the established
physical deadline/reserve seed, with the fallback disclosed. They are not solved
by this single-battery LP. Solver failure also uses that seed. Comfort is prepared
first by the shared wrapper and optional appliances priced afterward. Switching
costs are measured in final reporting but not modeled by this continuous LP.
A fresh native solver enforces a per-request solve deadline; no warm-start or
policy state is retained. Clearing only a HiGHS model does not reset its cumulative
run clock, so solver objects are deliberately not reused between requests. The
thread option remains at its default to coexist with the original optimizer
within the process-wide HiGHS scheduler; this planner never resets that scheduler. A native time limit bounds the solve, not the complete wrapper.
"""

import math
from statistics import median

from ..physics import EPSILON, transition
from .charge_window import _initial_seed
from .contract import PlannerCapabilities
from .evaluation import build_result, evaluate_schedule
from .shared import wrap_battery_planner



def _solve_relaxation(params, solver, max_slots, pv_scale, terminal_value_fraction):
    """Return a continuous solution and energy-row duals; import native code here."""
    import highspy
    import numpy as np
    b = params.battery_entities[0]
    n = min(len(params.usage_kwh), max_slots)
    charge_limit = max(b.charge_curve_kwh)
    discharge_limit = max(b.discharge_curve_kwh)
    objective = np.zeros(4 * n)
    lower = np.zeros(4 * n)
    upper = np.zeros(4 * n)
    lower[3 * n:] = b.minimum_kwh
    upper[3 * n:] = b.capacity_kwh
    row_lower, row_upper, starts, indices, values = [], [], [0], [], []

    def add_row(items, low, high):
        row_lower.append(low)
        row_upper.append(high)
        for col, value in items:
            indices.append(col)
            values.append(value)
        starts.append(len(indices))

    for slot in range(n):
        price = params.grid_import_price_per_kwh[slot]
        surplus = max(params.solar_input_kwh[slot] * pv_scale - params.usage_kwh[slot], 0)
        deficit = max(params.usage_kwh[slot] - params.solar_input_kwh[slot] * pv_scale, 0)
        objective[slot] = price + params.throughput_cost_per_kwh
        objective[n + slot] = params.grid_export_price_per_kwh[slot] + params.throughput_cost_per_kwh
        objective[2 * n + slot] = -price + params.throughput_cost_per_kwh
        if b.can_charge_from & 1:
            upper[slot] = (charge_limit if surplus <= EPSILON
                           else max(charge_limit - surplus, 0) if b.can_charge_from & 2 else 0)
        else:
            upper[slot] = 0
        upper[n + slot] = min(charge_limit, surplus) if b.can_charge_from & 2 else 0
        upper[2 * n + slot] = min(discharge_limit, deficit) if price > 0 else 0
        items = [(3 * n + slot, 1), (slot, -b.charge_efficiency),
                 (n + slot, -b.charge_efficiency), (2 * n + slot, 1 / b.discharge_efficiency)]
        if slot:
            items.append((3 * n + slot - 1, -1))
        initial = b.initial_kwh if slot == 0 else 0
        add_row(items, initial, initial)
        items = [(slot, 1), (n + slot, 1)]
        if discharge_limit > EPSILON:
            items.append((2 * n + slot, charge_limit / discharge_limit))
        add_row(items, -highspy.kHighsInf, charge_limit)
    credit = max(median(params.grid_import_price_per_kwh[max(0, n - 12):n]), 0) * b.discharge_efficiency * terminal_value_fraction
    objective[-1] = -credit
    lp = highspy.HighsLp()
    lp.num_col_, lp.num_row_ = 4 * n, len(row_lower)
    lp.col_cost_, lp.col_lower_, lp.col_upper_ = objective, lower, upper
    lp.row_lower_, lp.row_upper_ = np.asarray(row_lower), np.asarray(row_upper)
    lp.a_matrix_.num_col_, lp.a_matrix_.num_row_ = 4 * n, len(row_lower)
    lp.a_matrix_.format_ = highspy.MatrixFormat.kRowwise
    lp.a_matrix_.start_ = np.asarray(starts, dtype=np.int32)
    lp.a_matrix_.index_ = np.asarray(indices, dtype=np.int32)
    lp.a_matrix_.value_ = np.asarray(values, dtype=np.float64)
    solver.clearModel()
    solver.passModel(lp)
    run_status = solver.run()
    model_status = str(solver.getModelStatus())
    solver_runtime = solver.getRunTime()
    solution = solver.getSolution()
    return {
        "optimal": run_status == highspy.HighsStatus.kOk and solver.getModelStatus() == highspy.HighsModelStatus.kOptimal and solution.value_valid,
        "slots": n, "status": model_status, "run_status": str(run_status), "runtime": solver_runtime,
        "grid_charge": solution.col_value[:n], "discharge": solution.col_value[2 * n:3 * n],
        "energy_duals": solution.row_dual[::2], "terminal_credit": credit,
        "objective": solver.getObjectiveValue(), "charge_limit": charge_limit,
    }


def factory(*, max_slots=24, round_fraction=0.5, pv_scale=1.0,
            terminal_value_fraction=0.0, solver_seconds=0.05, dispatch_mode="rounded"):
    """Build one bounded LP per request; round and report actual policy replay."""
    if dispatch_mode not in {"rounded", "marginal"}:
        raise ValueError("dispatch_mode must be rounded or marginal")
    if type(max_slots) is not int or not 2 <= max_slots <= 96:
        raise ValueError("max_slots must be an integer between 2 and 96")
    for name, value in (("round_fraction", round_fraction), ("pv_scale", pv_scale),
                        ("terminal_value_fraction", terminal_value_fraction)):
        if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{name} must be finite and between zero and one")
    if type(solver_seconds) not in {int, float} or not math.isfinite(solver_seconds) or not 0 < solver_seconds <= 1:
        raise ValueError("solver_seconds must be finite, positive and at most one")
    def plan(params):
        batteries = params.battery_entities
        horizon = len(params.usage_kwh)
        rows = [["self_consume"] * horizon for _ in batteries]
        fallback_reason = None
        model_status = None
        run_status = None
        solved_slots = 0
        solver_runtime = 0.0
        if len(batteries) != 1:
            fallback_reason = "requires_single_battery"
        elif batteries[0].target:
            fallback_reason = "physical_target_preparation"
        elif batteries[0].initial_kwh < batteries[0].minimum_kwh - EPSILON:
            fallback_reason = "physical_reserve_recovery"
        else:
            import highspy
            solver = highspy.Highs()
            solver.setOptionValue("output_flag", False)
            solver.setOptionValue("solver", "simplex")
            solver.setOptionValue("time_limit", solver_seconds)
            relaxed = _solve_relaxation(params, solver, max_slots, pv_scale, terminal_value_fraction)
            model_status, solver_runtime = relaxed["status"], relaxed["runtime"]
            run_status = relaxed["run_status"]
            if not relaxed["optimal"]:
                fallback_reason = "no_optimal_relaxed_solution"
            else:
                solved_slots = relaxed["slots"]
                if dispatch_mode == "rounded":
                    for slot in range(solved_slots):
                        grid, discharge = relaxed["grid_charge"][slot], relaxed["discharge"][slot]
                        rows[0][slot] = ("grid_charge" if grid > EPSILON and grid >= round_fraction * relaxed["charge_limit"]
                                         else "self_consume" if discharge > EPSILON else "preserve")
                else:
                    b = batteries[0]
                    choices = ("self_consume", "preserve", "grid_charge") if b.can_charge_from & 1 else ("self_consume", "preserve")
                    level, previous = b.initial_kwh, 0
                    for slot in range(solved_slots):
                        # Energy balance is e[t]-e[t-1]-eta*c+d/eta=initial.
                        # The NEXT balance dual measures the future objective's
                        # derivative with respect to carried energy (negative
                        # when more stored energy reduces future purchase cost).
                        slope = relaxed["energy_duals"][slot + 1] if slot + 1 < solved_slots else -relaxed["terminal_credit"]
                        candidates = []
                        for action in choices:
                            step = transition(batteries, [level], [action],
                                              usage=params.usage_kwh[slot], pv=params.solar_input_kwh[slot])
                            charge, discharge = step.charge, sum(step.discharge)
                            mode = (1 if charge > EPSILON and charge >= params.action_deadband_kwh
                                    else -1 if discharge > EPSILON and discharge >= params.action_deadband_kwh else 0)
                            cost = (step.grid_import * params.grid_import_price_per_kwh[slot]
                                    - step.grid_export * params.grid_export_price_per_kwh[slot]
                                    + (charge + discharge) * params.throughput_cost_per_kwh
                                    + (mode != previous) * params.mode_switch_cost)
                            candidates.append((cost + slope * step.levels[0], action, step, mode))
                        # Stable action order favors self-consumption on exact
                        # economic ties, avoiding arbitrary surplus preservation.
                        chosen = min(candidates, key=lambda candidate: candidate[0])
                        rows[0][slot], level, previous = chosen[1], chosen[2].levels[0], chosen[3]
        if fallback_reason:
            rows, _ = _initial_seed(params)
        evaluation = evaluate_schedule(params, rows)
        return build_result(params, rows, evaluation=evaluation, diagnostics={
            "algorithm": "rounded_continuous_linear_dispatch", "evaluations": 1,
            "dispatch_mode": dispatch_mode, "max_slots": max_slots, "round_fraction": round_fraction, "pv_scale": pv_scale,
            "terminal_value_fraction": terminal_value_fraction, "solver_seconds": solver_seconds,
            "lp_solved_slots": solved_slots, "lp_model_status": model_status, "lp_run_status": run_status,
            "lp_solver_runtime": solver_runtime, "fallback_reason": fallback_reason,
            "integer_variables": 0, "scored_horizon_slots": horizon,
            "state_reuse": "fresh solver and per-request time budget; no policy state",
            "lookahead_semantics": "bounded LP prefix; complete physical policy reporting",
            "limitations": ["continuous relaxation rounded to discrete policies", "targets and fleet requests use physical seed", "maximum-rate curves and optional PV capture are relaxed"],
        })

    plan.capabilities = PlannerCapabilities()
    return wrap_battery_planner(plan)
