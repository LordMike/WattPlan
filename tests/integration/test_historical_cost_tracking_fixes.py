"""Regression tests for historical cost tracking review fixes."""

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.wattplan.historical_cost.models import (
    HistoricalMetric,
    PERIOD_THIS_MONTH,
    PERIOD_TODAY,
    SCENARIO_ACTUAL,
    SlotRecord,
)
from custom_components.wattplan.historical_cost.store import HistoricalCostStore


def _record(start: datetime, **changes) -> SlotRecord:
    values = {
        "start": start,
        "import_price": 1.0,
        "export_price": 0.5,
        "grid_import": 1.0,
        "grid_export": 0.0,
        "usage": 1.0,
        "pv": 0.0,
    }
    values.update(changes)
    return SlotRecord(**values)


async def _loaded_store(hass: HomeAssistant, freezer, now: datetime, *, slot_minutes: int = 60):
    """Return a loaded store with the clock frozen so pruning sees ``now``."""
    freezer.move_to(now)
    await hass.config.async_set_time_zone("UTC")
    store = HistoricalCostStore(
        hass,
        entry_id="fixes-entry",
        slot_minutes=slot_minutes,
        currency="DKK",
    )
    await store.async_load()
    return store


def _cost(store: HistoricalCostStore, period: str, now: datetime):
    return store.summary(
        metric=HistoricalMetric.COST,
        period=period,
        scenario=SCENARIO_ACTUAL,
        now=now,
    )


async def test_summaries_stay_zero_across_day_rollover(
    hass: HomeAssistant, freezer
) -> None:
    """Right after midnight "today" is zero until a new-day slot completes (COR-34)."""
    store = await _loaded_store(hass, freezer, datetime(2026, 5, 25, 0, 0, 5, tzinfo=UTC))
    store.data["tracking_started_at"] = datetime(2026, 5, 20, 8, tzinfo=UTC).isoformat()
    store.append_slot(_record(datetime(2026, 5, 24, 22, tzinfo=UTC)))
    store.append_slot(_record(datetime(2026, 5, 24, 23, tzinfo=UTC)))

    before = _cost(store, PERIOD_TODAY, datetime(2026, 5, 24, 23, 59, tzinfo=UTC))
    assert before.value == pytest.approx(2.0)

    # Midnight has passed but the 23:00 slot is the latest completed slot.
    after = _cost(store, PERIOD_TODAY, datetime(2026, 5, 25, 0, 0, 5, tzinfo=UTC))
    assert after.value == 0.0
    assert after.slots == 0
    assert after.missing_slots == 0

    store.append_slot(_record(datetime(2026, 5, 25, 0, tzinfo=UTC), grid_import=3.0))
    filled = _cost(store, PERIOD_TODAY, datetime(2026, 5, 25, 1, 0, 5, tzinfo=UTC))
    assert filled.value == pytest.approx(3.0)


async def test_summaries_stay_zero_across_month_rollover(
    hass: HomeAssistant, freezer
) -> None:
    """Right after the first of the month, month totals are zero, not unavailable."""
    store = await _loaded_store(hass, freezer, datetime(2026, 10, 1, 0, 0, 5, tzinfo=UTC))
    store.data["tracking_started_at"] = datetime(2026, 9, 3, 8, tzinfo=UTC).isoformat()
    store.append_slot(_record(datetime(2026, 9, 30, 23, tzinfo=UTC)))

    september = _cost(store, PERIOD_THIS_MONTH, datetime(2026, 9, 30, 23, 30, tzinfo=UTC))
    assert september.value == pytest.approx(1.0)

    october = _cost(store, PERIOD_THIS_MONTH, datetime(2026, 10, 1, 0, 0, 5, tzinfo=UTC))
    assert october.value == 0.0
    assert october.period_start.startswith("2026-10-01")

    today = _cost(store, PERIOD_TODAY, datetime(2026, 10, 1, 0, 0, 5, tzinfo=UTC))
    assert today.value == 0.0


async def test_summary_stays_unavailable_before_tracking_started(
    hass: HomeAssistant, freezer
) -> None:
    """A period that ended before tracking began has no value to report."""
    store = await _loaded_store(hass, freezer, datetime(2026, 5, 25, 8, tzinfo=UTC))
    store.data["tracking_started_at"] = datetime(2026, 5, 25, 8, tzinfo=UTC).isoformat()

    summary = _cost(store, PERIOD_TODAY, datetime(2026, 5, 24, 12, tzinfo=UTC))

    assert summary.value is None
