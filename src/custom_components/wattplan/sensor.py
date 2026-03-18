"""Sensor platform for WattPlan."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

from .const import (
    CONF_HOURS_TO_PLAN,
    CONF_OPTIONS_COUNT,
    CONF_SLOT_MINUTES,
    CONF_SOURCE_EXPORT_PRICE,
    CONF_SOURCE_MODE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
    CONF_SOURCES,
    DOMAIN,
    SOURCE_MODE_BUILT_IN,
    SOURCE_MODE_NOT_USED,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_COMFORT,
    SUBENTRY_TYPE_OPTIONAL,
)
from .coordinator import WattPlanCoordinator
from .sensors import (
    ActionSensor,
    BatteryTargetSocSensor,
    LastRunDurationSensor,
    LastRunSensor,
    NextActionSensor,
    NextRunSensor,
    OptionalTimestampSensor,
    PlanDetailsSensor,
    ProjectionSensor,
    ProjectionValueTransform,
    SourceStatusSensor,
    StatusMessageSensor,
    StatusSensor,
    UsageForecastSensor,
)
from .sensors.common import MAX_EXPOSED_PROJECTED_SAVINGS_PCT
from .sensor_specs import ENTRY_SENSOR_SPECS, OPTIONAL_SOURCE_STATUS_SPECS

ENTRY_FRIENDLY_NAMES: dict[str, str] = {
    "status": "Status",
    "status_message": "Status Message",
    "import_price_status": "Import Price Status",
    "export_price_status": "Export Price Status",
    "usage_status": "Usage Status",
    "usage_forecast": "Usage Forecast",
    "pv_status": "PV Status",
    "last_run": "Last Run",
    "next_run": "Next Run",
    "last_run_duration": "Last Run Duration",
    "plan_details": "Plan Details",
    "plan_details_hourly": "Plan Details Hourly",
}


def _subentry_slug(subentry: Any) -> str:
    """Return slug for subentry naming."""
    return slugify(str(subentry.data.get(CONF_NAME, subentry.title))) or "asset"


def _entry_slug(config_entry: ConfigEntry) -> str:
    """Return slug for config entry naming."""
    return slugify(config_entry.title) or "entry"


def _subentry_display_name(subentry: Any) -> str:
    """Return configured subentry display name, falling back to title."""
    return str(subentry.data.get(CONF_NAME, subentry.title))


def _duration_label(*, minutes: int) -> str:
    """Return a compact duration label for user-facing sensor names."""
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _entry_sensor_name(
    sensor_key: str, *, slot_minutes: int, hours_to_plan: int
) -> str:
    """Return explicit entry-level sensor name."""
    if sensor_key == "projected_cost_savings":
        return f"Projected Cost Savings over {_duration_label(minutes=hours_to_plan * 60)}"
    if sensor_key == "projected_savings_percentage":
        return (
            "Projected Savings Percentage over "
            f"{_duration_label(minutes=hours_to_plan * 60)}"
        )
    if sensor_key == "projected_cost_savings_this_interval":
        return f"Projected Cost Savings over {_duration_label(minutes=slot_minutes)}"
    if sensor_key == "projected_savings_percentage_this_interval":
        return (
            "Projected Savings Percentage over "
            f"{_duration_label(minutes=slot_minutes)}"
        )
    return ENTRY_FRIENDLY_NAMES[sensor_key]


def _subentry_sensor_name(subentry_name: str, sensor_key: str) -> str:
    """Return explicit subentry-level sensor name."""
    if sensor_key == "target":
        return f"({subentry_name}) Target"
    if sensor_key == "action":
        return f"({subentry_name}) Action"
    if sensor_key == "next_action":
        return f"({subentry_name}) Next Action"
    if sensor_key == "next_start_option":
        return f"({subentry_name}) Next Start Option"
    if sensor_key.startswith("option_") and sensor_key.endswith("_start"):
        option_number = sensor_key[len("option_") : -len("_start")]
        return f"({subentry_name}) Option {option_number} Start"
    raise ValueError(f"Unsupported subentry sensor key: {sensor_key}")
def _projected_savings_percentage_value_transform(
    _sensor: ProjectionSensor, value: float
) -> float | None:
    """Hide implausibly large savings percentages from the entity state."""
    if abs(value) > MAX_EXPOSED_PROJECTED_SAVINGS_PCT:
        return None
    return value


def _entry_sensor_kwargs(
    config_entry: ConfigEntry,
    *,
    entry_slug: str,
    sensor_key: str,
    slot_minutes: int,
    hours_to_plan: int,
) -> dict[str, Any]:
    """Return shared kwargs for one entry-level sensor."""
    return {
        "friendly_name": _entry_sensor_name(
            sensor_key, slot_minutes=slot_minutes, hours_to_plan=hours_to_plan
        ),
        "object_id": f"{entry_slug}_{sensor_key}",
        "unique_id": f"{config_entry.entry_id}:entry:{sensor_key}",
    }


def _subentry_sensor_kwargs(
    config_entry: ConfigEntry,
    *,
    entry_slug: str,
    subentry: Any,
    subentry_name: str,
    sensor_key: str,
) -> dict[str, Any]:
    """Return shared kwargs for one subentry-level sensor."""
    sub_slug = _subentry_slug(subentry)
    return {
        "friendly_name": _subentry_sensor_name(subentry_name, sensor_key),
        "object_id": f"{entry_slug}_{sub_slug}_{sensor_key}",
        "unique_id": f"{config_entry.entry_id}:{subentry.subentry_id}:{sensor_key}",
    }


def _configured_source(data: dict[str, Any], source_key: str) -> dict[str, Any]:
    """Return one configured source mapping when present."""
    source = data.get(CONF_SOURCES, {}).get(source_key, {})
    return source if isinstance(source, dict) else {}


def _has_enabled_source(data: dict[str, Any], source_key: str) -> bool:
    """Return whether a source is configured and not disabled."""
    source = _configured_source(data, source_key)
    return bool(source) and source.get(CONF_SOURCE_MODE) != SOURCE_MODE_NOT_USED


def _entry_sensors(
    config_entry: ConfigEntry,
    coordinator: WattPlanCoordinator,
    *,
    entry_slug: str,
    slot_minutes: int,
    hours_to_plan: int,
) -> list[SensorEntity]:
    """Build the always-present entry-level sensors."""
    def entry_kwargs(sensor_key: str) -> dict[str, Any]:
        return _entry_sensor_kwargs(
            config_entry,
            entry_slug=entry_slug,
            sensor_key=sensor_key,
            slot_minutes=slot_minutes,
            hours_to_plan=hours_to_plan,
        )

    spec_classes = {
        "status": StatusSensor,
        "status_message": StatusMessageSensor,
        "import_price_status": SourceStatusSensor,
        "last_run": LastRunSensor,
        "next_run": NextRunSensor,
        "last_run_duration": LastRunDurationSensor,
        "plan_details": PlanDetailsSensor,
        "plan_details_hourly": PlanDetailsSensor,
        "projected_cost_savings": ProjectionSensor,
        "projected_savings_percentage": ProjectionSensor,
        "projected_cost_savings_this_interval": ProjectionSensor,
        "projected_savings_percentage_this_interval": ProjectionSensor,
    }
    sensors: list[SensorEntity] = []
    for spec in ENTRY_SENSOR_SPECS:
        extra_kwargs = dict(spec.extra_kwargs)
        if spec.sensor_key in {
            "projected_savings_percentage",
            "projected_savings_percentage_this_interval",
        }:
            extra_kwargs["value_transform"] = _projected_savings_percentage_value_transform
        sensor_class = spec_classes[spec.sensor_key]
        sensors.append(
            sensor_class(config_entry, coordinator, **extra_kwargs, **entry_kwargs(spec.sensor_key))
        )

    return sensors


def _optional_subentry_sensors(
    config_entry: ConfigEntry,
    coordinator: WattPlanCoordinator,
    *,
    entry_slug: str,
    subentry: Any,
    subentry_name: str,
) -> list[SensorEntity]:
    """Build timestamp sensors for one optional subentry."""
    sensors: list[SensorEntity] = [
        OptionalTimestampSensor(
            config_entry,
            coordinator,
            subentry_id=subentry.subentry_id,
            key="next_start_option",
            **_subentry_sensor_kwargs(
                config_entry,
                entry_slug=entry_slug,
                subentry=subentry,
                subentry_name=subentry_name,
                sensor_key="next_start_option",
            ),
        )
    ]
    option_count = int(subentry.data[CONF_OPTIONS_COUNT])
    for option_index in range(1, option_count + 1):
        option_key = f"option_{option_index}_start"
        sensors.append(
            OptionalTimestampSensor(
                config_entry,
                coordinator,
                subentry_id=subentry.subentry_id,
                key=option_key,
                **_subentry_sensor_kwargs(
                    config_entry,
                    entry_slug=entry_slug,
                    subentry=subentry,
                    subentry_name=subentry_name,
                    sensor_key=option_key,
                ),
            )
        )
    return sensors


def _subentry_sensors(
    config_entry: ConfigEntry,
    coordinator: WattPlanCoordinator,
    runtime_data: Any,
    *,
    entry_slug: str,
    subentry: Any,
) -> list[SensorEntity]:
    """Build all sensors for one configured subentry."""
    subentry_name = _subentry_display_name(subentry)
    subentry_id = subentry.subentry_id

    def sensor_kwargs(sensor_key: str) -> dict[str, Any]:
        return _subentry_sensor_kwargs(
            config_entry,
            entry_slug=entry_slug,
            subentry=subentry,
            subentry_name=subentry_name,
            sensor_key=sensor_key,
        )

    if subentry.subentry_type == SUBENTRY_TYPE_BATTERY:
        return [
            BatteryTargetSocSensor(config_entry, runtime_data, subentry_id, **sensor_kwargs("target")),
            ActionSensor(
                config_entry,
                coordinator,
                subentry_id=subentry_id,
                group="batteries",
                **sensor_kwargs("action"),
            ),
            NextActionSensor(
                config_entry,
                coordinator,
                subentry_id=subentry_id,
                group="batteries",
                **sensor_kwargs("next_action"),
            ),
        ]
    if subentry.subentry_type == SUBENTRY_TYPE_COMFORT:
        return [
            ActionSensor(
                config_entry,
                coordinator,
                subentry_id=subentry_id,
                group="comforts",
                **sensor_kwargs("action"),
            ),
            NextActionSensor(
                config_entry,
                coordinator,
                subentry_id=subentry_id,
                group="comforts",
                **sensor_kwargs("next_action"),
            ),
        ]
    if subentry.subentry_type == SUBENTRY_TYPE_OPTIONAL:
        return _optional_subentry_sensors(
            config_entry,
            coordinator,
            entry_slug=entry_slug,
            subentry=subentry,
            subentry_name=subentry_name,
        )
    return []


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up WattPlan sensors for one config entry."""
    entry_slug = _entry_slug(config_entry)
    slot_minutes = int(config_entry.data[CONF_SLOT_MINUTES])
    hours_to_plan = int(config_entry.data[CONF_HOURS_TO_PLAN])
    runtime_data = config_entry.runtime_data
    coordinator = runtime_data.coordinator

    sensors = _entry_sensors(
        config_entry,
        coordinator,
        entry_slug=entry_slug,
        slot_minutes=slot_minutes,
        hours_to_plan=hours_to_plan,
    )

    for source_key, sensor_key in OPTIONAL_SOURCE_STATUS_SPECS:
        if _has_enabled_source(config_entry.data, source_key):
            sensors.append(
                SourceStatusSensor(
                    config_entry,
                    coordinator,
                    source_key=source_key,
                    **_entry_sensor_kwargs(
                        config_entry,
                        entry_slug=entry_slug,
                        sensor_key=sensor_key,
                        slot_minutes=slot_minutes,
                        hours_to_plan=hours_to_plan,
                    ),
                )
            )

    usage_source = _configured_source(config_entry.data, CONF_SOURCE_USAGE)
    if usage_source.get(CONF_SOURCE_MODE) == SOURCE_MODE_BUILT_IN:
        sensors.append(
            UsageForecastSensor(
                config_entry,
                coordinator,
                **_entry_sensor_kwargs(
                    config_entry,
                    entry_slug=entry_slug,
                    sensor_key="usage_forecast",
                    slot_minutes=slot_minutes,
                    hours_to_plan=hours_to_plan,
                ),
            )
        )

    for subentry in config_entry.subentries.values():
        sensors.extend(
            _subentry_sensors(
                config_entry,
                coordinator,
                runtime_data,
                entry_slug=entry_slug,
                subentry=subentry,
            )
        )

    async_add_entities(sensors)
