"""Diagnostic sensors for WattPlan."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL, UnitOfEnergy

from ..coordinator import WattPlanCoordinator
from .base import WattPlanCoordinatorSensor
from .common import TIMESTAMP_DEVICE_CLASS, as_datetime


class OptionalTimestampSensor(WattPlanCoordinatorSensor):
    """Timestamp sensor for optional load options."""

    _require_validated_actions = True

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
            device_class=TIMESTAMP_DEVICE_CLASS,
            **kwargs,
        )
        self._subentry_id = subentry_id
        self._key = key

    @property
    def native_value(self) -> datetime | None:
        """Return timestamp from optional diagnostics payload."""
        return as_datetime(self._diagnostic_value(self._key))

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
    # The full per-slot forecast is large and changes with every plan.
    _unrecorded_attributes = frozenset({"forecast", "time_key", "value_key"})
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
    _attr_device_class = TIMESTAMP_DEVICE_CLASS
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
