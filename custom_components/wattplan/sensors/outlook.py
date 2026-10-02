"""Plan Outlook sensor."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import Any

from ..plan_outlook import build_status_plan_outlook, render_stored_plan_outlook
from .base import WattPlanCoordinatorSensor

_LOGGER = logging.getLogger(__name__)


class PlanOutlookSensor(WattPlanCoordinatorSensor):
    """Expose concise accepted-plan prose without controlling any device."""

    # Keep the prose and its identifiers in history; the per-fact breakdown and
    # coverage timestamps change on every plan and bloat the Recorder.
    _unrecorded_attributes = frozenset(
        {
            "fact_details",
            "statements",
            "covered_start",
            "covered_end",
            "plan_created_at",
            "valid_until",
        }
    )
    _require_usable_plan = False
    _require_snapshot = False

    def __init__(self, *args: Any, language: str, **kwargs: Any) -> None:
        """Initialize one language-specific Plan Outlook sensor."""
        super().__init__(*args, **kwargs)
        self._language = language

    @property
    def native_value(self) -> str:
        """Return a semantic report identifier that changes with report content."""
        outlook = self._outlook()
        if outlook is None:
            return "plan_outlook_unavailable"
        return str(outlook.get("report_id", "plan_unavailable_high_unknown"))[:255]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return complete but bounded prose and diagnostic selection metadata."""
        outlook = self._outlook()
        if outlook is None:
            return {}
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

    @property
    def available(self) -> bool:
        """Return whether a report can be generated for the current plan."""
        if not super().available:
            return False
        if self.snapshot is None or not self.coordinator.has_usable_plan:
            return True
        if not self.coordinator.action_recommendations_validated:
            return True
        diagnostics = self.snapshot.diagnostics or {}
        if not isinstance(diagnostics.get("outlook"), dict):
            return False
        return self._outlook() is not None

    def _outlook(self) -> dict[str, Any] | None:
        now = datetime.now(tz=UTC).replace(second=0, microsecond=0)
        if self.coordinator.overall_status.get("has_usable_plan") is False:
            if self.snapshot is None:
                return build_status_plan_outlook(
                    "plan_unavailable", now=now, language=self._language
                )
            expired = "plan_stale" in self.coordinator.overall_status.get(
                "reason_codes", []
            )
            return build_status_plan_outlook(
                "plan_expired" if expired else "plan_unusable",
                now=now,
                language=self._language,
            )
        if not self.coordinator.action_recommendations_validated:
            return build_status_plan_outlook(
                "restored_unvalidated", now=now, language=self._language
            )
        if self.snapshot is None:
            return build_status_plan_outlook(
                "plan_unavailable", now=now, language=self._language
            )
        diagnostics = self.snapshot.diagnostics or {}
        outlook = diagnostics.get("outlook")
        if not isinstance(outlook, dict):
            return build_status_plan_outlook(
                "plan_unavailable", now=now, language=self._language
            )
        try:
            return render_stored_plan_outlook(
                outlook,
                now=now,
                source_health=self.coordinator.outlook_source_health,
                plan_failure_status=self.coordinator.outlook_failure_status,
                language=self._language,
            )
        except Exception:  # A report failure must not make an accepted plan unavailable.
            _LOGGER.exception(
                "Plan Outlook rendering failed (entry_id=%s, language=%s, "
                "plan_created_at=%s)",
                (
                    self.coordinator.config_entry.entry_id
                    if self.coordinator.config_entry is not None
                    else "unknown"
                ),
                self._language,
                outlook.get("plan_created_at"),
            )
            return None
