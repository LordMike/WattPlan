"""Search complete battery schedules through coarse-to-fine operating windows.

Implementation and differences:
    Begin with a physically constructed deadline/reserve policy and tariff seeds.
    Search deadline-anchored intervals, then progressively smaller cheap-charge,
    preservation and valuable-discharge windows. Paired moves change charge,
    intervening preservation and later release together, so an empty-battery
    plateau does not require accepting a worse intermediate schedule. Boundaries
    of existing runs are also refined. This uses neither a native solver nor an
    exhaustive list of all possible intervals; every candidate gets full replay.

Properties and ordering biases:
    A natural fit for broad tariff valleys/peaks and coherent operating runs.
    Scales reach single slots, but only a bounded set of separated, ranked
    windows is visited, so fragmented opportunities can be missed. Search moves
    generally change one battery; joint multi-battery improvements may need
    intermediate moves that the greedy acceptance rule rejects.
    The initial target construction charges as soon as needed, not necessarily
    at the cheapest time. Small budgets can leave that conservative seed intact.
    Deadline intervals are searched before tariff-only intervals, in battery
    input order. Tariff refinement rotates battery order between scales and
    reserves work for finer scales, reducing but not eliminating ordering bias.
    A slot-battery work cap reduces evaluations on large requests. Optional time
    limits are cooperative; initial replay and final reporting are mandatory.

Loads and shared behavior:
    ``shared`` places comfort before this search, using self-consumption replay;
    comfort start times are not charge-window decision variables. ``evaluation``
    supplies physics and target-first scoring, and ``reporting`` ranks each
    optional appliance afterward under the selected policies. Appliance cost
    ties favor earlier starts, but recommendations neither run loads first nor
    coordinate several optional appliances. Discrete modes do not guarantee
    exact targets or global optimality. Battery search is stateless.

Configuration:
    Named presets are defined in ``presets.py``. See
    ``docs/optimizer-profiles.md`` for user-facing choices and limitations.
"""

import math

from ..physics import EPSILON, transition
from .budget import SearchBudget
from .contract import PlannerCapabilities
from .evaluation import build_result, evaluate_schedule
from .recovery import reserve_recovery_seed
from .shared import wrap_battery_planner


_MAX_SLOT_BATTERY_EVALUATIONS = 196_608


def _copy(policies):
    return [row.copy() for row in policies]


def _initial_seed(params):
    """Construct a deadline-aware baseline before spending the first evaluation.

    A single physical pass establishes real charge/discharge progress, respecting
    curves, ingress, efficiencies and shared demand. Window search still refines
    it; this construction is not a feasibility or optimality guarantee.
    """
    batteries = params.battery_entities
    rows = [["self_consume"] * len(params.usage_kwh) for _ in batteries]
    last_deadline = max((b.target.timeslot for b in batteries if b.target), default=-1)
    levels = [b.initial_kwh for b in batteries]
    for slot in range(last_deadline + 1):
        for index, (battery, level) in enumerate(zip(batteries, levels, strict=True)):
            target = battery.target
            if target is None or slot > target.timeslot:
                continue
            lower = target.soc_kwh - target.tolerance_kwh
            upper = target.soc_kwh + target.tolerance_kwh
            if target.mode != "at_most" and level < lower - EPSILON:
                mode = "grid_charge" if battery.can_charge_from & 1 else "preserve"
            elif target.mode != "at_least" and level > upper + EPSILON:
                mode = "self_consume"
            else:
                mode = "preserve"
            rows[index][slot] = mode
        levels = transition(
            batteries, levels, [row[slot] for row in rows],
            usage=params.usage_kwh[slot], pv=params.solar_input_kwh[slot],
        ).levels
    return reserve_recovery_seed(params, rows), last_deadline + 1


def _windows(prices, usage, pv, width, count):
    """Rank a linear stream; retain only bounded cheap and valuable windows."""
    price_sum = [0.0]
    value_sum = [0.0]
    for price, load, solar in zip(prices, usage, pv, strict=True):
        price_sum.append(price_sum[-1] + price)
        value_sum.append(value_sum[-1] + max(load - solar, 0.0) * price)
    def ranked(prefix, descending=False):
        selected = []
        # Repeated linear scans retain only count intervals. Suppression avoids
        # spending every candidate on overlapping copies of the same peak.
        separation = max(width, len(prices) // (2 * count))
        for _ in range(count):
            start = min(
                (s for s in range(len(prices) - width + 1)
                 if all(abs(s - chosen) >= separation for chosen in selected)),
                key=lambda s: ((-1 if descending else 1) * (prefix[s + width] - prefix[s]), s),
                default=None,
            )
            if start is None:
                break
            selected.append(start)
        return selected

    cheap = ranked(price_sum)
    release = ranked(value_sum, descending=True)
    return cheap, release, price_sum


def _seeds(params):
    horizon = len(params.usage_kwh)
    prices = params.grid_import_price_per_kwh
    ordered = sorted(prices)
    for fraction in (0.0, 0.2, 0.4, 0.6):
        threshold = ordered[int(fraction * (horizon - 1))]
        rows = []
        for battery in params.battery_entities:
            efficiency = battery.charge_efficiency * battery.discharge_efficiency
            rows.append([
                "grid_charge" if battery.can_charge_from & 1 and price <= threshold
                else "self_consume" if price * efficiency > threshold + params.throughput_cost_per_kwh
                else "preserve"
                for price in prices
            ])
        yield f"tariff-{fraction}", rows
    if any(b.target for b in params.battery_entities):
        maximum = [["self_consume"] * horizon for _ in params.battery_entities]
        estimated = _copy(maximum)
        for index, battery in enumerate(params.battery_entities):
            target = battery.target
            if target is None or target.mode == "at_most":
                continue
            end = target.timeslot + 1
            maximum[index][:end] = [
                "grid_charge" if battery.can_charge_from & 1 else "preserve"
            ] * end
            estimated[index][:end] = ["preserve"] * end
            if battery.can_charge_from & 1:
                rate = max(battery.charge_curve_kwh) * battery.charge_efficiency
                count = min(end, math.ceil(max(0, target.soc_kwh - battery.initial_kwh) / rate)) if rate else 0
                for slot in sorted(range(end), key=lambda s: (prices[s], s))[:count]:
                    estimated[index][slot] = "grid_charge"
        yield "target-estimate", estimated
        yield "target-maximum", maximum


def factory(*, max_evaluations=128, max_seconds=None, window_candidates=3):
    """Score full horizons; options bound evaluations and interval ranking.

    Windows span every scale down to single slots. Paired moves buy a contiguous
    block, preserve until a later contiguous release block, and change all three
    intervals atomically, crossing the plateau of an empty battery.
    """
    SearchBudget(max_evaluations, max_seconds)
    if type(window_candidates) is not int or not 1 <= window_candidates <= 16:
        raise ValueError("window_candidates must be an integer between 1 and 16")

    def plan(params):
        horizon = len(params.usage_kwh)
        batteries = params.battery_entities
        work = horizon * max(1, len(batteries))
        limit = min(max_evaluations, max(1, _MAX_SLOT_BATTERY_EVALUATIONS // work))
        budget = SearchBudget(limit)
        # Count the unavoidable complete physical baseline before enabling time
        # checks. Even an already-expired tiny deadline returns a useful result.
        budget.take()
        budget.max_seconds = max_seconds
        best, target_seed_transitions = _initial_seed(params)
        best_eval = evaluate_schedule(params, best)
        baseline_score = best_eval.score
        seen = {tuple(tuple(row) for row in best)}
        initial_label = (
            "target-and-reserve" if target_seed_transitions
            else "reserve-recovery" if any(b.initial_kwh < b.minimum_kwh - EPSILON for b in batteries)
            else "self-consumption"
        )
        seeds = [initial_label]
        attempts, attempt_limit = 1, limit * 8
        accepted = {"seed": 0, "target": 0, "paired_window": 0, "window": 0, "boundary": 0}
        scales_visited = []

        def available():
            return budget.termination_reason is None and attempts < attempt_limit

        def consider(candidate, phase):
            nonlocal best, best_eval, attempts
            if not available():
                return False
            attempts += 1
            key = tuple(tuple(row) for row in candidate)
            if key in seen or not budget.take():
                return False
            seen.add(key)
            evaluation = evaluate_schedule(params, candidate)
            if evaluation.score < best_eval.score:
                best, best_eval = candidate, evaluation
                accepted[phase] += 1
            return True

        if batteries:
            for label, candidate in _seeds(params):
                if not available():
                    break
                if consider(candidate, "seed"):
                    seeds.append(label)

        # Deadline-anchored charge/preserve/release intervals are searched before
        # tariff-only windows. Geometric lengths cover long targets without an
        # exhaustive list of all possible start/end pairs.
        for index, battery in enumerate(batteries):
            target = battery.target
            if target is None:
                continue
            end = target.timeslot + 1
            lengths = {1, end}
            rate = max(battery.charge_curve_kwh) * battery.charge_efficiency
            if rate:
                estimate = math.ceil(abs(target.soc_kwh - battery.initial_kwh) / rate)
                lengths.update(max(1, min(end, estimate + delta)) for delta in (-1, 0, 1))
            width = 2
            while width < end:
                lengths.add(width)
                width *= 2
            for length in sorted(lengths):
                if not available():
                    break
                for start in dict.fromkeys((0, end - length)):
                    for mode in ("grid_charge", "self_consume", "preserve"):
                        if not available():
                            break
                        if mode == "grid_charge" and not battery.can_charge_from & 1:
                            continue
                        candidate = _copy(best)
                        candidate[index][:end] = ["preserve"] * end
                        candidate[index][start:start + length] = [mode] * length
                        consider(candidate, "target")

        widths = []
        width = 1
        while width < horizon:
            widths.append(width)
            width *= 2
        widths.append(horizon)
        # Seed policies establish a coarse tariff solution; descending interval
        # scales refine that solution. No O(horizon squared) interval allocation.
        for scale_index, width in enumerate(reversed(widths)):
            if not batteries or not available():
                break
            scales_visited.append(width)
            # Reserve evaluations for every finer scale, so early large moves
            # cannot consume the entire count budget.
            scale_limit = budget.evaluations + max(1, (limit - budget.evaluations) // (len(widths) - scale_index))
            def scale_available():
                return available() and budget.evaluations < scale_limit
            cheap, release, price_sum = _windows(
                params.grid_import_price_per_kwh,
                params.usage_kwh,
                params.solar_input_kwh,
                width,
                window_candidates,
            )
            # Rotate battery order between scales so bounded search visits all
            # equipment rather than exhausting its budget on the first battery.
            order = list(range(len(batteries)))
            offset = len(scales_visited) % len(order)
            order = order[offset:] + order[:offset]
            for rank in range(window_candidates):
                for index in order:
                    if not scale_available():
                        break
                    battery = batteries[index]
                    if battery.can_charge_from & 1 and rank < len(release):
                        stop = release[rank]
                        # Rank charging BEFORE this release, not independently
                        # over the full horizon where the cheapest may be later.
                        start = min(
                            range(max(0, stop - width + 1)),
                            key=lambda s: (price_sum[s + width] - price_sum[s], s),
                            default=None,
                        )
                        if start is not None:
                            candidate = _copy(best)
                            candidate[index][start:start + width] = ["grid_charge"] * width
                            candidate[index][start + width:stop] = ["preserve"] * (stop - start - width)
                            candidate[index][stop:stop + width] = ["self_consume"] * width
                            consider(candidate, "paired_window")
                    for mode, anchors in (("grid_charge", cheap), ("preserve", cheap), ("self_consume", release)):
                        if not scale_available():
                            break
                        if rank >= len(anchors) or (mode == "grid_charge" and not battery.can_charge_from & 1):
                            continue
                        start = anchors[rank]
                        candidate = _copy(best)
                        candidate[index][start:start + width] = [mode] * width
                        consider(candidate, "window")
            # Refine existing run boundaries by the current scale, including
            # target-seed boundaries that tariff ranking did not discover.
            for index in order:
                if not scale_available():
                    break
                row = best[index]
                boundaries = (s for s in range(1, horizon) if row[s] != row[s - 1])
                for boundary in boundaries:
                    if not scale_available():
                        break
                    for start, stop, mode in (
                        (max(0, boundary - width), boundary, row[boundary]),
                        (boundary, min(horizon, boundary + width), row[boundary - 1]),
                    ):
                        if not scale_available():
                            break
                        candidate = _copy(best)
                        candidate[index][start:stop] = [mode] * (stop - start)
                        consider(candidate, "boundary")

        return build_result(
            params, best, evaluation=best_eval,
            diagnostics={
                **budget.diagnostics("candidate_attempt_limit" if attempts >= attempt_limit else "window_search_complete" if batteries else "no_batteries"),
                "candidate_attempts": attempts,
                "candidate_attempt_limit": attempt_limit,
                "requested_max_evaluations": max_evaluations,
                "effective_max_evaluations": limit,
                "slot_battery_work_limit": _MAX_SLOT_BATTERY_EVALUATIONS,
                "window_candidates": window_candidates,
                "seeds_evaluated": seeds,
                "target_seed_transitions": target_seed_transitions,
                "seed_construction": "physical target and reserve preparation before counted evaluation",
                "accepted_changes": accepted,
                "scales_visited": scales_visited,
                "baseline_score": list(baseline_score),
                "scored_horizon_slots": horizon,
                "lookahead_semantics": "full supplied horizon; lookahead_slots ignored",
                "state_reuse": "stateless; current request evaluated independently",
                "limitations": ["bounded heuristic; feasibility and global optimum are not guaranteed", "discrete modes cannot request partial charging or battery export"],
            },
        )

    plan.capabilities = PlannerCapabilities()
    return wrap_battery_planner(plan)
