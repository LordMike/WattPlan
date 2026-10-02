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
from custom_components.wattplan.historical_cost import store as store_module
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


async def _setup_tracker(hass: HomeAssistant, freezer, start: datetime):
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


async def test_meter_reset_costs_only_the_reset_slot(
    hass: HomeAssistant, freezer
) -> None:
    """After a counter reset the new reading is the baseline (COR-35)."""
    start = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    tracker = await _setup_tracker(hass, freezer, start)

    await _tick(hass, freezer, tracker, start, 15, 101.0, 201.0)
    await _tick(hass, freezer, tracker, start, 30, 0.5, 0.5)  # counters reset
    assert tracker.store.last_meter_values()["grid_import"] == pytest.approx(0.5)
    await _tick(hass, freezer, tracker, start, 45, 1.5, 2.0)

    day = tracker.store.data["days"]["2026-05-24"]
    assert day["flags"] == [0, FLAG_METER_RESET, 0]
    assert day["grid_import"] == [pytest.approx(1.0), None, pytest.approx(1.0)]
    assert day["usage"] == [pytest.approx(1.0), None, pytest.approx(1.5)]


async def test_single_unavailable_reading_costs_one_slot(
    hass: HomeAssistant, freezer
) -> None:
    """The last finite baseline is kept and the spanning delta is flagged (COR-35)."""
    start = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    tracker = await _setup_tracker(hass, freezer, start)

    await _tick(hass, freezer, tracker, start, 15, "unavailable", "unavailable")
    assert tracker.store.last_meter_values()["grid_import"] == pytest.approx(100.0)
    await _tick(hass, freezer, tracker, start, 30, 102.0, 203.0)
    await _tick(hass, freezer, tracker, start, 45, 103.0, 204.0)

    day = tracker.store.data["days"]["2026-05-24"]
    assert day["flags"] == [FLAG_MISSING_METER, FLAG_MULTI_SLOT_DELTA, 0]
    # The slot after the outage carries the energy of both slots, flagged.
    assert day["grid_import"] == [None, pytest.approx(2.0), pytest.approx(1.0)]
    assert tracker.store.meter_stale_slots() == {}


async def test_longer_outage_is_not_booked_as_one_slot(
    hass: HomeAssistant, freezer
) -> None:
    """A delta that spans several slots cannot be attributed and stays missing."""
    start = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    tracker = await _setup_tracker(hass, freezer, start)

    await _tick(hass, freezer, tracker, start, 15, "unavailable", "unavailable")
    await _tick(hass, freezer, tracker, start, 30, "unavailable", "unavailable")
    await _tick(hass, freezer, tracker, start, 45, 104.0, 206.0)
    await _tick(hass, freezer, tracker, start, 60, 105.0, 207.0)

    day = tracker.store.data["days"]["2026-05-24"]
    assert day["flags"] == [FLAG_MISSING_METER] * 3 + [0]
    assert day["grid_import"] == [None, None, None, pytest.approx(1.0)]


async def _plan_with_export_source(
    hass: HomeAssistant, freezer, start: datetime, *, export_healthy: bool
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
            CONF_HISTORICAL_GRID_EXPORT_SENSOR: "sensor.grid_export_total",
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


async def test_healthy_export_source_prices_are_still_cached(
    hass: HomeAssistant, freezer
) -> None:
    """A healthy export source keeps feeding real prices to the cache."""
    start = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    tracker = await _plan_with_export_source(hass, freezer, start, export_healthy=True)

    assert tracker.store.cached_price(start, "export") == pytest.approx(0.1)
    assert tracker.store.cached_price(start + timedelta(minutes=15), "export") == pytest.approx(0.11)


async def test_failed_plan_keeps_earlier_real_export_prices(
    hass: HomeAssistant, freezer
) -> None:
    """A later plan without export prices must not erase earlier real ones."""
    start = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    tracker = await _plan_with_export_source(hass, freezer, start, export_healthy=True)

    tracker.remember_price_series(
        start_at=start,
        slot_minutes=15,
        import_prices=[2.0] * 4,
        export_prices=None,
    )

    assert tracker.store.cached_price(start, "import") == pytest.approx(2.0)
    assert tracker.store.cached_price(start, "export") == pytest.approx(0.1)


async def test_sensor_properties_share_one_computed_summary(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """available, native_value and attributes must not each rescan the store (PERF-11)."""
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
    calls = []
    original = store._records_between
    monkeypatch.setattr(
        store,
        "_records_between",
        lambda *args: calls.append(args) or original(*args),
    )

    assert sensor.available is True
    assert sensor.native_value == pytest.approx(4.0)
    assert sensor.extra_state_attributes["slots"] == 4
    assert len(calls) == 1

    # Another metric for the same period reuses the parsed records.
    store.summary(
        metric=HistoricalMetric.SAVINGS_VS_GRID_ONLY,
        period=PERIOD_TODAY,
        scenario=None,
    )
    assert len(calls) == 1

    # New data invalidates the cache.
    store.append_slot(_record(datetime(2026, 5, 25, 12, tzinfo=UTC)))
    assert sensor.native_value == pytest.approx(5.0)
    assert len(calls) == 2


async def test_summary_only_parses_days_overlapping_the_period(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retained days outside the requested period are not visited (PERF-11)."""
    store = await _loaded_store(hass, freezer, datetime(2026, 5, 25, 12, 0, 5, tzinfo=UTC))
    for day in range(1, 26):
        store.append_slot(_record(datetime(2026, 5, day, 10, tzinfo=UTC)))
    parsed: list[object] = []
    original = store_module._parse_utc
    monkeypatch.setattr(
        store_module, "_parse_utc", lambda raw: parsed.append(raw) or original(raw)
    )

    today = _cost(store, PERIOD_TODAY, datetime(2026, 5, 25, 12, tzinfo=UTC))

    assert today.value == pytest.approx(1.0)
    assert len(parsed) == 1
    parsed.clear()

    month = _cost(store, PERIOD_THIS_MONTH, datetime(2026, 5, 25, 12, tzinfo=UTC))

    assert month.value == pytest.approx(25.0)
    assert len(parsed) == 25


async def test_mark_dirty_does_not_prune_and_unchanged_prices_do_not_save(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pruning runs per processed slot and an unchanged price plan is a no-op (PERF-12)."""
    now = datetime(2026, 5, 25, 12, 0, 5, tzinfo=UTC)
    store = await _loaded_store(hass, freezer, now, slot_minutes=15)
    prunes = []
    dirty = []
    original_prune = store.prune
    original_dirty = store.mark_dirty
    monkeypatch.setattr(store, "prune", lambda n: prunes.append(n) or original_prune(n))
    monkeypatch.setattr(store, "mark_dirty", lambda: dirty.append(1) or original_dirty())
    slot = datetime(2026, 5, 25, 12, tzinfo=UTC)

    for _ in range(5):
        store.mark_dirty()
    assert prunes == []

    assert store.remember_price_series(
        start_at=slot, slot_minutes=15, import_prices=[1.0, 1.1], export_prices=[0.1, 0.1]
    )
    assert len(dirty) == 6
    assert not store.remember_price_series(
        start_at=slot, slot_minutes=15, import_prices=[1.0, 1.1], export_prices=[0.1, 0.1]
    )
    assert len(dirty) == 6
    assert store.remember_price_series(
        start_at=slot, slot_minutes=15, import_prices=[1.0, 1.2], export_prices=[0.1, 0.1]
    )
    assert len(dirty) == 7

    store.update_metadata(last_processed_slot=slot)
    assert len(prunes) == 1


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


async def test_long_downtime_backfill_is_clamped_and_written_once(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long outage creates at most the retained window in one batch (PERF-13)."""
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
    prunes = []
    appends = []
    original_prune = store.prune
    original_append = store.append_slots

    def _append(records):
        records = list(records)
        appends.append(records)
        original_append(records)

    monkeypatch.setattr(store, "prune", lambda n: prunes.append(n) or original_prune(n))
    monkeypatch.setattr(store, "append_slots", _append)

    now = start + timedelta(minutes=2)
    freezer.move_to(now)
    await tracker.async_process_completed_slot()

    assert len(appends) == 1
    assert len(prunes) == 1
    completed = start - timedelta(minutes=15)
    window_start = store.retention_start(now)
    expected = int((completed - window_start) / timedelta(minutes=15)) + 1
    assert len(appends[0]) == expected
    assert appends[0][0].start == window_start
    assert appends[0][-1].start == completed
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
