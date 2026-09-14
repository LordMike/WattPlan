"""Clock-aligned near-term MPC refresh with a reprojected policy tail."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math

import numpy as np

from . import mpc_power_optimizer as core
from .models import (
    COMFORT_SCHEDULER_VERSION,
    OptimizationParams,
    encode_state_blob,
    normalize_calculation_input,
)


PREFIX_SLOTS = 8
FULL_INTERVAL_SLOTS = 4
STATE_VERSION = 2
POLICIES = frozenset(("grid_charge", "preserve", "self_consume"))


def _decode(blob):
    if blob is None:
        return {}
    return json.loads(base64.urlsafe_b64decode(blob.encode("ascii")).decode("utf-8"))


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing planning timestamp")
    value = datetime.fromisoformat(value)
    if value.utcoffset() is None:
        raise ValueError("planning timestamp has no timezone")
    return value.astimezone(UTC)


def _signature(params):
    payload = params.model_dump(mode="json", exclude={
        "plan_start", "state", "grid_import_price_per_kwh",
        "grid_export_price_per_kwh", "solar_input_kwh", "usage_kwh",
        "optional_entities",
    })
    payload["horizon"] = len(params.grid_import_price_per_kwh)
    if params.comfort_entities:
        payload["comfort_scheduler_version"] = COMFORT_SCHEDULER_VERSION
    for battery in payload["battery_entities"]:
        battery.pop("initial_kwh", None)
        target = battery.get("target")
        if target is not None:
            # A moving relative slot still names the same absolute deadline.
            target["timeslot"] = (
                params.plan_start
                + timedelta(minutes=(target["timeslot"] + 1) * params.slot_minutes)
            ).isoformat()
    for comfort in payload["comfort_entities"]:
        for key in ("is_on_now", "on_history", "on_slots_last_rolling_window",
                    "off_streak_slots_now", "recent_avg_on_power_kw"):
            comfort.pop(key, None)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _initial_locks(params, metadata):
    """Carry first-slot commitments, not unexecuted future-plan transitions."""
    if not params.comfort_entities:
        return None
    modes = np.asarray([int(c.is_on_now) for c in params.comfort_entities])
    remaining = np.zeros(len(modes), dtype=np.int32)
    for i, comfort in enumerate(params.comfort_entities):
        minimum = (comfort.min_consecutive_on_slots if comfort.is_on_now
                   else comfort.min_consecutive_off_slots)
        if not comfort.is_on_now:
            remaining[i] = max(minimum - comfort.off_streak_slots_now, 0)
        history = comfort.on_history
        if history:
            observed_run = 0
            for mode in reversed(history):
                if mode != comfort.is_on_now:
                    break
                observed_run += 1
            # An observed transition gives an exact run length. A history made
            # entirely of the current mode is only a lower bound: do not keep
            # restarting a long lock when the history is shorter than that lock.
            if observed_run < len(history) or observed_run >= minimum:
                remaining[i] = max(remaining[i], minimum - observed_run, 0)
    plan = {
        "overlap_steps": 0,
        "initial_comfort_lock_mode": modes,
        "initial_comfort_lock_remaining": remaining,
    }
    if not isinstance(metadata, dict) or metadata.get("v") != STATE_VERSION:
        return plan
    try:
        previous_start = _timestamp(metadata.get("plan_start"))
    except (ValueError, OverflowError):
        return plan
    if params.plan_start < previous_start:
        return plan
    receipts = metadata.get("comfort_locks")
    if not isinstance(receipts, dict):
        return plan
    for i, comfort in enumerate(params.comfort_entities):
        receipt = receipts.get(comfort.name)
        if not isinstance(receipt, dict):
            continue
        on_minutes = comfort.min_consecutive_on_slots * params.slot_minutes
        off_minutes = comfort.min_consecutive_off_slots * params.slot_minutes
        if (type(receipt.get("mode")) is not bool
                or receipt["mode"] != comfort.is_on_now
                or receipt.get("min_on_minutes") != on_minutes
                or receipt.get("min_off_minutes") != off_minutes):
            continue
        try:
            until = _timestamp(receipt.get("until"))
        except (ValueError, OverflowError):
            continue
        maximum = timedelta(minutes=on_minutes if comfort.is_on_now else off_minutes)
        if not timedelta(0) <= until - previous_start <= maximum:
            continue
        remaining[i] = max(remaining[i], 0, math.ceil(
            (until - params.plan_start).total_seconds() / (params.slot_minutes * 60)
        ))
    return plan


def _valid_policies(state, batteries, slots):
    rows = state.get("battery_policy_states")
    return (
        isinstance(rows, list) and len(rows) == batteries
        and all(isinstance(row, list) and len(row) == slots
                and all(isinstance(p, str) and p in POLICIES for p in row)
                for row in rows)
    )


def _finish(result, params, signature, last_full, mode, reason, fresh_slots, new_slots=0):
    state = _decode(result["state"])
    count = len(params.battery_entities)
    state["battery_policy_states"] = [
        [point["state"] for point in result["entities"][i]["schedule"]]
        for i in range(count)
    ]
    receipts = {}
    for i, comfort in enumerate(params.comfort_entities):
        first = bool(result["entities"][count + i]["schedule"][0]["enabled"])
        remaining = int(state["comfort_lock_remaining"][i][0])
        if first != comfort.is_on_now:
            remaining = (comfort.min_consecutive_on_slots if first
                         else comfort.min_consecutive_off_slots)
        receipts[comfort.name] = {
            "mode": first,
            "until": (params.plan_start + timedelta(
                minutes=remaining * params.slot_minutes
            )).isoformat(),
            "min_on_minutes": comfort.min_consecutive_on_slots * params.slot_minutes,
            "min_off_minutes": comfort.min_consecutive_off_slots * params.slot_minutes,
        }
    phase = int((params.plan_start - last_full) // timedelta(minutes=params.slot_minutes))
    state["cadence_prefix"] = {
        "v": STATE_VERSION,
        "plan_start": params.plan_start.isoformat(),
        "slot_minutes": params.slot_minutes,
        "last_full_start": last_full.isoformat(),
        "config_signature": signature,
        "comfort_locks": receipts,
    }
    result["state"] = encode_state_blob(state)
    slots = len(params.grid_import_price_per_kwh)
    result["cadence"] = {
        "mode": mode, "reason": reason, "phase": phase,
        "prefix_slots": PREFIX_SLOTS, "full_interval_slots": FULL_INTERVAL_SLOTS,
        "optimized_steps": fresh_slots, "replayed_steps": slots - fresh_slots,
        "new_tail_steps": new_slots, "tail_age_slots": phase,
        "fallback_reason": reason if mode == "fallback_full" else None,
    }
    for entity in result["entities"]:
        for t, point in enumerate(entity["schedule"]):
            point["freshness"] = (
                "scheduled" if entity["type"] == "comfort" else
                "optimized" if t < fresh_slots else
                "provisional" if new_slots and t >= slots - new_slots else
                "reprojected"
            )
    return result


def _fresh_prefix_reuse(result, normalized, locks):
    """Reuse only decisions just solved for these exact inputs in this call."""
    state = _decode(result["state"])
    slots = normalized.total_steps
    reuse = dict(locks or {})
    reuse["overlap_steps"] = PREFIX_SLOTS
    for name in ("battery_charge", "battery_charge_grid", "battery_charge_pv",
                 "battery_discharge", "battery_preserve", "comfort_on",
                 "comfort_lock_mode", "comfort_lock_remaining"):
        rows = (len(normalized.comfort_entities) if name.startswith("comfort_")
                else len(normalized.battery_entities))
        reuse[name] = np.asarray(state[name]).reshape(rows, slots)
    policies = [[None] * slots for _ in normalized.battery_entities]
    for b, row in enumerate(policies):
        row[:PREFIX_SLOTS] = [
            p["state"] for p in result["entities"][b]["schedule"][:PREFIX_SLOTS]
        ]
    return reuse, policies


def _merge_discarded_comfort_work(result, prefix):
    prior = prefix.get("comfort_placement")
    if prior is None:
        return
    current = result.get("comfort_placement")
    if current is None:
        return
    count_fields = (
        "candidate_cost_calls",
        "candidates_generated",
        "candidates_considered",
        "candidates_evaluated",
        "candidates_rejected",
        "candidates_not_improving",
        "candidates_unvisited",
        "accepted_moves",
        "additional_planning_passes",
        "additional_successful_solves",
    )
    for field in count_fields:
        current[field] = int(current.get(field, 0)) + int(prior.get(field, 0))
    current["discarded_prefix_work"] = prior


def _full(params, normalized, locks, signature, reason, *, fallback=False, prefix=None):
    comfort_replan_budget = 1
    if prefix is not None:
        comfort_replan_budget -= int(
            prefix.get("comfort_placement", {}).get(
                "additional_planning_passes", 0
            )
        )
    if prefix is None:
        result = core.optimize_internal(
            normalized,
            reuse_plan_override=locks,
            comfort_replan_budget=comfort_replan_budget,
        )
    else:
        reuse, policies = _fresh_prefix_reuse(prefix, normalized, locks)
        result = core.optimize_internal(
            normalized, reuse_plan_override=reuse, battery_policy_override=policies,
            comfort_replan_budget=comfort_replan_budget,
        )
        result["execution_time"] += prefix["execution_time"]
        result["successful_solves"] += prefix["successful_solves"]
        result["reused_steps"] = 0  # All decisions were optimized in this call.
        _merge_discarded_comfort_work(result, prefix)
    return _finish(result, params, signature, params.plan_start,
                   "fallback_full" if fallback else "full", reason,
                   normalized.total_steps)


def _tail_plan(normalized, state, shift, locks):
    slots = normalized.total_steps
    modes = [[None] * slots for _ in normalized.battery_entities]
    for t in range(PREFIX_SLOTS, slots):
        previous = t + shift
        for b in range(len(modes)):
            modes[b][t] = (state["battery_policy_states"][b][previous]
                           if previous < slots else "self_consume")
    return {
        **(locks or {"overlap_steps": 0}),
        "policy_reused_tail_steps": slots - PREFIX_SLOTS - shift,
    }, modes


def _tail_violation(result, normalized):
    """Validate reused battery decisions; comfort is freshly scheduled each call."""
    state = _decode(result["state"])
    slots = normalized.total_steps
    levels = np.asarray(state["battery_levels"], dtype=float)
    for i, battery in enumerate(normalized.battery_entities):
        if np.any(levels[i, PREFIX_SLOTS + 1:] < battery.minimum_kwh - core.EPSILON):
            return "battery_min_unmet"
        target = core._battery_target_bounds_kwh(battery)
        if target is not None and target["timeslot"] >= PREFIX_SLOTS:
            level = levels[i, target["timeslot"] + 1]
            if ((target["lower_kwh"] is not None
                 and level < target["lower_kwh"] - core.EPSILON)
                    or (target["upper_kwh"] is not None
                        and level > target["upper_kwh"] + core.EPSILON)):
                return "battery_target_unmet"
    # A coarse cached grid-charge policy can become incompatible with revised PV
    # or ingress permissions. Do not label PV energy as grid-backed charging.
    if normalized.battery_entities:
        grid = np.asarray(state["battery_charge_grid"], dtype=float)
        pv = np.asarray(state["battery_charge_pv"], dtype=float)
        discharge = np.asarray(state["battery_discharge"], dtype=float)
        for t in range(PREFIX_SLOTS, slots):
            load = float(normalized.usage[t]) + sum(
                float(c.power_usage_kwh) * state["comfort_on"][i][t]
                for i, c in enumerate(normalized.comfort_entities)
            )
            solar = float(normalized.solar_input[t])
            grid_charge = float(np.sum(grid[:, t]))
            pv_charge = float(np.sum(pv[:, t]))
            spent = float(np.sum(discharge[:, t]))
            imported = max(load + grid_charge + pv_charge - solar - spent, 0.0)
            if (grid_charge > imported + core.EPSILON
                    or pv_charge > max(solar - load, 0.0) + core.EPSILON
                    or spent > max(load - solar, 0.0) + core.EPSILON):
                return "tail_energy_routing"
    return None


def optimize(params: OptimizationParams) -> dict:
    """Refresh eight slots between full plans, using explicit forecast timestamps."""
    normalized = normalize_calculation_input(params)
    state = _decode(params.state)
    metadata = state.get("cadence_prefix")
    if params.plan_start is None:
        # No clock means no inferred slide. Preserve the legacy API for old states.
        if metadata is not None:
            return core.optimize_internal(normalized, reuse_plan_override=None)
        return core.optimize_internal(normalized)
    signature = _signature(params)
    locks = _initial_locks(params, metadata)
    if normalized.total_steps <= PREFIX_SLOTS + 1:
        return _full(params, normalized, locks, signature, "short_horizon")
    if not isinstance(metadata, dict) or metadata.get("v") != STATE_VERSION:
        return _full(params, normalized, locks, signature,
                     "initial_plan" if not state else "legacy_state", fallback=bool(state))
    try:
        previous_start = _timestamp(metadata.get("plan_start"))
        last_full = _timestamp(metadata.get("last_full_start"))
        slot = timedelta(minutes=params.slot_minutes)
        elapsed = params.plan_start - previous_start
        age = previous_start - last_full
        if metadata.get("slot_minutes") != params.slot_minutes:
            reason = "slot_duration_changed"
        elif age < timedelta(0) or age % slot or age >= FULL_INTERVAL_SLOTS * slot:
            reason = "invalid_cadence_state"
        elif elapsed not in (timedelta(0), slot):
            reason = "nonconsecutive_window"
        elif (normalized.state is None or normalized.state.num_steps != normalized.total_steps
              or metadata.get("config_signature") != signature):
            reason = "configuration_changed"
        elif (normalized.state.comfort_on.shape != (len(normalized.comfort_entities), normalized.total_steps)
              or not _valid_policies(state, len(normalized.battery_entities), normalized.total_steps)):
            reason = "invalid_policy_state"
        else:
            reason = None
    except (ValueError, OverflowError, TypeError):
        reason = "invalid_cadence_state"
    if reason is not None:
        return _full(params, normalized, locks, signature, reason, fallback=True)
    if params.plan_start - last_full >= FULL_INTERVAL_SLOTS * slot:
        return _full(params, normalized, locks, signature, "scheduled_full")
    shift = int(elapsed // slot)
    tail, modes = _tail_plan(normalized, state, shift, locks)
    result = core.optimize_internal(
        normalized, reuse_plan_override=tail, policy_tail_start=PREFIX_SLOTS,
        battery_policy_override=modes,
    )
    violation = _tail_violation(result, normalized)
    if violation is not None:
        full = _full(params, normalized, locks, signature, violation,
                     fallback=True, prefix=result)
        full["cadence"]["retained_fresh_prefix_steps"] = PREFIX_SLOTS
        return full
    return _finish(result, params, signature, last_full, "repair",
                   "same_window_refresh" if shift == 0 else "prefix_refresh",
                   PREFIX_SLOTS, shift)
