"""Runtime state helpers for the WattPlan integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from homeassistant.config_entries import ConfigEntry

from .coordinator import WattPlanCoordinator


@dataclass
class BatteryTarget:
    """Battery target requested by the user."""

    soc_kwh: float
    reach_at: datetime


@dataclass
class WattPlanRuntimeData:
    """Runtime data for one loaded WattPlan config entry."""

    coordinator: WattPlanCoordinator
    last_run_at: datetime
    optimizer_state: str | None = None
    runtime_update_listeners: set[Callable[[], None]] = field(default_factory=set)
    battery_targets: dict[str, BatteryTarget] = field(default_factory=dict)
    battery_target_update_listeners: dict[str, set[Callable[[], None]]] = field(
        default_factory=dict
    )


type WattPlanConfigEntry = ConfigEntry[WattPlanRuntimeData]


def mark_runtime_updated(runtime_data: WattPlanRuntimeData, *, when: datetime) -> None:
    """Update runtime timestamp and notify listeners."""
    runtime_data.last_run_at = when
    for listener in list(runtime_data.runtime_update_listeners):
        listener()
