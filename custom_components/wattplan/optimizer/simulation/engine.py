"""Causal closed-loop execution against recorded, completed actual intervals."""

from bisect import bisect_right
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import timedelta
import math
from time import monotonic
from typing import Any, Protocol

from ..models import OptimizationParams, has_mobility_inputs
from ..planners.contract import validate_battery_result as validate_result
from ..planners.presets import algorithm_settings, get_preset
from ..planners.registry import resolve_factory
from .adapters import DEFAULT_PLANNER
from .dataset import Dataset, canonical
from .worker import PlannerTimeout
from ..physics import (
    battery_power_limits,
    grid_charge_headroom_sufficient,
    transition,
    transition_at,
)

RUN_TIMEOUT_SECONDS = 120.0


class RunTimeout(TimeoutError):
    """The entire replay exhausted its wall-clock budget."""

    def __init__(self, timeout: float, elapsed: float):
        super().__init__(f"Simulation exceeded {timeout:g}s whole-run limit")
        self.timeout_seconds = timeout
        self.elapsed_seconds = elapsed


class RunBudget:
    """One deadline shared by worker startup, every cycle, and simulation work."""

    def __init__(self, timeout_seconds: float = RUN_TIMEOUT_SECONDS):
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Run timeout must be finite and positive")
        self.timeout_seconds = min(timeout_seconds, RUN_TIMEOUT_SECONDS)
        self.started_at = monotonic()

    def elapsed(self) -> float:
        return monotonic() - self.started_at

    def remaining(self) -> float:
        elapsed = self.elapsed()
        if elapsed >= self.timeout_seconds:
            raise RunTimeout(self.timeout_seconds, elapsed)
        return self.timeout_seconds - elapsed


PREFERENCE_FIELDS = frozenset(
    {
        "rolling_window_slots",
        "lookahead_slots",
        "throughput_cost_per_kwh",
        "action_deadband_kwh",
        "mode_switch_cost",
        "solver_time_limit_seconds",
        "infer_battery_preserve_policy",
    }
)


@dataclass(frozen=True)
class Variant:
    name: str
    planner: str = DEFAULT_PLANNER
    settings: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    preset: str | None = None

    def __post_init__(self):
        if not self.name or not isinstance(self.name, str):
            raise ValueError("Variant name must be nonempty")
        if not isinstance(self.settings, dict) or not isinstance(self.options, dict):
            raise ValueError("Variant settings and options must be objects")
        if set(self.settings) - PREFERENCE_FIELDS:
            raise ValueError(
                "Variants may override planning preferences, not physical equipment, targets, or input series"
            )
        resolve_factory(self.planner)
        if self.preset is not None:
            preset = get_preset(self.preset)
            if self.planner != preset.planner or self.options != preset.options:
                raise ValueError(
                    "A preset variant must use the preset's planner and options"
                )
        canonical(self.settings)
        canonical(self.options)


class Planner(Protocol):
    def call(self, payload: dict, *, reset: bool, timeout: float) -> dict: ...


def simulate(
    dataset: Dataset,
    variant: Variant,
    planner: Planner,
    *,
    budget: RunBudget | None = None,
) -> dict[str, Any]:
    """Execute a variant; any planner failure invalidates the whole scored run."""
    budget = budget or RunBudget()
    windows, skipped = dataset.windows()
    report = {
        "variant": variant.name,
        "status": "complete",
        "cycles": [],
        "intervals": [],
        "segments": [],
        "skipped_decisions": skipped,
        "failure": None,
        "failure_details": None,
        "run_timeout_seconds": budget.timeout_seconds,
        "run_wall_seconds": None,
        "completed_cycles": 0,
        "inferred_range": dataset.range_info(),
        "patches": deepcopy(dataset.patches),
    }
    if not windows:
        report.update(
            status="no_coverage", failure="No decisions overlap usable actuals"
        )
        report["totals"] = None
        report["run_wall_seconds"] = budget.elapsed()
        return report

    def failed(err: Exception, at) -> dict[str, Any]:
        if isinstance(err, PlannerTimeout):
            err = RunTimeout(budget.timeout_seconds, budget.elapsed())
        if isinstance(err, RunTimeout):
            # Local test adapters need only call(); process-backed adapters can
            # also be stopped when simulation overhead exhausts the deadline.
            close = getattr(planner, "close", None)
            if close is not None:
                close(kill=True)
        report.update(
            status="failed",
            failure=f"{at.isoformat()}: {type(err).__name__}: {err}",
            failure_details={
                "at": at.isoformat(),
                "exception_type": type(err).__name__,
                **(
                    {
                        "scope": "run",
                        "timeout_seconds": err.timeout_seconds,
                        "elapsed_seconds": err.elapsed_seconds,
                        "completed_cycles": report["completed_cycles"],
                        "completed_planning_calls": len(report["cycles"]),
                    }
                    if isinstance(err, RunTimeout)
                    else {}
                ),
            },
            totals=None,
            run_wall_seconds=budget.elapsed(),
        )
        return report

    actual_starts = [a.start for a in dataset.actuals]
    levels, state = [], None
    mobility_run = False
    battery_flows_by_interval = []
    current = None
    for window in windows:
        try:
            budget.remaining()
            payload = deepcopy(window.event.params)
            payload.update(variant.settings)
            if variant.preset is not None:
                # Match Home Assistant: a named preset fixes algorithm tuning
                # after any externally supplied experimental preferences.
                payload.update(
                    algorithm_settings(
                        variant.preset,
                        plan_slots=len(payload["grid_import_price_per_kwh"]),
                    )
                )
            reset = window.reset_reason is not None
            if reset:
                levels = [b["initial_kwh"] for b in payload["battery_entities"]]
                state = None
                current = {
                    "segment": window.segment,
                    "start": window.start.isoformat(),
                    "reason": window.reset_reason,
                    "initial_soc_kwh": {
                        b["name"]: level
                        for b, level in zip(payload["battery_entities"], levels)
                    },
                    "target_checks": [],
                }
                report["segments"].append(current)
            payload["state"] = state
            for battery, level in zip(payload["battery_entities"], levels, strict=True):
                battery["initial_kwh"] = level
            params = OptimizationParams(**payload)
            active_mobility = has_mobility_inputs(params)
            if active_mobility:
                mobility_run = True
                report["mobility_assumption"] = "controlled synthetic known mobility from request metadata; not historical EV observations"
                current.setdefault("departure_checks", [])
            if params.comfort_entities:
                raise ValueError(
                    "Comfort loads are not supported by historical simulation v1"
                )
            reply = planner.call(payload, reset=reset, timeout=budget.remaining())
            budget.remaining()
            result = reply["result"]
            schedules = validate_result(result, params)
            state = result.get("state")
            report["cycles"].append(
                {
                    "at": window.start.isoformat(),
                    "segment": window.segment,
                    "wall_seconds": reply["wall_seconds"],
                    "cpu_seconds": reply["cpu_seconds"],
                    "initial_soc_kwh": {
                        b.name: level
                        for b, level in zip(params.battery_entities, levels)
                    },
                    "cadence": result.get("cadence"),
                    "diagnostics": result.get("diagnostics"),
                    "successful_solves": result.get("successful_solves"),
                    "suboptimal_reasons": result.get("suboptimal_reasons"),
                    "projected_cost": result.get("projections", {}).get(
                        "projected_cost"
                    ),
                }
            )
            slot = timedelta(minutes=params.slot_minutes)
            cursor = window.start
            last_action = None
            eligible = None
            while cursor < window.end:
                budget.remaining()
                actual = dataset.actuals[bisect_right(actual_starts, cursor) - 1]
                action_index = int((cursor - window.event.start) // slot)
                action_end = window.event.start + (action_index + 1) * slot
                end = min(window.end, actual.end, action_end)
                if action_index != last_action:
                    eligible = [
                        grid_charge_headroom_sufficient(
                            b, level, battery_power_limits(b, level)[0]
                        )
                        for b, level in zip(params.battery_entities, levels)
                    ]
                    last_action = action_index
                fraction = (end - cursor) / (actual.end - actual.start)
                usage, pv = (
                    actual.values["usage_kwh"] * fraction,
                    actual.values["pv_kwh"] * fraction,
                )
                modes = [
                    schedules[b.name][action_index]["state"]
                    for b in params.battery_entities
                ]
                transition_fn = transition_at if active_mobility else transition
                levels_before = levels
                step = transition_fn(
                    params.battery_entities,
                    levels,
                    modes,
                    **({"slot": action_index, "apply_external": end == action_end}
                       if active_mobility else {}),
                    usage=usage,
                    pv=pv,
                    slot_fraction=(end - cursor) / slot,
                    grid_eligible=eligible,
                )
                budget.remaining()
                levels = step.levels
                import_cost = step.grid_import * actual.values["import_price"]
                export_revenue = step.grid_export * actual.values["export_price"]
                report["intervals"].append(
                    {
                        "start": cursor.isoformat(),
                        "end": end.isoformat(),
                        "segment": window.segment,
                        "usage_kwh": usage,
                        "simulation_patch": actual.values.get("simulation_patch"),
                        "pv_kwh": pv,
                        "grid_import_kwh": step.grid_import,
                        "grid_export_kwh": step.grid_export,
                        "import_cost": import_cost,
                        "export_revenue": export_revenue,
                        "net_cost": import_cost - export_revenue,
                        "charge_kwh": step.charge,
                        "discharge_kwh": sum(step.discharge),
                        "loss_kwh": step.loss,
                        "throughput_kwh": step.charge + sum(step.discharge),
                        "soc_kwh": {
                            b.name: level
                            for b, level in zip(params.battery_entities, levels)
                        },
                        "actions": {
                            b.name: mode
                            for b, mode in zip(params.battery_entities, modes)
                        },
                        "measured_grid_import_kwh": (
                            actual.values["grid_import_kwh"] * fraction
                            if actual.values.get("grid_import_kwh") is not None
                            else None
                        ),
                        "measured_grid_export_kwh": (
                            actual.values["grid_export_kwh"] * fraction
                            if actual.values.get("grid_export_kwh") is not None
                            else None
                        ),
                    }
                )
                # Keep physical flows until run-wide mobility is known, including
                # connected windows with no mobility events of their own.
                battery_flows_by_interval.append([
                    {
                        "name": battery.name,
                        "charge_kwh": step.pv_charge[i] + step.grid_charge[i],
                        "discharge_kwh": step.discharge[i],
                        "level_before_kwh": levels_before[i],
                        "level_before_external_kwh": (
                            step.levels_before_external or step.levels
                        )[i],
                        "level_kwh": step.levels[i],
                    }
                    for i, battery in enumerate(params.battery_entities)
                ])
                if active_mobility:
                    report["intervals"][-1].update(
                        external_energy_kwh=sum(step.external_energy or ()),
                        external_unserved_kwh=sum(step.external_shortfall or ()),
                        external_energy_by_battery={b.name: (step.external_energy or [0.0] * len(levels))[i]
                            for i, b in enumerate(params.battery_entities)})
                    if end == action_end:
                        before = step.levels_before_external or step.levels
                        for i, battery in enumerate(params.battery_entities):
                            for requirement in battery.departure_requirements:
                                if requirement.timeslot == action_index:
                                    current["departure_checks"].append({"battery": battery.name,
                                        "at": end.isoformat(), "soc_kwh": before[i],
                                        "minimum_kwh": requirement.minimum_kwh,
                                        "miss_kwh": max(requirement.minimum_kwh - before[i], 0.0)})
                # Targets are evaluated at their recorded inclusive deadline,
                # never at the optimizer's predicted level or outside coverage.
                for battery, level in zip(params.battery_entities, levels):
                    target = battery.target
                    if (
                        target is not None
                        and end == window.event.start + (target.timeslot + 1) * slot
                    ):
                        lower = target.soc_kwh - target.tolerance_kwh
                        upper = target.soc_kwh + target.tolerance_kwh
                        miss = (
                            max(lower - level, 0)
                            if target.mode == "at_least"
                            else max(level - upper, 0)
                            if target.mode == "at_most"
                            else max(lower - level, level - upper, 0)
                        )
                        current["target_checks"].append(
                            {
                                "battery": battery.name,
                                "at": end.isoformat(),
                                "miss_kwh": miss,
                                "soc_kwh": level,
                            }
                        )
                cursor = end
                budget.remaining()
            current["end"] = window.end.isoformat()
            current["final_soc_kwh"] = {
                b.name: level for b, level in zip(params.battery_entities, levels)
            }
            budget.remaining()
            report["completed_cycles"] += 1
        except Exception as err:
            return failed(err, window.start)
    keys = (
        "usage_kwh",
        "pv_kwh",
        "grid_import_kwh",
        "grid_export_kwh",
        "import_cost",
        "export_revenue",
        "net_cost",
        "charge_kwh",
        "discharge_kwh",
        "loss_kwh",
        "throughput_kwh",
    )
    report["totals"] = {
        key: sum(row[key] for row in report["intervals"]) for key in keys
    }
    report["totals"]["wall_seconds"] = sum(
        row["wall_seconds"] for row in report["cycles"]
    )
    report["totals"]["cpu_seconds"] = sum(
        row["cpu_seconds"] for row in report["cycles"]
    )
    report["totals"]["target_misses"] = sum(
        check["miss_kwh"] > 1e-6
        for segment in report["segments"]
        for check in segment["target_checks"]
    )
    report["totals"]["covered_seconds"] = sum(
        (w.end - w.start).total_seconds() for w in windows
    )
    if mobility_run:
        for row, flows in zip(report["intervals"], battery_flows_by_interval):
            row["battery_flows"] = flows
            row.setdefault("external_energy_kwh", 0.0)
            row.setdefault("external_unserved_kwh", 0.0)
            row.setdefault("external_energy_by_battery", {name: 0.0 for name in row["soc_kwh"]})
        checks = [check for segment in report["segments"] for check in segment.get("departure_checks", [])]
        totals = report["totals"]
        totals["external_energy_kwh"] = sum(row.get("external_energy_kwh", 0.0) for row in report["intervals"])
        totals["external_unserved_kwh"] = sum(row.get("external_unserved_kwh", 0.0) for row in report["intervals"])
        totals["departure_misses"] = sum(check["miss_kwh"] > 1e-6 for check in checks)
        totals["service_failures"] = totals["departure_misses"] + sum(
            row.get("external_unserved_kwh", 0.0) > 1e-6 for row in report["intervals"])
        totals["service_feasible"] = totals["service_failures"] == 0
    try:
        budget.remaining()
    except RunTimeout as err:
        return failed(err, windows[-1].end)
    report["run_wall_seconds"] = budget.elapsed()
    return report
