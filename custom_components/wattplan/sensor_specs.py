"""Declarative sensor specifications for WattPlan."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .const import (
    CONF_SOURCE_EXPORT_PRICE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
)


@dataclass(frozen=True, slots=True)
class SensorSpec:
    """Declarative entry-level sensor definition."""

    sensor_key: str
    sensor_class: type
    extra_kwargs: dict[str, Any] = field(default_factory=dict)


ENTRY_SENSOR_SPECS: tuple[SensorSpec, ...] = (
    SensorSpec("status", object),
    SensorSpec("status_message", object),
    SensorSpec("import_price_status", object, {"source_key": CONF_SOURCE_IMPORT_PRICE}),
    SensorSpec("last_run", object),
    SensorSpec("next_run", object),
    SensorSpec("last_run_duration", object),
    SensorSpec("plan_details", object, {"details_key": "plan_details"}),
    SensorSpec("plan_details_hourly", object, {"details_key": "plan_details_hourly"}),
    SensorSpec(
        "projected_cost_savings",
        object,
        {
            "projection_key": "projected_savings_cost",
            "aggregate_mode": "horizon",
            "use_home_currency": True,
        },
    ),
    SensorSpec(
        "projected_savings_percentage",
        object,
        {
            "projection_key": "projected_savings_pct",
            "aggregate_mode": "horizon",
            "native_unit_of_measurement": "%",
        },
    ),
    SensorSpec(
        "projected_cost_savings_this_interval",
        object,
        {
            "projection_key": "projected_savings_cost",
            "aggregate_mode": "next_interval",
            "use_home_currency": True,
        },
    ),
    SensorSpec(
        "projected_savings_percentage_this_interval",
        object,
        {
            "projection_key": "projected_savings_pct",
            "aggregate_mode": "next_interval",
            "native_unit_of_measurement": "%",
        },
    ),
)

OPTIONAL_SOURCE_STATUS_SPECS: tuple[tuple[str, str], ...] = (
    (CONF_SOURCE_USAGE, "usage_status"),
    (CONF_SOURCE_EXPORT_PRICE, "export_price_status"),
    (CONF_SOURCE_PV, "pv_status"),
)
