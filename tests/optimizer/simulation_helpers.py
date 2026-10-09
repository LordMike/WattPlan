"""Outcome tests execute planner actions through the shared simulator."""

from datetime import UTC, datetime

from custom_components.wattplan.optimizer import OptimizationParams, optimize
from custom_components.wattplan.optimizer.simulation.engine import Variant, simulate
from custom_components.wattplan.optimizer.simulation.scenarios import synthetic_dataset


def simulate_scenario(payload):
    """Execute successive planning cycles against synthetic fixture actuals."""
    payload = {"plan_start": datetime(2026, 9, 20, tzinfo=UTC), **payload}
    data = synthetic_dataset(payload, len(payload["grid_import_price_per_kwh"]))
    # These finite outcome cases do not repeat their tariff cycle. Retain the
    # supplied forecast at absolute times, then explicitly extend its final
    # value so later requests still satisfy the API's minimum horizon length.
    series = {
        key: list(data.events[0].params[key])
        for key in (
            "grid_import_price_per_kwh",
            "grid_export_price_per_kwh",
            "usage_kwh",
            "solar_input_kwh",
        )
    }
    for tick, event in enumerate(data.events):
        for key, values in series.items():
            event.params[key] = values[tick:] + [values[-1]] * tick

    class Planner:
        def call(self, request, **kwargs):
            result = optimize(OptimizationParams(**request))
            return {"result": result, "wall_seconds": 0.0, "cpu_seconds": 0.0}

    report = simulate(data, Variant("outcome"), Planner())
    assert report["status"] == "complete", report["failure"]
    return report
