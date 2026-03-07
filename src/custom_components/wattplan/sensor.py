"""Sensor platform for WattPlan."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, MATCH_ALL, UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import (
    CONF_OPTIONS_COUNT,
    CONF_SLOT_MINUTES,
    CONF_SOURCE_MODE,
    CONF_SOURCE_USAGE,
    CONF_SOURCES,
    DOMAIN,
    SOURCE_MODE_BUILT_IN,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_COMFORT,
    SUBENTRY_TYPE_OPTIONAL,
)
from .coordinator import CoordinatorSnapshot, WattPlanCoordinator

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


def _subentry_slug(subentry: Any) -> str:
    """Return slug for subentry naming."""
    return slugify(str(subentry.data.get(CONF_NAME, subentry.title))) or "asset"


def _entry_slug(config_entry: ConfigEntry) -> str:
    """Return slug for config entry naming."""
    return slugify(config_entry.title) or "entry"


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
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


class WattPlanCoordinatorSensor(CoordinatorEntity[WattPlanCoordinator], SensorEntity):
    """Base WattPlan sensor backed by coordinator snapshots."""

    _attr_should_poll = False
    _require_snapshot = True

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        *,
        object_id: str,
        unique_id: str,
        device_class: SensorDeviceClass | None = None,
    ) -> None:
        """Initialize coordinator-backed sensor."""
        super().__init__(coordinator)
        self._attr_object_id = object_id
        self._attr_name = object_id
        self._attr_unique_id = unique_id
        self._attr_device_info = _entry_device_info(config_entry)
        if device_class is not None:
            self._attr_device_class = device_class

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if not super().available:
            return False
        if self.coordinator.is_stale:
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

    @property
    def native_value(self) -> str | None:
        """Return planner status value from snapshot."""
        if snapshot := self.snapshot:
            return snapshot.planner_status
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return status context."""
        attrs = self.coordinator.error_attributes()
        if snapshot := self.snapshot:
            optimizer = {}
            if isinstance(snapshot.diagnostics, dict):
                optimizer = snapshot.diagnostics.get("optimizer", {})
            reason_keys = []
            if isinstance(optimizer, dict):
                raw_reason_keys = optimizer.get("suboptimal_reasons", [])
                if isinstance(raw_reason_keys, list):
                    reason_keys = [str(reason) for reason in raw_reason_keys]
            attrs.update(
                {
                    "snapshot_created_at": snapshot.created_at,
                    "planner_message": snapshot.planner_message,
                    "suboptimal_reason_keys": reason_keys,
                    "suboptimal_reason_descriptions": [
                        SUBOPTIMAL_REASON_DESCRIPTIONS.get(
                            reason, f"Unknown suboptimal reason: {reason}"
                        )
                        for reason in reason_keys
                    ],
                }
            )
        return attrs


class ActionSensor(WattPlanCoordinatorSensor):
    """Action sensor with next action timestamp attributes."""

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
        """Return next action metadata."""
        data = self._action_data()
        attrs: dict[str, str] = {}
        timestamp = _as_datetime(data.get("next_action_timestamp"))
        if timestamp is not None:
            attrs["next_action_timestamp"] = timestamp.isoformat()

        next_action = data.get("next_action")
        if isinstance(next_action, str):
            attrs["next_action"] = next_action

        if self._group == "batteries" and (charge_source := data.get("charge_source")):
            attrs["charge_source"] = str(charge_source)

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
        unique_id: str,
    ) -> None:
        """Initialize battery target sensor."""
        self._attr_object_id = object_id
        self._attr_name = object_id
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
        if target := self._runtime_data.battery_targets.get(self._subentry_id):
            return target.soc_kwh
        return None

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return timing metadata for the current user intent."""
        if target := self._runtime_data.battery_targets.get(self._subentry_id):
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
        if not self.snapshot:
            return None
        diagnostics = self.snapshot.diagnostics or {}
        optional_data = diagnostics.get("optionals", {})
        if not isinstance(optional_data, dict):
            return None
        subentry_data = optional_data.get(self._subentry_id, {})
        if not isinstance(subentry_data, dict):
            return None
        return _as_datetime(subentry_data.get(self._key))


class UsageForecastSensor(WattPlanCoordinatorSensor):
    """Sensor exposing built-in usage forecast in adapter-compatible format."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_entity_registry_enabled_default = False
    _attr_suggested_display_precision = 2

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
        plan_details = diagnostics.get("plan_details")
        if not isinstance(plan_details, dict):
            return None
        return plan_details


class ProjectionSensor(WattPlanCoordinatorSensor):
    """Sensor exposing projected cost savings metrics from the optimizer."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        *,
        projection_key: str,
        use_home_currency: bool = False,
        native_unit_of_measurement: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize projected savings sensor."""
        super().__init__(config_entry, coordinator, **kwargs)
        self._projection_key = projection_key
        if use_home_currency:
            self._attr_native_unit_of_measurement = coordinator.hass.config.currency
            self._attr_suggested_display_precision = 2
        elif native_unit_of_measurement is not None:
            self._attr_native_unit_of_measurement = native_unit_of_measurement
            if native_unit_of_measurement == "%":
                self._attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        """Return the first timeslot projected optimizer metric."""
        optimizer = self._optimizer_diagnostics()
        if optimizer is None:
            return None
        projections = optimizer.get("projections")
        if not isinstance(projections, dict):
            return None
        per_slot = projections.get("per_slot")
        if not isinstance(per_slot, list) or not per_slot:
            return None
        first_slot = per_slot[0]
        if not isinstance(first_slot, dict):
            return None
        try:
            return float(first_slot[self._projection_key])
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the planning span and per-interval projected values."""
        optimizer = self._optimizer_diagnostics()
        if optimizer is None:
            return None
        span_start = optimizer.get("span_start")
        span_end = optimizer.get("span_end")
        projections = optimizer.get("projections")
        if (
            not isinstance(span_start, str)
            or not isinstance(span_end, str)
            or not isinstance(projections, dict)
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

        return {
            "span_start": span_start,
            "span_end": span_end,
            "total": projections.get(self._projection_key),
            "values": values,
        }

    def _optimizer_diagnostics(self) -> dict[str, Any] | None:
        """Return optimizer diagnostics from the current snapshot."""
        if not self.snapshot:
            return None
        diagnostics = self.snapshot.diagnostics or {}
        optimizer = diagnostics.get("optimizer")
        if not isinstance(optimizer, dict):
            return None
        return optimizer


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up WattPlan sensors for one config entry."""
    entry_slug = _entry_slug(config_entry)
    runtime_data = config_entry.runtime_data
    coordinator = runtime_data.coordinator

    sensors: list[SensorEntity] = [
        StatusSensor(
            config_entry,
            coordinator,
            object_id=f"{entry_slug}_status",
            unique_id=f"{config_entry.entry_id}:entry:status",
        ),
        LastRunSensor(
            config_entry,
            coordinator,
            object_id=f"{entry_slug}_last_run",
            unique_id=f"{config_entry.entry_id}:entry:last_run",
            device_class=SensorDeviceClass.TIMESTAMP,
        ),
        NextRunSensor(
            config_entry,
            coordinator,
            object_id=f"{entry_slug}_next_run",
            unique_id=f"{config_entry.entry_id}:entry:next_run",
            device_class=SensorDeviceClass.TIMESTAMP,
        ),
        LastRunDurationSensor(
            config_entry,
            coordinator,
            object_id=f"{entry_slug}_last_run_duration",
            unique_id=f"{config_entry.entry_id}:entry:last_run_duration",
        ),
        ProjectionSensor(
            config_entry,
            coordinator,
            projection_key="projected_savings_cost",
            use_home_currency=True,
            object_id=f"{entry_slug}_projected_cost_savings",
            unique_id=f"{config_entry.entry_id}:entry:projected_cost_savings",
        ),
        ProjectionSensor(
            config_entry,
            coordinator,
            projection_key="projected_savings_pct",
            object_id=f"{entry_slug}_projected_savings_percentage",
            unique_id=f"{config_entry.entry_id}:entry:projected_savings_percentage",
            native_unit_of_measurement="%",
        ),
        PlanDetailsSensor(
            config_entry,
            coordinator,
            object_id=f"{entry_slug}_plan_details",
            unique_id=f"{config_entry.entry_id}:entry:plan_details",
        ),
    ]

    usage_source = config_entry.data.get(CONF_SOURCES, {}).get(CONF_SOURCE_USAGE, {})
    if (
        isinstance(usage_source, dict)
        and usage_source.get(CONF_SOURCE_MODE) == SOURCE_MODE_BUILT_IN
    ):
        sensors.append(
            UsageForecastSensor(
                config_entry,
                coordinator,
                object_id=f"{entry_slug}_usage_forecast",
                unique_id=f"{config_entry.entry_id}:entry:usage_forecast",
            )
        )

    for subentry in config_entry.subentries.values():
        sub_slug = _subentry_slug(subentry)
        if subentry.subentry_type == SUBENTRY_TYPE_BATTERY:
            sensors.extend(
                [
                    # This reflects the user's current intention from
                    # `wattplan.set_target`; it stays unknown until set.
                    BatteryTargetSocSensor(
                        config_entry,
                        runtime_data,
                        subentry.subentry_id,
                        object_id=f"{entry_slug}_{sub_slug}_target",
                        unique_id=f"{config_entry.entry_id}:{subentry.subentry_id}:target",
                    ),
                    ActionSensor(
                        config_entry,
                        coordinator,
                        subentry_id=subentry.subentry_id,
                        group="batteries",
                        object_id=f"{entry_slug}_{sub_slug}_action",
                        unique_id=f"{config_entry.entry_id}:{subentry.subentry_id}:action",
                    ),
                ]
            )
        elif subentry.subentry_type == SUBENTRY_TYPE_COMFORT:
            sensors.extend(
                [
                    ActionSensor(
                        config_entry,
                        coordinator,
                        subentry_id=subentry.subentry_id,
                        group="comforts",
                        object_id=f"{entry_slug}_{sub_slug}_action",
                        unique_id=f"{config_entry.entry_id}:{subentry.subentry_id}:action",
                    ),
                ]
            )
        elif subentry.subentry_type == SUBENTRY_TYPE_OPTIONAL:
            option_count = int(subentry.data[CONF_OPTIONS_COUNT])

            sensors.extend(
                [
                    OptionalTimestampSensor(
                        config_entry,
                        coordinator,
                        subentry_id=subentry.subentry_id,
                        key="next_start_option",
                        object_id=f"{entry_slug}_{sub_slug}_next_start_option",
                        unique_id=(
                            f"{config_entry.entry_id}:{subentry.subentry_id}:"
                            "next_start_option"
                        ),
                    ),
                    OptionalTimestampSensor(
                        config_entry,
                        coordinator,
                        subentry_id=subentry.subentry_id,
                        key="next_end_option",
                        object_id=f"{entry_slug}_{sub_slug}_next_end_option",
                        unique_id=(
                            f"{config_entry.entry_id}:{subentry.subentry_id}:"
                            "next_end_option"
                        ),
                    ),
                ]
            )
            for option_index in range(1, option_count + 1):
                option_key = f"option_{option_index}_start"
                sensors.append(
                    OptionalTimestampSensor(
                        config_entry,
                        coordinator,
                        subentry_id=subentry.subentry_id,
                        key=option_key,
                        object_id=f"{entry_slug}_{sub_slug}_option_{option_index}_start",
                        unique_id=(
                            f"{config_entry.entry_id}:{subentry.subentry_id}:"
                            f"option_{option_index}_start"
                        ),
                    )
                )

    async_add_entities(sensors)
