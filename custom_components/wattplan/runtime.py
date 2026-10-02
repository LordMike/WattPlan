"""Runtime state helpers for the WattPlan integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

from .coordinator import WattPlanCoordinator
from .historical_cost.tracker import HistoricalCostTracker

if TYPE_CHECKING:
    from .target_persistence import BatteryTargetStore

_LOGGER = logging.getLogger(__name__)


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
    outlook_languages: tuple[str, ...]
    historical_tracker: HistoricalCostTracker | None = None
    optimizer_state: str | None = None
    runtime_update_listeners: set[Callable[[], None]] = field(default_factory=set)
    battery_targets: dict[str, BatteryTarget] = field(default_factory=dict)
    battery_target_update_listeners: dict[str, set[Callable[[], None]]] = field(
        default_factory=dict
    )
    battery_target_store: BatteryTargetStore | None = None


type WattPlanConfigEntry = ConfigEntry[WattPlanRuntimeData]


def mark_runtime_updated(runtime_data: WattPlanRuntimeData, *, when: datetime) -> None:
    """Update runtime timestamp and notify listeners."""
    runtime_data.last_run_at = when
    for listener in list(runtime_data.runtime_update_listeners):
        try:
            listener()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Runtime update listener failed")
