"""Helpers for user-supplied battery target lifecycle."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util


def _notify_target_listeners(runtime_data: Any, subentry_id: str) -> None:
    """Push target updates to any entities reflecting this battery target."""
    for listener in list(runtime_data.battery_target_update_listeners.get(subentry_id, ())):
        listener()


def set_battery_target(runtime_data: Any, subentry_id: str, target: Any) -> None:
    """Store a battery target and notify entities that mirror it."""
    runtime_data.battery_targets[subentry_id] = target
    _notify_target_listeners(runtime_data, subentry_id)


def clear_battery_target(runtime_data: Any, subentry_id: str) -> bool:
    """Remove a battery target and notify listeners when state changed."""
    if runtime_data.battery_targets.pop(subentry_id, None) is None:
        return False
    _notify_target_listeners(runtime_data, subentry_id)
    return True


def get_active_battery_target(
    runtime_data: Any,
    subentry_id: str,
    *,
    now: datetime | None = None,
) -> Any | None:
    """Return the target only while its deadline is still in the future."""
    target = runtime_data.battery_targets.get(subentry_id)
    if target is None:
        return None

    current_time = now or dt_util.utcnow()
    if target.reach_at <= current_time:
        return None
    return target


def clear_expired_battery_targets(
    runtime_data: Any,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Drop any target whose deadline has already passed.

    This keeps planning behavior deterministic: once the requested deadline is in
    the past, the target should no longer influence optimizer input or UI state.
    """
    current_time = now or dt_util.utcnow()
    expired = [
        subentry_id
        for subentry_id, target in runtime_data.battery_targets.items()
        if target.reach_at <= current_time
    ]
    for subentry_id in expired:
        clear_battery_target(runtime_data, subentry_id)
    return expired
