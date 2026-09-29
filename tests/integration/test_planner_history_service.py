"""End-to-end planner recording and daily export service."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest

from custom_components.wattplan.const import (
    CONF_ACTION_EMISSION_ENABLED,
    CONF_HOURS_TO_PLAN,
    CONF_PLANNER_REPRODUCTION_RETENTION_DAYS,
    CONF_PLANNING_ENABLED,
    CONF_RECORD_PLANNER_REPRODUCTIONS,
    CONF_SLOT_MINUTES,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_MODE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
    CONF_SOURCES,
    CONF_TEMPLATE,
    DOMAIN,
    SERVICE_EXPORT_PLANNER_REPRODUCTIONS,
    SERVICE_RUN_OPTIMIZE_NOW,
    SOURCE_MODE_NOT_USED,
    SOURCE_MODE_TEMPLATE,
)
from tests.common import MockConfigEntry

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def test_scheduled_and_manual_plans_are_exported_by_local_date(
    hass: HomeAssistant,
) -> None:
    """The response service exposes both initial and manual successful plans."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            "name": "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.2, 0.25, 0.3, 0.35] }}",
                },
                CONF_SOURCE_USAGE: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
                CONF_SOURCE_PV: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
            CONF_RECORD_PLANNER_REPRODUCTIONS: True,
            CONF_PLANNER_REPRODUCTION_RETENTION_DAYS: 14,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.home_planner_reproduction") is None

    await hass.services.async_call(
        DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True
    )
    local_date = datetime.now(tz=UTC).astimezone(ZoneInfo(hass.config.time_zone)).date()
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_EXPORT_PLANNER_REPRODUCTIONS,
        {"date": local_date.isoformat()},
        blocking=True,
        return_response=True,
    )
    assert response["date"] == local_date.isoformat()
    assert response["min_available_date"] == local_date.isoformat()
    assert response["max_available_date"] == local_date.isoformat()
    records = [json.loads(line) for line in response["data"].splitlines()]
    assert len(records) >= 2
    assert all(record["schema_version"] == 1 for record in records)
    assert all("result" in record and "optimizer_params" in record["request"] for record in records)

    for selector in (
        {"entry_id": entry.entry_id},
        {"name": "home"},
        {"entity_id": "sensor.home_last_run"},
        {"entity_id": "sensor.home_last_run", "entry_id": entry.entry_id},
    ):
        selected = await hass.services.async_call(
            DOMAIN,
            SERVICE_EXPORT_PLANNER_REPRODUCTIONS,
            {"date": local_date.isoformat(), **selector},
            blocking=True,
            return_response=True,
        )
        assert selected["data"] == response["data"]

    with pytest.raises(ServiceValidationError, match="different setups"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_EXPORT_PLANNER_REPRODUCTIONS,
            {
                "date": local_date.isoformat(),
                "entity_id": "sensor.home_last_run",
                "entry_id": "a-different-entry",
            },
            blocking=True,
            return_response=True,
        )
    with pytest.raises(ServiceValidationError, match="WattPlan setup"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_EXPORT_PLANNER_REPRODUCTIONS,
            {"date": local_date.isoformat(), "entity_id": "sensor.not_wattplan"},
            blocking=True,
            return_response=True,
        )

    empty = await hass.services.async_call(
        DOMAIN,
        SERVICE_EXPORT_PLANNER_REPRODUCTIONS,
        {"date": "2000-01-01"},
        blocking=True,
        return_response=True,
    )
    assert empty["data"] == ""
    assert empty["max_available_date"] == local_date.isoformat()

    with pytest.raises(ServiceValidationError, match="YYYY-MM-DD"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_EXPORT_PLANNER_REPRODUCTIONS,
            {"date": "2026-09-99"},
            blocking=True,
            return_response=True,
        )
