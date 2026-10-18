"""Recording actuals is independent of planner success and preserves quality."""

from datetime import UTC, datetime, timedelta
import json

import pytest

from custom_components.wattplan.historical_cost.actuals import export_actual
from custom_components.wattplan.historical_cost.models import SlotRecord
from custom_components.wattplan.historical_cost.store import HistoricalCostStore
from tests.integration.test_historical_cost_tracking_fixes import (
    _plan_with_export_source,
    _setup_tracker,
    _tick,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def test_completed_actuals_record_without_planning_and_preserve_missing_pv(
    hass, freezer
):
    start = datetime(2026, 5, 24, 10, tzinfo=UTC)
    tracker = await _setup_tracker(hass, freezer, start, record_plans=True)
    history = tracker.entry.runtime_data.coordinator.planner_history
    # The initial setup plan predates these meter intervals. No more plans run.
    await _tick(hass, freezer, tracker, start, 15, 101.0, 201.0)
    await _tick(hass, freezer, tracker, start, 30, 102.0, 202.0)
    response = await history.async_read_date(start.date())
    records = [json.loads(line) for line in response["data"].splitlines()]
    updates = [record for record in records if record.get("record_type") == "actuals"]
    assert len(updates) == 2
    plans = [record for record in records if record.get("record_type") == "plan"]
    assert plans
    assert all(record["actuals_status"] == "tracking_enabled" for record in plans)
    assert response["actuals_status"] == "tracking_enabled"
    first, second = [record["actuals"][0] for record in updates]
    assert "partial_interval" in first["quality"]
    assert "partial_interval" not in second["quality"]
    assert second["usage_kwh"] == 1
    assert second["pv_kwh"] is None  # tracker convenience zero is not a measured zero
    assert "missing_pv_kwh" in second["quality"]
    assert second["source_roles"]["usage"] == "sensor.usage_total"
    retained = tracker.store.export_actuals(start, start + timedelta(minutes=30))
    assert retained == [first, second]


def test_legacy_zero_export_without_meter_has_unknown_counterfactual_price():
    record = SlotRecord(
        start=datetime(2026, 9, 22, tzinfo=UTC),
        import_price=1,
        export_price=0,
        grid_import=1,
        grid_export=0,
        usage=1,
        pv=0,
    )
    exported = export_actual(record, 15, {"usage": "sensor.usage", "pv": "sensor.pv"})
    assert exported["export_price"] is None
    assert "missing_export_price" in exported["quality"]
    assert "legacy_provenance" in exported["quality"]


async def test_configured_export_tariff_survives_absent_export_meter(
    hass, freezer
):
    start = datetime(2026, 5, 24, 10, tzinfo=UTC)
    tracker = await _plan_with_export_source(
        hass, freezer, start, export_healthy=True, export_meter=False
    )
    tracker.store.update_metadata(
        last_processed_slot=start - timedelta(minutes=15),
        last_meter_values={"grid_import": 100.0, "usage": 200.0, "pv": 0.0},
        meter_config=tracker._meter_config(),
    )
    await _tick(hass, freezer, tracker, start, 15, 101.0, 201.0)
    [actual] = tracker.store.export_actuals(start, start + timedelta(minutes=15))
    assert actual["export_price"] == pytest.approx(0.1)


async def test_actuals_retain_interval_duration_and_currency_after_configuration_change(
    hass, freezer
):
    start = datetime(2026, 9, 22, tzinfo=UTC)
    freezer.move_to(start)
    store = HistoricalCostStore(
        hass, entry_id="interval-metadata", slot_minutes=15, currency="DKK"
    )
    await store.async_load()
    for offset, minutes, currency in ((0, 15, "DKK"), (15, 30, "EUR")):
        store.append_slot(
            SlotRecord(
                start=start + timedelta(minutes=offset),
                import_price=1,
                export_price=0,
                grid_import=1,
                grid_export=0,
                usage=1,
                pv=0,
                actuals_metadata={
                    "slot_minutes": minutes,
                    "currency": currency,
                    "source_roles": {"usage": "sensor.load", "pv": "sensor.pv"},
                },
            )
        )
    store.slot_minutes = 60
    result = store.export_actuals(start, start + timedelta(hours=1))
    assert result[0]["end"] == (start + timedelta(minutes=15)).isoformat()
    assert result[1]["end"] == (start + timedelta(minutes=45)).isoformat()
    assert [record["currency"] for record in result] == ["DKK", "EUR"]
