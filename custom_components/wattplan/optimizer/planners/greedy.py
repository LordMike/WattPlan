"""Bounded constructive tariff trades, using only request forecasts."""

from ..physics import EPSILON
from .budget import SearchBudget
from .contract import PlannerCapabilities
from .evaluation import build_result, evaluate_schedule, target_miss


def factory(*, max_evaluations=256, trade_window_slots=24):
    """Return a stateless greedy planner; legacy lookahead_slots is ignored.

    Each pass visits a ranked action once. A tariff trade buys at one slot and
    preserves that battery until one later release slot. There are no restarts,
    convergence loops, solver calls, or combinations of arbitrary neighborhoods.
    """
    SearchBudget(max_evaluations)
    if type(trade_window_slots) is not int or not 1 <= trade_window_slots <= 672:
        raise ValueError("trade_window_slots must be an integer between 1 and 672")

    def plan(params):
        batteries = params.battery_entities
        horizon = len(params.usage_kwh)
        prices = params.grid_import_price_per_kwh
        policies = [
            ["self_consume" if price > 0 else "preserve" for price in prices]
            for _ in batteries
        ]
        budget = SearchBudget(max_evaluations)
        budget.take()
        best = evaluate_schedule(params, policies)
        accepted = {"target": 0, "preserve": 0, "trade": 0, "discharge": 0}

        def consider(candidate, phase):
            nonlocal policies, best
            if not budget.take():
                return None
            evaluation = evaluate_schedule(params, candidate)
            if evaluation.score < best.score:
                policies, best = candidate, evaluation
                accepted[phase] += 1
            return evaluation

        # Construct a monotone target chain rather than demanding each action
        # immediately help. This crosses empty/full-battery plateaus without a
        # general local search; only the best fully replayed schedule is retained.
        target_allowance = max(1, max_evaluations // (4 * max(1, len(batteries))))
        for index, battery in enumerate(batteries):
            target = battery.target
            if (
                target is None
                or target_miss(target, best.steps[target.timeslot].levels[index]) == 0
            ):
                continue
            deadline = target.timeslot
            base = [row[:] for row in policies]
            base[index][: deadline + 1] = ["preserve"] * (deadline + 1)
            evaluation = consider(base, "target")
            if evaluation is None:
                break
            level = evaluation.steps[deadline].levels[index]
            direction = "grid_charge" if level < target.soc_kwh else "self_consume"
            if direction == "grid_charge" and not battery.can_charge_from & 1:
                continue
            order = sorted(
                range(deadline + 1),
                key=lambda slot: (
                    prices[slot] if direction == "grid_charge" else -prices[slot],
                    slot,
                ),
            )
            chain = base
            for slot in order[:target_allowance]:
                candidate = [row[:] for row in chain]
                candidate[index][slot] = direction
                evaluation = consider(candidate, "target")
                if evaluation is None:
                    break
                chain = candidate
                level = evaluation.steps[deadline].levels[index]
                if target_miss(target, level) == 0:
                    break
                if (
                    direction == "grid_charge"
                    and level > target.soc_kwh + target.tolerance_kwh
                ):
                    break
                if (
                    direction == "self_consume"
                    and level < target.soc_kwh - target.tolerance_kwh
                ):
                    break

        # Reserve existing energy away from the cheapest deficits first. An
        # action is retained only if the full replay improves the shared score.
        phase_limit = budget.evaluations + max(
            1, (max_evaluations - budget.evaluations) // 4
        )
        for slot in sorted(range(horizon), key=lambda slot: (prices[slot], slot)):
            for index in range(len(batteries)):
                if policies[index][slot] != "self_consume":
                    continue
                candidate = [row[:] for row in policies]
                candidate[index][slot] = "preserve"
                consider(candidate, "preserve")
                if budget.evaluations >= phase_limit or budget.termination_reason:
                    break
            if budget.evaluations >= phase_limit or budget.termination_reason:
                break

        # One best-priced release opportunity per buy slot. Preserve between
        # buying and releasing so a cheaper intervening load cannot consume the
        # bought energy first. Shared physics evaluates PV competition, ingress,
        # efficiency, taper curves, target deadlines and signed tariffs exactly.
        phase_limit = budget.evaluations + max(
            1, 2 * (max_evaluations - budget.evaluations) // 3
        )
        for slot in sorted(range(horizon), key=lambda slot: (prices[slot], slot)):
            stop = min(horizon, slot + trade_window_slots + 1)
            for index, battery in enumerate(batteries):
                if not battery.can_charge_from & 1:
                    continue
                # Buying without preservation can retain an existing target-safe
                # discharge. Try that greedy insertion before a bundled trade.
                plain = [row[:] for row in policies]
                plain[index][slot] = "grid_charge"
                if plain != policies:
                    consider(plain, "trade")
                    if budget.evaluations >= phase_limit or budget.termination_reason:
                        break
                release = max(
                    (
                        s
                        for s in range(slot + 1, stop)
                        if prices[s] > 0 and best.steps[s].grid_import > EPSILON
                    ),
                    key=lambda s: (prices[s], -s),
                    default=None,
                )
                candidate = [row[:] for row in policies]
                candidate[index][slot] = "grid_charge"
                if release is not None and prices[release] > 0:
                    candidate[index][slot + 1 : release] = ["preserve"] * (
                        release - slot - 1
                    )
                    candidate[index][release] = "self_consume"
                if candidate == policies:
                    continue
                consider(candidate, "trade")
                if budget.evaluations >= phase_limit or budget.termination_reason:
                    break
            if budget.evaluations >= phase_limit or budget.termination_reason:
                break

        # Release energy left by an accepted trade, most valuable deficits first.
        for slot in sorted(range(horizon), key=lambda slot: (-prices[slot], slot)):
            if (
                prices[slot] <= 0
                or params.usage_kwh[slot] <= params.solar_input_kwh[slot] + EPSILON
            ):
                continue
            for index in range(len(batteries)):
                if policies[index][slot] == "self_consume":
                    continue
                candidate = [row[:] for row in policies]
                candidate[index][slot] = "self_consume"
                consider(candidate, "discharge")
                if budget.termination_reason:
                    break
            if budget.termination_reason:
                break
        return build_result(
            params,
            policies,
            evaluation=best,
            diagnostics={
                **budget.diagnostics("greedy_passes_complete"),
                "accepted_changes": accepted,
                "trade_window_slots": trade_window_slots,
                "forecast_horizon_slots": horizon,
                "lookahead_slots_used": False,
                "target_miss_kwh": best.target_miss_kwh,
            },
        )

    plan.capabilities = PlannerCapabilities()
    return plan
