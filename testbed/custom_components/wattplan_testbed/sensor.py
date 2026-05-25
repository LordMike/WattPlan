"""Sensor platform for the WattPlan testbed integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    DOMAIN,
    SUBENTRY_BATTERY,
    SUBENTRY_LOAD,
    SUBENTRY_PRICE,
    SUBENTRY_PV,
)
from .runtime import TestbedRuntime, subentry_name, subentry_slug


class TestbedSensor(RestoreSensor):
    """Base testbed sensor."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(
        self,
        runtime: TestbedRuntime,
        subentry: Any,
        *,
        key: str,
        name_suffix: str,
        object_suffix: str,
    ) -> None:
        """Initialize the sensor."""
        self._runtime = runtime
        self._subentry = subentry
        object_id = f"{runtime.entry_slug}_{subentry_slug(subentry)}_{object_suffix}"
        self._attr_object_id = object_id
        self.internal_integration_suggested_object_id = object_id
        self._attr_unique_id = f"{runtime.entry.entry_id}:{subentry.subentry_id}:{key}"
        self._attr_name = f"{subentry_name(subentry)} {name_suffix}"
        self._attr_device_info = runtime.device_info(subentry=subentry)

    async def async_added_to_hass(self) -> None:
        """Register for runtime refreshes."""
        await super().async_added_to_hass()
        self._runtime.register_entity(self)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister from runtime refreshes."""
        self._runtime.unregister_entity(self)
        await super().async_will_remove_from_hass()


class FutureValuesSensor(TestbedSensor):
    """Sensor with WattPlan-compatible future-value attributes."""

    def __init__(
        self,
        runtime: TestbedRuntime,
        subentry: Any,
        *,
        kind: str,
        key: str,
        name_suffix: str,
        object_suffix: str,
        unit: str,
        device_class: SensorDeviceClass | None = None,
    ) -> None:
        """Initialize future-values sensor."""
        super().__init__(
            runtime,
            subentry,
            key=key,
            name_suffix=name_suffix,
            object_suffix=object_suffix,
        )
        self._kind = kind
        self._attr_native_unit_of_measurement = unit
        self._attr_state_class = SensorStateClass.MEASUREMENT
        if device_class is not None:
            self._attr_device_class = device_class

    @property
    def native_value(self) -> float | None:
        """Return current generated value."""
        points = self._runtime.future_points(self._subentry, kind=self._kind)
        if not points:
            return None
        return float(points[0]["value"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return future values for WattPlan entity adapters."""
        points = self._runtime.future_points(self._subentry, kind=self._kind)
        return {
            "future_values": points,
            "generator": self._subentry.data.get("generator"),
            "source_kind": self._kind,
            "future_window_hours": 24,
        }


class PriceSensor(FutureValuesSensor):
    """One simulated price source."""

    def __init__(self, runtime: TestbedRuntime, subentry: Any) -> None:
        """Initialize price sensor."""
        currency = str(runtime.hass.config.currency or "USD")
        super().__init__(
            runtime,
            subentry,
            kind="price",
            key="price",
            name_suffix="Price",
            object_suffix="price",
            unit=f"{currency}/kWh",
        )


class LoadEnergySensor(TestbedSensor):
    """Cumulative load energy sensor for built-in usage source tests."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    async def async_added_to_hass(self) -> None:
        """Restore and register the cumulative load value."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_sensor_data()
        restored_value = None
        if restored is not None and restored.native_value is not None:
            restored_value = float(restored.native_value)
        self._runtime.initialize_load_energy(self._subentry, restored_value)

    @property
    def native_value(self) -> float:
        """Return current cumulative load value."""
        return self._runtime.load_energy_state(self._subentry)


class PvPowerSensor(TestbedSensor):
    """PV production power sensor with future-value attributes."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        """Return current generated PV power."""
        points = self._runtime.future_points(self._subentry, kind="pv")
        if not points:
            return None
        return float(points[0]["value"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return planner-compatible power future values."""
        return {
            "future_values": self._runtime.future_points(self._subentry, kind="pv"),
            "generator": self._subentry.data.get("generator"),
            "source_kind": "pv",
            "future_window_hours": 24,
        }


class BatterySocSensor(TestbedSensor):
    """Battery SoC sensor."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE

    @property
    def available(self) -> bool:
        """Return whether the SoC state is usable."""
        return self._runtime.battery_soc_state(self._subentry.subentry_id) is not None

    @property
    def native_value(self) -> float | None:
        """Return current battery SoC percent."""
        value = self._runtime.battery_soc_state(self._subentry.subentry_id)
        return None if value is None else float(value)


def _source_sensors(runtime: TestbedRuntime, subentry: Any) -> list[SensorEntity]:
    """Return sensors for one source subentry."""
    if subentry.subentry_type == SUBENTRY_PRICE:
        return [
            PriceSensor(runtime, subentry),
        ]
    if subentry.subentry_type == SUBENTRY_PV:
        return [
            PvPowerSensor(
                runtime,
                subentry,
                key="pv_power",
                name_suffix="PV Power",
                object_suffix="pv_power",
            )
        ]
    if subentry.subentry_type == SUBENTRY_LOAD:
        return [
            FutureValuesSensor(
                runtime,
                subentry,
                kind="load",
                key="load_power",
                name_suffix="Load Power",
                object_suffix="load_power",
                unit=UnitOfPower.WATT,
                device_class=SensorDeviceClass.POWER,
            ),
            LoadEnergySensor(
                runtime,
                subentry,
                key="load_energy",
                name_suffix="Load Energy",
                object_suffix="load_energy",
            ),
        ]
    if subentry.subentry_type == SUBENTRY_BATTERY:
        return [
            BatterySocSensor(
                runtime,
                subentry,
                key="soc",
                name_suffix="SoC",
                object_suffix="soc",
            )
        ]
    return []


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up testbed sensors."""
    runtime: TestbedRuntime = config_entry.runtime_data
    sensors: list[SensorEntity] = []
    for subentry in config_entry.subentries.values():
        sensors.extend(_source_sensors(runtime, subentry))
    async_add_entities(sensors)
