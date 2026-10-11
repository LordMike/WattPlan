"""Select hybrid PV uncertainty from the current supplied forecast only.

After shared comfort placement, the next 24 forecast hours provide surplus and
deficit energy. Surplus divided by the larger of deficit and AC storage headroom
selects one configured symmetric PV scenario spread before one raw hybrid call.
An independent threshold optionally selects a search-only grid charging premium
from the same ratio. Both premium defaults are zero, preserving existing behavior.
The hybrid passive gate and all constrained-feature fallbacks remain intact.
Fleet headroom is summed for selection, although hybrid always uses value dispatch
for fleets and its joint-state fallback does not apply PV scenario assumptions.
The premium is currency per AC kWh entering battery grid charging in value search;
passive dispatch, fleet fallback, and target repair omit it. Selected-premium
diagnostics describe configuration, not its use in every accepted policy.

This is a forecast heuristic, not calibrated uncertainty: optimistic forecasts
can select less protection. No dates, histories, observations, or cached plans
enter selection. Reporting retains original forecasts and comfort runs once.
Reported tariff costs and evaluation scores exclude the premium.
"""

import math

from .contract import PlannerCapabilities
from .hybrid_policy import factory as hybrid_factory
from .shared import wrap_battery_planner


def factory(*, low_surplus_spread=1.0, high_surplus_spread=0.0,
            spread_switch_ratio=0.5, surplus_ratio=1.5, energy_intervals=6,
            terminal_value_fraction=0.0, prerefill_guard=False,
            low_surplus_grid_charge_premium_per_kwh=0.0,
            high_surplus_grid_charge_premium_per_kwh=0.0, premium_switch_ratio=0.01):
    """Select spread and premium independently at their forecast ratio thresholds."""
    for name, value in (("low_surplus_spread", low_surplus_spread),
                        ("high_surplus_spread", high_surplus_spread)):
        if (type(value) not in {int, float} or not math.isfinite(value)
                or not 0 <= value <= 1):
            raise ValueError(f"{name} must be finite and between zero and one")
    if (type(spread_switch_ratio) not in {int, float}
            or not math.isfinite(spread_switch_ratio) or spread_switch_ratio <= 0):
        raise ValueError("spread_switch_ratio must be finite and positive")
    for name, value in (("low_surplus_grid_charge_premium_per_kwh", low_surplus_grid_charge_premium_per_kwh),
                        ("high_surplus_grid_charge_premium_per_kwh", high_surplus_grid_charge_premium_per_kwh)):
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    if (type(premium_switch_ratio) not in {int, float}
            or not math.isfinite(premium_switch_ratio) or premium_switch_ratio <= 0):
        raise ValueError("premium_switch_ratio must be finite and positive")

    options = dict(surplus_ratio=surplus_ratio, energy_intervals=energy_intervals,
                   terminal_value_fraction=terminal_value_fraction,
                   prerefill_guard=prerefill_guard)
    spreads = (low_surplus_spread, high_surplus_spread)
    premiums = (low_surplus_grid_charge_premium_per_kwh, high_surplus_grid_charge_premium_per_kwh)
    implementations = {
        (spread_high, premium_high): hybrid_factory(
            pv_scenario_spread=spreads[spread_high],
            grid_charge_premium_per_kwh=premiums[premium_high], **options).__wrapped__
        for spread_high in (False, True) for premium_high in (False, True)
    }
    premium_enabled = any(premiums)

    def plan(params):
        slots = min(len(params.usage_kwh), math.ceil(24 * 60 / params.slot_minutes))
        surplus = sum(max(pv - use, 0.0) for pv, use in zip(
            params.solar_input_kwh[:slots], params.usage_kwh[:slots], strict=True))
        deficit = sum(max(use - pv, 0.0) for pv, use in zip(
            params.solar_input_kwh[:slots], params.usage_kwh[:slots], strict=True))
        headroom = sum(max(b.capacity_kwh - b.initial_kwh, 0.0) / b.charge_efficiency
                       for b in params.battery_entities)
        denominator = max(deficit, headroom)
        # A positive surplus with no deficit or headroom has an unbounded ratio;
        # represent it explicitly rather than emitting non-JSON infinity.
        ratio = surplus / denominator if denominator else (None if surplus else 0.0)
        high_surplus = surplus > 0 and (ratio is None or ratio >= spread_switch_ratio)
        spread = high_surplus_spread if high_surplus else low_surplus_spread
        premium_high = surplus > 0 and (ratio is None or ratio >= premium_switch_ratio)
        result = implementations[high_surplus, premium_high](params)
        result.setdefault("diagnostics", {})["adaptive_solar"] = {
            "selected_regime": "high_surplus" if high_surplus else "low_surplus",
            "selected_pv_scenario_spread": spread,
            "low_surplus_spread": low_surplus_spread,
            "high_surplus_spread": high_surplus_spread,
            "spread_switch_ratio": spread_switch_ratio,
            "forecast_surplus_kwh": surplus,
            "forecast_deficit_kwh": deficit,
            "ac_storage_headroom_kwh": headroom,
            "ratio_denominator_kwh": denominator,
            "forecast_surplus_ratio": ratio,
            "ratio_unbounded": ratio is None,
            "gate_forecast_slots": slots,
            "gate_forecast_hours": slots * params.slot_minutes / 60,
            "evaluated_policy_count": 1,
            **({
                "selected_premium_regime": "high_surplus" if premium_high else "low_surplus",
                "selected_grid_charge_premium_per_kwh": premiums[premium_high],
                "low_surplus_grid_charge_premium_per_kwh": low_surplus_grid_charge_premium_per_kwh,
                "high_surplus_grid_charge_premium_per_kwh": high_surplus_grid_charge_premium_per_kwh,
                "premium_switch_ratio": premium_switch_ratio,
            } if premium_enabled else {}),
        }
        return result

    plan.capabilities = PlannerCapabilities()
    return wrap_battery_planner(plan)
