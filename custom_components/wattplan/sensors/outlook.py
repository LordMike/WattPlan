"""Plan Outlook sensor."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..plan_outlook import build_status_plan_outlook, render_stored_plan_outlook
from .base import WattPlanCoordinatorSensor


class PlanOutlookSensor(WattPlanCoordinatorSensor):
    """Expose concise accepted-plan prose without controlling any device."""

    _require_usable_plan = False
    _require_snapshot = False

    @property
    def native_value(self) -> str:
        """Return a semantic report identifier that changes with report content."""
        outlook = self._outlook()
        return str(outlook.get("report_id", "plan_unavailable_high_unknown"))[:255]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return complete but bounded prose and diagnostic selection metadata."""
        outlook = self._outlook()
        keys = {
            "report_id",
            "semantic_id",
            "headline",
            "line_1",
            "line_2",
            "text",
            "language",
            "information_value",
            "topic",
            "selected_facts",
            "fact_details",
            "basis",
            "covered_start",
            "covered_end",
            "plan_created_at",
            "valid_until",
        }
        return {key: outlook[key] for key in keys if key in outlook}

    def _outlook(self) -> dict[str, Any]:
        now = datetime.now(tz=UTC).replace(second=0, microsecond=0)
        if self.coordinator.overall_status.get("has_usable_plan") is False:
            if self.snapshot is None:
                return build_status_plan_outlook("plan_unavailable", now=now)
            expired = "plan_stale" in self.coordinator.overall_status.get(
                "reason_codes", []
            )
            return build_status_plan_outlook(
                "plan_expired" if expired else "plan_unusable", now=now
            )
        if not self.coordinator.action_recommendations_validated:
            return build_status_plan_outlook("restored_unvalidated", now=now)
        if self.snapshot is None:
            return build_status_plan_outlook("plan_unavailable", now=now)
        diagnostics = self.snapshot.diagnostics or {}
        outlook = diagnostics.get("outlook")
        if not isinstance(outlook, dict):
            return build_status_plan_outlook("plan_unavailable", now=now)
        return render_stored_plan_outlook(
            outlook,
            now=now,
            source_health=self.coordinator.outlook_source_health,
            plan_failure_status=self.coordinator.outlook_failure_status,
        )
