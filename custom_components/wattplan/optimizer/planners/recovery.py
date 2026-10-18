"""Shared reserve recovery priority and a physically constructed search seed."""

from ..physics import EPSILON, transition


# Match the original solver's per-slot minimum-energy slack preference. This is
# an optimization preference, never a billed cost or a claim of infeasibility.
RESERVE_SHORTFALL_PENALTY = 5000.0


def reserve_shortfall_cost(batteries, levels):
    """Penalize each end-of-slot shortfall without clamping physical energy."""
    return RESERVE_SHORTFALL_PENALTY * sum(
        max(battery.minimum_kwh - level, 0.0)
        for battery, level in zip(batteries, levels, strict=True)
        if level < battery.minimum_kwh - EPSILON
    )


def reserve_recovery_seed(params, policies):
    """Start recovery immediately, then retain the supplied economic policy.

    Only initially deficient requests need this construction replay. The result
    is a search candidate whose full evaluation must count against the search
    budget. PV-only batteries remain constrained to their actual ingress. This
    helper never changes the input, invents energy, or overrides a final action.
    """
    batteries = params.battery_entities
    if not any(b.initial_kwh < b.minimum_kwh - EPSILON for b in batteries):
        return policies
    rows = [list(row) for row in policies]
    levels = [b.initial_kwh for b in batteries]
    for slot, (usage, pv) in enumerate(
        zip(params.usage_kwh, params.solar_input_kwh, strict=True)
    ):
        if all(
            level >= b.minimum_kwh - EPSILON
            for b, level in zip(batteries, levels, strict=True)
        ):
            break
        for index, (battery, level) in enumerate(zip(batteries, levels, strict=True)):
            if level < battery.minimum_kwh - EPSILON:
                rows[index][slot] = (
                    "grid_charge" if battery.can_charge_from & 1 else "preserve"
                )
        levels = transition(
            batteries, levels, [row[slot] for row in rows], usage=usage, pv=pv
        ).levels
    return rows
