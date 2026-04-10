"""Support state types for the WattPlan coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

type TimingEntry = tuple[str, int]


class CycleTrigger(StrEnum):
    """Trigger source for plan and emit cycles."""

    SCHEDULE = "schedule"
    SERVICE = "service"


class Stage(StrEnum):
    """Coordinator stages."""

    PLAN = "plan"
    EMIT = "emit"


class StageErrorKind(StrEnum):
    """Classified stage failure reasons."""

    LOCKED = "locked"
    SOURCE_FETCH = "source_fetch"
    SOURCE_PARSE = "source_parse"
    SOURCE_VALIDATION = "source_validation"
    PLANNER_INPUT = "planner_input"
    PLANNER_EXECUTION = "planner_execution"
    EMIT_NO_SNAPSHOT = "emit_no_snapshot"
    EMIT_PROJECTION = "emit_projection"
    INTERNAL = "internal"


class PlanningStageError(Exception):
    """Error raised for categorized planning stage failures."""

    def __init__(
        self,
        kind: StageErrorKind,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize planning stage error."""
        super().__init__(message)
        self.kind = kind
        self.details = details or {}


class EmitStageError(Exception):
    """Error raised for categorized emission stage failures."""

    def __init__(
        self,
        kind: StageErrorKind,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize emit stage error."""
        super().__init__(message)
        self.kind = kind
        self.details = details or {}


@dataclass(slots=True)
class StageErrorState:
    """Runtime error state for one coordinator stage."""

    has_error: bool = False
    kind: StageErrorKind | None = None
    message: str | None = None
    at: datetime | None = None
    details: dict[str, Any] | None = None
    consecutive_failures: int = 0
    skipped_locked_count: int = 0


__all__ = [
    "CycleTrigger",
    "EmitStageError",
    "PlanningStageError",
    "Stage",
    "StageErrorKind",
    "StageErrorState",
    "TimingEntry",
]
