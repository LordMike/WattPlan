"""Snapshot model helpers for the WattPlan coordinator."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import UTC, datetime
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
    planner_message: str | None = None
    diagnostics: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize snapshot for storage."""
        return {
            "created_at": self.created_at.isoformat(),
            "planner_status": self.planner_status,
            "planner_message": self.planner_message,
            "diagnostics": self.diagnostics,
        }

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

        return cls(
            created_at=created_at,
            planner_status=planner_status,
            planner_message=planner_message,
            diagnostics=diagnostics,
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
