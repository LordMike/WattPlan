"""Regression tests for historical cost tracking review fixes."""

from datetime import UTC, datetime, timedelta

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
from custom_components.wattplan.historical_cost.store import HistoricalCostStore

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
