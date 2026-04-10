"""Source issue and health status helpers for the coordinator."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..const import (
    CONF_SOURCE_EXPORT_PRICE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_MODE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
    SOURCE_MODE_NOT_USED,
)
from ..coordinator_parts import CoordinatorSnapshot
from ..source_fixup import SourceFixupProvider, SourceHealthKind, SourceHealthState
from ..source_issues import (
    build_source_issue,
    source_display_name,
    source_fill_defaults_needed,
    sync_source_issues,
)
from ..source_types import SourceProvider


class SourceStatusManager:
    """Own source issues and public status payloads for one coordinator."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._active_source_issues: dict[str, Any] = {}
        self._source_statuses: dict[str, dict[str, Any]] = {}
        self._overall_status: dict[str, Any] = self._default_overall_status()

    def reset(self) -> None:
        """Clear transient source state before a fresh planning run."""
        self._active_source_issues = {}
        self._source_statuses = {}

    def overall_status(self, *, is_stale: bool) -> dict[str, Any]:
        """Return current top-level health payload."""
        payload = dict(self._overall_status)
        if is_stale:
            payload.update(
                {
                    "status": "failed",
                    "reason_codes": ["coordinator_stale"],
                    "reason_summary": "No usable plan is available because coordinator state is stale",
                    "has_usable_plan": False,
                    "is_stale": False,
                }
            )
        return payload

    def source_status(self, source_key: str) -> dict[str, Any] | None:
        """Return current source health payload for one source."""
        status = self._source_statuses.get(source_key)
        if status is None:
            return None
        return dict(status)

    def source_health_diagnostics(self) -> dict[str, dict[str, Any]]:
        """Return a stable copy of per-source public health payloads."""
        return {
            source_key: dict(payload)
            for source_key, payload in self._source_statuses.items()
        }

    def record_source_issue_if_needed(
        self,
        *,
        entry: ConfigEntry,
        source_key: str,
        source_config: dict[str, Any],
        provider: SourceProvider,
    ) -> None:
        """Translate shared source health into one repair issue per source."""
        if not isinstance(provider, SourceFixupProvider):
            self._active_source_issues.pop(source_key, None)
            self._source_statuses[source_key] = self._build_source_status(
                source_key=source_key,
                source_config=source_config,
                health=None,
            )
            return

        health = provider.last_health
        self._source_statuses[source_key] = self._build_source_status(
            source_key=source_key,
            source_config=source_config,
            health=health,
        )
        if health.kind is SourceHealthKind.OK:
            self._active_source_issues.pop(source_key, None)
            return

        issue_kind = (
            "source_unavailable"
            if health.kind is SourceHealthKind.UNAVAILABLE
            else "source_incomplete"
        )
        self._active_source_issues[source_key] = build_source_issue(
            entry=entry,
            source_key=source_key,
            kind=issue_kind,
            source_name=source_display_name(source_key),
            consequence=self._source_consequence(source_key, health.kind),
            expires_at=health.expires_at if health.using_stale else None,
            available_count=health.available_count,
            required_count=health.required_count,
            is_fixable=(
                health.kind is SourceHealthKind.INCOMPLETE
                and source_fill_defaults_needed(source_config)
            ),
        )

    def sync_source_issues(self, entry: ConfigEntry) -> None:
        """Publish the current source issue set to the repairs dashboard."""
        sync_source_issues(
            self._hass,
            entry_id=entry.entry_id,
            issues=list(self._active_source_issues.values()),
        )

    def recompute_overall_status(
        self,
        *,
        planner_output: dict[str, Any],
        snapshot: CoordinatorSnapshot | None,
    ) -> None:
        """Recompute the public integration status from source and planner state."""
        reason_codes: list[str] = []
        affected_sources: list[str] = []
        critical_sources_failed: list[str] = []
        is_stale = False
        status = "ok"

        for source_key, source_status in self._source_statuses.items():
            source_state = str(source_status.get("status", "ok"))
            if bool(source_status.get("is_stale", False)):
                is_stale = True
            if source_state == "ok":
                continue
            affected_sources.append(source_key.removeprefix("source_"))
            if source_key == CONF_SOURCE_IMPORT_PRICE:
                if source_state == "failed":
                    critical_sources_failed.append("import_price")
                    reason_codes.append("source_import_price_failed_critical")
                    status = "failed"
                else:
                    reason_codes.append("source_import_price_degraded")
                    if status != "failed":
                        status = "degraded"
                continue
            if source_key == CONF_SOURCE_USAGE:
                if source_state == "failed":
                    critical_sources_failed.append("usage")
                    reason_codes.append("source_usage_failed_critical")
                    status = "failed"
                else:
                    reason_codes.append("source_usage_degraded")
                    if status != "failed":
                        status = "degraded"
                continue
            if source_key == CONF_SOURCE_PV:
                reason_codes.append("source_pv_failed_noncritical")
                if status != "failed":
                    status = "degraded"
                continue
            if source_key == CONF_SOURCE_EXPORT_PRICE:
                reason_codes.append("source_export_price_failed_noncritical")
                if status != "failed":
                    status = "degraded"

        optimizer = planner_output.get("diagnostics", {}).get("optimizer", {})
        if (
            status != "failed"
            and isinstance(optimizer, dict)
            and bool(optimizer.get("suboptimal", False))
        ):
            status = "degraded"
            reason_codes.append("optimizer_suboptimal")

        has_usable_plan = status != "failed"
        if not has_usable_plan:
            reason_summary = "No usable plan is available"
        elif status == "degraded":
            if is_stale and affected_sources:
                reason_summary = (
                    f"Plan is available using stale {affected_sources[0].replace('_', ' ')} data"
                )
            elif affected_sources:
                reason_summary = (
                    f"Plan is available, but {affected_sources[0].replace('_', ' ')} is degraded"
                )
            elif "optimizer_suboptimal" in reason_codes:
                reason_summary = "Plan is available, but the optimizer returned a degraded result"
            else:
                reason_summary = "Plan is available, but degraded"
        else:
            reason_summary = "Plan is healthy"

        self._overall_status = {
            "status": status,
            "reason_codes": reason_codes,
            "reason_summary": reason_summary,
            "affected_sources": affected_sources,
            "critical_sources_failed": critical_sources_failed,
            "is_stale": is_stale,
            "has_usable_plan": has_usable_plan,
            "expires_at": next(
                (
                    payload.get("expires_at")
                    for payload in self._source_statuses.values()
                    if payload.get("is_stale") and payload.get("expires_at")
                ),
                None,
            ),
            "plan_created_at": snapshot.created_at.isoformat() if snapshot is not None else None,
        }

    def mark_failed_status(self, err: Exception) -> None:
        """Mark the public health model as failed after a planning error."""
        existing_sources = [
            source_key.removeprefix("source_")
            for source_key, source_status in self._source_statuses.items()
            if source_status.get("status") == "failed"
        ]
        self._overall_status = {
            "status": "failed",
            "reason_codes": ["planner_failed"],
            "reason_summary": str(err),
            "affected_sources": existing_sources,
            "critical_sources_failed": [
                source_key
                for source_key in existing_sources
                if source_key in {"import_price", "usage"}
            ],
            "is_stale": False,
            "has_usable_plan": False,
            "expires_at": None,
            "plan_created_at": None,
        }

    def apply_restored_snapshot(self, snapshot: CoordinatorSnapshot) -> None:
        """Set top-level status after restoring a cached snapshot."""
        planner_status = str(snapshot.planner_status)
        restored_status = planner_status if planner_status in {"ok", "degraded"} else "ok"
        self._overall_status = {
            "status": restored_status,
            "reason_codes": [],
            "reason_summary": (
                snapshot.planner_message
                if snapshot.planner_message is not None
                else "Restored plan snapshot"
            ),
            "affected_sources": [],
            "critical_sources_failed": [],
            "is_stale": False,
            "has_usable_plan": True,
            "expires_at": None,
            "plan_created_at": snapshot.created_at.isoformat(),
        }

    def _build_source_status(
        self,
        *,
        source_key: str,
        source_config: dict[str, Any],
        health: SourceHealthState | None,
    ) -> dict[str, Any]:
        """Return stable public status payload for one configured source."""
        is_critical = self._source_is_critical(source_key, source_config)
        provider_kind = str(source_config.get(CONF_SOURCE_MODE, "unknown"))
        if health is None or health.kind is SourceHealthKind.OK:
            return {
                "status": "ok",
                "reason_code": "fresh",
                "reason_summary": "Source is healthy",
                "is_stale": False,
                "is_critical": is_critical,
                "available_count": None,
                "required_count": None,
                "expires_at": None,
                "provider_kind": provider_kind,
            }

        if health.using_stale:
            status = "degraded"
            reason_code = (
                "incomplete_stale_backed"
                if health.kind is SourceHealthKind.INCOMPLETE
                else "stale_reuse"
            )
            reason_summary = "Source is using stale fallback data"
        elif is_critical:
            status = "failed"
            reason_code = (
                "not_covering_horizon"
                if health.kind is SourceHealthKind.INCOMPLETE
                else "unavailable"
            )
            reason_summary = "Source is unavailable for planning"
        else:
            status = "degraded"
            reason_code = (
                "not_covering_horizon"
                if health.kind is SourceHealthKind.INCOMPLETE
                else "unavailable_noncritical"
            )
            reason_summary = "Source is unavailable, but planning can continue"

        return {
            "status": status,
            "reason_code": reason_code,
            "reason_summary": reason_summary,
            "is_stale": health.using_stale,
            "is_critical": is_critical,
            "available_count": health.available_count,
            "required_count": health.required_count,
            "expires_at": health.expires_at.isoformat() if health.expires_at else None,
            "provider_kind": provider_kind,
        }

    def _source_consequence(
        self, source_key: str, health_kind: SourceHealthKind
    ) -> str:
        """Return source-specific planning consequences for repairs text."""
        if source_key == CONF_SOURCE_PV:
            if health_kind is SourceHealthKind.UNAVAILABLE:
                return "WattPlan will continue, but it will plan without solar contribution."
            return "WattPlan will continue, but it will plan without solar contribution for the missing period."
        if source_key == CONF_SOURCE_EXPORT_PRICE:
            if health_kind is SourceHealthKind.UNAVAILABLE:
                return "WattPlan will continue, but exported power will be valued at zero."
            return "WattPlan will continue, but exported power will be valued at zero for the missing period."
        if health_kind is SourceHealthKind.UNAVAILABLE:
            return "WattPlan will stop producing new plans."
        return "WattPlan will stop producing new plans."

    def _source_is_critical(
        self, source_key: str, source_config: dict[str, Any]
    ) -> bool:
        """Return whether one configured source is critical to usable planning."""
        if source_key == CONF_SOURCE_IMPORT_PRICE:
            return True
        if source_key == CONF_SOURCE_USAGE:
            return source_config.get(CONF_SOURCE_MODE) != SOURCE_MODE_NOT_USED
        return False

    @staticmethod
    def _default_overall_status() -> dict[str, Any]:
        return {
            "status": "failed",
            "reason_codes": ["planner_failed"],
            "reason_summary": "No usable plan is available",
            "affected_sources": [],
            "critical_sources_failed": [],
            "is_stale": False,
            "has_usable_plan": False,
            "expires_at": None,
            "plan_created_at": None,
        }
