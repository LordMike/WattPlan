"""Explicitly synthetic scenarios for solver outcome tests and benchmarks."""

from copy import deepcopy
from datetime import datetime, timedelta

from ..models import OptimizationParams
from .dataset import Actual, Dataset, Event


def shift_request(
    payload: dict, tick: int, *, state=None, soc: dict | None = None
) -> dict:
    """Rotate a synthetic forecast and preserve absolute target deadlines."""
    result = deepcopy(payload)
    start = payload["plan_start"]
    if isinstance(start, str):
        start = datetime.fromisoformat(start)
    result["plan_start"] = start + timedelta(
        minutes=int(payload.get("slot_minutes", 15)) * tick
    )
    result["state"] = state
    for key in (
        "grid_import_price_per_kwh",
        "grid_export_price_per_kwh",
        "solar_input_kwh",
        "usage_kwh",
    ):
        values = result.get(key, [])
        if values:
            offset = tick % len(values)
            result[key] = values[offset:] + values[:offset]
    for battery in result["battery_entities"]:
        for key, fill in (("connected_slots", False), ("external_energy_kwh", 0.0)):
            values = battery.get(key)
            if values is not None:
                # Exhausted absolute fixture forecasts cannot invent periodic trips.
                battery[key] = values[tick:] + [fill] * min(tick, len(values))
        requirements = []
        for requirement in battery.get("departure_requirements", []):
            deadline = requirement["timeslot"] - tick
            if deadline >= 0:
                requirements.append({**requirement, "timeslot": deadline})
        if "departure_requirements" in battery:
            battery["departure_requirements"] = requirements
        if soc is not None:
            battery["initial_kwh"] = soc[battery["name"]]
        target = battery.get("target")
        if target is not None:
            timeslot = int(target["timeslot"]) - tick
            if timeslot < 0:
                battery.pop("target")
            else:
                target["timeslot"] = timeslot
    optional_entities = []
    for entity in result.get("optional_entities", []):
        entity["start_after_timeslot"] = max(
            int(entity.get("start_after_timeslot", 0)) - tick, 0
        )
        entity["start_before_timeslot"] = int(entity["start_before_timeslot"]) - tick
        if (
            entity["start_before_timeslot"] - int(entity["duration_timeslots"])
            >= entity["start_after_timeslot"]
        ):
            optional_entities.append(entity)
    result["optional_entities"] = optional_entities
    return result


def synthetic_dataset(payload: dict, cycles: int) -> Dataset:
    """Use forecast values as declared synthetic actuals, never as historical facts."""
    events, actuals = [], []
    for tick in range(cycles):
        params = OptimizationParams(**shift_request(payload, tick))
        if params.comfort_entities:
            raise ValueError(
                "Synthetic battery simulation does not model comfort consumption"
            )
        start = params.plan_start
        if start is None:
            raise ValueError("Synthetic scenarios need plan_start")
        events.append(Event(start, start, params.model_dump(mode="json"), False))
        end = start + timedelta(minutes=params.slot_minutes)
        actuals.append(
            Actual(
                start,
                end,
                {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "usage_kwh": params.usage_kwh[0],
                    "pv_kwh": params.solar_input_kwh[0],
                    "import_price": params.grid_import_price_per_kwh[0],
                    "export_price": params.grid_export_price_per_kwh[0],
                    "quality": [],
                    "source_roles": {"usage": "synthetic", "pv": "synthetic"},
                },
            )
        )
    return Dataset(
        "synthetic",
        None,
        "UTC",
        events,
        actuals,
        [],
        [
            "Synthetic actuals equal fixture forecasts; this is not a historical backtest."
        ],
    )
