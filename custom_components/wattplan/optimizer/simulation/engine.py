"""Causal closed-loop execution against recorded, completed actual intervals."""

from bisect import bisect_right
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol

from ..models import OptimizationParams
from ..planners.contract import validate_battery_result as validate_result
from ..planners.registry import resolve_factory
from .adapters import DEFAULT_PLANNER
from .dataset import Dataset, canonical
from ..physics import (
    battery_power_limits,
    grid_charge_headroom_sufficient,
    transition,
)

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
        canonical(self.settings)
        canonical(self.options)


class Planner(Protocol):
    def call(self, payload: dict, *, reset: bool, timeout: float) -> dict: ...


def simulate(dataset: Dataset, variant: Variant, planner: Planner) -> dict[str, Any]:
    """Execute a variant; any planner failure invalidates the whole scored run."""
    windows, skipped = dataset.windows()
    report = {
        "variant": variant.name,
        "status": "complete",
        "cycles": [],
        "intervals": [],
        "segments": [],
        "skipped_decisions": skipped,
        "failure": None,
        "inferred_range": dataset.range_info(),
        "patches": deepcopy(dataset.patches),
    }
    if not windows:
        report.update(
            status="no_coverage", failure="No decisions overlap usable actuals"
        )
        report["totals"] = None
        return report
    actual_starts = [a.start for a in dataset.actuals]
    levels, state = [], None
    current = None
    for window in windows:
        try:
            payload = deepcopy(window.event.params)
            payload.update(variant.settings)
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
            if params.comfort_entities:
                raise ValueError(
                    "Comfort loads are not supported by historical simulation v1"
                )
            reply = planner.call(
                payload, reset=reset, timeout=max(60, params.slot_minutes * 60)
            )
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
                step = transition(
                    params.battery_entities,
                    levels,
                    modes,
                    usage=usage,
                    pv=pv,
                    slot_fraction=(end - cursor) / slot,
                    grid_eligible=eligible,
                )
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
            current["end"] = window.end.isoformat()
            current["final_soc_kwh"] = {
                b.name: level for b, level in zip(params.battery_entities, levels)
            }
        except Exception as err:
            report.update(
                status="failed",
                failure=f"{window.start.isoformat()}: {type(err).__name__}: {err}",
            )
            report["totals"] = None
            return report
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
    return report
