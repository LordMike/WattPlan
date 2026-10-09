"""Small adapters over the existing OptimizationParams -> result API."""

from ..models import OptimizationParams
from .physics import transition

DEFAULT_PLANNER = (
    "custom_components.wattplan.optimizer.simulation.adapters:highs_factory"
)
BASELINE_PLANNER = (
    "custom_components.wattplan.optimizer.simulation.adapters:self_consumption_factory"
)


def highs_factory():
    from .. import optimize

    return optimize


def self_consumption_factory():
    """A deterministic baseline, with the same physical equipment as each run."""

    def plan(params: OptimizationParams) -> dict:
        batteries = params.battery_entities
        levels = [b.initial_kwh for b in batteries]
        entities = [
            {"name": b.name, "type": "battery", "schedule": []} for b in batteries
        ]
        for usage, pv in zip(params.usage_kwh, params.solar_input_kwh, strict=True):
            step = transition(
                batteries, levels, ["self_consume"] * len(batteries), usage=usage, pv=pv
            )
            levels = step.levels
            for entity, level in zip(entities, levels, strict=True):
                entity["schedule"].append({"state": "self_consume", "level": level})
        return {"entities": entities, "state": None}

    return plan
