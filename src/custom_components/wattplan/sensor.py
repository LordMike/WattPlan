"""Sensor platform for WattPlan."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, MATCH_ALL, UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .target_runtime import get_active_battery_target

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
from .coordinator import CoordinatorSnapshot, WattPlanCoordinator
from .datetime_utils import parse_datetime_like

SUBOPTIMAL_REASON_DESCRIPTIONS: dict[str, str] = {
    "battery_min_unmet": (
        "At least one battery dropped below its configured minimum energy"
    ),
    "battery_target_unmet": (
        "A battery target was not met by its configured deadline"
    ),
    "comfort_target_unmet": (
        "A comfort load did not reach its required on-time within the rolling window"
    ),
    "comfort_max_off_unmet": (
        "A comfort load stayed off longer than its configured maximum off time"
    ),
}

BATTERY_CHARGE_SOURCE_LABELS: dict[str, str] = {
    "n": "(N)one",
    "g": "(G)rid",
    "p": "(P)V",
    "gp": "(G)rid and (P)V",
}

MAX_EXPOSED_PROJECTED_SAVINGS_PCT = 200.0
ProjectionValueTransform = Callable[["ProjectionSensor", float], float | None]

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


def _entry_device_info(config_entry: ConfigEntry) -> DeviceInfo:
    """Return shared device info for all entry entities."""
    return DeviceInfo(
        identifiers={(DOMAIN, config_entry.entry_id)},
        name=f"WattPlan {config_entry.title}",
        manufacturer="WattPlan",
        model="Planner",
    )


def _as_datetime(value: Any) -> datetime | None:
    """Convert a dynamic value to datetime when possible."""
    return parse_datetime_like(value)


def _friendly_charge_source_label(charge_source: str) -> str:
    """Return a user-facing charge source label for compact planner codes."""
    return BATTERY_CHARGE_SOURCE_LABELS.get(charge_source, charge_source)


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

    sensors: list[SensorEntity] = [
        sensor_class(config_entry, coordinator, **extra_kwargs, **entry_kwargs(sensor_key))
        for sensor_class, sensor_key, extra_kwargs in [
            (StatusSensor, "status", {}),
            (StatusMessageSensor, "status_message", {}),
            (
                SourceStatusSensor,
                "import_price_status",
                {"source_key": CONF_SOURCE_IMPORT_PRICE},
            ),
            (LastRunSensor, "last_run", {}),
            (NextRunSensor, "next_run", {}),
            (LastRunDurationSensor, "last_run_duration", {}),
            (PlanDetailsSensor, "plan_details", {"details_key": "plan_details"}),
            (
                PlanDetailsSensor,
                "plan_details_hourly",
                {"details_key": "plan_details_hourly"},
            ),
            (
                ProjectionSensor,
                "projected_cost_savings",
                {
                    "projection_key": "projected_savings_cost",
                    "aggregate_mode": "horizon",
                    "use_home_currency": True,
                },
            ),
            (
                ProjectionSensor,
                "projected_savings_percentage",
                {
                    "projection_key": "projected_savings_pct",
                    "aggregate_mode": "horizon",
                    "value_transform": _projected_savings_percentage_value_transform,
                    "native_unit_of_measurement": "%",
                },
            ),
            (
                ProjectionSensor,
                "projected_cost_savings_this_interval",
                {
                    "projection_key": "projected_savings_cost",
                    "aggregate_mode": "next_interval",
                    "use_home_currency": True,
                },
            ),
            (
                ProjectionSensor,
                "projected_savings_percentage_this_interval",
                {
                    "projection_key": "projected_savings_pct",
                    "aggregate_mode": "next_interval",
                    "value_transform": _projected_savings_percentage_value_transform,
                    "native_unit_of_measurement": "%",
                },
            ),
        ]
    ]

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


class WattPlanCoordinatorSensor(CoordinatorEntity[WattPlanCoordinator], SensorEntity):
    """Base WattPlan sensor backed by coordinator snapshots."""

    _attr_should_poll = False
    _require_snapshot = True
    _require_usable_plan = False

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        *,
        object_id: str,
        friendly_name: str,
        unique_id: str,
        device_class: SensorDeviceClass | None = None,
    ) -> None:
        """Initialize coordinator-backed sensor."""
        super().__init__(coordinator)
        self._attr_object_id = object_id
        self._attr_name = friendly_name
        self.internal_integration_suggested_object_id = object_id
        self._attr_unique_id = unique_id
        self._attr_device_info = _entry_device_info(config_entry)
        if device_class is not None:
            self._attr_device_class = device_class

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if not super().available:
            return False
        if self._require_usable_plan and not self.coordinator.has_usable_plan:
            return False
        if self._require_snapshot and self.coordinator.snapshot is None:
            return False
        return True

    @property
    def snapshot(self) -> CoordinatorSnapshot | None:
        """Return current immutable coordinator snapshot."""
        return self.coordinator.snapshot


class StaticValueSensor(WattPlanCoordinatorSensor):
    """Simple sensor with static native value."""

    _require_snapshot = False

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        *,
        native_value: Any,
        native_unit_of_measurement: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize static sensor."""
        super().__init__(config_entry, coordinator, **kwargs)
        self._attr_native_value = native_value
        if native_unit_of_measurement is not None:
            self._attr_native_unit_of_measurement = native_unit_of_measurement


class StatusSensor(WattPlanCoordinatorSensor):
    """Status sensor projected from the latest snapshot."""

    _require_snapshot = False

    @property
    def native_value(self) -> str | None:
        """Return planner status value from snapshot."""
        return str(self.coordinator.overall_status.get("status"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return status context."""
        status = self.coordinator.overall_status
        return {
            "reason_codes": list(status.get("reason_codes", [])),
            "reason_summary": str(status.get("reason_summary", "")),
            "affected_sources": list(status.get("affected_sources", [])),
            "critical_sources_failed": list(
                status.get("critical_sources_failed", [])
            ),
            "is_stale": bool(status.get("is_stale", False)),
            "has_usable_plan": bool(status.get("has_usable_plan", False)),
            "plan_created_at": status.get("plan_created_at"),
            "expires_at": status.get("expires_at"),
        }


class StatusMessageSensor(WattPlanCoordinatorSensor):
    """Human-readable summary of current integration health."""

    _require_snapshot = False
    _attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> str | None:
        """Return summary text from coordinator health."""
        summary = self.coordinator.overall_status.get("reason_summary")
        return str(summary) if summary is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return supporting machine-readable reasons."""
        return {
            "reason_codes": list(self.coordinator.overall_status.get("reason_codes", []))
        }


class SourceStatusSensor(WattPlanCoordinatorSensor):
    """Status sensor for one configured source."""

    _require_snapshot = False
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        *,
        source_key: str,
        **kwargs: Any,
    ) -> None:
        """Initialize source status sensor."""
        super().__init__(config_entry, coordinator, **kwargs)
        self._source_key = source_key

    @property
    def native_value(self) -> str | None:
        """Return current source state."""
        status = self.coordinator.source_status(self._source_key)
        if status is None:
            return None
        return str(status.get("status"))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return stable public source health payload."""
        status = self.coordinator.source_status(self._source_key)
        if status is None:
            return None
        return {
            "reason_code": status.get("reason_code"),
            "reason_summary": status.get("reason_summary"),
            "is_stale": bool(status.get("is_stale", False)),
            "is_critical": bool(status.get("is_critical", False)),
            "available_count": status.get("available_count"),
            "required_count": status.get("required_count"),
            "expires_at": status.get("expires_at"),
            "provider_kind": status.get("provider_kind"),
        }


class ActionSensor(WattPlanCoordinatorSensor):
    """Action sensor with next action timestamp attributes."""

    _require_usable_plan = True

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        *,
        subentry_id: str,
        group: str,
        **kwargs: Any,
    ) -> None:
        """Initialize action sensor."""
        super().__init__(config_entry, coordinator, **kwargs)
        self._subentry_id = subentry_id
        self._group = group

    def _action_data(self) -> dict[str, Any]:
        """Return action data for this subentry from snapshot diagnostics."""
        if not self.snapshot:
            return {}
        diagnostics = self.snapshot.diagnostics or {}
        group_data = diagnostics.get(self._group, {})
        if isinstance(group_data, dict):
            subentry_data = group_data.get(self._subentry_id, {})
            if isinstance(subentry_data, dict):
                return subentry_data
        return {}

    @property
    def native_value(self) -> str | None:
        """Return current action label."""
        return self._action_data().get("action")

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return action metadata."""
        data = self._action_data()
        attrs: dict[str, str] = {}
        if self._group == "batteries" and (charge_source := data.get("charge_source")):
            charge_source_code = str(charge_source)
            attrs["charge_source"] = charge_source_code
            attrs["charge_source_friendly"] = _friendly_charge_source_label(
                charge_source_code
            )

        return attrs or None


class NextActionSensor(WattPlanCoordinatorSensor):
    """Disabled-by-default sensor exposing the next planned action."""

    _require_usable_plan = True
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        *,
        subentry_id: str,
        group: str,
        **kwargs: Any,
    ) -> None:
        """Initialize next-action sensor."""
        super().__init__(config_entry, coordinator, **kwargs)
        self._subentry_id = subentry_id
        self._group = group

    def _action_data(self) -> dict[str, Any]:
        """Return action data for this subentry from snapshot diagnostics."""
        if not self.snapshot:
            return {}
        diagnostics = self.snapshot.diagnostics or {}
        group_data = diagnostics.get(self._group, {})
        if isinstance(group_data, dict):
            subentry_data = group_data.get(self._subentry_id, {})
            if isinstance(subentry_data, dict):
                return subentry_data
        return {}

    @property
    def native_value(self) -> str | None:
        """Return the next action label."""
        next_action = self._action_data().get("next_action")
        return str(next_action) if isinstance(next_action, str) else None

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return next-action metadata."""
        data = self._action_data()
        attrs: dict[str, str] = {}
        timestamp = _as_datetime(data.get("next_action_timestamp"))
        if timestamp is not None:
            attrs["timestamp"] = timestamp.isoformat()

        if self._group == "batteries" and (charge_source := data.get("next_charge_source")):
            charge_source_code = str(charge_source)
            attrs["charge_source"] = charge_source_code
            attrs["charge_source_friendly"] = _friendly_charge_source_label(
                charge_source_code
            )

        return attrs or None


class BatteryTargetSocSensor(SensorEntity):
    """Sensor for a battery target set by the user."""

    _attr_should_poll = False
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        config_entry: ConfigEntry,
        runtime_data: Any,
        subentry_id: str,
        *,
        object_id: str,
        friendly_name: str,
        unique_id: str,
    ) -> None:
        """Initialize battery target sensor."""
        self._attr_object_id = object_id
        self._attr_name = friendly_name
        self.internal_integration_suggested_object_id = object_id
        self._attr_unique_id = unique_id
        self._attr_device_info = _entry_device_info(config_entry)
        self._runtime_data = runtime_data
        self._subentry_id = subentry_id

    async def async_added_to_hass(self) -> None:
        """Register updates so service calls can push state immediately."""
        await super().async_added_to_hass()
        listeners = self._runtime_data.battery_target_update_listeners.setdefault(
            self._subentry_id, set()
        )
        listeners.add(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister update callback."""
        listeners = self._runtime_data.battery_target_update_listeners.get(
            self._subentry_id
        )
        if listeners is not None:
            listeners.discard(self.async_write_ha_state)
            if not listeners:
                self._runtime_data.battery_target_update_listeners.pop(
                    self._subentry_id, None
                )
        await super().async_will_remove_from_hass()

    @property
    def native_value(self) -> float | None:
        """Return target SoC, or unknown when no user intent is set."""
        if target := get_active_battery_target(self._runtime_data, self._subentry_id):
            return target.soc_kwh
        return None

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return timing metadata for the current user intent."""
        if target := get_active_battery_target(self._runtime_data, self._subentry_id):
            return {"by": target.reach_at.isoformat()}
        return {"by": "not_set"}


class LastRunSensor(WattPlanCoordinatorSensor):
    """Last successful run timestamp sensor."""

    _require_snapshot = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        """Return timestamp for last successful stage."""
        return self.coordinator.last_success_at


class NextRunSensor(WattPlanCoordinatorSensor):
    """Next run timestamp sensor."""

    _require_snapshot = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        **kwargs: Any,
    ) -> None:
        """Initialize next run sensor."""
        super().__init__(config_entry, coordinator, **kwargs)
        self._slot_minutes = int(config_entry.data[CONF_SLOT_MINUTES])

    @property
    def native_value(self) -> datetime | None:
        """Return next run time based on scheduler state."""
        return self.coordinator.next_refresh_at


class LastRunDurationSensor(WattPlanCoordinatorSensor):
    """Last run duration sensor in milliseconds."""

    _require_snapshot = False
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> int | None:
        """Return last cycle duration in milliseconds."""
        return self.coordinator.last_duration_ms

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return execution timing details for the latest planner run."""
        timings = self.coordinator.last_run_timings
        if timings is None:
            return None
        return {"timings": timings}


class OptionalTimestampSensor(WattPlanCoordinatorSensor):
    """Timestamp sensor for optional load options."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        *,
        subentry_id: str,
        key: str,
        **kwargs: Any,
    ) -> None:
        """Initialize optional timestamp sensor."""
        super().__init__(
            config_entry,
            coordinator,
            device_class=SensorDeviceClass.TIMESTAMP,
            **kwargs,
        )
        self._subentry_id = subentry_id
        self._key = key

    @property
    def native_value(self) -> datetime | None:
        """Return timestamp from optional diagnostics payload."""
        return _as_datetime(self._diagnostic_value(self._key))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the option end timestamp on optional start sensors."""
        end_key = self._end_key()
        if end_key is None:
            return None
        end_at = self._diagnostic_value(end_key)
        if end_at is None:
            return None
        return {"end_timestamp": end_at}

    def _diagnostic_value(self, key: str) -> Any:
        """Return one optional diagnostics value for this subentry."""
        if not self.snapshot:
            return None
        diagnostics = self.snapshot.diagnostics or {}
        optional_data = diagnostics.get("optionals", {})
        if not isinstance(optional_data, dict):
            return None
        subentry_data = optional_data.get(self._subentry_id, {})
        if not isinstance(subentry_data, dict):
            return None
        return subentry_data.get(key)

    def _end_key(self) -> str | None:
        """Return the diagnostics key holding the corresponding end timestamp."""
        if self._key == "next_start_option":
            return "next_end_option"
        if self._key.startswith("option_") and self._key.endswith("_start"):
            return f"{self._key[:-6]}_end"
        return None


class UsageForecastSensor(WattPlanCoordinatorSensor):
    """Sensor exposing built-in usage forecast in adapter-compatible format."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_entity_registry_enabled_default = False
    _attr_suggested_display_precision = 2
    _require_usable_plan = True

    @property
    def native_value(self) -> float | None:
        """Return the first forecast value for quick glance usage."""
        points = self._forecast_points()
        if not points:
            return None
        try:
            return float(points[0]["value"])
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return full forecast payload as `{start, value}` objects."""
        points = self._forecast_points()
        if not points:
            return None
        return {"forecast": points, "time_key": "start", "value_key": "value"}

    def _forecast_points(self) -> list[dict[str, Any]]:
        """Return usage forecast points from snapshot diagnostics."""
        if not self.snapshot:
            return []
        diagnostics = self.snapshot.diagnostics or {}
        sources = diagnostics.get("sources", {})
        if not isinstance(sources, dict):
            return []
        points = sources.get("usage_forecast")
        if not isinstance(points, list):
            return []
        return [point for point in points if isinstance(point, dict)]


class PlanDetailsSensor(WattPlanCoordinatorSensor):
    """Diagnostic sensor exposing graph-friendly plan arrays."""

    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    # Keep the large graph payload out of recorder; the card reads live state.
    _unrecorded_attributes = frozenset({MATCH_ALL})
    _require_usable_plan = True

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        *,
        details_key: str,
        **kwargs: Any,
    ) -> None:
        """Initialize one plan details sensor variant."""
        super().__init__(config_entry, coordinator, **kwargs)
        self._details_key = details_key

    @property
    def native_value(self) -> datetime | None:
        """Return the snapshot timestamp so state changes on each new plan."""
        if snapshot := self.snapshot:
            return snapshot.created_at
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return plan details when the coordinator included them."""
        if not self.snapshot:
            return None
        diagnostics = self.snapshot.diagnostics or {}
        plan_details = diagnostics.get(self._details_key)
        if not isinstance(plan_details, dict):
            return None
        return plan_details


class ProjectionSensor(WattPlanCoordinatorSensor):
    """Sensor exposing projected cost savings metrics from the optimizer."""

    _require_usable_plan = True

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        *,
        projection_key: str,
        aggregate_mode: str = "horizon",
        value_transform: ProjectionValueTransform | None = None,
        use_home_currency: bool = False,
        native_unit_of_measurement: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize projected savings sensor."""
        super().__init__(config_entry, coordinator, **kwargs)
        self._projection_key = projection_key
        self._aggregate_mode = aggregate_mode
        self._value_transform = value_transform
        if use_home_currency:
            self._attr_native_unit_of_measurement = coordinator.hass.config.currency
            self._attr_suggested_display_precision = 2
        elif native_unit_of_measurement is not None:
            self._attr_native_unit_of_measurement = native_unit_of_measurement
            if native_unit_of_measurement == "%":
                self._attr_suggested_display_precision = 1
        if aggregate_mode == "next_interval":
            self._attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> float | None:
        """Return the projected metric for the configured aggregation mode."""
        optimizer = self._optimizer_diagnostics()
        if optimizer is None:
            return None
        projections = self._projections(optimizer)
        if projections is None:
            return None
        series = self._selected_projection_series(projections)
        if series is None:
            return None
        value = self._coerce_projection_value(series, self._projection_key)
        if value is None:
            return None
        if self._value_transform is not None:
            return self._value_transform(self, value)
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the planning span and per-interval projected values."""
        optimizer = self._optimizer_diagnostics()
        if optimizer is None:
            return None
        span_start = optimizer.get("span_start")
        span_end = optimizer.get("span_end")
        projections = self._projections(optimizer)
        if (
            not isinstance(span_start, str)
            or not isinstance(span_end, str)
            or projections is None
        ):
            return None
        per_slot = projections.get("per_slot")
        if not isinstance(per_slot, list):
            return None

        values: list[float] = []
        for slot in per_slot:
            if not isinstance(slot, dict):
                continue
            try:
                values.append(float(slot[self._projection_key]))
            except (KeyError, TypeError, ValueError):
                continue

        attributes: dict[str, Any] = {
            "span_start": span_start,
            "span_end": span_end,
            "total": projections.get(self._projection_key),
            "values": values,
        }
        if self._projection_key == "projected_savings_pct":
            attributes["formula"] = "(1 - projected_cost / baseline_cost) * 100"
            attributes["baseline_cost"] = projections.get("baseline_cost")
            attributes["projected_cost"] = projections.get("projected_cost")
            attributes["projected_savings_cost"] = projections.get(
                "projected_savings_cost"
            )
            attributes["baseline_cost_values"] = self._per_slot_values(
                per_slot, "baseline_cost"
            )
            attributes["projected_cost_values"] = self._per_slot_values(
                per_slot, "projected_cost"
            )
            attributes["projected_savings_cost_values"] = self._per_slot_values(
                per_slot, "projected_savings_cost"
            )
            attributes["max_exposed_percentage"] = MAX_EXPOSED_PROJECTED_SAVINGS_PCT
        return attributes

    def _optimizer_diagnostics(self) -> dict[str, Any] | None:
        """Return optimizer diagnostics from the current snapshot."""
        if not self.snapshot:
            return None
        diagnostics = self.snapshot.diagnostics or {}
        optimizer = diagnostics.get("optimizer")
        if not isinstance(optimizer, dict):
            return None
        return optimizer

    def _projections(self, optimizer: dict[str, Any]) -> dict[str, Any] | None:
        """Return optimizer projections when present."""
        projections = optimizer.get("projections")
        return projections if isinstance(projections, dict) else None

    def _selected_projection_series(
        self, projections: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Return the active projection aggregate for this sensor."""
        if self._aggregate_mode == "horizon":
            return projections
        per_slot = projections.get("per_slot")
        if not isinstance(per_slot, list) or not per_slot:
            return None
        first_slot = per_slot[0]
        return first_slot if isinstance(first_slot, dict) else None

    def _coerce_projection_value(
        self, source: dict[str, Any], key: str
    ) -> float | None:
        """Return a numeric projection value when available."""
        try:
            return float(source[key])
        except (KeyError, TypeError, ValueError):
            return None

    def _per_slot_values(self, per_slot: list[Any], key: str) -> list[float]:
        """Return one numeric per-slot projection series."""
        values: list[float] = []
        for slot in per_slot:
            if not isinstance(slot, dict):
                continue
            value = self._coerce_projection_value(slot, key)
            if value is not None:
                values.append(value)
        return values


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

    for source_key, sensor_key in (
        (CONF_SOURCE_USAGE, "usage_status"),
        (CONF_SOURCE_EXPORT_PRICE, "export_price_status"),
        (CONF_SOURCE_PV, "pv_status"),
    ):
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
