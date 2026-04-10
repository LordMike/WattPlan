"""Diagnostic and projection sensors for WattPlan."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL, UnitOfEnergy

from ..coordinator import WattPlanCoordinator
from .base import WattPlanCoordinatorSensor
from .common import MAX_EXPOSED_PROJECTED_SAVINGS_PCT, TIMESTAMP_DEVICE_CLASS, as_datetime

ProjectionValueTransform = Callable[["ProjectionSensor", float], float | None]


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
