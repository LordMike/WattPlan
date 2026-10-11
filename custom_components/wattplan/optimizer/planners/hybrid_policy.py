"""Choose one battery policy from a causal forecast-surplus gate.

The next 24 supplied forecast hours, after shared comfort placement, determine
whether passive self-consumption replaces interpolated value dispatch. Enough PV
surplus must cover the configured multiple of both forecast deficits and current
AC storage headroom. This is a coarse forecast heuristic, not a guarantee that
PV arrives before demand or that all surplus can enter the battery. It can miss
tariff opportunities on solar-rich days and inherit persistence forecast errors.

Targets, low reserves, multiple batteries, PV-ineligible batteries, signed negative
tariffs and nonzero wear/switch preferences retain value dispatch. Exactly one
battery implementation runs; the shared wrapper places comfort and restores its
continuity once. Reporting replays unchanged forecasts and ranks optional loads
as independent recommendations. Value dispatch retains its approximate energy
interpolation and documented joint-DP fallback for fleets. No actual observations,
calendar rules or cached optimizer schedules enter selection.

``terminal_value_fraction`` passes through to the selected value policy's explicit
stored-energy search heuristic. It does not change the passive branch or credit
reported electricity cost; terminal battery levels remain visible. Zero is the
default, and positive fractions can retain energy past useful opportunities.
``pv_scenario_spread`` passes through the value policy's independent symmetric
per-slot PV uncertainty assumption. It leaves gate inputs and reported physical
projections unchanged and is unused when the passive branch is selected.
Optional ``prerefill_guard`` rejects passive dispatch when it predicts a dear
import before natural PV charging restores the initial SoC, and an earlier cheap
grid buy is locally feasible. It probes shared physics, not a full alternative
action sequence; intervening consumption and saturation can change the potential
benefit. This heuristic adds prefix transitions but never a second planner call.
"""

import math

from ..physics import EPSILON, transition
from .contract import PlannerCapabilities
from .self_consumption import factory as baseline_factory
from .shared import wrap_battery_planner
from .value_policy import factory as value_factory


def _prerefill_opportunity(params, slots):
    """Probe locally feasible pre-solar trades, without promising their benefit."""
    battery = params.battery_entities[0]
    level = battery.initial_kwh
    efficiency = battery.charge_efficiency * battery.discharge_efficiency
    cheapest = None
    details = {"reject_passive": False, "refill_slot": None,
               "dear_deficit_slot": None, "earlier_buy_slot": None,
               "potential_import_cover_kwh": 0.0, "probe_transitions": 0}
    for slot in range(slots):
        usage, pv = params.usage_kwh[slot], params.solar_input_kwh[slot]
        price = params.grid_import_price_per_kwh[slot]
        passive = transition([battery], [level], ["self_consume"], usage=usage, pv=pv)
        details["probe_transitions"] += 1
        if (passive.pv_charge[0] > EPSILON
            and (passive.levels[0] >= battery.initial_kwh - EPSILON
                 or passive.levels[0] >= battery.capacity_kwh - EPSILON)):
            details["refill_slot"] = slot
            break
        if (passive.grid_import > EPSILON and cheapest is not None
            and cheapest[0] / efficiency + EPSILON < price):
            details.update(reject_passive=True, dear_deficit_slot=slot,
                earlier_buy_slot=cheapest[1], potential_import_cover_kwh=min(
                    passive.grid_import, cheapest[2] * efficiency))
            break
        if battery.can_charge_from & 1:
            buy = transition([battery], [level], ["grid_charge"], usage=usage, pv=pv)
            details["probe_transitions"] += 1
            if buy.grid_charge[0] > EPSILON and (cheapest is None or price < cheapest[0]):
                cheapest = (price, slot, buy.grid_charge[0])
        level = passive.levels[0]
    return details


def factory(*, surplus_ratio=1.5, energy_intervals=4, terminal_value_fraction=0.0,
            pv_scenario_spread=0.0, prerefill_guard=False):
    """Use passive dispatch only when forecast surplus clears the gate."""
    if (type(surplus_ratio) not in {int, float}
        or not math.isfinite(surplus_ratio) or surplus_ratio <= 0):
        raise ValueError("surplus_ratio must be finite and positive")
    if type(prerefill_guard) is not bool:
        raise ValueError("prerefill_guard must be a boolean")
    value = value_factory(energy_intervals=energy_intervals,
                          terminal_value_fraction=terminal_value_fraction,
                          pv_scenario_spread=pv_scenario_spread).__wrapped__
    baseline = baseline_factory().__wrapped__

    def plan(params):
        slots = min(len(params.usage_kwh), math.ceil(24 * 60 / params.slot_minutes))
        surplus = sum(max(pv - use, 0.0) for pv, use in
                      zip(params.solar_input_kwh[:slots], params.usage_kwh[:slots], strict=True))
        deficit = sum(max(use - pv, 0.0) for pv, use in
                      zip(params.solar_input_kwh[:slots], params.usage_kwh[:slots], strict=True))
        batteries = params.battery_entities
        reason, headroom = None, 0.0
        if len(batteries) != 1:
            reason = "joint_or_empty_battery_request"
        else:
            battery = batteries[0]
            headroom = max(battery.capacity_kwh - battery.initial_kwh, 0.0) / battery.charge_efficiency
            if battery.target is not None:
                reason = "battery_target"
            elif battery.initial_kwh < battery.minimum_kwh - EPSILON:
                reason = "reserve_recovery"
            elif not battery.can_charge_from & 2:
                reason = "pv_ingress_forbidden"
        if reason is None and (params.throughput_cost_per_kwh or params.mode_switch_cost
                               or params.action_deadband_kwh):
            reason = "battery_preferences"
        if reason is None and any(price < 0 for price in
                                 (*params.grid_import_price_per_kwh, *params.grid_export_price_per_kwh)):
            reason = "negative_tariff"
        passive = reason is None and surplus > EPSILON and surplus >= surplus_ratio * max(deficit, headroom)
        prerefill = None
        if passive and prerefill_guard:
            prerefill = _prerefill_opportunity(params, slots)
            if prerefill["reject_passive"]:
                passive, reason = False, "dear_deficit_before_pv_refill"
        result = (baseline if passive else value)(params)
        result.setdefault("diagnostics", {})["hybrid_gate"] = {
            "selected_policy": "self_consumption" if passive else "value_policy",
            "forced_value_reason": reason,
            "surplus_ratio": surplus_ratio,
            "forecast_surplus_kwh": surplus,
            "forecast_deficit_kwh": deficit,
            "ac_storage_headroom_kwh": headroom,
            "gate_forecast_slots": slots,
            "gate_forecast_hours": slots * params.slot_minutes / 60,
            "energy_intervals": energy_intervals,
            "terminal_value_fraction": terminal_value_fraction,
            "pv_scenario_spread": pv_scenario_spread,
            "prerefill_guard": prerefill_guard,
            "prerefill_probe": prerefill,
            "evaluated_policy_count": 1,
        }
        return result

    plan.capabilities = PlannerCapabilities()
    return wrap_battery_planner(plan)
