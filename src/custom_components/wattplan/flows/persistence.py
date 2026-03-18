"""Persistence abstractions for shared flow behavior."""

from __future__ import annotations

from typing import Any, Protocol


class SourceFlowPersistence(Protocol):
    """Persistence contract for config and options source flows."""

    def stored_source(self, key: str) -> dict[str, Any]:
        """Return the currently persisted source config for one source key."""

    async def handle_source_marked_not_used(self, key: str) -> Any:
        """Persist a disabled source and continue the flow."""

    async def default_source_step(self) -> Any:
        """Return the fallback step when no staged source is active."""

    async def commit_reviewed_source(self, key: str, resolved_pending: dict[str, Any]) -> Any:
        """Persist the reviewed source and continue the flow."""

    def review_form_last_step(self, key: str) -> bool:
        """Return whether the review form should be marked as last-step."""
