"""Tests for WattPlan testbed helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "testbed/custom_components"))
sys.path.insert(0, str(REPO_ROOT))

from scripts import testbed_backfill
from wattplan_testbed.config_flow import normalize_entry_data
from wattplan_testbed.generators import (
    GenerationContext,
    cumulative_load_states,
    load_power_points,
    points_to_wh_hours,
    power_points_to_wh_hours,
    pv_power_points,
    price_points,
    pv_points,
)
from wattplan_testbed.runtime import TestbedRuntime as WattPlanTestbedRuntime


def test_generators_are_deterministic() -> None:
    """Same config and context should produce the same values."""
    ctx = GenerationContext(
        start_at=datetime(2026, 1, 1, tzinfo=UTC),
        slot_minutes=15,
        slots=8,
        seed=42,
    )
    config = {
        "base_offset": 0.25,
        "factor": 1.2,
        "noise": 0.02,
        "phase_hours": 1,
    }

    assert price_points(config, ctx) == price_points(config, ctx)
    assert load_power_points(config, ctx) == load_power_points(config, ctx)


def test_root_entry_data_migrates_old_slot_fields() -> None:
    """Old root slot/horizon fields should map to the update interval."""
    assert normalize_entry_data(
        {"name": "Legacy", "slot_minutes": 30, "horizon_hours": 72}
    ) == {"name": "Legacy", "update_interval_minutes": 30}


def test_price_subentry_generates_one_price_series() -> None:
    """Import/export prices are separate subentries with one price sensor each."""
    ctx = GenerationContext(
        start_at=datetime(2026, 1, 1, tzinfo=UTC),
        slot_minutes=15,
        slots=4,
        seed=42,
    )
    import_points = price_points({"base_offset": 0.35, "noise": 0.0}, ctx)
    export_points = price_points({"base_offset": 0.10, "noise": 0.0}, ctx)

    assert len(import_points) == len(export_points) == 4
    assert import_points[0]["value"] > export_points[0]["value"]


def test_pv_points_convert_to_energy_provider_wh_hours() -> None:
    """PV generator output should map cleanly to HA Energy forecast shape."""
    ctx = GenerationContext(
        start_at=datetime(2026, 6, 1, 10, tzinfo=UTC),
        slot_minutes=60,
        slots=3,
        seed=5,
    )
    points = pv_points(
        {"peak_kwh": 2.0, "cloud_factor": 0.0, "factor": 1.0, "phase_hours": 0},
        ctx,
    )
    wh_hours = points_to_wh_hours(points)
    power_wh_hours = power_points_to_wh_hours(
        pv_power_points(
            {"peak_kwh": 2.0, "cloud_factor": 0.0, "factor": 1.0, "phase_hours": 0},
            ctx,
        ),
        slot_minutes=ctx.slot_minutes,
    )

    assert list(wh_hours) == [point["start"] for point in points]
    assert all(value >= 0 for value in wh_hours.values())
    assert any(value > 0 for value in wh_hours.values())
    assert power_wh_hours == pytest.approx(wh_hours)


def test_pv_power_points_follow_daylight_curve() -> None:
    """PV power should be off at night and strongest around midday."""
    ctx = GenerationContext(
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
        slot_minutes=60,
        slots=24,
        seed=5,
    )
    points = pv_power_points(
        {"peak_kwh": 2.0, "cloud_factor": 0.0, "factor": 1.0, "phase_hours": 0},
        ctx,
    )
    values = [float(point["value"]) for point in points]

    assert values[0] == 0.0
    assert values[5] == 0.0
    assert values[12] > values[9] > values[7] > 0.0
    assert values[19] == 0.0


def test_cumulative_load_states_are_monotonic() -> None:
    """Backfilled load energy should be cumulative for built-in usage tests."""
    states = cumulative_load_states(
        {
            "profile": "heavy",
            "factor": 1.0,
            "noise": 0.0,
            "phase_hours": 0.0,
            "initial_total_kwh": 100.0,
        },
        start_at=datetime(2026, 1, 1, tzinfo=UTC),
        end_at=datetime(2026, 1, 1, 4, tzinfo=UTC),
        slot_minutes=60,
        seed=1,
    )

    values = [value for _at, value in states]
    timestamps = [at for at, _value in states]
    assert values == sorted(values)
    assert len(timestamps) == len(set(timestamps))
    assert values[0] == 100.0
    assert values[-1] > values[0]


def test_live_load_energy_accumulates_slot_usage(monkeypatch) -> None:
    """Live load energy should advance from the previous total."""

    class FrozenDateTime(datetime):
        current = datetime(2026, 1, 1, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is None:
                return cls.current.replace(tzinfo=None)
            return cls.current.astimezone(tz)

    import wattplan_testbed.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "datetime", FrozenDateTime)
    subentry = SimpleNamespace(
        subentry_id="load1",
        subentry_type="load",
        title="Load: Heavy Load",
        data={
            "name": "Heavy Load",
            "profile": "heavy",
            "factor": 1.0,
            "noise": 0.04,
            "phase_hours": 0.0,
            "initial_total_kwh": 100.0,
            "seed": 22,
        },
    )
    entry = SimpleNamespace(
        entry_id="entry1",
        title="WattPlan Testbed",
        data={"update_interval_minutes": 60},
        subentries={"load1": subentry},
    )
    runtime = WattPlanTestbedRuntime(SimpleNamespace(), entry)

    runtime.initialize_load_energy(subentry)
    first = runtime.load_energy_state(subentry)
    FrozenDateTime.current = datetime(2026, 1, 1, 1, 30, tzinfo=UTC)
    second = runtime.load_energy_state(subentry)
    FrozenDateTime.current = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
    third = runtime.load_energy_state(subentry)

    assert first == 100.0
    assert third > second > first


def test_backfill_entity_ids_include_all_core_assets() -> None:
    """Entry-wide backfill should discover every core testbed entity."""
    entry = {
        "title": "WattPlan Testbed",
        "entry_id": "entry1",
        "data": {"update_interval_minutes": 15},
        "subentries": [
            {
                "subentry_id": "price1",
                "subentry_type": "price",
                "title": "Price: Demo Import Price",
                "data": {"name": "Demo Import Price"},
            },
            {
                "subentry_id": "price2",
                "subentry_type": "price",
                "title": "Price: Demo Export Price",
                "data": {"name": "Demo Export Price"},
            },
            {
                "subentry_id": "pv1",
                "subentry_type": "pv",
                "title": "PV: Demo PV",
                "data": {"name": "Demo PV"},
            },
            {
                "subentry_id": "load1",
                "subentry_type": "load",
                "title": "Load: Heavy Load",
                "data": {"name": "Heavy Load"},
            },
            {
                "subentry_id": "battery1",
                "subentry_type": "battery",
                "title": "Battery: Home Battery",
                "data": {"name": "Home Battery"},
            },
        ],
    }

    entities = testbed_backfill._entity_ids(entry)

    assert set(entities) == {
        "sensor.wattplan_testbed_demo_import_price_price",
        "sensor.wattplan_testbed_demo_export_price_price",
        "sensor.wattplan_testbed_demo_pv_pv_power",
        "sensor.wattplan_testbed_heavy_load_load_power",
        "sensor.wattplan_testbed_heavy_load_load_energy",
        "sensor.wattplan_testbed_home_battery_soc",
        "binary_sensor.wattplan_testbed_home_battery_available",
    }


def test_backfill_attrs_use_price_currency_and_power_units() -> None:
    """Backfill should write recorder metadata matching the corrected entity model."""
    assert testbed_backfill._state_attrs("price", currency="EUR") == {
        "state_class": "measurement",
        "unit_of_measurement": "EUR/kWh",
    }
    assert testbed_backfill._state_attrs("load_power", currency="EUR") == {
        "device_class": "power",
        "state_class": "measurement",
        "unit_of_measurement": "W",
    }
