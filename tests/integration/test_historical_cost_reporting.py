"""Historical cost reporting regressions using real stores and sensor classes."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wattplan.historical_cost.models import (
    FLAG_GAP,
    FLAG_METER_RESET,
    FLAG_MISSING_EXPORT_PRICE,
    FLAG_MISSING_IMPORT_PRICE,
    FLAG_MISSING_METER,
    FLAG_SELF_CONSUMPTION_UNAVAILABLE,
    SlotRecord,
)
from custom_components.wattplan.historical_cost.store import HistoricalCostStore
from custom_components.wattplan.sensors.historical import (
    HISTORICAL_SENSOR_DESCRIPTIONS,
    HistoricalCostSensor,
)


def sensor_for(hass, store, key):
    """Use real sensor accessors and aggregation; replace only tracker plumbing."""
    tracker = SimpleNamespace(
        hass=hass,
        summary=store.summary,
        scenario_enabled=lambda _: True,
        self_consumption_simulation_attributes=lambda: {},
    )
    entry = MockConfigEntry(domain="wattplan", title="Review")
    description = next(d for d in HISTORICAL_SENSOR_DESCRIPTIONS if d.key == key)
    return HistoricalCostSensor(entry, tracker, description, entry_slug="review")


BASE_RECORD = SlotRecord(
    start=datetime(2026, 9, 11, 10, tzinfo=UTC),
    import_price=1,
    export_price=.5,
    grid_import=1,
    grid_export=.2,
    usage=2,
    pv=1,
    self_consumption_grid_import=.5,
    self_consumption_grid_export=.1,
    self_consumption_segment_id="segment-1",
)
EXPECTED_SLOT_VALUES = {
    "historical_actual_cost_today": .9,
    "historical_grid_only_cost_today": 2,
    "historical_self_consumption_cost_today": .45,
    "historical_savings_vs_grid_only_today": 1.1,
    "historical_savings_vs_self_consumption_today": -.45,
}


@pytest.mark.parametrize(
    ("case", "changes", "flags", "valid_keys"),
    [
        (
            "missing PV",
            {"pv": None},
            FLAG_MISSING_METER,
            {
                "historical_actual_cost_today",
                "historical_grid_only_cost_today",
                "historical_savings_vs_grid_only_today",
            },
        ),
        (
            "reset PV",
            {"pv": None},
            FLAG_METER_RESET,
            {
                "historical_actual_cost_today",
                "historical_grid_only_cost_today",
                "historical_savings_vs_grid_only_today",
            },
        ),
        (
            "missing usage",
            {"usage": None},
            FLAG_MISSING_METER,
            {"historical_actual_cost_today"},
        ),
        (
            "reset usage",
            {"usage": None},
            FLAG_METER_RESET,
            {"historical_actual_cost_today"},
        ),
        (
            "missing grid import",
            {"grid_import": None},
            FLAG_MISSING_METER,
            {
                "historical_grid_only_cost_today",
                "historical_self_consumption_cost_today",
            },
        ),
        (
            "reset grid import",
            {"grid_import": None},
            FLAG_METER_RESET,
            {
                "historical_grid_only_cost_today",
                "historical_self_consumption_cost_today",
            },
        ),
        (
            "missing grid export",
            {"grid_export": None},
            FLAG_MISSING_METER,
            {
                "historical_grid_only_cost_today",
                "historical_self_consumption_cost_today",
            },
        ),
        (
            "reset grid export",
            {"grid_export": None},
            FLAG_METER_RESET,
            {
                "historical_grid_only_cost_today",
                "historical_self_consumption_cost_today",
            },
        ),
        (
            "missing export price",
            {"export_price": None},
            FLAG_MISSING_EXPORT_PRICE,
            {"historical_grid_only_cost_today"},
        ),
        (
            "missing simulation state",
            {"self_consumption_segment_id": None},
            FLAG_SELF_CONSUMPTION_UNAVAILABLE,
            {
                "historical_actual_cost_today",
                "historical_grid_only_cost_today",
                "historical_savings_vs_grid_only_today",
            },
        ),
        (
            "missing import price",
            {"import_price": None},
            FLAG_MISSING_IMPORT_PRICE,
            set(),
        ),
        ("explicit gap", {}, FLAG_GAP, set()),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
async def test_scenario_dependencies_control_metric_coverage(
    hass, freezer, case, changes, flags, valid_keys
):
    """Each cost and savings metric should reject only its required missing inputs."""
    now = datetime(2026, 9, 11, 12, tzinfo=UTC)
    freezer.move_to(now)
    store = HistoricalCostStore(hass, entry_id="review", slot_minutes=60, currency="DKK")
    store.append_slot(BASE_RECORD)
    store.append_slot(
        replace(
            BASE_RECORD,
            start=BASE_RECORD.start + timedelta(hours=1),
            flags=flags,
            **changes,
        )
    )

    for key, slot_value in EXPECTED_SLOT_VALUES.items():
        sensor = sensor_for(hass, store, key)
        second_slot_valid = key in valid_keys
        assert sensor.available, case
        assert sensor.native_value == pytest.approx(
            slot_value * (2 if second_slot_valid else 1)
        ), case
        assert sensor.extra_state_attributes["missing_slots"] == (
            0 if second_slot_valid else 1
        ), case


async def test_shared_meter_flags_do_not_override_complete_legacy_fields(
    hass, freezer
):
    """Legacy shared flags remain diagnostic when all scenario inputs are retained."""
    now = datetime(2026, 9, 11, 12, tzinfo=UTC)
    freezer.move_to(now)
    store = HistoricalCostStore(
        hass, entry_id="legacy-flags", slot_minutes=60, currency="DKK"
    )
    store.append_slot(
        replace(BASE_RECORD, flags=FLAG_MISSING_METER | FLAG_METER_RESET)
    )

    for key, slot_value in EXPECTED_SLOT_VALUES.items():
        sensor = sensor_for(hass, store, key)
        assert sensor.native_value == pytest.approx(slot_value)
        assert sensor.extra_state_attributes["missing_slots"] == 0


async def test_signed_costs_keep_scenario_specific_coverage(hass, freezer):
    """Finite signed tariffs and costs remain valid across every scenario."""
    now = datetime(2026, 9, 11, 12, tzinfo=UTC)
    freezer.move_to(now)
    store = HistoricalCostStore(hass, entry_id="signed", slot_minutes=60, currency="DKK")
    store.append_slot(replace(BASE_RECORD, import_price=-1, export_price=.5))

    expected = {
        "historical_actual_cost_today": -1.1,
        "historical_grid_only_cost_today": -2,
        "historical_self_consumption_cost_today": -.55,
        "historical_savings_vs_grid_only_today": -.9,
        "historical_savings_vs_self_consumption_today": .55,
    }
    for key, value in expected.items():
        sensor = sensor_for(hass, store, key)
        assert sensor.native_value == pytest.approx(value)
        assert sensor.extra_state_attributes["missing_slots"] == 0
