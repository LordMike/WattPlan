"""Shared state primitives for WattPlan flows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SourceFlowState:
    """Shared staged source state for setup and options flows."""

    last_source_available_count: int | None = None
    pending_source_key: str | None = None
    pending_source: dict[str, Any] | None = None
    pending_source_input: dict[str, Any] | None = None
    pending_source_step_id: str | None = None
    pending_source_summary: dict[str, Any] | None = None
    # Remote fetches made while reviewing the pending source, reused on accept.
    pending_fetch_cache: dict[Any, Any] = field(default_factory=dict)

    def clear_pending_source(self) -> None:
        """Reset all staged source-review state."""
        self.pending_source_key = None
        self.pending_source = None
        self.pending_source_input = None
        self.pending_source_step_id = None
        self.pending_source_summary = None
        self.pending_fetch_cache = {}
