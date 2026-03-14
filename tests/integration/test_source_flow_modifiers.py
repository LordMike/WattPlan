"""Test source modifier persistence in WattPlan flows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from custom_components.wattplan.const import (
    ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
    ADAPTER_TYPE_ATTRIBUTE_VALUES,
    ADAPTER_TYPE_AUTO_DETECT,
    ADAPTER_TYPE_SERVICE_RESPONSE,
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
    CONF_PROVIDERS,
    CONF_RESAMPLE_MODE,
    CONF_SERVICE,
    CONF_SLOT_MINUTES,
    CONF_SOURCE_MODE,
    CONF_SOURCE_IMPORT_PRICE,
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
    SOURCE_MODE_SERVICE_ADAPTER,
    SOURCE_MODE_TEMPLATE,
)
import pytest
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType

from tests.common import MockConfigEntry

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

SECTION_SOURCE_MANUAL = "manual"


def _numeric_template(count: int) -> str:
    """Return a template string that renders a native numeric list."""
    values = [float(index) for index in range(count)]
    return f"{{{{ {values!r} }}}}"


async def _finish_config_entry_creation(
    hass: HomeAssistant, result: dict[str, Any]
) -> dict[str, Any]:
    """Advance through final config-flow steps before entry creation."""
    while result["type"] is FlowResultType.FORM:
        if result["step_id"] == "source_export_price":
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
            )
            continue
        if result["step_id"] == "setup_complete":
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {}
            )
            continue
        break
    return result


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
    result = await _finish_config_entry_creation(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY

    return hass.config_entries.async_entries(DOMAIN)[0]


async def test_config_flow_persists_price_template_modifiers(
    hass: HomeAssistant,
) -> None:
    """Test that source modifiers persist from config flow template step."""
    entry = await _create_entry_with_price_template(hass)

    price = entry.data[CONF_SOURCES][CONF_SOURCE_IMPORT_PRICE]
    assert price[CONF_SOURCE_MODE] == SOURCE_MODE_TEMPLATE
    assert price[CONF_AGGREGATION_MODE] == AGGREGATION_MODE_MIN
    assert price[CONF_CLAMP_MODE] == CLAMP_MODE_NEAREST
    assert price[CONF_RESAMPLE_MODE] == RESAMPLE_MODE_FORWARD_FILL
    assert price[CONF_EDGE_FILL_MODE] == EDGE_FILL_MODE_HOLD


async def test_source_review_shows_raw_and_adjusted_coverage(
    hass: HomeAssistant,
) -> None:
    """Review should distinguish native coverage from repaired planner coverage."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "requirements"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "planner_setup"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Coverage review",
            CONF_SLOT_MINUTES: "60",
            CONF_HOURS_TO_PLAN: "24",
        },
    )
    assert result["step_id"] == "source_price"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE}
    )
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
    assert (
        result["description_placeholders"]["raw_coverage_summary"]
        == "12 usable intervals, 24 needed, 60-minute resolution"
    )
    assert (
        result["description_placeholders"]["adjusted_coverage_summary"]
        == "24 usable intervals, 24 needed, 60-minute resolution"
    )
    assert "only **12 usable intervals, 24 needed, 60-minute resolution** came from the source itself" in result[
        "description_placeholders"
    ]["review_text"]


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
            "entity_id": ["sensor.price_provider"],
            CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_VALUES,
            SECTION_SOURCE_MANUAL: {
                CONF_NAME: "prices",
                "time_key": "start",
                "value_key": "value",
            },
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
    price = updated.data[CONF_SOURCES][CONF_SOURCE_IMPORT_PRICE]
    provider = price[CONF_PROVIDERS][0]
    assert price[CONF_SOURCE_MODE] == SOURCE_MODE_ENTITY_ADAPTER
    assert provider[CONF_ADAPTER_TYPE] == ADAPTER_TYPE_ATTRIBUTE_VALUES
    assert provider[CONF_NAME] == "prices"
    assert price[CONF_FIXUP_PROFILE] == FIXUP_PROFILE_EXTEND
    assert price[CONF_AGGREGATION_MODE] == AGGREGATION_MODE_MAX
    assert price[CONF_CLAMP_MODE] == CLAMP_MODE_NONE
    assert price[CONF_RESAMPLE_MODE] == RESAMPLE_MODE_NONE
    assert price[CONF_EDGE_FILL_MODE] == EDGE_FILL_MODE_NONE

    # Optional sources remain explicitly optional.
    assert updated.data[CONF_SOURCES][CONF_SOURCE_USAGE][CONF_SOURCE_MODE] == SOURCE_MODE_NOT_USED
    assert updated.data[CONF_SOURCES][CONF_SOURCE_PV][CONF_SOURCE_MODE] == SOURCE_MODE_NOT_USED


async def test_config_flow_auto_detects_entity_adapter(
    hass: HomeAssistant,
) -> None:
    """Entity adapter auto-detect should persist resolved explicit fields."""
    start = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    hass.states.async_set(
        "sensor.first",
        "ok",
        {
            "prices": {
                "home": [
                    {
                        "starts_at": (start + timedelta(hours=hour)).isoformat(),
                        "amount": float(hour + 1),
                    }
                    for hour in range(6)
                ]
            }
        },
    )
    hass.states.async_set(
        "sensor.second",
        "ok",
        {
            "prices": {
                "home": [
                    {
                        "starts_at": (start + timedelta(hours=hour + 6)).isoformat(),
                        "amount": float(hour + 7),
                    }
                    for hour in range(6)
                ]
            }
        },
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Auto entity", CONF_SLOT_MINUTES: "60", CONF_HOURS_TO_PLAN: "12"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
        },
    )
    assert result["step_id"] == "source_price_adapter"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "entity_id": ["sensor.first", "sensor.second"],
            CONF_ADAPTER_TYPE: ADAPTER_TYPE_AUTO_DETECT,
            SECTION_SOURCE_MANUAL: {
                CONF_NAME: "",
                "time_key": "",
                "value_key": "",
            },
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
    diagnostic_text = result["description_placeholders"]["diagnostic_text"]
    assert "`sensor.first` -> `prices.home` / `starts_at` / `amount`" in diagnostic_text
    assert "`sensor.second` -> `prices.home` / `starts_at` / `amount`" in diagnostic_text
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"accept_source_summary": True}
    )
    assert result["step_id"] == "source_usage"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED}
    )
    assert result["step_id"] == "source_pv"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED}
    )
    result = await _finish_config_entry_creation(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    price = entry.data[CONF_SOURCES][CONF_SOURCE_IMPORT_PRICE]
    assert price[CONF_SOURCE_MODE] == SOURCE_MODE_ENTITY_ADAPTER
    assert len(price[CONF_PROVIDERS]) == 2
    assert [provider["entity_id"] for provider in price[CONF_PROVIDERS]] == [
        "sensor.first",
        "sensor.second",
    ]
    assert all(
        provider[CONF_ADAPTER_TYPE] == ADAPTER_TYPE_ATTRIBUTE_OBJECTS
        for provider in price[CONF_PROVIDERS]
    )
    assert {provider[CONF_NAME] for provider in price[CONF_PROVIDERS]} == {"prices.home"}
    assert {provider["time_key"] for provider in price[CONF_PROVIDERS]} == {"starts_at"}
    assert {provider["value_key"] for provider in price[CONF_PROVIDERS]} == {"amount"}


async def test_config_flow_persists_explicit_multi_entity_adapter(
    hass: HomeAssistant,
) -> None:
    """Explicit entity mapping should preserve all selected entities."""
    start = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    hass.states.async_set(
        "sensor.today",
        "ok",
        {
            "detailedForecast": [
                {
                    "period_start": (start + timedelta(hours=hour)).isoformat(),
                    "pv_estimate": float(hour + 1),
                }
                for hour in range(6)
            ]
        },
    )
    hass.states.async_set(
        "sensor.tomorrow",
        "ok",
        {
            "detailedForecast": [
                {
                    "period_start": (start + timedelta(hours=hour + 6)).isoformat(),
                    "pv_estimate": float(hour + 7),
                }
                for hour in range(6)
            ]
        },
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Multi entity", CONF_SLOT_MINUTES: "60", CONF_HOURS_TO_PLAN: "12"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER}
    )
    assert result["step_id"] == "source_price_adapter"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "entity_id": ["sensor.today", "sensor.tomorrow"],
            CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
            SECTION_SOURCE_MANUAL: {
                CONF_NAME: "detailedForecast",
                "time_key": "period_start",
                "value_key": "pv_estimate",
            },
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
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED}
    )
    assert result["step_id"] == "source_pv"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED}
    )
    result = await _finish_config_entry_creation(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    price = entry.data[CONF_SOURCES][CONF_SOURCE_IMPORT_PRICE]
    assert [provider["entity_id"] for provider in price[CONF_PROVIDERS]] == [
        "sensor.today",
        "sensor.tomorrow",
    ]
    assert {provider[CONF_NAME] for provider in price[CONF_PROVIDERS]} == {"detailedForecast"}
    assert {provider["time_key"] for provider in price[CONF_PROVIDERS]} == {"period_start"}
    assert {provider["value_key"] for provider in price[CONF_PROVIDERS]} == {"pv_estimate"}


async def test_config_flow_routes_failed_entity_auto_detect_to_review(
    hass: HomeAssistant,
) -> None:
    """Entity adapter semantic failures should be reported on review."""
    hass.states.async_set("sensor.bad_prices", "ok", {"prices": [{"foo": "bar"}]})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Auto detect failure",
            CONF_SLOT_MINUTES: "60",
            CONF_HOURS_TO_PLAN: "12",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER},
    )
    assert result["step_id"] == "source_price_adapter"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "entity_id": ["sensor.bad_prices"],
            CONF_ADAPTER_TYPE: ADAPTER_TYPE_AUTO_DETECT,
            SECTION_SOURCE_MANUAL: {
                CONF_NAME: "",
                "time_key": "",
                "value_key": "",
            },
            CONF_FIXUP_PROFILE: FIXUP_PROFILE_REPAIR,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_review"
    assert result["errors"] == {"base": "auto_detect_no_match"}
    assert result["data_schema"].schema == {}
    assert (
        result["description_placeholders"]["diagnostic_text"]
        == "**Auto-detect**\n\n"
        "- WattPlan could not build a usable forecast source from the selected input.\n\n"
        "- ❌ Not usable: `sensor.bad_prices`. Found list data in `prices`, but no "
        "timestamp field WattPlan can use. Ensure you picked an entity with forecast "
        "data in its attributes, or switch to manual mapping."
    )

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_price_adapter"


async def test_config_flow_failed_entity_auto_detect_previews_usable_providers(
    hass: HomeAssistant,
) -> None:
    """Review preview should preserve detected providers after partial auto-detect failure."""
    start = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    hass.states.async_set(
        "sensor.good_prices",
        "ok",
        {
            "prices": {
                "home": [
                    {
                        "starts_at": (start + timedelta(hours=hour)).isoformat(),
                        "amount": float(hour + 1),
                    }
                    for hour in range(6)
                ]
            }
        },
    )
    hass.states.async_set("sensor.bad_prices", "ok", {"prices": [{"foo": "bar"}]})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Partial auto detect preview",
            CONF_SLOT_MINUTES: "60",
            CONF_HOURS_TO_PLAN: "12",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER},
    )
    assert result["step_id"] == "source_price_adapter"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "entity_id": ["sensor.good_prices", "sensor.bad_prices"],
            CONF_ADAPTER_TYPE: ADAPTER_TYPE_AUTO_DETECT,
            SECTION_SOURCE_MANUAL: {
                CONF_NAME: "",
                "time_key": "",
                "value_key": "",
            },
            CONF_FIXUP_PROFILE: FIXUP_PROFILE_REPAIR,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_review"
    assert result["errors"] == {"base": "auto_detect_no_match"}
    assert (
        result["description_placeholders"]["raw_coverage_summary"]
        == "6 usable intervals, 12 needed, 60-minute resolution"
    )
    assert (
        "`sensor.good_prices`. Found forecast data in `prices.home` using `starts_at` "
        "for time and `amount` for value."
        in result["description_placeholders"]["diagnostic_text"]
    )


async def test_options_flow_auto_detects_service_adapter(
    hass: HomeAssistant,
) -> None:
    """Service adapter auto-detect should persist resolved explicit fields."""
    start = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)

    async def _handle_prices(call: ServiceCall) -> dict[str, object]:
        return {
            "prices": {
                "home": [
                    {
                        "start_time": (start + timedelta(hours=hour)).isoformat(),
                        "end_time": (start + timedelta(hours=hour + 1)).isoformat(),
                        "price": float(hour + 1),
                    }
                    for hour in range(12)
                ]
            }
        }

    hass.services.async_register(
        "test",
        "prices",
        _handle_prices,
        supports_response=SupportsResponse.ONLY,
    )
    entry = await _create_entry_with_price_template(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "source_price"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_SERVICE_ADAPTER}
    )
    assert result["step_id"] == "source_price_service"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SERVICE: "test.prices",
            CONF_ADAPTER_TYPE: ADAPTER_TYPE_AUTO_DETECT,
            SECTION_SOURCE_MANUAL: {
                CONF_NAME: "",
                "time_key": "",
                "value_key": "",
            },
            CONF_FIXUP_PROFILE: FIXUP_PROFILE_EXTEND,
            "advanced": {
                CONF_AGGREGATION_MODE: AGGREGATION_MODE_MAX,
                CONF_CLAMP_MODE: CLAMP_MODE_NONE,
                CONF_RESAMPLE_MODE: RESAMPLE_MODE_NONE,
                CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_NONE,
            },
        },
    )
    assert result["step_id"] == "source_review"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"accept_source_summary": True}
    )
    assert result["type"] is FlowResultType.MENU

    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    price = updated.data[CONF_SOURCES][CONF_SOURCE_IMPORT_PRICE]
    provider = price[CONF_PROVIDERS][0]
    assert price[CONF_SOURCE_MODE] == SOURCE_MODE_SERVICE_ADAPTER
    assert provider[CONF_SERVICE] == "test.prices"
    assert provider[CONF_ADAPTER_TYPE] == ADAPTER_TYPE_SERVICE_RESPONSE
    assert provider[CONF_NAME] == "prices.home"
    assert provider["time_key"] == "start_time"
    assert provider["value_key"] == "price"


async def test_options_flow_routes_failed_service_auto_detect_to_review(
    hass: HomeAssistant,
) -> None:
    """Service adapter semantic failures should be reported on review."""

    async def _handle_bad_prices(call: ServiceCall) -> dict[str, object]:
        return {"prices": [{"foo": "bar"}]}

    hass.services.async_register(
        "test",
        "bad_prices",
        _handle_bad_prices,
        supports_response=SupportsResponse.ONLY,
    )
    entry = await _create_entry_with_price_template(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "source_price"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_SERVICE_ADAPTER}
    )
    assert result["step_id"] == "source_price_service"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SERVICE: "test.bad_prices",
            CONF_ADAPTER_TYPE: ADAPTER_TYPE_AUTO_DETECT,
            SECTION_SOURCE_MANUAL: {
                CONF_NAME: "",
                "time_key": "",
                "value_key": "",
            },
            CONF_FIXUP_PROFILE: FIXUP_PROFILE_REPAIR,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_review"
    assert result["errors"] == {"base": "auto_detect_no_match"}
    assert result["data_schema"].schema == {}

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_price_service"


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
    result = await _finish_config_entry_creation(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    usage = result["data"][CONF_SOURCES][CONF_SOURCE_USAGE]
    assert usage[CONF_SOURCE_MODE] == SOURCE_MODE_BUILT_IN
    assert usage[CONF_PROVIDERS][0]["entity_id"] == "sensor.house_load_kwh"
    assert usage[CONF_PROVIDERS][0][CONF_HISTORY_DAYS] == 14


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
    with pytest.raises(vol.Invalid, match="built_in_requires_energy_kwh"):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "entity_id": "sensor.bad_load_source",
                CONF_HISTORY_DAYS: 14,
            },
        )


async def test_config_flow_persists_pv_energy_provider_source(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PV Energy provider mode should save provider and advanced settings."""
    forecast_start = datetime.now(tz=UTC).replace(
        minute=0, second=0, microsecond=0
    )
    entry = MockConfigEntry(
        domain="forecast_solar",
        entry_id="solar-entry",
        title="Solcast",
        state=ConfigEntryState.LOADED,
    )
    entry.async_unload = AsyncMock(return_value=True)
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
                            (forecast_start + timedelta(hours=hour)).isoformat(): (
                                hour * 1000.0
                            )
                            for hour in range(24)
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
    assert result["step_id"] == "source_export_price"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED}
    )
    result = await _finish_config_entry_creation(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY

    created_entry = hass.config_entries.async_entries(DOMAIN)[0]
    pv = created_entry.data[CONF_SOURCES][CONF_SOURCE_PV]
    assert pv[CONF_SOURCE_MODE] == SOURCE_MODE_ENERGY_PROVIDER
    assert pv[CONF_PROVIDERS][0][CONF_CONFIG_ENTRY_ID] == entry.entry_id
    assert pv[CONF_FIXUP_PROFILE] == FIXUP_PROFILE_EXTEND
    assert pv[CONF_AGGREGATION_MODE] == AGGREGATION_MODE_MAX
    assert pv[CONF_CLAMP_MODE] == CLAMP_MODE_NONE
    assert pv[CONF_RESAMPLE_MODE] == RESAMPLE_MODE_NONE
    assert pv[CONF_EDGE_FILL_MODE] == EDGE_FILL_MODE_HOLD
