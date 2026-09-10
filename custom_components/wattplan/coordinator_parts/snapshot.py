"""Snapshot model helpers for the WattPlan coordinator."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Any

from ..datetime_utils import parse_datetime_like


def parse_snapshot_datetime(value: Any) -> datetime | None:
    """Parse a datetime-like restore value."""
    parsed = parse_datetime_like(value)
    if parsed is None:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class CoordinatorSnapshot:
    """Immutable snapshot produced by the planning stage."""

    created_at: datetime
    planner_status: str
    action_schedules: dict[str, Any]
    planner_message: str | None = None
    diagnostics: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize snapshot for storage."""
        return {
            "created_at": self.created_at.isoformat(),
            "planner_status": self.planner_status,
            "planner_message": self.planner_message,
            "diagnostics": self.diagnostics,
            "action_schedules": self.action_schedules,
        }

    def action_data(
        self,
        group: str,
        subentry_id: str,
        *,
        at: datetime | None = None,
    ) -> dict[str, str]:
        """Return current and next actions from the retained executable schedule."""
        schedules = self.action_schedules
        if not isinstance(schedules, dict):
            return {}

        start_at = parse_snapshot_datetime(schedules.get("start_at"))
        try:
            slot_minutes = int(schedules.get("slot_minutes", 0))
        except (TypeError, ValueError):
            return {}
        group_schedules = schedules.get(group)
        if (
            start_at is None
            or slot_minutes <= 0
            or not isinstance(group_schedules, dict)
        ):
            return {}

        actions = group_schedules.get(subentry_id)
        if not isinstance(actions, list) or not actions:
            return {}

        current_at = at or datetime.now(tz=UTC)
        if current_at.tzinfo is None:
            current_at = current_at.replace(tzinfo=UTC)
        else:
            current_at = current_at.astimezone(UTC)
        elapsed_seconds = (current_at - start_at).total_seconds()
        slot_seconds = slot_minutes * 60
        slot_index = int(elapsed_seconds // slot_seconds)
        if slot_index < 0 or slot_index >= len(actions):
            return {}

        current_action = actions[slot_index]
        if not isinstance(current_action, str):
            return {}
        data = {"action": current_action}
        for next_index in range(slot_index + 1, len(actions)):
            next_action = actions[next_index]
            if not isinstance(next_action, str):
                return {}
            if next_action == current_action:
                continue
            data["next_action"] = next_action
            data["next_action_timestamp"] = (
                start_at + timedelta(minutes=next_index * slot_minutes)
            ).isoformat()
            break
        return data

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CoordinatorSnapshot | None:
        """Deserialize snapshot from storage payload."""
        created_at = parse_snapshot_datetime(payload.get("created_at"))
        planner_status = payload.get("planner_status")
        if created_at is None or not isinstance(planner_status, str):
            return None

        planner_message = payload.get("planner_message")
        if not isinstance(planner_message, str | type(None)):
            return None

        diagnostics = payload.get("diagnostics")
        if not isinstance(diagnostics, dict | type(None)):
            return None

        action_schedules = payload.get("action_schedules")
        if not isinstance(action_schedules, dict):
            return None

        return cls(
            created_at=created_at,
            planner_status=planner_status,
            planner_message=planner_message,
            diagnostics=diagnostics,
            action_schedules=action_schedules,
        )


def snapshot_schema_id() -> str:
    """Return schema identity for serialized snapshot cache."""
    schema_descriptor = {
        "fields": [
            {
                "name": field.name,
                "type": str(field.type),
            }
            for field in fields(CoordinatorSnapshot)
        ],
    }
    encoded = json.dumps(schema_descriptor, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode()).hexdigest()[:16]
    return f"CoordinatorSnapshot:{digest}"


__all__ = ["CoordinatorSnapshot", "parse_snapshot_datetime", "snapshot_schema_id"]
