"""Regression tests for historical cost tracking review fixes."""

import asyncio
from datetime import UTC, datetime, timedelta

from types import SimpleNamespace

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import CONF_NAME, UnitOfEnergy
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wattplan.const import (
    ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
    CONF_ACTION_EMISSION_ENABLED,
    CONF_ADAPTER_TYPE,
    CONF_FIXUP_PROFILE,
    CONF_HISTORICAL_COST_TRACKING_ENABLED,
    CONF_HISTORICAL_GRID_EXPORT_SENSOR,
    CONF_HISTORICAL_GRID_IMPORT_SENSOR,
    CONF_HISTORICAL_PV_SENSOR,
    CONF_HISTORICAL_SIMULATE_SELF_CONSUMPTION,
    CONF_HISTORICAL_USAGE_SENSOR,
    CONF_HOURS_TO_PLAN,
    CONF_PLANNING_ENABLED,
    CONF_RECORD_PLANNER_REPRODUCTIONS,
    CONF_SLOT_MINUTES,
    CONF_SOURCE_EXPORT_PRICE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_MODE,
    CONF_SOURCES,
    CONF_TEMPLATE,
    CONF_TIME_KEY,
    CONF_VALUE_KEY,
    DOMAIN,
    FIXUP_PROFILE_STRICT,
    SOURCE_MODE_ENTITY_ADAPTER,
    SOURCE_MODE_TEMPLATE,
)
from custom_components.wattplan.coordinator import CycleTrigger
from custom_components.wattplan.historical_cost.models import (
    FLAG_METER_RESET,
    FLAG_MISSING_EXPORT_PRICE,
    FLAG_MISSING_METER,
    FLAG_MULTI_SLOT_DELTA,
    HistoricalMetric,
    PERIOD_THIS_MONTH,
    PERIOD_TODAY,
    SCENARIO_ACTUAL,
    SlotRecord,
)
from custom_components.wattplan.historical_cost.store import HistoricalCostStore
from custom_components.wattplan.sensors.historical import (
    HISTORICAL_SENSOR_DESCRIPTIONS,
    HistoricalCostSensor,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


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


def _set_meter(hass: HomeAssistant, entity_id: str, value: float | str) -> None:
    hass.states.async_set(
        entity_id,
        str(value),
        {
            "device_class": SensorDeviceClass.ENERGY,
            "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        },
    )


async def _setup_tracker(hass: HomeAssistant, freezer, start: datetime, *, record_plans=False):
    """Set up a 15-minute tracker with import/usage meters and a fixed price."""
    freezer.move_to(start)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 15,
            CONF_HOURS_TO_PLAN: 1,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [1.0, 1.0, 1.0, 1.0] }}",
                },
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_RECORD_PLANNER_REPRODUCTIONS: record_plans,
            CONF_ACTION_EMISSION_ENABLED: False,
            CONF_HISTORICAL_COST_TRACKING_ENABLED: True,
            CONF_HISTORICAL_GRID_IMPORT_SENSOR: "sensor.grid_import_total",
            CONF_HISTORICAL_USAGE_SENSOR: "sensor.usage_total",
            CONF_HISTORICAL_GRID_EXPORT_SENSOR: None,
            CONF_HISTORICAL_PV_SENSOR: None,
            CONF_HISTORICAL_SIMULATE_SELF_CONSUMPTION: False,
        },
    )
    entry.add_to_hass(hass)
    _set_meter(hass, "sensor.grid_import_total", 100.0)
    _set_meter(hass, "sensor.usage_total", 200.0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    tracker = entry.runtime_data.historical_tracker
    assert tracker is not None
    tracker.remember_price_series(
        start_at=start,
        slot_minutes=15,
        import_prices=[1.0] * 8,
        export_prices=[0.0] * 8,
    )
    return tracker


async def _tick(hass, freezer, tracker, start: datetime, minutes: int, grid_import, usage):
    freezer.move_to(start + timedelta(minutes=minutes, seconds=2))
    _set_meter(hass, "sensor.grid_import_total", grid_import)
    _set_meter(hass, "sensor.usage_total", usage)
    await tracker.async_process_completed_slot()


async def test_meter_gaps_cost_only_the_affected_slots(
    hass: HomeAssistant, freezer
) -> None:
    """Resets and missing readings never leak energy into the wrong slot (COR-35)."""
    start = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    tracker = await _setup_tracker(hass, freezer, start)
    tracker.remember_price_series(
        start_at=start,
        slot_minutes=15,
        import_prices=[1.0] * 16,
        export_prices=[0.0] * 16,
    )

    await _tick(hass, freezer, tracker, start, 15, 101.0, 201.0)
    # Counters reset: the new reading is the baseline.
    await _tick(hass, freezer, tracker, start, 30, 0.5, 0.5)
    assert tracker.store.last_meter_values()["grid_import"] == pytest.approx(0.5)
    await _tick(hass, freezer, tracker, start, 45, 1.5, 2.0)
    # A single unavailable reading keeps the last finite baseline; the next
    # delta spans two slots and is flagged.
    await _tick(hass, freezer, tracker, start, 60, "unavailable", "unavailable")
    assert tracker.store.last_meter_values()["grid_import"] == pytest.approx(1.5)
    await _tick(hass, freezer, tracker, start, 75, 3.5, 4.0)
    await _tick(hass, freezer, tracker, start, 90, 4.5, 5.0)
    # A non-finite reading is treated like an unavailable one.
    await _tick(hass, freezer, tracker, start, 105, "nan", "nan")
    await _tick(hass, freezer, tracker, start, 120, 6.5, 7.0)
    await _tick(hass, freezer, tracker, start, 135, 7.5, 8.0)
    assert tracker.store.meter_stale_slots() == {}
    # A delta that spans several slots cannot be attributed and stays missing.
    await _tick(hass, freezer, tracker, start, 150, "unavailable", "unavailable")
    await _tick(hass, freezer, tracker, start, 165, "unavailable", "unavailable")
    await _tick(hass, freezer, tracker, start, 180, 10.5, 11.0)
    await _tick(hass, freezer, tracker, start, 195, 11.5, 12.0)

    day = tracker.store.data["days"]["2026-05-24"]
    assert day["flags"] == [
        0, FLAG_METER_RESET, 0,
        FLAG_MISSING_METER, FLAG_MULTI_SLOT_DELTA, 0,
        FLAG_MISSING_METER, FLAG_MULTI_SLOT_DELTA, 0,
        FLAG_MISSING_METER, FLAG_MISSING_METER, FLAG_MISSING_METER, 0,
    ]
    assert day["grid_import"] == [
        pytest.approx(1.0), None, pytest.approx(1.0),
        None, pytest.approx(2.0), pytest.approx(1.0),
        None, pytest.approx(2.0), pytest.approx(1.0),
        None, None, None, pytest.approx(1.0),
    ]
    assert day["usage"][:3] == [pytest.approx(1.0), None, pytest.approx(1.5)]


async def _plan_with_export_source(
    hass: HomeAssistant,
    freezer,
    start: datetime,
    *,
    export_healthy: bool,
    export_meter: bool = True,
):
    """Plan once with a configured export source that is healthy or missing."""
    freezer.move_to(start + timedelta(seconds=2))
    points = [
        {"start": (start + timedelta(minutes=15 * i)).isoformat(), "value": 0.1 + i / 100}
        for i in range(4)
    ]
    if export_healthy:
        hass.states.async_set("sensor.export_price_forecast", "ok", {"prices": points})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 15,
            CONF_HOURS_TO_PLAN: 1,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [1.0, 1.0, 1.0, 1.0] }}",
                },
                CONF_SOURCE_EXPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
                    "entity_id": "sensor.export_price_forecast",
                    CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
                    CONF_NAME: "prices",
                    CONF_TIME_KEY: "start",
                    CONF_VALUE_KEY: "value",
                    CONF_FIXUP_PROFILE: FIXUP_PROFILE_STRICT,
                },
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
            CONF_HISTORICAL_COST_TRACKING_ENABLED: True,
            CONF_HISTORICAL_GRID_IMPORT_SENSOR: "sensor.grid_import_total",
            CONF_HISTORICAL_USAGE_SENSOR: "sensor.usage_total",
            CONF_HISTORICAL_GRID_EXPORT_SENSOR: (
                "sensor.grid_export_total" if export_meter else None
            ),
            CONF_HISTORICAL_PV_SENSOR: None,
            CONF_HISTORICAL_SIMULATE_SELF_CONSUMPTION: False,
        },
    )
    entry.add_to_hass(hass)
    _set_meter(hass, "sensor.grid_import_total", 100.0)
    _set_meter(hass, "sensor.grid_export_total", 10.0)
    _set_meter(hass, "sensor.usage_total", 200.0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await entry.runtime_data.coordinator.async_plan(trigger=CycleTrigger.SERVICE)
    return entry.runtime_data.historical_tracker


async def test_unavailable_export_source_prices_are_not_cached_as_zero(
    hass: HomeAssistant, freezer
) -> None:
    """Zeros substituted for a failed export source must not become prices (COR-36)."""
    start = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    tracker = await _plan_with_export_source(hass, freezer, start, export_healthy=False)

    assert tracker.store.cached_price(start, "import") == pytest.approx(1.0)
    assert tracker.store.cached_price(start, "export") is None

    # The completed slot falls back to a live read, which also fails: the slot
    # is flagged instead of silently booking zero export revenue.
    tracker.store.update_metadata(
        last_processed_slot=start - timedelta(minutes=15),
        last_meter_values={"grid_import": 100.0, "grid_export": 10.0, "usage": 200.0, "pv": 0.0},
        meter_config=tracker._meter_config(),
    )
    freezer.move_to(start + timedelta(minutes=15, seconds=2))
    _set_meter(hass, "sensor.grid_import_total", 101.0)
    _set_meter(hass, "sensor.grid_export_total", 10.5)
    _set_meter(hass, "sensor.usage_total", 201.0)
    await tracker.async_process_completed_slot()

    day = tracker.store.data["days"]["2026-05-24"]
    assert day["flags"] == [FLAG_MISSING_EXPORT_PRICE]
    assert day["export_price"] == [None]


async def test_healthy_export_prices_are_cached_and_survive_a_later_priceless_plan(
    hass: HomeAssistant, freezer
) -> None:
    """A healthy export source feeds the cache and a later plan cannot erase it."""
    start = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    tracker = await _plan_with_export_source(hass, freezer, start, export_healthy=True)

    assert tracker.store.cached_price(start, "export") == pytest.approx(0.1)
    assert tracker.store.cached_price(start + timedelta(minutes=15), "export") == pytest.approx(0.11)

    tracker.remember_price_series(
        start_at=start,
        slot_minutes=15,
        import_prices=[2.0] * 4,
        export_prices=None,
    )

    assert tracker.store.cached_price(start, "import") == pytest.approx(2.0)
    assert tracker.store.cached_price(start, "export") == pytest.approx(0.1)


async def test_sensor_properties_follow_the_store(
    hass: HomeAssistant, freezer
) -> None:
    """available, native_value and attributes agree and pick up new slots."""
    store = await _loaded_store(hass, freezer, datetime(2026, 5, 25, 12, 0, 5, tzinfo=UTC))
    for hour in range(8, 12):
        store.append_slot(_record(datetime(2026, 5, 25, hour, tzinfo=UTC)))
    tracker = SimpleNamespace(
        hass=hass,
        summary=store.summary,
        scenario_enabled=lambda _: True,
        self_consumption_simulation_attributes=lambda: {},
    )
    description = next(
        d
        for d in HISTORICAL_SENSOR_DESCRIPTIONS
        if d.key == "historical_actual_cost_today"
    )
    sensor = HistoricalCostSensor(
        MockConfigEntry(domain=DOMAIN, title="Review"),
        tracker,
        description,
        entry_slug="review",
    )
    assert sensor.available is True
    assert sensor.native_value == pytest.approx(4.0)
    assert sensor.extra_state_attributes["slots"] == 4

    # New data invalidates the cached summary.
    store.append_slot(_record(datetime(2026, 5, 25, 12, tzinfo=UTC)))
    assert sensor.native_value == pytest.approx(5.0)


async def test_price_cache_only_keeps_unrecorded_slots(
    hass: HomeAssistant, freezer
) -> None:
    """Prices of recorded slots are dropped instead of kept for 60 days (PERF-12)."""
    now = datetime(2026, 5, 25, 12, 0, 5, tzinfo=UTC)
    store = await _loaded_store(hass, freezer, now, slot_minutes=60)
    store.update_metadata(last_processed_slot=datetime(2026, 5, 25, 11, tzinfo=UTC))

    # A plan that starts in the past only retains the slots still to be recorded.
    store.remember_price_series(
        start_at=datetime(2026, 5, 24, 11, tzinfo=UTC),
        slot_minutes=60,
        import_prices=[1.0] * 30,
        export_prices=[0.1] * 30,
    )
    cached = sorted(store.data["price_cache"])
    assert cached[0] == "2026-05-25T09:00:00Z"
    assert cached[-1] == "2026-05-25T16:00:00Z"

    # Moving the cursor forward trims what has been recorded since.
    store.update_metadata(last_processed_slot=datetime(2026, 5, 25, 14, tzinfo=UTC))
    assert min(store.data["price_cache"]) == "2026-05-25T12:00:00Z"


async def test_long_downtime_backfill_is_clamped_to_the_retained_window(
    hass: HomeAssistant, freezer
) -> None:
    """A long outage creates at most the retained window (PERF-13)."""
    start = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    await hass.config.async_set_time_zone("UTC")
    tracker = await _setup_tracker(hass, freezer, start)
    store = tracker.store
    store.update_metadata(
        last_processed_slot=start - timedelta(days=200),
        last_meter_values={
            "grid_import": 100.0,
            "grid_export": 0.0,
            "usage": 200.0,
            "pv": 0.0,
        },
        meter_config=tracker._meter_config(),
    )
    now = start + timedelta(minutes=2)
    freezer.move_to(now)
    await tracker.async_process_completed_slot()

    completed = start - timedelta(minutes=15)
    window_start = store.retention_start(now)
    assert store.has_slot(window_start)
    assert not store.has_slot(window_start - timedelta(minutes=15))
    assert store.has_slot(completed)
    assert len(store.data["days"]) == 60
    assert store.last_processed_slot() == completed


async def test_tick_in_flight_during_shutdown_does_not_rearm_timer(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unload must not leave a live timer behind a running tick (ROB-21)."""
    start = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    tracker = await _setup_tracker(hass, freezer, start)
    assert tracker._unsub_timer is not None

    release = asyncio.Event()
    events: list[str] = []

    async def _slow_refresh(now=None):
        events.append("tick started")
        await release.wait()
        events.append("tick finished")

    original_flush = tracker.store.async_flush

    async def _flush():
        events.append("flush")
        await original_flush()

    monkeypatch.setattr(tracker, "async_refresh", _slow_refresh)
    monkeypatch.setattr(tracker.store, "async_flush", _flush)

    async def _hold_lock_like_a_tick():
        async with tracker._process_lock:
            await tracker._async_timer(start + timedelta(minutes=15, seconds=5))

    tick = hass.async_create_task(_hold_lock_like_a_tick())
    await asyncio.sleep(0)
    shutdown = hass.async_create_task(tracker.async_shutdown())
    await asyncio.sleep(0)
    assert events == ["tick started"]  # shutdown waits for the process lock

    release.set()
    await tick
    await shutdown

    assert events == ["tick started", "tick finished", "flush"]
    assert tracker._unsub_timer is None

    # A late refresh after unload does nothing and cannot schedule a timer.
    monkeypatch.undo()
    await tracker.async_process_completed_slot(start + timedelta(hours=3))
    assert tracker._unsub_timer is None
    assert tracker.store.last_processed_slot() == start
