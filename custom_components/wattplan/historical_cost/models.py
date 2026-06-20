"""Shared models and constants for historical cost tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

RETENTION_DAYS = 60
STORE_VERSION = 1
SAVE_DELAY_SECONDS = 10

SCENARIO_ACTUAL = "actual"
SCENARIO_GRID_ONLY = "grid_only"
SCENARIO_SELF_CONSUMPTION = "self_consumption"

PERIOD_TODAY = "today"
PERIOD_THIS_MONTH = "this_month"

FLAG_GAP = 1
FLAG_MISSING_IMPORT_PRICE = 2
FLAG_MISSING_EXPORT_PRICE = 4
FLAG_MISSING_METER = 8
FLAG_METER_RESET = 16
FLAG_SELF_CONSUMPTION_UNAVAILABLE = 32

DAY_ARRAY_KEYS: tuple[str, ...] = (
    "starts",
    "import_price",
    "export_price",
    "grid_import",
    "grid_export",
    "usage",
    "pv",
    "self_consumption_grid_import",
    "self_consumption_grid_export",
    "flags",
)


class HistoricalMetric(StrEnum):
    """Supported historical entity metric kinds."""

    COST = "cost"
    SAVINGS_VS_GRID_ONLY = "savings_vs_grid_only"
    SAVINGS_VS_SELF_CONSUMPTION = "savings_vs_self_consumption"


@dataclass(frozen=True, slots=True)
class HistoricalSensorDescription:
    """Description for one historical cost sensor."""

    key: str
    metric: HistoricalMetric
    period: str
    scenario: str | None
    name: str
    enabled_default: bool


@dataclass(frozen=True, slots=True)
class SlotRecord:
    """One retained historical slot."""

    start: datetime
    import_price: float | None
    export_price: float | None
    grid_import: float | None
    grid_export: float | None
    usage: float | None
    pv: float | None
    flags: int = 0
    self_consumption_grid_import: float | None = None
    self_consumption_grid_export: float | None = None


def default_store_payload(
    *, slot_minutes: int, currency: str, started_at: datetime
) -> dict[str, Any]:
    """Return an empty store payload for the current schema."""
    return {
        "version": STORE_VERSION,
        "slot_minutes": int(slot_minutes),
        "currency": currency,
        "tracking_started_at": started_at.isoformat(),
        "last_processed_slot": None,
        "last_meter_values": {},
        "meter_config": {},
        "price_cache": {},
        "days": {},
        "simulation_state": {
            "self_consumption": {
                "batteries": {},
            }
        },
    }


def empty_day_payload() -> dict[str, list[Any]]:
    """Return an empty day payload with all expected arrays."""
    return {key: [] for key in DAY_ARRAY_KEYS}
