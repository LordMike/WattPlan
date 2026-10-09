"""Historical cost and savings sensors."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import CALLBACK_TYPE

from ..historical_cost.models import (
    HistoricalMetric,
    HistoricalSensorDescription,
    PERIOD_LAST_SLOT,
    PERIOD_THIS_MONTH,
    PERIOD_TODAY,
    SCENARIO_ACTUAL,
    SCENARIO_GRID_ONLY,
    SCENARIO_SELF_CONSUMPTION,
)
from ..historical_cost.tracker import HistoricalCostTracker
from .common import entry_device_info

HISTORICAL_SENSOR_DESCRIPTIONS: tuple[HistoricalSensorDescription, ...] = (
    HistoricalSensorDescription(
        key="energy_balance_discrepancy",
        metric=HistoricalMetric.ENERGY_BALANCE_DISCREPANCY,
        period=PERIOD_LAST_SLOT,
        scenario=None,
        name="Energy Balance Discrepancy",
        enabled_default=True,
    ),
    HistoricalSensorDescription(
        key="energy_balance_discrepancy_today",
        metric=HistoricalMetric.ENERGY_BALANCE_DISCREPANCY,
        period=PERIOD_TODAY,
        scenario=None,
        name="Energy Balance Discrepancy Today",
        enabled_default=True,
    ),
    HistoricalSensorDescription(
        key="historical_actual_cost_today",
        metric=HistoricalMetric.COST,
        period=PERIOD_TODAY,
        scenario=SCENARIO_ACTUAL,
        name="Actual Cost Today",
        enabled_default=True,
    ),
    HistoricalSensorDescription(
        key="historical_grid_only_cost_today",
        metric=HistoricalMetric.COST,
        period=PERIOD_TODAY,
        scenario=SCENARIO_GRID_ONLY,
        name="Grid Only Cost Today",
        enabled_default=True,
    ),
    HistoricalSensorDescription(
        key="historical_self_consumption_cost_today",
        metric=HistoricalMetric.COST,
        period=PERIOD_TODAY,
        scenario=SCENARIO_SELF_CONSUMPTION,
        name="Simple Self Consumption Cost Today",
        enabled_default=True,
    ),
    HistoricalSensorDescription(
        key="historical_savings_vs_grid_only_today",
        metric=HistoricalMetric.SAVINGS_VS_GRID_ONLY,
        period=PERIOD_TODAY,
        scenario=None,
        name="Savings Today vs Grid Only",
        enabled_default=True,
    ),
    HistoricalSensorDescription(
        key="historical_savings_vs_self_consumption_today",
        metric=HistoricalMetric.SAVINGS_VS_SELF_CONSUMPTION,
        period=PERIOD_TODAY,
        scenario=None,
        name="Savings Today vs Simple Self Consumption",
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
        key="historical_grid_only_cost_this_month",
        metric=HistoricalMetric.COST,
        period=PERIOD_THIS_MONTH,
        scenario=SCENARIO_GRID_ONLY,
        name="Grid Only Cost This Month",
        enabled_default=False,
    ),
    HistoricalSensorDescription(
        key="historical_self_consumption_cost_this_month",
        metric=HistoricalMetric.COST,
        period=PERIOD_THIS_MONTH,
        scenario=SCENARIO_SELF_CONSUMPTION,
        name="Simple Self Consumption Cost This Month",
        enabled_default=False,
    ),
    HistoricalSensorDescription(
        key="historical_savings_vs_grid_only_this_month",
        metric=HistoricalMetric.SAVINGS_VS_GRID_ONLY,
        period=PERIOD_THIS_MONTH,
        scenario=None,
        name="Savings This Month vs Grid Only",
        enabled_default=False,
    ),
    HistoricalSensorDescription(
        key="historical_savings_vs_self_consumption_this_month",
        metric=HistoricalMetric.SAVINGS_VS_SELF_CONSUMPTION,
        period=PERIOD_THIS_MONTH,
        scenario=None,
        name="Savings This Month vs Simple Self Consumption",
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
        if description.metric is HistoricalMetric.ENERGY_BALANCE_DISCREPANCY:
            power = description.period == PERIOD_LAST_SLOT
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_device_class = (
                SensorDeviceClass.POWER if power else SensorDeviceClass.ENERGY
            )
            self._attr_native_unit_of_measurement = (
                UnitOfPower.WATT if power else UnitOfEnergy.WATT_HOUR
            )
            self._attr_state_class = (
                SensorStateClass.MEASUREMENT if power else SensorStateClass.TOTAL
            )
            self._attr_suggested_display_precision = 1
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
        value = self._summary().value
        if (
            value is not None
            and self._description.metric is HistoricalMetric.ENERGY_BALANCE_DISCREPANCY
        ):
            value *= 1000
            if self._description.period == PERIOD_LAST_SLOT:
                value *= 60 / self._tracker.slot_minutes
        return value

    @property
    def last_reset(self) -> datetime | None:
        """Identify the daily reset of the signed energy total for statistics."""
        if (
            self._description.metric is HistoricalMetric.ENERGY_BALANCE_DISCREPANCY
            and self._description.period == PERIOD_TODAY
        ):
            summary = self._summary()
            if summary.value is not None:
                return datetime.fromisoformat(summary.period_start)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return period and retention metadata."""
        summary = self._summary()
        attributes = {
            "tracking_started_at": summary.tracking_started_at,
            "last_complete_slot": summary.last_complete_slot,
            "slots": summary.slots,
            "missing_slots": summary.missing_slots,
            "period_start": summary.period_start,
            "period_end": summary.period_end,
            "scenario": summary.scenario,
        }
        if self._description.metric is HistoricalMetric.ENERGY_BALANCE_DISCREPANCY:
            valid_slots = summary.slots - summary.missing_slots
            hours = valid_slots * self._tracker.slot_minutes / 60
            attributes.update(self._tracker.energy_balance_attributes())
            attributes.update({
                "valid_slots": valid_slots,
                "covered_hours": hours,
                "average_discrepancy_w": (
                    round(summary.value * 1000 / hours, 1)
                    if summary.value is not None and hours else None
                ),
            })
        if self._is_self_consumption_sensor():
            attributes["reference_segment_ids"] = list(summary.reference_segment_ids)
            attributes["reference_segment_count"] = len(summary.reference_segment_ids)
            attributes.update(self._tracker.self_consumption_simulation_attributes())
        return attributes

    def _summary(self):
        return self._tracker.summary(
            metric=self._description.metric,
            period=self._description.period,
            scenario=self._description.scenario,
        )

    def _scenario_enabled(self) -> bool:
        description = self._description
        if description.metric is HistoricalMetric.ENERGY_BALANCE_DISCREPANCY:
            return not self._tracker.energy_balance_attributes()["missing_battery_meters"]
        if description.metric is HistoricalMetric.SAVINGS_VS_GRID_ONLY:
            return self._tracker.scenario_enabled(SCENARIO_GRID_ONLY)
        if description.metric is HistoricalMetric.SAVINGS_VS_SELF_CONSUMPTION:
            return self._tracker.scenario_enabled(SCENARIO_SELF_CONSUMPTION)
        return self._tracker.scenario_enabled(description.scenario)

    def _is_self_consumption_sensor(self) -> bool:
        description = self._description
        return (
            description.scenario == SCENARIO_SELF_CONSUMPTION
            or description.metric is HistoricalMetric.SAVINGS_VS_SELF_CONSUMPTION
        )


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
