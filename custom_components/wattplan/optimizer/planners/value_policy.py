"""Backward interpolated value-function battery dispatch.

Unlike forward frontier DP, this planner computes a cost-to-go table at fixed
energy knots, then chooses every policy at the exact carried physical level.
Shared physics owns all knot and forward transitions; interpolation estimates
future target miss and economics, never rounds the executed energy. Optional
charge knots capture full-charge saturation and grid eligibility breakpoints. Physical
charge/idle/discharge history is part of the value table for switching costs.

Coarse grids make this inexpensive for one battery but can smooth narrow target
windows and grid-charge eligibility discontinuities. Any observed target miss
triggers a bounded finer joint-state DP repair; the better shared target-first
score wins. Repair remains approximate and cannot certify target feasibility. This is approximate dynamic
programming, not an exact optimality guarantee. Multiple batteries use the
existing bounded joint-state DP explicitly, avoiding exponential grid products.
Comfort is placed before battery dispatch and optional loads ranked afterward by
the shared wrapper. Fleet fallback does not use forecast scalings or terminal
credit and reports that distinction in diagnostics.

PV/demand multipliers are explicit forecast robustness assumptions applied only
to the search model; reporting always replays the accepted policies against the
unaltered request. A terminal value fraction optionally estimates stored energy
at the horizon from the final twelve forecast tariffs. It is a search heuristic,
not a credit in reported electricity cost. All configuration is stateless; no
actual future observations, dates, corpus-specific rules or random draws enter
policy selection. Lower PV assumptions can protect against optimistic persistence
forecasts but can buy unnecessarily before real sunshine; excessive terminal
credit can retain energy too long. Grid resolution trades runtime for fidelity.

An optional PV scenario spread uses equal-probability factors (1-spread,1+spread)
independently at each forecast slot. It averages each physical branch's immediate
cost, target miss and interpolated successor value, including branch mode history.
Forward action selection uses the same expected score at the unaltered carried
level; accepted policies are replayed against original inputs. Identical zero-PV
branches share work. This uncertainty model is a configurable assumption, not a
calibrated weather distribution: it ignores cross-hour weather correlation and
can overpay for protection. Spread zero keeps the deterministic policy path.

An optional grid-charge premium adds a search-only cost per AC kWh entering
the battery from the grid. Scenario branches use their own physical grid-charge
amounts. It never charges ordinary household imports or PV charging, and does
not enter reported costs. Fleet fallback and target repair omit this heuristic.

Optional first-action rollout evaluates two forecast slots at exact physical
states before interpolating the remaining value table. Each action precedes its
own slot's PV branch; the second action may depend on the first branch's observed
state. Only slot zero uses this refinement. The tail remains approximate, and
fleet fallback and target repair do not use rollout.
Diagnostics distinguish evaluated rollout from an accepted policy replaced by
target repair; the initial rollout score always describes the pre-repair search.

Optional paired demand spread couples low PV with high demand and high PV with
low demand in two equal-weight branches, retaining both forecast means. This is
an explicit negative dependence assumption, not calibrated uncertainty. A relative
slot bound limits demand uncertainty while PV scenarios and planning continue
through the full horizon. Prepared demand includes fixed comfort placement, so
the spread describes aggregate demand without rescheduling comfort by branch.
Reporting replays original inputs; fleet fallback and target repair omit it.

Optional PV-only tail probability changes the distribution after the bounded
paired-demand slots. Low PV keeps factor (1-spread) with probability q; high PV
uses (1+q*spread/(1-q)) with probability (1-q), preserving the supplied PV mean.
Active paired slots retain their equal weights. The default q=.5 is unchanged;
nondefault probabilities require bounded paired demand and omit rollout. This is
an explicit risk assumption, not calibrated weather probability. Nominal reporting,
fleet fallback and target repair do not use this tail distribution.

Optional rollout PV persistence reuses one latent PV factor for the first two
rollout slots, then resets to the independent value-table tail. Only different
observed PV outcomes in slot zero reveal the factor for the second action.
Zero first-slot PV reveals nothing, so the next action still precedes its own
PV realization. Equal battery levels do not hide distinguishable generation.
Perfect two-slot persistence is an explicit assumption, not calibrated weather.
This isolated model requires rollout and does not support paired demand spread.

An optional exact initial-energy knot anchors idle-state continuation values.
It adds at most one table node, retains valid below-reserve initial states, and
deduplicates by exact equality without snapping adjacent floating-point values.
Fleet fallback and target repair do not use this table refinement.
"""

from bisect import bisect_right
import math
from statistics import median

from ..physics import EPSILON, MIN_GRID_CHARGE_SLOT_FRACTION, transition
from .contract import PlannerCapabilities
from .dynamic_programming import factory as joint_factory
from .evaluation import build_result, evaluate_schedule, target_miss
from .recovery import reserve_shortfall_cost
from .shared import wrap_battery_planner


def _interpolate(knots, values, level):
    index = min(max(bisect_right(knots, level) - 1, 0), len(knots) - 2)
    weight = min(max((level - knots[index]) / (knots[index + 1] - knots[index]), 0.0), 1.0)
    a, b = values[index], values[index + 1]
    return (a[0] + weight * (b[0] - a[0]), a[1] + weight * (b[1] - a[1]))


def _charge_knots(battery):
    """Piecewise roots for full-charge saturation and grid headroom eligibility."""
    if not battery.can_charge_from & 1:
        return []
    curve = list(battery.charge_curve_kwh)
    if len(curve) == 1:
        curve *= 2
    levels = [battery.capacity_kwh * n / (len(curve) - 1) for n in range(len(curve))]
    roots = []
    for fraction in (1.0, MIN_GRID_CHARGE_SLOT_FRACTION):
        slack = EPSILON * battery.charge_efficiency if fraction < 1 else 0.0
        for low, high, low_limit, high_limit in zip(levels, levels[1:], curve, curve[1:]):
            a = battery.capacity_kwh - low - fraction * battery.charge_efficiency * low_limit + slack
            b = battery.capacity_kwh - high - fraction * battery.charge_efficiency * high_limit + slack
            if a == b or a * b > 0:
                continue
            root = low + (high - low) * a / (a - b)
            if 0 < root < battery.capacity_kwh:
                roots.append(root)
    return roots


def factory(*, energy_intervals=8, pv_scale=1.0, demand_scale=1.0,
            terminal_value_fraction=0.0, include_charge_knots=False, pv_scenario_spread=0.0,
            grid_charge_premium_per_kwh=0.0, first_action_rollout=False,
            paired_demand_scenario_spread=0.0, paired_demand_scenario_slots=None,
            rollout_pv_persistence=False, include_initial_knot=False,
            pv_tail_low_probability=0.5):
    """Return a deterministic interpolated cost-to-go planner."""
    if type(energy_intervals) is not int or not 2 <= energy_intervals <= 128:
        raise ValueError("energy_intervals must be an integer between 2 and 128")
    for name, value in (("pv_scale", pv_scale), ("demand_scale", demand_scale),
                        ("terminal_value_fraction", terminal_value_fraction),
                        ("grid_charge_premium_per_kwh", grid_charge_premium_per_kwh)):
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    if type(include_charge_knots) is not bool:
        raise ValueError("include_charge_knots must be a boolean")
    if type(include_initial_knot) is not bool:
        raise ValueError("include_initial_knot must be a boolean")
    if type(first_action_rollout) is not bool:
        raise ValueError("first_action_rollout must be a boolean")
    if type(rollout_pv_persistence) is not bool:
        raise ValueError("rollout_pv_persistence must be a boolean")
    if (type(pv_scenario_spread) not in {int, float}
            or not math.isfinite(pv_scenario_spread) or not 0 <= pv_scenario_spread <= 1):
        raise ValueError("pv_scenario_spread must be finite and between zero and one")
    if (type(paired_demand_scenario_spread) not in {int, float}
            or not math.isfinite(paired_demand_scenario_spread)
            or not 0 <= paired_demand_scenario_spread <= 1):
        raise ValueError("paired_demand_scenario_spread must be finite and between zero and one")
    if paired_demand_scenario_slots is not None and (
            type(paired_demand_scenario_slots) is not int or paired_demand_scenario_slots <= 0):
        raise ValueError("paired_demand_scenario_slots must be None or a positive integer")
    if (type(pv_tail_low_probability) not in {int, float}
            or not math.isfinite(pv_tail_low_probability)
            or not 0 < pv_tail_low_probability <= 0.5):
        raise ValueError("pv_tail_low_probability must be finite and greater than zero and at most 0.5")
    tail_modified = pv_tail_low_probability != 0.5
    if tail_modified:
        if (not pv_scenario_spread or not paired_demand_scenario_spread
                or paired_demand_scenario_slots is None):
            raise ValueError("nondefault pv_tail_low_probability requires positive PV and "
                             "paired demand spreads and an explicit demand slot bound")
        if first_action_rollout or rollout_pv_persistence:
            raise ValueError("nondefault pv_tail_low_probability does not support rollout")
    tail_high_pv = 1 + pv_tail_low_probability * pv_scenario_spread / (1 - pv_tail_low_probability)
    if rollout_pv_persistence and not first_action_rollout:
        raise ValueError("rollout_pv_persistence requires first_action_rollout")
    if rollout_pv_persistence and paired_demand_scenario_spread:
        raise ValueError("rollout_pv_persistence does not support paired_demand_scenario_spread")
    scenario_active = bool(pv_scenario_spread or paired_demand_scenario_spread)
    capabilities = PlannerCapabilities()
    fallback = joint_factory(max_states=8, max_expansions=1024)
    target_repair = joint_factory(soc_step_kwh=0.05, max_states=64, max_expansions=20000)

    def plan(params):
        batteries = params.battery_entities
        if len(batteries) != 1:
            result = fallback(params)
            result["diagnostics"]["value_policy_fallback"] = "bounded_joint_state_dp"
            result["diagnostics"]["forecast_adjustments_applied"] = False
            if tail_modified:
                result["diagnostics"].update(pv_tail_low_probability=pv_tail_low_probability,
                    pv_tail_evaluated=False, pv_tail_applied=False,
                    pv_tail_start_slot=None, pv_tail_effective_slots=0,
                    pv_tail_factors=[1 - pv_scenario_spread, tail_high_pv],
                    pv_tail_weights=[pv_tail_low_probability, 1 - pv_tail_low_probability])
            if include_initial_knot:
                result["diagnostics"].update(include_initial_knot=True,
                    initial_knot_evaluated=False, initial_knot_added=False,
                    initial_knot_applied=False, initial_knot_value_kwh=None)
            if rollout_pv_persistence:
                result["diagnostics"].update(rollout_pv_persistence=True,
                    rollout_pv_persistence_evaluated=False, rollout_pv_persistence_applied=False,
                    rollout_pv_factor_revealed=False, initial_pv_persistence_search_score=None)
            if paired_demand_scenario_spread:
                result["diagnostics"].update(
                    paired_demand_scenario_spread=paired_demand_scenario_spread,
                    paired_demand_scenario_slots=paired_demand_scenario_slots,
                    paired_demand_scenario_evaluated=False, paired_demand_scenario_applied=False,
                    paired_demand_scenario_effective_slots=0, paired_demand_scenario_effective_hours=0.0,
                    scenario_branch_evaluations=0)
            if first_action_rollout:
                result["diagnostics"].update(first_action_rollout=True, first_action_rollout_evaluated=False,
                    first_action_rollout_applied=False, rollout_search_transitions=0)
            if grid_charge_premium_per_kwh:
                result["diagnostics"].update(
                    grid_charge_premium_per_kwh=grid_charge_premium_per_kwh,
                    grid_charge_premium_applied=False)
            return result
        battery = batteries[0]
        horizon = len(params.usage_kwh)
        demand_slots = min(horizon, paired_demand_scenario_slots or horizon)
        persistence_evaluated = rollout_pv_persistence and horizon >= 2 and pv_scenario_spread > 0
        first_pv = params.solar_input_kwh[0] * pv_scale
        factor_revealed = (persistence_evaluated
            and first_pv * (1 - pv_scenario_spread) != first_pv * (1 + pv_scenario_spread))
        knots = sorted(set([battery.capacity_kwh * n / energy_intervals
                            for n in range(energy_intervals + 1)]
                           + [battery.minimum_kwh]))
        if include_charge_knots:
            knots = sorted(set(knots + _charge_knots(battery)))
        if battery.target:
            knots = sorted(set(knots + [min(max(v, 0.0), battery.capacity_kwh)
                for v in (battery.target.soc_kwh - battery.target.tolerance_kwh,
                          battery.target.soc_kwh + battery.target.tolerance_kwh)]))
        initial_knot_added = include_initial_knot and battery.initial_kwh not in knots
        if initial_knot_added:
            # Exact equality deduplicates existing nodes without moving a valid
            # carried state, including below-reserve recovery states.
            knots = sorted(knots + [battery.initial_kwh])
        choices = ("self_consume", "preserve", "grid_charge") if battery.can_charge_from & 1 else ("self_consume", "preserve")
        terminal_price = max(median(params.grid_import_price_per_kwh[-12:]), 0.0)
        credit = terminal_price * battery.discharge_efficiency * terminal_value_fraction
        terminal = [(0.0, -max(k - battery.minimum_kwh, 0.0) * credit) for k in knots]
        tables = [None] * (horizon + 1)
        tables[horizon] = [terminal, terminal, terminal]
        transitions = 0
        scenario_branches = 0

        def candidates(slot, level, pv_factor=1.0, *, demand_factor=1.0, successor_value=None):
            nonlocal transitions
            rows = []
            for action in choices:
                step = transition(batteries, [level], [action],
                    usage=params.usage_kwh[slot] * demand_scale * demand_factor,
                    pv=params.solar_input_kwh[slot] * pv_scale * pv_factor)
                transitions += 1
                charge, discharge = step.charge, sum(step.discharge)
                mode = (1 if charge > EPSILON and charge >= params.action_deadband_kwh
                        else -1 if discharge > EPSILON and discharge >= params.action_deadband_kwh else 0)
                miss = target_miss(battery.target, step.levels[0]) if battery.target and battery.target.timeslot == slot else 0.0
                future_miss, future_cost = (
                    _interpolate(knots, tables[slot + 1][mode + 1], step.levels[0])
                    if successor_value is None else successor_value(slot + 1, step.levels[0], mode))
                cost = (step.grid_import * params.grid_import_price_per_kwh[slot]
                        - step.grid_export * params.grid_export_price_per_kwh[slot]
                        + (charge + discharge) * params.throughput_cost_per_kwh
                        + reserve_shortfall_cost(batteries, step.levels))
                if grid_charge_premium_per_kwh:
                    cost += sum(step.grid_charge) * grid_charge_premium_per_kwh
                rows.append((miss + future_miss, cost + future_cost, mode, action))
            return rows

        def scenario_candidates(slot, level, *, successor_value=None):
            nonlocal scenario_branches
            if paired_demand_scenario_spread:
                spread = paired_demand_scenario_spread if slot < demand_slots else 0.0
                low_pv, high_pv = 1 - pv_scenario_spread, 1 + pv_scenario_spread
                high_demand, low_demand = 1 + spread, 1 - spread
                pv = params.solar_input_kwh[slot] * pv_scale
                usage = params.usage_kwh[slot] * demand_scale
                identical = (pv * low_pv == pv * high_pv
                             and usage * high_demand == usage * low_demand)
                scenario_branches += 1 if identical else 2
                low = candidates(slot, level, low_pv, demand_factor=high_demand,
                                 successor_value=successor_value)
                if identical:
                    return low
                high = candidates(slot, level, high_pv, demand_factor=low_demand,
                                  successor_value=successor_value)
                return [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2,
                         (a[2], b[2]), a[3]) for a, b in zip(low, high, strict=True)]
            # Exactly zero forecast PV has identical branches; avoid duplicate physics.
            if params.solar_input_kwh[slot] == 0 or pv_scale == 0:
                return candidates(slot, level, successor_value=successor_value)
            low = candidates(slot, level, 1 - pv_scenario_spread, successor_value=successor_value)
            high = candidates(slot, level, 1 + pv_scenario_spread, successor_value=successor_value)
            return [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2,
                     (a[2], b[2]), a[3]) for a, b in zip(low, high, strict=True)]

        def scenario_score(row, previous):
            modes = row[2]
            switches = ((modes != previous) if type(modes) is int
                        else ((modes[0] != previous) + (modes[1] != previous)) / 2)
            return row[0], row[1] + switches * params.mode_switch_cost

        if tail_modified:
            half_weight_candidates = scenario_candidates
            half_weight_score = scenario_score

            def scenario_candidates(slot, level, *, successor_value=None):
                nonlocal scenario_branches
                if slot < demand_slots:
                    return half_weight_candidates(slot, level, successor_value=successor_value)
                # Demand is nominal in this tail. Keep the cloud outcome while
                # preserving the supplied PV mean with unequal probabilities.
                low_pv = 1 - pv_scenario_spread
                pv = params.solar_input_kwh[slot] * pv_scale
                identical = pv * low_pv == pv * tail_high_pv
                scenario_branches += 1 if identical else 2
                low = candidates(slot, level, low_pv, successor_value=successor_value)
                if identical:
                    return low
                high = candidates(slot, level, tail_high_pv, successor_value=successor_value)
                q = pv_tail_low_probability
                # The optional fifth field is a weight, never an averaged mode.
                return [(q*a[0] + (1-q)*b[0], q*a[1] + (1-q)*b[1],
                         (a[2], b[2]), a[3], q) for a, b in zip(low, high, strict=True)]

            def scenario_score(row, previous):
                if len(row) == 4:
                    return half_weight_score(row, previous)
                modes, q = row[2], row[4]
                switches = q * (modes[0] != previous) + (1-q) * (modes[1] != previous)
                return row[0], row[1] + switches * params.mode_switch_cost

        candidate_fn = scenario_candidates if scenario_active else candidates
        first_expected_score = None
        rollout_transitions = 0
        rollout_score = None

        def exact_successor(slot, level, previous):
            """Choose the next action before its PV branch, at the observed state."""
            nonlocal rollout_transitions
            before = transitions
            rows = candidate_fn(slot, level)
            rollout_transitions += transitions - before
            return min(scenario_score(row, previous) for row in rows)

        def persistent_successor(slot, level, previous, pv_factor):
            """The prior PV observation reveals this factor, not the current action."""
            nonlocal rollout_transitions
            before = transitions
            rows = candidates(slot, level, pv_factor)
            rollout_transitions += transitions - before
            return min(scenario_score(row, previous) for row in rows)

        for slot in range(horizon - 1, -1, -1):
            table = [[], [], []]
            for level in knots:
                rows = candidate_fn(slot, level)
                for previous in (-1, 0, 1):
                    if scenario_active:
                        table[previous + 1].append(min(scenario_score(row, previous) for row in rows))
                    else:
                        table[previous + 1].append(min((miss, cost + (mode != previous) * params.mode_switch_cost)
                            for miss, cost, mode, _ in rows))
            tables[slot] = table
        level, previous = battery.initial_kwh, 0
        policies = [[]]
        for slot in range(horizon):
            if first_action_rollout and slot == 0 and horizon >= 2:
                if factor_revealed:
                    low = candidates(slot, level, 1 - pv_scenario_spread,
                        successor_value=lambda s, k, mode: persistent_successor(s, k, mode, 1 - pv_scenario_spread))
                    high = candidates(slot, level, 1 + pv_scenario_spread,
                        successor_value=lambda s, k, mode: persistent_successor(s, k, mode, 1 + pv_scenario_spread))
                    rows = [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2,
                             (a[2], b[2]), a[3]) for a, b in zip(low, high, strict=True)]
                else:
                    rows = candidate_fn(slot, level, successor_value=exact_successor)
            else:
                rows = candidate_fn(slot, level)
            if scenario_active:
                best = min(rows, key=lambda row: scenario_score(row, previous))
                if slot == 0:
                    first_expected_score = scenario_score(best, previous)
            else:
                best = min(rows, key=lambda row: (row[0], row[1] + (row[2] != previous) * params.mode_switch_cost))
            if first_action_rollout and slot == 0 and horizon >= 2:
                rollout_score = scenario_score(best, previous)
            action = best[3]
            policies[0].append(action)
            # The accepted path carries the unadjusted request's exact energy.
            step = transition(batteries, [level], [action], usage=params.usage_kwh[slot], pv=params.solar_input_kwh[slot])
            level = step.levels[0]
            charge, discharge = step.charge, sum(step.discharge)
            previous = (1 if charge > EPSILON and charge >= params.action_deadband_kwh
                        else -1 if discharge > EPSILON and discharge >= params.action_deadband_kwh else 0)
        evaluation = evaluate_schedule(params, policies)
        repair = None
        if evaluation.target_miss_kwh > EPSILON:
            repaired_result = target_repair(params)
            repaired_policies = [[entry["state"] for entry in entity["schedule"]]
                for entity in repaired_result["entities"] if entity["type"] == "battery"]
            repaired_evaluation = evaluate_schedule(params, repaired_policies)
            improved = repaired_evaluation.score < evaluation.score
            repair = {"algorithm": "bounded_joint_state_dp", "improved": improved,
                      "original_target_miss_kwh": evaluation.target_miss_kwh,
                      "expansions": repaired_result["diagnostics"]["expansions"], "evaluations": 2,
                      "forecast_adjustments_applied": False}
            if grid_charge_premium_per_kwh:
                repair["grid_charge_premium_applied"] = False
            if first_action_rollout:
                repair["first_action_rollout_applied"] = False
            if paired_demand_scenario_spread:
                repair["paired_demand_scenario_applied"] = False
            if rollout_pv_persistence:
                repair["rollout_pv_persistence_applied"] = False
            if include_initial_knot:
                repair["initial_knot_applied"] = False
            if tail_modified:
                repair["pv_tail_applied"] = False
            if improved:
                policies, evaluation = repaired_policies, repaired_evaluation
        return build_result(params, policies, evaluation=evaluation, diagnostics={
            "algorithm": ("scenario_expected_backward_value_policy" if scenario_active
                          else "interpolated_backward_value_policy"), "approximate": True,
            **({"pv_scenario_spread": pv_scenario_spread,
                "pv_scenario_factors": [1 - pv_scenario_spread, 1 + pv_scenario_spread],
                "pv_scenario_weights": [0.5, 0.5],
                "initial_value_policy_expected_score": first_expected_score,
                "scenario_assumption": ("independent per-slot equal-weight paired PV/demand in bounded active slots; mean-preserving unequal-weight PV-only tail"
                                        if tail_modified else
                                        "independent per-slot paired low PV/high aggregate demand and high PV/low aggregate demand"
                                        if paired_demand_scenario_spread else "independent symmetric per-slot PV factors")}
               if scenario_active else {}),
            **({"paired_demand_scenario_spread": paired_demand_scenario_spread,
                "paired_demand_scenario_slots": paired_demand_scenario_slots,
                "paired_demand_scenario_factors": [1 + paired_demand_scenario_spread, 1 - paired_demand_scenario_spread],
                "paired_demand_scenario_evaluated": True,
                "paired_demand_scenario_applied": not (repair and repair["improved"]),
                "paired_demand_scenario_effective_slots": demand_slots,
                "paired_demand_scenario_effective_hours": demand_slots * params.slot_minutes / 60,
                "scenario_branch_evaluations": scenario_branches}
               if paired_demand_scenario_spread else {}),
            **({"pv_tail_low_probability": pv_tail_low_probability,
                "pv_tail_evaluated": demand_slots < horizon,
                "pv_tail_applied": demand_slots < horizon and not (repair and repair["improved"]),
                "pv_tail_start_slot": demand_slots,
                "pv_tail_effective_slots": horizon - demand_slots,
                "pv_tail_factors": [1 - pv_scenario_spread, tail_high_pv],
                "pv_tail_weights": [pv_tail_low_probability, 1 - pv_tail_low_probability]}
               if tail_modified else {}),
            "energy_intervals": energy_intervals, "energy_knots": len(knots),
            **({"include_initial_knot": True, "initial_knot_evaluated": True,
                "initial_knot_added": initial_knot_added,
                "initial_knot_value_kwh": battery.initial_kwh,
                "initial_knot_applied": not (repair and repair["improved"])}
               if include_initial_knot else {}),
            "include_charge_knots": include_charge_knots, "target_repair": repair,
            "pv_scale": pv_scale, "demand_scale": demand_scale,
            "terminal_value_fraction": terminal_value_fraction,
            **({"rollout_pv_persistence": True,
                "rollout_pv_persistence_evaluated": persistence_evaluated,
                "rollout_pv_persistence_applied": persistence_evaluated and not (repair and repair["improved"]),
                "rollout_pv_factor_revealed": factor_revealed,
                "initial_pv_persistence_search_score": rollout_score if persistence_evaluated else None,
                "rollout_pv_persistence_assumption": "one latent PV factor in slots zero and one; independent tail from slot two"}
               if rollout_pv_persistence else {}),
            **({"first_action_rollout": True, "first_action_rollout_evaluated": horizon >= 2,
                "first_action_rollout_applied": horizon >= 2 and not (repair and repair["improved"]),
                "rollout_search_transitions": rollout_transitions,
                "initial_rollout_search_score": rollout_score}
               if first_action_rollout else {}),
            **({"grid_charge_premium_per_kwh": grid_charge_premium_per_kwh,
                "grid_charge_premium_applied": True}
               if grid_charge_premium_per_kwh else {}),
            "terminal_search_credit_per_kwh": credit,
            "forecast_adjustments_applied": True, "search_transitions": transitions,
            "evaluations": 3 if repair is not None else 1, "termination_reason": "value_tables_complete",
            "forecast_horizon_slots": horizon,
        })

    plan.capabilities = capabilities
    return wrap_battery_planner(plan)
