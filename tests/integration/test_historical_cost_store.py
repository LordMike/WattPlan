"""Persisted-state tests for the historical cost store."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from custom_components.wattplan.historical_cost.models import (
    FLAG_MISSING_IMPORT_PRICE,
    FLAG_MISSING_METER,
    HistoricalMetric,
    SCENARIO_ACTUAL,
    SlotRecord,
)
from custom_components.wattplan.historical_cost.store import HistoricalCostStore
import pytest

from homeassistant.core import HomeAssistant


async def test_historical_store_sanitizes_nonfinite_persisted_values(
    hass: HomeAssistant,
) -> None:
    """Load must preserve records but make corrupt numerics explicitly missing."""
    store = HistoricalCostStore(
        hass, entry_id="corrupt-entry", slot_minutes=60, currency="DKK"
    )
    starts = [f"2026-09-11T{hour:02d}:00:00Z" for hour in range(12, 16)]
    payload = {
        "version": 1,
        "slot_minutes": 60,
        "currency": "DKK",
        "tracking_started_at": starts[0],
        "last_processed_slot": starts[-1],
        "last_meter_values": {"grid_import": float("nan"), "usage": 4.0},
        "meter_config": {},
        "price_cache": {},
        "simulation_state": {
            "self_consumption": {
                "batteries": {
                    "bad": {"soc_kwh": float("inf")},
                    "good": {"soc_kwh": 1.0},
                }
            }
        },
        "days": {
            "2026-09-11": {
                "starts": starts,
                "import_price": [-1.0, float("nan"), 1e308, 1e308],
                "export_price": [0.0, 0.0, 0.0, 0.0],
                "grid_import": [1.0, 1.0, 1e308, 1e308],
                "grid_export": [0.0, 0.0, 0.0, 0.0],
                "usage": [1.0, 1.0, 1.0, 1.0],
                "pv": [0.0, 0.0, 0.0, 0.0],
                "self_consumption_grid_import": [1.0, 1.0, 1.0, 1.0],
                "self_consumption_grid_export": [0.0, 0.0, 0.0, 0.0],
                "flags": [0, 0, 0, 0],
            }
        },
    }
    with patch.object(store._store, "async_load", return_value=payload):
        await store.async_load()

    day = store.data["days"]["2026-09-11"]
    assert day["import_price"] == [-1.0, None, 1e308, 1e308]
    assert day["flags"] == [0, FLAG_MISSING_IMPORT_PRICE, 0, 0]
    assert store.last_meter_values() == {"grid_import": None, "usage": 4.0}
    assert store.simulation_soc() == {}
    assert store.simulation_continuity()["valid"] is False
    assert store.simulation_continuity()["untrusted_reason"] == (
        "legacy_state_unverifiable"
    )
    summary = store.summary(
        metric=HistoricalMetric.COST,
        period="today",
        scenario=SCENARIO_ACTUAL,
        now=datetime(2026, 9, 11, 18, 0, tzinfo=UTC),
    )
    assert summary.value == pytest.approx(-1.0)
    assert summary.slots == 4
    assert summary.missing_slots == 3

    store.append_slot(
        SlotRecord(
            start=datetime(2026, 9, 11, 16, 0, tzinfo=UTC),
            import_price=float("-inf"),
            export_price=0.0,
            grid_import=float("nan"),
            grid_export=0.0,
            usage=1.0,
            pv=0.0,
        )
    )
    assert day["import_price"][-1] is None
    assert day["grid_import"][-1] is None
    assert day["flags"][-1] == FLAG_MISSING_IMPORT_PRICE | FLAG_MISSING_METER


async def test_historical_store_persists_valid_reference_segment(
    hass: HomeAssistant,
) -> None:
    """A valid reference segment and its provenance survive a store restart."""
    started_at = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    first = HistoricalCostStore(
        hass, entry_id="continuity-entry", slot_minutes=60, currency="DKK"
    )
    await first.async_load()
    configuration = [{"subentry_id": "battery", "capacity_kwh": 1.0}]
    assert first.start_simulation_segment(
        {"battery": 0.75},
        started_at=started_at,
        reason="tracking_initialized",
        configuration=configuration,
    )
    payload = first.data

    restored = HistoricalCostStore(
        hass, entry_id="continuity-entry", slot_minutes=60, currency="DKK"
    )
    with patch.object(restored._store, "async_load", return_value=payload):
        await restored.async_load()

    assert restored.simulation_soc() == {"battery": pytest.approx(0.75)}
    assert restored.simulation_continuity() == {
        "valid": True,
        "segment_id": "segment-1",
        "segment_started_at": "2026-09-11T12:00:00Z",
        "segment_reason": "tracking_initialized",
        "untrusted_since": None,
        "untrusted_reason": None,
        "configuration": configuration,
    }


async def test_historical_store_rejects_corrupt_valid_reference_state(
    hass: HomeAssistant,
) -> None:
    """A persisted valid marker cannot make nonfinite simulation state trusted."""
    store = HistoricalCostStore(
        hass, entry_id="bad-continuity", slot_minutes=60, currency="DKK"
    )
    payload = {
        "version": 1,
        "slot_minutes": 60,
        "currency": "DKK",
        "tracking_started_at": "2026-09-11T10:00:00Z",
        "last_processed_slot": "2026-09-11T11:00:00Z",
        "last_meter_values": {},
        "meter_config": {},
        "price_cache": {},
        "days": {},
        "simulation_state": {
            "self_consumption": {
                "batteries": {"battery": {"soc_kwh": float("nan")}},
                "valid": True,
                "segment_counter": 1,
                "segment_id": "segment-1",
                "configuration": [{"subentry_id": "battery"}],
            }
        },
    }
    with patch.object(store._store, "async_load", return_value=payload):
        await store.async_load()

    assert store.simulation_soc() == {}
    assert store.simulation_continuity()["valid"] is False
    assert store.simulation_continuity()["untrusted_reason"] == (
        "invalid_persisted_state"
    )


async def test_historical_store_invalidates_legacy_reference_after_energy_gap(
    hass: HomeAssistant,
) -> None:
    """Upgrade must not preserve already-invented post-gap reference losses."""
    store = HistoricalCostStore(
        hass, entry_id="legacy-gap", slot_minutes=60, currency="DKK"
    )
    payload = {
        "version": 1,
        "slot_minutes": 60,
        "currency": "DKK",
        "tracking_started_at": "2026-09-11T10:00:00Z",
        "last_processed_slot": "2026-09-11T12:00:00Z",
        "last_meter_values": {},
        "meter_config": {},
        "price_cache": {},
        "simulation_state": {
            "self_consumption": {"batteries": {"battery": {"soc_kwh": 1.0}}}
        },
        "days": {
            "2026-09-11": {
                "starts": [
                    "2026-09-11T10:00:00Z",
                    "2026-09-11T11:00:00Z",
                    "2026-09-11T12:00:00Z",
                ],
                "import_price": [1.0, 1.0, 1.0],
                "export_price": [0.0, 0.0, 0.0],
                "grid_import": [0.0, None, 1.0],
                "grid_export": [0.0, None, 0.0],
                "usage": [1.0, None, 1.0],
                "pv": [0.0, None, 0.0],
                "self_consumption_grid_import": [0.0, None, 0.0],
                "self_consumption_grid_export": [0.0, None, 0.0],
                "flags": [0, FLAG_MISSING_METER, 0],
            }
        },
    }
    with patch.object(store._store, "async_load", return_value=payload):
        await store.async_load()

    day = store.data["days"]["2026-09-11"]
    assert day["self_consumption_grid_import"] == [0.0, None, None]
    assert day["self_consumption_segment_id"] == ["legacy-segment-1", None, None]
    summary = store.summary(
        metric=HistoricalMetric.SAVINGS_VS_SELF_CONSUMPTION,
        period="today",
        scenario=None,
        now=datetime(2026, 9, 11, 18, tzinfo=UTC),
    )
    assert summary.value == pytest.approx(0.0)
    assert summary.missing_slots == 2
    assert summary.reference_segment_ids == ("legacy-segment-1",)
