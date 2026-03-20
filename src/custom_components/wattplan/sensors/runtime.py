"""Runtime timestamp sensors for WattPlan."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime

from ..coordinator import WattPlanCoordinator
from .base import WattPlanCoordinatorSensor
from .common import TIMESTAMP_DEVICE_CLASS


class LastRunSensor(WattPlanCoordinatorSensor):
    """Last successful run timestamp sensor."""

    _require_snapshot = False
    _attr_device_class = TIMESTAMP_DEVICE_CLASS

    @property
    def native_value(self) -> datetime | None:
        """Return timestamp for last successful stage."""
        return self.coordinator.last_success_at


class NextRunSensor(WattPlanCoordinatorSensor):
    """Next run timestamp sensor."""

    _require_snapshot = False
    _attr_device_class = TIMESTAMP_DEVICE_CLASS

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        **kwargs: Any,
    ) -> None:
        """Initialize next run sensor."""
        super().__init__(config_entry, coordinator, **kwargs)
        self._slot_minutes = int(config_entry.data["slot_minutes"])

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
