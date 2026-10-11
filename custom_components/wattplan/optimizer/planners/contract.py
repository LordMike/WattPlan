"""Shared planner results and the narrower battery-only replay validator."""

from dataclasses import dataclass
import math
from typing import Any, Literal, NotRequired, Protocol, TypedDict

from ..models import OptimizationParams, has_mobility_inputs
from ..physics import POLICIES


class BatteryAction(TypedDict):
    state: Literal["self_consume", "preserve", "grid_charge"]
    level: float


class BatterySchedule(TypedDict):
    name: str
    type: Literal["battery"]
    schedule: list[BatteryAction]


class ComfortAction(TypedDict):
    enabled: bool
    level: float


class ComfortSchedule(TypedDict):
    name: str
    type: Literal["comfort"]
    schedule: list[ComfortAction]


class PlannerResult(TypedDict):
    entities: list[BatterySchedule | ComfortSchedule]
    state: str | None
    projections: dict[str, Any]
    diagnostics: dict[str, Any]
    energy_flows: dict[str, Any]
    optional_entity_options: list[dict[str, Any]]
    fitness: float
    avg_price: float
    execution_time: NotRequired[float]
    suboptimal: bool
    overconstrained: bool
    suboptimal_reasons: list[str]
    problems: list[str]


@dataclass(frozen=True)
class PlannerCapabilities:
    """Unsupported comfort inputs fail; ignored advisory features are reported."""

    comfort: bool = False
    mobility: bool = False
    optional_loads: Literal["supported", "ignored"] = "ignored"
    targets: Literal["optimized", "observed"] = "optimized"
    # Scope describes intended search/control, not an optimality guarantee.
    # "household" also permits mobility economics when mobility=True.
    economic_scope: Literal["household", "mobility", "none"] = "household"
    reserve_recovery: bool = True
    ignored_preferences: tuple[str, ...] = (
        "rolling_window_slots",
        "solver_time_limit_seconds",
        "infer_battery_preserve_policy",
        "lookahead_slots",
    )

    def validate(self, params: OptimizationParams) -> list[str]:
        if has_mobility_inputs(params) and not self.mobility:
            raise ValueError("Planner does not support battery mobility inputs")
        if params.comfort_entities and not self.comfort:
            raise ValueError("Planner does not support comfort loads")
        notes = []
        if self.ignored_preferences:
            notes.append(
                "Preferences not used by this implementation: "
                + ", ".join(self.ignored_preferences)
            )
        if params.optional_entities and self.optional_loads == "ignored":
            notes.append("Optional load recommendations are not implemented")
        if self.targets == "observed" and any(
            b.target for b in params.battery_entities
        ):
            notes.append(
                "Battery targets are measured but not optimized by this baseline"
            )
        return notes


class Planner(Protocol):
    capabilities: PlannerCapabilities

    def __call__(self, params: OptimizationParams) -> PlannerResult: ...


def validate_battery_result(
    result: dict, params: OptimizationParams
) -> dict[str, list[dict]]:
    """Validate the simulation contract, not the larger HA response contract.

    Projected levels are required to be finite; actual execution independently
    applies physical limits. HiGHS projected controls can differ from its emitted
    named policies, so this validator deliberately does not equate the two.
    """
    if not isinstance(result, dict) or not isinstance(result.get("entities"), list):
        raise ValueError("Planner output needs an entities list")
    schedules = {}
    expected = {b.name: b for b in params.battery_entities}
    for entity in result["entities"]:
        if not isinstance(entity, dict) or entity.get("type") != "battery":
            raise ValueError("Unexpected executable entity in battery-only simulation")
        name = entity.get("name")
        if name not in expected or name in schedules:
            raise ValueError("Missing, duplicate, or unknown battery output")
        schedule = entity.get("schedule")
        if not isinstance(schedule, list) or len(schedule) != len(params.usage_kwh):
            raise ValueError("Battery schedule must cover the recorded horizon")
        for action in schedule:
            if not isinstance(action, dict) or action.get("state") not in POLICIES:
                raise ValueError("Unknown battery action")
            if (
                action["state"] == "grid_charge"
                and not expected[name].can_charge_from & 1
            ):
                raise ValueError("Grid-charge action is forbidden for this battery")
            level = action.get("level")
            if (
                isinstance(level, bool)
                or not isinstance(level, (int, float))
                or not math.isfinite(level)
            ):
                raise ValueError("Nonfinite or missing projected battery level")
        schedules[name] = schedule
    if set(schedules) != set(expected):
        raise ValueError("Planner did not return all configured batteries")
    if result.get("state") is not None:
        OptimizationParams._validate_state_blob(result["state"])
    return schedules
