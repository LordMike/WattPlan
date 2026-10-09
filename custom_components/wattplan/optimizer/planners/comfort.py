"""Shared comfort scheduling around battery-only planner implementations.

Constraint construction is the same as HiGHS. Bounded placement uses battery
self-consumption replay as its cost estimate before the selected battery search.
It is a heuristic; it does not call a second planner or a native solver.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
import json

from ..comfort_placement import ComfortPlacementInput, place_comfort_schedules
from ..comfort_schedule import fixed_comfort_schedule, initial_comfort_history
from ..models import OptimizationParams, encode_state_blob
from .evaluation import evaluate_schedule


def _fingerprint(params):
    return {
        "slot_minutes": params.slot_minutes,
        "window": params.rolling_window_slots,
        "entities": [
            entity.model_dump(exclude={
                "is_on_now", "on_slots_last_rolling_window", "on_history",
                "off_streak_slots_now", "measured_power_source",
                "recent_avg_on_power_kw",
            })
            for entity in params.comfort_entities
        ],
    }


def _advance(entity, schedule, mode, remaining):
    """Return lock, runtime and history trajectories at each slot boundary."""
    previous = entity.is_on_now
    off_streak = 0 if previous else entity.off_streak_slots_now
    boundaries = [(bool(mode), int(remaining), bool(previous), int(off_streak))]
    for enabled in schedule:
        if enabled != previous:
            minimum = (entity.min_consecutive_on_slots if enabled
                       else entity.min_consecutive_off_slots)
            mode, remaining = enabled, minimum - 1
        else:
            remaining = max(remaining - 1, 0)
            mode = enabled
        off_streak = 0 if enabled else off_streak + 1
        previous = enabled
        boundaries.append((bool(mode), int(remaining), bool(previous), int(off_streak)))
    return boundaries


def _initial_locks(params, prior):
    modes, remaining = [], []
    for entity in params.comfort_entities:
        mode = entity.is_on_now
        if mode:
            # Ordered observations can establish an unfinished ON run even if
            # this is the first planner invocation or the preset has changed.
            streak = 0
            for value in reversed(entity.on_history or []):
                if not value:
                    break
                streak += 1
            lock = max(entity.min_consecutive_on_slots - streak, 0) if streak else 0
        else:
            lock = max(entity.min_consecutive_off_slots - entity.off_streak_slots_now, 0)
        modes.append(mode)
        remaining.append(lock)
    status = "no_previous_comfort_state"
    if not prior:
        return modes, remaining, status
    try:
        if prior["fingerprint"] != _fingerprint(params):
            return modes, remaining, "configuration_changed"
        if params.plan_start is None or prior["plan_start"] is None:
            return modes, remaining, "unaligned_plan_start"
        elapsed = (params.plan_start - datetime.fromisoformat(prior["plan_start"])).total_seconds()
        slot_seconds = params.slot_minutes * 60
        if elapsed < 0 or elapsed % slot_seconds:
            return modes, remaining, "unaligned_plan_start"
        offset = int(elapsed // slot_seconds)
        for index, entity in enumerate(params.comfort_entities):
            old = prior["entities"][index]
            old_mode, old_remaining, old_on, old_off = old["boundaries"][offset]
            if old_on != entity.is_on_now or (not old_on and old_off != entity.off_streak_slots_now):
                return modes, remaining, "runtime_changed"
            history_size = params.rolling_window_slots - 1
            expected_history = (old["history"] + old["schedule"][:offset])[-history_size:] if history_size else []
            if entity.on_history is not None and list(entity.on_history) != expected_history:
                return modes, remaining, "history_changed"
        # Apply only after all runtime observations agree with the trajectory.
        for index, old in enumerate(prior["entities"]):
            mode, lock, _, _ = old["boundaries"][offset]
            modes[index], remaining[index] = bool(mode), max(remaining[index], int(lock))
        return modes, remaining, "aligned"
    except (KeyError, TypeError, IndexError, ValueError, OverflowError):
        return modes, remaining, "invalid_previous_comfort_state"


@dataclass
class PreparedComfort:
    """Battery request plus the comfort result to merge after battery reporting."""

    original: OptimizationParams
    params: OptimizationParams
    inputs: tuple = ()
    placement: object | None = None
    continuity: str = "no_comfort"

    def finalize(self, result: dict) -> dict:
        if self.placement is None:
            return result
        result = dict(result)
        entities = list(result.get("entities", []))
        reasons = set(result.get("suboptimal_reasons", []))
        state_entities, violations = [], []
        for entity, constraint, schedule, entity_violations in zip(
            self.original.comfort_entities, self.inputs,
            self.placement.schedules, self.placement.violations, strict=True,
        ):
            history = list(constraint.on_history)
            entries = []
            for enabled in schedule:
                # Public level is remaining ON-slot requirement after this slot,
                # computed from the W-1 observations carried to the next slot.
                history.append(enabled)
                history = history[-(self.original.rolling_window_slots - 1):] if self.original.rolling_window_slots > 1 else []
                entries.append({"enabled": enabled, "level": float(max(entity.target_on_slots_per_rolling_window - sum(history), 0))})
            entities.append({"name": entity.name, "type": "comfort", "schedule": entries})
            if entity.on_history is None:
                reasons.add("comfort_history_unavailable")
            for violation in entity_violations:
                violations.append({"entity": entity.name, "code": violation.code, "slot": violation.slot})
                reasons.add({"rolling_target": "comfort_target_unmet", "max_off": "comfort_max_off_unmet"}.get(violation.code, "comfort_min_run_unmet"))
            state_entities.append({
                "history": list(constraint.on_history), "schedule": list(schedule),
                "boundaries": _advance(entity, schedule, constraint.lock_mode, constraint.lock_remaining),
            })
        result["entities"] = entities
        result["suboptimal_reasons"] = sorted(reasons)
        result["problems"] = sorted(set(result.get("problems", [])) | reasons)
        result["suboptimal"] = bool(result.get("suboptimal") or reasons)
        result["overconstrained"] = bool(result.get("overconstrained") or reasons)
        diagnostics = {**result.get("diagnostics", {})}
        diagnostics["comfort"] = {
            "status": self.placement.status, "violations": violations,
            "continuity": self.continuity,
            "placement_cost_model": "battery_self_consumption",
            "candidates_evaluated": self.placement.candidates_evaluated,
            "accepted_moves": self.placement.accepted_moves,
            "baseline_cost": self.placement.baseline_cost,
            "final_cost": self.placement.final_cost,
            "optimality": "heuristic",
        }
        result["diagnostics"] = diagnostics
        result["comfort_placement"] = diagnostics["comfort"]
        result["state"] = encode_state_blob({
            "v": 1,
            "battery_state": result.get("state"),
            "comfort_state": {
                "fingerprint": _fingerprint(self.original),
                "plan_start": self.original.plan_start.isoformat() if self.original.plan_start else None,
                "entities": state_entities,
            },
        })
        projections = dict(result.get("projections", {}))
        if "per_slot" in projections:
            from .reporting import cost_projection
            projections.update(cost_projection(self.original, [slot["projected_cost"] for slot in projections["per_slot"]]))
        flows = result.get("energy_flows", {}).get("per_slot")
        if flows is not None:
            result["energy_flows"] = {**result["energy_flows"], "per_slot": [
                {**flow, "base_load_kwh": self.original.usage_kwh[t],
                 "comfort_load_kwh": self.params.usage_kwh[t] - self.original.usage_kwh[t]}
                for t, flow in enumerate(flows)
            ]}
        result["projections"] = projections
        return result


def prepare_comfort(params: OptimizationParams) -> PreparedComfort:
    """Prepare shared comfort controls and expose only modeled load to search."""
    battery_state, prior = params.state, None
    if params.state is not None:
        state = json.loads(base64.urlsafe_b64decode(params.state))
        if "comfort_state" in state:
            battery_state, prior = state.get("battery_state"), state["comfort_state"]
    if not params.comfort_entities:
        local = params if battery_state == params.state else params.model_copy(update={"state": battery_state})
        return PreparedComfort(params, local)
    modes, locks, continuity = _initial_locks(params, prior)
    schedules = fixed_comfort_schedule(
        params.comfort_entities, params.rolling_window_slots, len(params.usage_kwh), modes, locks,
    )
    inputs = tuple(
        ComfortPlacementInput(
            name=entity.name, schedule=tuple(bool(value) for value in schedule),
            on_history=tuple(bool(value) for value in initial_comfort_history(entity, params.rolling_window_slots)),
            rolling_window_slots=params.rolling_window_slots,
            target_on_slots_per_rolling_window=entity.target_on_slots_per_rolling_window,
            min_consecutive_on_slots=entity.min_consecutive_on_slots,
            min_consecutive_off_slots=entity.min_consecutive_off_slots,
            max_consecutive_off_slots=entity.max_consecutive_off_slots,
            is_on_now=entity.is_on_now,
            off_streak_slots_now=0 if entity.is_on_now else entity.off_streak_slots_now,
            lock_remaining=lock, lock_mode=mode,
        ) for entity, schedule, mode, lock in zip(params.comfort_entities, schedules, modes, locks, strict=True)
    )

    def modeled(schedules):
        usage = [base + sum(entity.power_usage_kwh * schedule[t] for entity, schedule in zip(params.comfort_entities, schedules, strict=True)) for t, base in enumerate(params.usage_kwh)]
        return params.model_copy(update={"comfort_entities": [], "usage_kwh": usage, "state": battery_state})

    policies = [["self_consume"] * len(params.usage_kwh) for _ in params.battery_entities]
    placement = place_comfort_schedules(inputs, lambda schedules: evaluate_schedule(modeled(schedules), policies).tariff_cost)
    return PreparedComfort(params, modeled(placement.schedules), inputs, placement, continuity)
