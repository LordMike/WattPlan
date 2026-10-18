"""Use loss-aware tariff thresholds with a bounded forecast solar reserve.

This deterministic policy constructs one complete schedule with shared physical
transitions, then performs one counted evaluation for reporting. It never reads
recorded actuals or retains a history between requests. Optional demand reserve
releases low-price energy when enough remains for higher-price forecast deficits
before a meaningful PV refill (one tenth of battery capacity in a slot). It caps
that scan at reserve_slots; grid_enabled can disable economic grid purchases
while retaining mandatory below-reserve and deadline recovery. Thresholds come only from
the supplied forecast, so inaccurate tariff persistence can still trigger poor
arbitrage. A positive margin discourages weak trades and ``pv_reserve`` discounts
forecast surplus to leave room for solar without trusting it completely.

Unlike search planners, construction does not optimize the forecast objective:
conservative rejection of apparently profitable trades is deliberate. Grid
charging is discrete and cannot request fractional power; therefore the reserve
is a gating threshold, not an exact storage target. Future demand and surplus
estimates ignore inter-battery allocation while the physical pass and final
report use the actual shared allocation. Battery order can affect PV ingress.

Comfort is placed first by the shared wrapper; optional appliances are advisory
recommendations priced afterward. Target-bearing batteries use the established
physical deadline seed, prioritizing feasibility over this tariff rule, and
reserve recovery uses the same shared policy as other non-solver planners.
The full supplied horizon is reported, with no terminal energy credit. Exports
are surplus PV only: the inverter-policy contract does not support battery export.
Construction is O(batteries * horizon * reserve_slots) with bounded reserve slots;
there is no candidate search, wall deadline, random seed, or optimality claim.
"""

import math

from ..physics import EPSILON, transition
from .charge_window import _initial_seed
from .contract import PlannerCapabilities
from .evaluation import build_result, evaluate_schedule
from .shared import wrap_battery_planner


def factory(*, charge_quantile=0.15, discharge_quantile=0.35, margin=0.10,
            pv_reserve=0.5, reserve_slots=12, max_fill=1.0,
            demand_reserve=False, future_price_margin=0.1, grid_enabled=True):
    """Construct a causal threshold policy; all numerical options are explicit."""
    for name, value in (("charge_quantile", charge_quantile),
                        ("discharge_quantile", discharge_quantile),
                        ("pv_reserve", pv_reserve), ("max_fill", max_fill)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{name} must be a finite number between zero and one")
    if isinstance(margin, bool) or not isinstance(margin, (int, float)) or not math.isfinite(margin) or margin < 0:
        raise ValueError("margin must be finite and nonnegative")
    if type(demand_reserve) is not bool or type(grid_enabled) is not bool:
        raise ValueError("demand_reserve and grid_enabled must be booleans")
    if isinstance(future_price_margin, bool) or not isinstance(future_price_margin, (int, float)) or not math.isfinite(future_price_margin) or future_price_margin < 0:
        raise ValueError("future_price_margin must be finite and nonnegative")
    if type(reserve_slots) is not int or not 1 <= reserve_slots <= 96:
        raise ValueError("reserve_slots must be an integer between 1 and 96")

    def plan(params):
        batteries = params.battery_entities
        horizon = len(params.usage_kwh)
        prices = params.grid_import_price_per_kwh
        ordered = sorted(prices)
        cheap = ordered[int(charge_quantile * (horizon - 1))]
        dear = ordered[int(discharge_quantile * (horizon - 1))]
        rows = [["self_consume"] * horizon for _ in batteries]
        target_rows, _ = _initial_seed(params) if any(b.target for b in batteries) else (None, 0)
        levels = [b.initial_kwh for b in batteries]
        for slot in range(horizon):
            end = min(horizon, slot + reserve_slots + 1)
            surplus = sum(max(params.solar_input_kwh[s] - params.usage_kwh[s], 0) for s in range(slot + 1, end))
            for index, battery in enumerate(batteries):
                efficiency = battery.charge_efficiency * battery.discharge_efficiency
                fill_limit = battery.minimum_kwh + max_fill * (battery.capacity_kwh - battery.minimum_kwh)
                if battery.can_charge_from & 2:
                    fill_limit -= pv_reserve * surplus * battery.charge_efficiency
                # Avoid filling for weak spreads. Loss-adjusted future avoided
                # import value must exceed today's cost plus throughput margin.
                charge = (grid_enabled and battery.can_charge_from & 1 and prices[slot] <= cheap
                          and prices[slot] + margin + params.throughput_cost_per_kwh < dear * efficiency
                          and levels[index] < max(battery.minimum_kwh, fill_limit) - EPSILON)
                mode = "grid_charge" if charge else "self_consume" if prices[slot] >= dear else "preserve"
                if demand_reserve and mode == "preserve":
                    # Keep only energy needed by more expensive deficits before
                    # the next meaningful forecast solar refill. A tiny surplus
                    # does not end the reserve window. The shared transition
                    # still determines real ingress and proportional discharge.
                    needed = 0.0
                    for future in range(slot + 1, end):
                        solar = params.solar_input_kwh[future]
                        usage = params.usage_kwh[future]
                        if (battery.can_charge_from & 2
                                and solar - usage >= 0.1 * battery.capacity_kwh):
                            break
                        if prices[future] > prices[slot] + future_price_margin:
                            needed += max(usage - solar, 0) / battery.discharge_efficiency
                    current_need = max(params.usage_kwh[slot] - params.solar_input_kwh[slot], 0) / battery.discharge_efficiency
                    available = max(levels[index] - battery.minimum_kwh, 0)
                    if needed <= EPSILON or available - current_need >= needed - EPSILON:
                        mode = "self_consume"
                if levels[index] < battery.minimum_kwh - EPSILON:
                    mode = "grid_charge" if battery.can_charge_from & 1 else "preserve"
                if battery.target and slot <= battery.target.timeslot:
                    mode = target_rows[index][slot]
                rows[index][slot] = mode
            levels = transition(batteries, levels, [row[slot] for row in rows],
                                usage=params.usage_kwh[slot], pv=params.solar_input_kwh[slot]).levels
        evaluation = evaluate_schedule(params, rows)
        return build_result(params, rows, evaluation=evaluation, diagnostics={
            "evaluations": 1, "termination_reason": "threshold_policy_complete",
            "charge_quantile": charge_quantile, "discharge_quantile": discharge_quantile,
            "margin": margin, "pv_reserve": pv_reserve, "reserve_slots": reserve_slots,
            "max_fill": max_fill, "demand_reserve": demand_reserve,
            "future_price_margin": future_price_margin, "grid_enabled": grid_enabled, "charge_threshold": cheap, "discharge_threshold": dear,
            "scored_horizon_slots": horizon,
            "lookahead_semantics": "full supplied horizon; lookahead_slots ignored",
            "state_reuse": "stateless; current request evaluated independently",
            "limitations": ["forecast threshold heuristic; no optimality guarantee", "discrete grid charging can exceed intended solar headroom"],
        })

    plan.capabilities = PlannerCapabilities()
    return wrap_battery_planner(plan)
