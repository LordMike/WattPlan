"""Test source modifier persistence in WattPlan flows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from custom_components.wattplan.const import (
    ADAPTER_TYPE_ATTRIBUTE_VALUES,
    AGGREGATION_MODE_MAX,
    AGGREGATION_MODE_MIN,
    CLAMP_MODE_NEAREST,
    CLAMP_MODE_NONE,
    CONF_ADAPTER_TYPE,
    CONF_AGGREGATION_MODE,
    CONF_CLAMP_MODE,
    CONF_CONFIG_ENTRY_ID,
    CONF_EDGE_FILL_MODE,
    CONF_FIXUP_PROFILE,
    CONF_HISTORY_DAYS,
    CONF_HOURS_TO_PLAN,
    CONF_RESAMPLE_MODE,
    CONF_SLOT_MINUTES,
    CONF_SOURCE_MODE,
    CONF_SOURCE_PRICE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
    CONF_SOURCES,
    CONF_TEMPLATE,
    DOMAIN,
    EDGE_FILL_MODE_HOLD,
    EDGE_FILL_MODE_NONE,
    FIXUP_PROFILE_EXTEND,
    FIXUP_PROFILE_REPAIR,
    RESAMPLE_MODE_FORWARD_FILL,
    RESAMPLE_MODE_NONE,
    SOURCE_MODE_BUILT_IN,
    SOURCE_MODE_ENERGY_PROVIDER,
    SOURCE_MODE_ENTITY_ADAPTER,
    SOURCE_MODE_NOT_USED,
    SOURCE_MODE_TEMPLATE,
)
import pytest

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from tests.common import MockConfigEntry

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _numeric_template(count: int) -> str:
    """Return a template string that renders a native numeric list."""
    values = [float(index) for index in range(count)]
    return f"{{{{ {values!r} }}}}"


async def _create_entry_with_price_template(hass: HomeAssistant) -> config_entries.ConfigEntry:
    """Create one config entry with price template and optional sources disabled."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "requirements"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "planner_setup"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Flow test",
            CONF_SLOT_MINUTES: "60",
            CONF_HOURS_TO_PLAN: "12",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_price"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_price_template"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_TEMPLATE: _numeric_template(12),
            CONF_FIXUP_PROFILE: FIXUP_PROFILE_REPAIR,
            "advanced": {
                CONF_AGGREGATION_MODE: AGGREGATION_MODE_MIN,
                CONF_CLAMP_MODE: CLAMP_MODE_NEAREST,
                CONF_RESAMPLE_MODE: RESAMPLE_MODE_FORWARD_FILL,
                CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_HOLD,
            },
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_review"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"accept_source_summary": True}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_usage"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_pv"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    return hass.config_entries.async_entries(DOMAIN)[0]


async def test_config_flow_persists_price_template_modifiers(
    hass: HomeAssistant,
) -> None:
    """Test that source modifiers persist from config flow template step."""
    entry = await _create_entry_with_price_template(hass)

    price = entry.data[CONF_SOURCES][CONF_SOURCE_PRICE]
    assert price[CONF_SOURCE_MODE] == SOURCE_MODE_TEMPLATE
    assert price[CONF_AGGREGATION_MODE] == AGGREGATION_MODE_MIN
    assert price[CONF_CLAMP_MODE] == CLAMP_MODE_NEAREST
    assert price[CONF_RESAMPLE_MODE] == RESAMPLE_MODE_FORWARD_FILL
    assert price[CONF_EDGE_FILL_MODE] == EDGE_FILL_MODE_HOLD


async def test_options_flow_persists_price_adapter_modifiers(
    hass: HomeAssistant,
) -> None:
    """Test that source modifiers persist from options flow adapter step."""
    entry = await _create_entry_with_price_template(hass)

    hass.states.async_set("sensor.price_provider", "ok", {"prices": [1.0] * 12})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "source_price"}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_price_adapter"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "entity_id": "sensor.price_provider",
            CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_VALUES,
            CONF_NAME: "prices",
            "time_key": "start",
            "value_key": "value",
            CONF_FIXUP_PROFILE: FIXUP_PROFILE_EXTEND,
            "advanced": {
                CONF_AGGREGATION_MODE: AGGREGATION_MODE_MAX,
                CONF_CLAMP_MODE: CLAMP_MODE_NONE,
                CONF_RESAMPLE_MODE: RESAMPLE_MODE_NONE,
                CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_NONE,
            },
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_review"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"accept_source_summary": True}
    )
    assert result["type"] is FlowResultType.MENU

    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    price = updated.data[CONF_SOURCES][CONF_SOURCE_PRICE]
    assert price[CONF_SOURCE_MODE] == SOURCE_MODE_ENTITY_ADAPTER
    assert price[CONF_ADAPTER_TYPE] == ADAPTER_TYPE_ATTRIBUTE_VALUES
    assert price[CONF_NAME] == "prices"
    assert price[CONF_FIXUP_PROFILE] == FIXUP_PROFILE_EXTEND
    assert price[CONF_AGGREGATION_MODE] == AGGREGATION_MODE_MAX
    assert price[CONF_CLAMP_MODE] == CLAMP_MODE_NONE
    assert price[CONF_RESAMPLE_MODE] == RESAMPLE_MODE_NONE
    assert price[CONF_EDGE_FILL_MODE] == EDGE_FILL_MODE_NONE

    # Optional sources remain explicitly optional.
    assert updated.data[CONF_SOURCES][CONF_SOURCE_USAGE][CONF_SOURCE_MODE] == SOURCE_MODE_NOT_USED
    assert updated.data[CONF_SOURCES][CONF_SOURCE_PV][CONF_SOURCE_MODE] == SOURCE_MODE_NOT_USED


async def test_config_flow_persists_usage_built_in_source(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Built-in usage source mode should save entity and history days."""
    hass.config.components.add("recorder")
    hass.states.async_set(
        "sensor.house_load_kwh",
        "123.4",
        {
            "device_class": "energy",
            "unit_of_measurement": "kWh",
            "state_class": "total_increasing",
        },
    )
    monkeypatch.setattr(
        "custom_components.wattplan.forecast_provider.ForecastProvider.async_debug_payload",
        AsyncMock(
            return_value={
                "forecast_values": [1.0] * 12,
                "raw_history_states": [
                    {"last_changed": "2026-02-20T00:00:00+00:00", "state": "1.0"},
                    {"last_changed": "2026-03-01T00:00:00+00:00", "state": "2.0"},
                ],
                "raw_statistics_rows": [],
            }
        ),
    )
    monkeypatch.setattr(
        "custom_components.wattplan.forecast_provider.ForecastProvider.async_values",
        AsyncMock(side_effect=lambda window: [1.0] * window.slots),
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Built in usage", CONF_SLOT_MINUTES: "60", CONF_HOURS_TO_PLAN: "12"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_TEMPLATE: _numeric_template(12),
            CONF_FIXUP_PROFILE: FIXUP_PROFILE_REPAIR,
            "advanced": {
                CONF_AGGREGATION_MODE: AGGREGATION_MODE_MIN,
                CONF_CLAMP_MODE: CLAMP_MODE_NEAREST,
                CONF_RESAMPLE_MODE: RESAMPLE_MODE_FORWARD_FILL,
                CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_HOLD,
            },
        },
    )
    assert result["step_id"] == "source_review"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"accept_source_summary": True}
    )
    assert result["step_id"] == "source_usage"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_BUILT_IN}
    )
    assert result["step_id"] == "source_usage_built_in"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "entity_id": "sensor.house_load_kwh",
            CONF_HISTORY_DAYS: 14,
        },
    )
    assert result["step_id"] == "source_review"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"accept_source_summary": True}
    )
    assert result["step_id"] == "source_pv"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    usage = result["data"][CONF_SOURCES][CONF_SOURCE_USAGE]
    assert usage[CONF_SOURCE_MODE] == SOURCE_MODE_BUILT_IN
    assert usage["entity_id"] == "sensor.house_load_kwh"
    assert usage[CONF_HISTORY_DAYS] == 14


async def test_built_in_usage_source_shows_sensor_validation_error(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Built-in usage mode should reject non-kWh energy sensors."""

    class _FakeRecorder:
        """Recorder stub with empty history/statistics responses."""

        def __init__(self) -> None:
            self._calls = 0

        async def async_add_executor_job(self, _job: object) -> dict[str, list[object]]:
            self._calls += 1
            return {"sensor.bad_load_source": []}

    hass.config.components.add("recorder")
    monkeypatch.setattr(
        "custom_components.wattplan.forecast_provider.get_instance",
        lambda _hass: _FakeRecorder(),
    )
    hass.states.async_set(
        "sensor.bad_load_source",
        "123",
        {
            "state_class": "measurement",
            "device_class": "energy",
            "unit_of_measurement": "W",
        },
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Built in usage", CONF_SLOT_MINUTES: "60", CONF_HOURS_TO_PLAN: "12"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_TEMPLATE: _numeric_template(12),
            CONF_FIXUP_PROFILE: FIXUP_PROFILE_REPAIR,
            "advanced": {
                CONF_AGGREGATION_MODE: AGGREGATION_MODE_MIN,
                CONF_CLAMP_MODE: CLAMP_MODE_NEAREST,
                CONF_RESAMPLE_MODE: RESAMPLE_MODE_FORWARD_FILL,
                CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_HOLD,
            },
        },
    )
    assert result["step_id"] == "source_review"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"accept_source_summary": True}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_BUILT_IN}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "entity_id": "sensor.bad_load_source",
            CONF_HISTORY_DAYS: 14,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "built_in_requires_energy_kwh"


async def test_config_flow_persists_pv_energy_provider_source(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PV Energy provider mode should save provider and advanced settings."""
    start_at = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    entry = MockConfigEntry(
        domain="forecast_solar",
        entry_id="solar-entry",
        title="Solcast",
        state=ConfigEntryState.LOADED,
    )
    entry.add_to_hass(hass)

    monkeypatch.setattr(
        "custom_components.wattplan.config_flow.async_get_energy_solar_forecast_entries",
        AsyncMock(return_value=[entry]),
    )
    monkeypatch.setattr(
        "custom_components.wattplan.source_provider.async_get_energy_solar_forecast_platforms",
        AsyncMock(
            return_value={
                "forecast_solar": AsyncMock(
                    return_value={
                        "wh_hours": {
                            (start_at + timedelta(hours=hour)).isoformat(): hour * 1000.0
                            for hour in range(48)
                        }
                    }
                )
            }
        ),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Energy PV", CONF_SLOT_MINUTES: "60", CONF_HOURS_TO_PLAN: "48"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_TEMPLATE: _numeric_template(48),
            CONF_FIXUP_PROFILE: FIXUP_PROFILE_REPAIR,
            "advanced": {
                CONF_AGGREGATION_MODE: AGGREGATION_MODE_MIN,
                CONF_CLAMP_MODE: CLAMP_MODE_NEAREST,
                CONF_RESAMPLE_MODE: RESAMPLE_MODE_FORWARD_FILL,
                CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_HOLD,
            },
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"accept_source_summary": True}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED}
    )
    assert result["step_id"] == "source_pv"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_ENERGY_PROVIDER}
    )
    assert result["step_id"] == "source_pv_energy_provider"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_CONFIG_ENTRY_ID: entry.entry_id,
            CONF_FIXUP_PROFILE: FIXUP_PROFILE_EXTEND,
            "advanced": {
                CONF_AGGREGATION_MODE: AGGREGATION_MODE_MAX,
                CONF_CLAMP_MODE: CLAMP_MODE_NONE,
                CONF_RESAMPLE_MODE: RESAMPLE_MODE_NONE,
                CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_HOLD,
            },
        },
    )
    assert result["step_id"] == "source_review"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"accept_source_summary": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    created_entry = hass.config_entries.async_entries(DOMAIN)[0]
    pv = created_entry.data[CONF_SOURCES][CONF_SOURCE_PV]
    assert pv[CONF_SOURCE_MODE] == SOURCE_MODE_ENERGY_PROVIDER
    assert pv[CONF_CONFIG_ENTRY_ID] == entry.entry_id
    assert pv[CONF_FIXUP_PROFILE] == FIXUP_PROFILE_EXTEND
    assert pv[CONF_AGGREGATION_MODE] == AGGREGATION_MODE_MAX
    assert pv[CONF_CLAMP_MODE] == CLAMP_MODE_NONE
    assert pv[CONF_RESAMPLE_MODE] == RESAMPLE_MODE_NONE
    assert pv[CONF_EDGE_FILL_MODE] == EDGE_FILL_MODE_HOLD
