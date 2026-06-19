"""Historical cost and savings sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE

from ..const import DOMAIN
from ..historical_cost.models import (
    HistoricalMetric,
    HistoricalSensorDescription,
    PERIOD_THIS_MONTH,
    PERIOD_TODAY,
    SCENARIO_ACTUAL,
    SCENARIO_NO_BATTERY,
    SCENARIO_SELF_CONSUMPTION,
)
from ..historical_cost.tracker import HistoricalCostTracker
from .common import entry_device_info

HISTORICAL_SENSOR_DESCRIPTIONS: tuple[HistoricalSensorDescription, ...] = (
    HistoricalSensorDescription(
        key="historical_actual_cost_today",
        metric=HistoricalMetric.COST,
        period=PERIOD_TODAY,
        scenario=SCENARIO_ACTUAL,
        name="Actual Cost Today",
        enabled_default=True,
    ),
    HistoricalSensorDescription(
        key="historical_no_battery_cost_today",
        metric=HistoricalMetric.COST,
        period=PERIOD_TODAY,
        scenario=SCENARIO_NO_BATTERY,
        name="No Battery Cost Today",
        enabled_default=True,
    ),
    HistoricalSensorDescription(
        key="historical_self_consumption_cost_today",
        metric=HistoricalMetric.COST,
        period=PERIOD_TODAY,
        scenario=SCENARIO_SELF_CONSUMPTION,
        name="Self Consumption Cost Today",
        enabled_default=True,
    ),
    HistoricalSensorDescription(
        key="historical_savings_vs_no_battery_today",
        metric=HistoricalMetric.SAVINGS_VS_NO_BATTERY,
        period=PERIOD_TODAY,
        scenario=None,
        name="Savings Today vs No Battery",
        enabled_default=True,
    ),
    HistoricalSensorDescription(
        key="historical_savings_vs_self_consumption_today",
        metric=HistoricalMetric.SAVINGS_VS_SELF_CONSUMPTION,
        period=PERIOD_TODAY,
        scenario=None,
        name="Savings Today vs Self Consumption",
        enabled_default=True,
    ),
    HistoricalSensorDescription(
        key="historical_actual_cost_this_month",
        metric=HistoricalMetric.COST,
        period=PERIOD_THIS_MONTH,
        scenario=SCENARIO_ACTUAL,
        name="Actual Cost This Month",
        enabled_default=False,
    ),
    HistoricalSensorDescription(
        key="historical_no_battery_cost_this_month",
        metric=HistoricalMetric.COST,
        period=PERIOD_THIS_MONTH,
        scenario=SCENARIO_NO_BATTERY,
        name="No Battery Cost This Month",
        enabled_default=False,
    ),
    HistoricalSensorDescription(
        key="historical_self_consumption_cost_this_month",
        metric=HistoricalMetric.COST,
        period=PERIOD_THIS_MONTH,
        scenario=SCENARIO_SELF_CONSUMPTION,
        name="Self Consumption Cost This Month",
        enabled_default=False,
    ),
    HistoricalSensorDescription(
        key="historical_savings_vs_no_battery_this_month",
        metric=HistoricalMetric.SAVINGS_VS_NO_BATTERY,
        period=PERIOD_THIS_MONTH,
        scenario=None,
        name="Savings This Month vs No Battery",
        enabled_default=False,
    ),
    HistoricalSensorDescription(
        key="historical_savings_vs_self_consumption_this_month",
        metric=HistoricalMetric.SAVINGS_VS_SELF_CONSUMPTION,
        period=PERIOD_THIS_MONTH,
        scenario=None,
        name="Savings This Month vs Self Consumption",
        enabled_default=False,
    ),
)


class HistoricalCostSensor(SensorEntity):
    """Historical cost or savings aggregate sensor."""

    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        config_entry: ConfigEntry,
        tracker: HistoricalCostTracker,
        description: HistoricalSensorDescription,
        *,
        entry_slug: str,
    ) -> None:
        """Initialize the sensor."""
        self._tracker = tracker
        self._description = description
        self._attr_name = description.name
        self._attr_object_id = f"{entry_slug}_{description.key}"
        self.internal_integration_suggested_object_id = self._attr_object_id
        self._attr_unique_id = f"{config_entry.entry_id}:historical:{description.key}"
        self._attr_native_unit_of_measurement = tracker.hass.config.currency
        self._attr_entity_registry_enabled_default = description.enabled_default
        self._attr_device_info = entry_device_info(config_entry)
        self._remove_listener: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to tracker updates."""
        self._remove_listener = self._tracker.async_add_listener(
            self.async_write_ha_state
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from tracker updates."""
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None

    @property
    def available(self) -> bool:
        """Return if this aggregate currently has a value."""
        if not self._scenario_enabled():
            return False
        return self._summary().value is not None

    @property
    def native_value(self) -> float | None:
        """Return the aggregate value."""
        if not self._scenario_enabled():
            return None
        return self._summary().value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return period and retention metadata."""
        summary = self._summary()
        return {
            "tracking_started_at": summary.tracking_started_at,
            "last_complete_slot": summary.last_complete_slot,
            "slots": summary.slots,
            "missing_slots": summary.missing_slots,
            "period_start": summary.period_start,
            "period_end": summary.period_end,
            "scenario": summary.scenario,
        }

    def _summary(self):
        return self._tracker.summary(
            metric=self._description.metric,
            period=self._description.period,
            scenario=self._description.scenario,
        )

    def _scenario_enabled(self) -> bool:
        description = self._description
        if description.metric is HistoricalMetric.SAVINGS_VS_NO_BATTERY:
            return self._tracker.scenario_enabled(SCENARIO_NO_BATTERY)
        if description.metric is HistoricalMetric.SAVINGS_VS_SELF_CONSUMPTION:
            return self._tracker.scenario_enabled(SCENARIO_SELF_CONSUMPTION)
        return self._tracker.scenario_enabled(description.scenario)


def build_historical_sensors(
    config_entry: ConfigEntry,
    tracker: HistoricalCostTracker,
    *,
    entry_slug: str,
) -> list[HistoricalCostSensor]:
    """Build all historical cost sensors for one config entry."""
    return [
        HistoricalCostSensor(
            config_entry,
            tracker,
            description,
            entry_slug=entry_slug,
        )
        for description in HISTORICAL_SENSOR_DESCRIPTIONS
    ]
