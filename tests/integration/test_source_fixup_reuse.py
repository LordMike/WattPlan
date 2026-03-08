"""Tests for stale source reuse in the shared fixup layer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.wattplan.const import FIXUP_PROFILE_EXTEND
from custom_components.wattplan.source_fixup import (
    CachedSourceValues,
    SourceFixupProvider,
)
from custom_components.wattplan.source_types import (
    SourceProvider,
    SourceProviderError,
    SourceWindow,
)


def _window(*, start_at: datetime | None = None, slots: int = 24) -> SourceWindow:
    """Return a common planner window for stale-reuse tests."""

    return SourceWindow(
        start_at=start_at or datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        slot_minutes=60,
        slots=slots,
    )


class _SuccessThenErrorProvider(SourceProvider):
    """Return one successful window and then fail on later calls."""

    def __init__(self, values: list[float]) -> None:
        """Initialize the stateful provider."""
        self._values = values
        self._call_count = 0

    async def async_values(self, window: SourceWindow) -> list[float]:
        """Return the first window and fail on every later request."""

        self._call_count += 1
        if self._call_count == 1:
            return self._values[: window.slots]
        raise SourceProviderError(
            "source_fetch",
            "temporary failure",
            details={"available_count": 0},
        )


def test_stale_reuse_shifts_extends_and_expires() -> None:
    """Stale reuse should shift forward, extend from the full cache, and expire."""
    provider = SourceFixupProvider(
        _SuccessThenErrorProvider([float(index) for index in range(24)]),
        profile=FIXUP_PROFILE_EXTEND,
    )
    initial_window = _window()
    provider._last_success = CachedSourceValues(
        start_at=initial_window.start_at,
        slot_minutes=initial_window.slot_minutes,
        values=[float(index) for index in range(24)],
    )

    shifted_values = provider._reuse_last_success(
        _window(start_at=initial_window.start_at + timedelta(hours=1))
    )
    extended_values = provider._reuse_last_success(
        _window(start_at=initial_window.start_at + timedelta(hours=23))
    )
    expired_values = provider._reuse_last_success(
        _window(start_at=initial_window.start_at + timedelta(days=1))
    )

    assert shifted_values == [float(index) for index in range(1, 24)] + [0.0]
    assert extended_values == [23.0] + [float(index) for index in range(23)]
    assert expired_values is None
