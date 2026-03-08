"""Shared source provider types for WattPlan."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class SourceProviderError(Exception):
    """Raised when a source provider cannot resolve valid values."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize source provider error."""
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class SourceWindow:
    """Requested planner window for one source provider call."""

    start_at: datetime
    slot_minutes: int
    slots: int


class SourceProvider(ABC):
    """Interface for providers that resolve one numeric value per interval."""

    @abstractmethod
    async def async_values(self, window: SourceWindow) -> list[float]:
        """Return exactly `window.slots` values for the requested window."""
