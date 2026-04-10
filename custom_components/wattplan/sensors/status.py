"""Status-oriented WattPlan sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry

from ..coordinator import WattPlanCoordinator
from .base import WattPlanCoordinatorSensor


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
            "critical_sources_failed": list(status.get("critical_sources_failed", [])),
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
