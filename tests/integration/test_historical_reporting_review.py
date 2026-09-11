"""Reporting-only review regressions using real stores and sensor classes."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wattplan.historical_cost.models import FLAG_MISSING_METER, SlotRecord
from custom_components.wattplan.historical_cost.store import HistoricalCostStore
from custom_components.wattplan.sensors.historical import (
    HISTORICAL_SENSOR_DESCRIPTIONS,
    HistoricalCostSensor,
)


def sensor_for(hass, store, key):
    """Use real sensor accessors and aggregation; replace only tracker plumbing."""
    tracker = SimpleNamespace(
        hass=hass, summary=store.summary, scenario_enabled=lambda _: True,
    )
    entry = MockConfigEntry(domain="wattplan", title="Review")
    description = next(d for d in HISTORICAL_SENSOR_DESCRIPTIONS if d.key == key)
    return HistoricalCostSensor(entry, tracker, description, entry_slug="review")


@pytest.mark.parametrize("missing_pv", [False, pytest.param(True, marks=pytest.mark.xfail(
    strict=True, reason="G2: shared meter flag discards valid actual/grid-only costs",
))])
@pytest.mark.parametrize("key", [
    "historical_actual_cost_today", "historical_savings_vs_grid_only_today",
])
async def test_missing_pv_cannot_change_measurable_grid_costs(hass, freezer, missing_pv, key):
    """Two known 1-unit grid bills and two 1-unit savings must sum to two."""
    now = datetime(2026, 9, 11, 12, tzinfo=UTC)
    freezer.move_to(now)
    store = HistoricalCostStore(hass, entry_id="review", slot_minutes=60, currency="DKK")
    for i in range(2):
        store.append_slot(SlotRecord(
            start=now-timedelta(hours=2-i), import_price=1, export_price=0,
            grid_import=1, grid_export=0, usage=2,
            pv=None if missing_pv and i == 1 else 0,
            flags=FLAG_MISSING_METER if missing_pv and i == 1 else 0,
        ))
    sensor = sensor_for(hass, store, key)
    assert sensor.available
    assert sensor.native_value == pytest.approx(2)


@pytest.mark.xfail(strict=True, reason="G3: overflowed savings is omitted without missing coverage")
async def test_savings_subtraction_overflow_is_counted_missing(hass, freezer):
    """Individually finite scenario costs do not guarantee finite savings."""
    now = datetime(2026, 9, 11, 12, tzinfo=UTC)
    freezer.move_to(now)
    store = HistoricalCostStore(hass, entry_id="overflow", slot_minutes=60, currency="DKK")
    for i, energy in enumerate([.5, 1e308]):
        store.append_slot(SlotRecord(
            start=now-timedelta(hours=2-i), import_price=1, export_price=1,
            grid_import=0, grid_export=energy, usage=energy, pv=energy,
        ))
    sensor = sensor_for(hass, store, "historical_savings_vs_grid_only_today")
    assert sensor.native_value == 1
    assert sensor.extra_state_attributes["missing_slots"] == 1
