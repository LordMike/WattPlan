"""Post-provider source fixup for WattPlan."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from .const import (
    CONF_EDGE_FILL_MODE,
    CONF_FIXUP_PROFILE,
    CONF_RESAMPLE_MODE,
    EDGE_FILL_MODE_NONE,
    FIXUP_PROFILE_EXTEND,
    FIXUP_PROFILE_REPAIR,
    FIXUP_PROFILE_STRICT,
    RESAMPLE_MODE_NONE,
)
from .source_types import SourceProvider, SourceProviderError, SourceWindow


def effective_provider_config(source_config: dict[str, Any]) -> dict[str, Any]:
    """Return provider config after applying the selected fixup policy."""
    effective = dict(source_config)
    profile = str(effective.get(CONF_FIXUP_PROFILE, FIXUP_PROFILE_REPAIR))
    if profile == FIXUP_PROFILE_STRICT:
        effective[CONF_RESAMPLE_MODE] = RESAMPLE_MODE_NONE
        effective[CONF_EDGE_FILL_MODE] = EDGE_FILL_MODE_NONE
    return effective


@dataclass(frozen=True, slots=True)
class CachedSourceValues:
    """Remember the last successful source window for transient failures.

    The cache stores the fully normalized values from a successful run so a
    later refresh can shift the same pattern forward when one provider call
    fails. The cache is bounded by its original start time and slot size, so
    stale data cannot be reused forever.
    """

    start_at: datetime
    slot_minutes: int
    values: list[float]


class SourceHealthKind(StrEnum):
    """Classify the current-cycle health of one source."""

    OK = "ok"
    UNAVAILABLE = "unavailable"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class SourceHealthState:
    """Describe the current-cycle source result after fixup/reuse decisions."""

    kind: SourceHealthKind
    available_count: int
    required_count: int
    using_stale: bool
    expires_at: datetime | None
    error_code: str | None = None
    reason: str | None = None


class SourceFixupProvider(SourceProvider):
    """Apply post-provider fixups so the final result matches the horizon."""

    def __init__(
        self,
        provider: SourceProvider,
        *,
        profile: str,
    ) -> None:
        """Initialize the fixup wrapper."""
        self._provider = provider
        self._profile = profile
        self._last_success: CachedSourceValues | None = None
        self._last_health = SourceHealthState(
            kind=SourceHealthKind.OK,
            available_count=0,
            required_count=0,
            using_stale=False,
            expires_at=None,
        )

    @property
    def last_health(self) -> SourceHealthState:
        """Return the current-cycle source health from the last resolution."""
        return self._last_health

    async def async_values(self, window: SourceWindow) -> list[float]:
        """Return exactly `window.slots` values after fixup."""
        try:
            values = await self._provider.async_values(window)
        except SourceProviderError as err:
            repaired = await self._recover_values(window, err)
            if repaired is not None:
                return repaired
            self._last_health = self._health_from_error(
                window=window,
                error=err,
                using_stale=False,
            )
            raise

        # Cache the normalized result so one bad refresh does not immediately
        # topple planning. Reuse always shifts from the cached start time rather
        # than replaying the values at the wrong timestamps.
        self._last_success = CachedSourceValues(
            start_at=window.start_at,
            slot_minutes=window.slot_minutes,
            values=list(values),
        )
        self._last_health = SourceHealthState(
            kind=SourceHealthKind.OK,
            available_count=len(values),
            required_count=window.slots,
            using_stale=False,
            expires_at=self._cached_end_at(self._last_success),
        )
        return values

    async def _recover_values(
        self, window: SourceWindow, original_error: SourceProviderError
    ) -> list[float] | None:
        """Return repaired values for a transient failure when possible.

        We prefer the current provider's data when it can still return a
        partial window, because that keeps the freshest intervals. If that
        fails, we fall back to the last successful normalized window and shift
        it forward to the current start time.
        """

        if self._profile == FIXUP_PROFILE_EXTEND:
            with_current = await self._repeat_t_minus_24h(window, original_error)
            if with_current is not None:
                self._last_health = SourceHealthState(
                    kind=SourceHealthKind.OK,
                    available_count=len(with_current),
                    required_count=window.slots,
                    using_stale=False,
                    expires_at=self._cached_end_at(self._last_success),
                )
                return with_current

        reused = self._reuse_last_success(window)
        if reused is not None:
            self._last_health = self._health_from_error(
                window=window,
                error=original_error,
                using_stale=True,
            )
        return reused

    async def _repeat_t_minus_24h(
        self, window: SourceWindow, original_error: SourceProviderError
    ) -> list[float] | None:
        """Extend the missing tail by repeating data from 24 hours earlier."""
        day_slots = int((24 * 60) / window.slot_minutes)
        available_count = int(original_error.details.get("available_count", 0))
        known_slots = max(day_slots, available_count)
        if day_slots <= 0 or known_slots < day_slots:
            return None

        base_window = replace(window, slots=min(window.slots, known_slots))
        try:
            values = await self._provider.async_values(base_window)
        except SourceProviderError:
            return None

        if len(values) < day_slots:
            return None

        completed = list(values)
        while len(completed) < window.slots:
            repeat_index = len(completed) - day_slots
            if repeat_index < 0:
                return None
            completed.append(completed[repeat_index])
        return completed[: window.slots]

    def _reuse_last_success(self, window: SourceWindow) -> list[float] | None:
        """Shift the last good window forward and extend from the full cache.

        The cached window is only reusable while the requested start still lies
        inside that original window. Once the requested start moves past the
        cached end, the stale window has fully elapsed and cannot help.
        """

        cached = self._last_success
        if cached is None or cached.slot_minutes != window.slot_minutes:
            return None

        slot_seconds = window.slot_minutes * 60
        offset_seconds = (window.start_at - cached.start_at).total_seconds()
        if offset_seconds < 0 or offset_seconds % slot_seconds != 0:
            return None

        offset_slots = int(offset_seconds // slot_seconds)
        if offset_slots >= len(cached.values):
            return None

        if len(cached.values) >= offset_slots + window.slots:
            return cached.values[offset_slots : offset_slots + window.slots]

        if self._profile != FIXUP_PROFILE_EXTEND:
            return None

        day_slots = int((24 * 60) / window.slot_minutes)
        if day_slots <= 0 or len(cached.values) < day_slots:
            return None

        # Extend from the full cached pattern, not just the still-overlapping
        # tail, so stale reuse keeps the same daily shape as time advances.
        completed = list(cached.values)
        target_length = offset_slots + window.slots
        while len(completed) < target_length:
            repeat_index = len(completed) - day_slots
            if repeat_index < 0:
                return None
            completed.append(completed[repeat_index])

        return completed[offset_slots:target_length]

    def _health_from_error(
        self,
        *,
        window: SourceWindow,
        error: SourceProviderError,
        using_stale: bool,
    ) -> SourceHealthState:
        """Map one provider error to the issue-level source health category.

        The issue model is intentionally coarse. We only distinguish between
        "no usable data at all / call failed" and "some data existed but not
        enough values survived normalization to cover the horizon".
        """

        available_count = int(error.details.get("available_count", 0))
        required_count = int(error.details.get("required_count", window.slots))
        if available_count > 0 and available_count < required_count:
            kind = SourceHealthKind.INCOMPLETE
        else:
            kind = SourceHealthKind.UNAVAILABLE

        return SourceHealthState(
            kind=kind,
            available_count=available_count,
            required_count=required_count,
            using_stale=using_stale,
            expires_at=self._cached_end_at(self._last_success),
            error_code=error.code,
            reason=str(error.details.get("provider_reason") or error.details.get("reason") or ""),
        )

    def _cached_end_at(self, cached: CachedSourceValues | None) -> datetime | None:
        """Return the original coverage end for the last successful window."""
        if cached is None:
            return None
        return cached.start_at + timedelta(
            minutes=cached.slot_minutes * len(cached.values)
        )
